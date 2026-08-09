# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive the packaged backup-account script without root and without users.

The account lifecycle decides whether ``purge`` may delete a host account and
its home directory, so its refusals have to be observable on a developer
machine. Every account tool is a recording stub backed by a plain-text passwd
table; the home directory itself is real, because "the operator's files are
still there" is exactly what is under test.
"""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_BIN = ROOT / "packaging" / "appliance" / "bin"
ACCOUNT_SCRIPT = PACKAGE_BIN / "backup-account.sh"
PRERM_SCRIPT = ROOT / "packaging" / "appliance" / "debian" / "prerm"
POSTRM_SCRIPT = ROOT / "packaging" / "appliance" / "debian" / "postrm"

BACKUP_USER = "ems-backup"

STUBBED_TOOLS = (
    "getent",
    "adduser",
    "deluser",
    "delgroup",
    "usermod",
    "chage",
    "setfacl",
    "chown",
    # The maintainer scripts must never reach the developer's own systemd or
    # SSH daemon, so both are answered locally.
    "systemctl",
    "sshd",
    "mountpoint",
    "umount",
)

_STUB = """#!/bin/sh
printf '%s' "$(basename "$0")" >> "$EMS_STUB_CALLS"
for argument in "$@"; do printf ' %s' "$argument" >> "$EMS_STUB_CALLS"; done
printf '\\n' >> "$EMS_STUB_CALLS"
exec "$EMS_STUB_DIR/dispatch.sh" "$(basename "$0")" "$@"
"""

_DISPATCH = r"""#!/bin/sh
tool=$1
shift
passwd_file="$EMS_STUB_DIR/passwd"
group_file="$EMS_STUB_DIR/group"

last_argument() {
    for value in "$@"; do :; done
    printf '%s' "$value"
}

case "$tool" in
    getent)
        case "$1" in
            passwd) file=$passwd_file ;;
            group) file=$group_file ;;
            *) exit 2 ;;
        esac
        [ -f "$file" ] || exit 2
        line=$(grep "^$2:" "$file" | head -n 1)
        [ -n "$line" ] || exit 2
        printf '%s\n' "$line"
        exit 0
        ;;
    adduser)
        [ "${EMS_STUB_ADDUSER_RC:-0}" = 0 ] || exit "$EMS_STUB_ADDUSER_RC"
        name=$(last_argument "$@")
        home=$EMS_STUB_NEW_HOME
        shell=/usr/sbin/nologin
        printf '%s:x:1500:1500::%s:%s\n' "$name" "$home" "$shell" >> "$passwd_file"
        printf '%s:x:1500:\n' "$name" >> "$group_file"
        mkdir -p "$home"
        exit 0
        ;;
    deluser)
        [ "${EMS_STUB_DELUSER_RC:-0}" = 0 ] || exit "$EMS_STUB_DELUSER_RC"
        name=$(last_argument "$@")
        grep -v "^$name:" "$passwd_file" > "$passwd_file.next" 2>/dev/null || : > "$passwd_file.next"
        mv "$passwd_file.next" "$passwd_file"
        exit 0
        ;;
    delgroup)
        name=$(last_argument "$@")
        grep -v "^$name:" "$group_file" > "$group_file.next" 2>/dev/null || : > "$group_file.next"
        mv "$group_file.next" "$group_file"
        exit 0
        ;;
    usermod)
        exit "${EMS_STUB_USERMOD_RC:-0}"
        ;;
    chage)
        exit "${EMS_STUB_CHAGE_RC:-0}"
        ;;
    setfacl)
        exit "${EMS_STUB_SETFACL_RC:-0}"
        ;;
    systemctl|sshd)
        exit "${EMS_STUB_HOST_RC:-0}"
        ;;
    mountpoint)
        exit 1
        ;;
    umount)
        exit 0
        ;;
    chown)
        exit 0
        ;;
esac
exit 0
"""


class BackupAccountHarness:
    """A fake passwd database plus a real home directory tree."""

    def __init__(self, tmp_path, *, account=BACKUP_USER):
        self.root = Path(tmp_path)
        self.account = account
        self.state_dir = self.root / "var" / "lib" / "ems-appliance-manager"
        self.config_dir = self.root / "etc" / "ems-appliance-manager"
        self.log_dir = self.root / "var" / "log" / "ems-appliance-manager"
        self.runtime_dir = self.root / "run" / "ems-appliance-manager"
        self.systemd_dir = self.root / "etc" / "systemd" / "system"
        self.sshd_dir = self.root / "etc" / "ssh" / "sshd_config.d"
        self.marker = self.state_dir / "agent" / "package-state" / "backup-account.json"
        self.home = self.root / "var" / "lib" / account
        self.install_root = self.root / "opt" / "ems-solarflow"
        self.export_root = self.root / "srv" / "ems-appliance-export"
        self.package_bin = PACKAGE_BIN
        self.stub_dir = self.root / "stubs"
        self.calls_file = self.stub_dir / "calls.log"
        self.environment = {}
        self._install_stubs()
        self.marker.parent.mkdir(parents=True, exist_ok=True)

    # --- host state -------------------------------------------------------

    def add_account(self, *, home=None, shell="/bin/sh", uid=1500):
        home = Path(home) if home is not None else self.home
        with (self.stub_dir / "passwd").open("a", encoding="utf-8") as handle:
            handle.write(f"{self.account}:x:{uid}:{uid}::{home}:{shell}\n")
        with (self.stub_dir / "group").open("a", encoding="utf-8") as handle:
            handle.write(f"{self.account}:x:{uid}:\n")
        home.mkdir(parents=True, exist_ok=True)
        return home

    def account_exists(self):
        table = (self.stub_dir / "passwd").read_text(encoding="utf-8")
        return any(line.startswith(f"{self.account}:") for line in table.splitlines())

    def write_key(self, name="authorized_keys", text="ssh-ed25519 AAAA test\n"):
        keys = self.home / ".ssh" / name
        keys.parent.mkdir(parents=True, exist_ok=True)
        keys.write_text(text, encoding="utf-8")
        return keys

    def record(self):
        try:
            return json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def write_record(self, **fields):
        payload = {
            "account": self.account,
            "created_by_package": True,
            "home": str(self.home),
            "home_created_by_package": True,
            "original_shell": "",
            "original_expiry": "",
            "original_group": "",
            "authorized_keys": str(self.home / ".ssh" / "authorized_keys"),
        }
        payload.update(fields)
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload

    # --- execution --------------------------------------------------------

    def _install_stubs(self):
        self.stub_dir.mkdir(parents=True, exist_ok=True)
        (self.stub_dir / "passwd").write_text("", encoding="utf-8")
        (self.stub_dir / "group").write_text("", encoding="utf-8")
        dispatch = self.stub_dir / "dispatch.sh"
        dispatch.write_text(_DISPATCH, encoding="utf-8")
        dispatch.chmod(0o755)
        for tool in STUBBED_TOOLS:
            stub = self.stub_dir / tool
            stub.write_text(_STUB, encoding="utf-8")
            stub.chmod(0o755)
        self.calls_file.write_text("", encoding="utf-8")

    def _environment(self, extra):
        env = dict(os.environ)
        env["PATH"] = f"{self.stub_dir}:{env.get('PATH', '')}"
        env["EMS_STUB_DIR"] = str(self.stub_dir)
        env["EMS_STUB_CALLS"] = str(self.calls_file)
        env["EMS_STUB_NEW_HOME"] = str(self.home)
        env["EMS_APPLIANCE_BACKUP_USER"] = self.account
        env["EMS_APPLIANCE_STATE_DIR"] = str(self.state_dir)
        env["EMS_APPLIANCE_BACKUP_HOME"] = str(self.home)
        env["EMS_APPLIANCE_INSTALL_ROOT"] = str(self.install_root)
        env["EMS_APPLIANCE_EXPORT_ROOT"] = str(self.export_root)
        env["EMS_APPLIANCE_CONFIG_DIR"] = str(self.config_dir)
        env["EMS_APPLIANCE_LOG_DIR"] = str(self.log_dir)
        env["EMS_APPLIANCE_RUNTIME_DIR"] = str(self.runtime_dir)
        env["EMS_APPLIANCE_SYSTEMD_DIR"] = str(self.systemd_dir)
        env["EMS_APPLIANCE_SSHD_DIR"] = str(self.sshd_dir)
        env.update({key: str(value) for key, value in self.environment.items()})
        env.update({key: str(value) for key, value in extra.items()})
        return env

    def run(self, *arguments, **environment):
        return subprocess.run(
            [str(ACCOUNT_SCRIPT), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=self._environment(environment),
        )

    def run_prerm(self, action="remove", **environment):
        environment.setdefault("EMS_APPLIANCE_BIN", str(self.stub_dir / "ems-appliance"))
        environment.setdefault("EMS_APPLIANCE_LIBDIR", str(PACKAGE_BIN))
        return subprocess.run(
            [str(PRERM_SCRIPT), action],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=self._environment(environment),
        )

    def run_postrm(self, action="purge", **environment):
        environment.setdefault("EMS_APPLIANCE_LIBDIR", str(PACKAGE_BIN))
        return subprocess.run(
            [str(POSTRM_SCRIPT), action],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=self._environment(environment),
        )

    def stub_command(self, name, body):
        """Install an executable stub for a command the maintainer scripts call."""

        path = self.stub_dir / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def calls(self):
        return [
            line.strip()
            for line in self.calls_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
