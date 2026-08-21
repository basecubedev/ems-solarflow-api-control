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

from tests.helpers.appliance_acl import ACL_STUB
from tests.helpers.appliance_object_identity import object_identity

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_BIN = ROOT / "packaging" / "appliance" / "bin"
ACCOUNT_SCRIPT = PACKAGE_BIN / "backup-account.sh"
PRERM_SCRIPT = ROOT / "packaging" / "appliance" / "debian" / "prerm"
POSTRM_SCRIPT = ROOT / "packaging" / "appliance" / "debian" / "postrm"

BACKUP_USER = "ems-backup"
ACL_MANIFEST_SCHEMA = 3

STUBBED_TOOLS = (
    "getent",
    "adduser",
    "deluser",
    "delgroup",
    "usermod",
    "chage",
    "setfacl",
    "getfacl",
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
    setfacl|getfacl)
        [ "${EMS_STUB_SETFACL_RC:-0}" = 0 ] || exit "$EMS_STUB_SETFACL_RC"
        exec python3 "$EMS_STUB_DIR/acl.py" "$tool" "$@"
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
        self.acl_db = self.stub_dir / "acl.json"
        self.package_state = self.state_dir / "agent" / "package-state"
        self.acl_manifest = self.package_state / "acl-manifest.tsv"
        self.managed_keys = self.package_state / "managed-keys.list"
        self.quarantine_dir = self.root / "var" / "backups" / "ems-appliance-manager"
        self.origin_dir = self.root / "usr" / "lib" / "ems-appliance-manager"
        self.origin_nonce = ""
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

    def remove_account(self):
        """Delete the account outside the package, the way an operator would."""

        for name in ("passwd", "group"):
            table = self.stub_dir / name
            kept = [
                line
                for line in table.read_text(encoding="utf-8").splitlines()
                if not line.startswith(f"{self.account}:")
            ]
            table.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
        return True

    def account_field(self, index):
        for line in (self.stub_dir / "passwd").read_text(encoding="utf-8").splitlines():
            fields = line.split(":")
            if fields and fields[0] == self.account:
                return fields[index]
        return ""

    @staticmethod
    def object_identity(path):
        return object_identity(path)

    def write_acl_manifest(
        self,
        entries=(),
        roots=(),
        *,
        user=None,
        install_root=None,
        schema=ACL_MANIFEST_SCHEMA,
        identities=None,
    ):
        """The v3 manifest the export setup writes.

        ``entries`` are ``(path, scope, previous, granted)`` — ``previous`` is
        ``None`` when this package introduced the entry. ``roots`` are
        ``(path, mode)`` and let purge look for descendants nobody attributed.
        ``identities`` overrides the recorded identity of individual objects,
        which is how a test can present the one case the allocator decides:
        an object that was replaced and inherited the recorded inode.
        """

        identities = {str(key): value for key, value in (identities or {}).items()}
        lines = [
            f"# ems-appliance ACL manifest v{schema}",
            f"schema={schema}",
            f"user={user or self.account}",
            f"install_root={install_root or self.install_root}",
            "installation_id=test-installation",
            "recorded_at=2026-01-01T00:00:00Z",
        ]
        for path, mode in roots:
            identity = identities.get(str(path), object_identity(path))
            lines.append(f"root\t{path}\t{identity}\t{mode}")
        for path, scope, previous, granted in entries:
            identity = identities.get(str(path), object_identity(path))
            preexisting = "yes" if previous else "no"
            lines.append(
                f"entry\t{path}\t{identity}\t{scope}"
                f"\t{preexisting}\t{previous or '-'}\t{granted}"
            )
        self.acl_manifest.parent.mkdir(parents=True, exist_ok=True)
        self.acl_manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.acl_manifest

    def account_exists(self):
        table = (self.stub_dir / "passwd").read_text(encoding="utf-8")
        return any(line.startswith(f"{self.account}:") for line in table.splitlines())

    def write_key(self, name="authorized_keys", text="ssh-ed25519 AAAA test\n", *, managed=True):
        """Write a key file; ``managed`` records it the way the appliance does."""

        keys = self.home / ".ssh" / name
        keys.parent.mkdir(parents=True, exist_ok=True)
        keys.write_text(text, encoding="utf-8")
        if managed:
            for line in text.splitlines():
                fields = line.split()
                if len(fields) >= 2:
                    self.register_managed_key(fields[1])
        return keys

    def register_managed_key(self, blob):
        """Record a key body hash the way ``AuthorizedKeysStore`` does."""

        import hashlib

        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        self.managed_keys.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if self.managed_keys.is_file():
            existing = self.managed_keys.read_text(encoding="utf-8").split()
        if digest not in existing:
            with self.managed_keys.open("a", encoding="utf-8") as handle:
                handle.write(digest + "\n")
        return digest

    def record(self):
        try:
            return json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # --- ACL state --------------------------------------------------------

    def acl_state(self):
        try:
            return json.loads(self.acl_db.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def set_acl(self, path, name, perms, *, kind="access"):
        state = self.acl_state()
        entries = state.setdefault(str(path), {"access": {}, "default": {}})
        entries[kind][name] = perms
        self.acl_db.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        return entries

    def acl_entry(self, path, name, *, kind="access"):
        return (self.acl_state().get(str(path)) or {}).get(kind, {}).get(name)

    def write_record(self, *, marker_nonce=None, write_marker=True, **fields):
        """The ownership record ``ensure`` writes, including the bound identity."""

        from appliance import backup_ownership

        home = Path(fields.get("home") or self.home)
        try:
            entry = home.stat()
            device, inode = str(entry.st_dev), str(entry.st_ino)
        except OSError:
            device, inode = "", ""
        uid = int(self.account_field(2) or 1500)
        gid = int(self.account_field(3) or 1500)
        nonce = marker_nonce or backup_ownership.new_marker_nonce()
        payload = {
            "schema_version": backup_ownership.RECORD_SCHEMA_VERSION,
            "account": self.account,
            "created_by_package": True,
            "uid": uid,
            "primary_gid": gid,
            "home": str(home),
            "home_device": device,
            "home_inode": inode,
            "home_marker": str(home / backup_ownership.HOME_MARKER_NAME),
            "home_marker_nonce": nonce,
            "home_created_by_package": True,
            "installation_id": "test-installation",
            "managed_keys_file": str(self.managed_keys),
            "authorized_keys": str(home / ".ssh" / "authorized_keys"),
        }
        payload.update(fields)
        if write_marker and home.is_dir():
            self.write_home_marker(home, nonce, account=payload["account"], uid=uid, gid=gid)
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload

    @property
    def origin(self):
        from appliance import backup_ownership

        return self.origin_dir / backup_ownership.ACCOUNT_ORIGIN_NAME

    def write_origin(self, **fields):
        """The declaration the build chroot bakes into the slot root."""

        from appliance import backup_ownership

        self.origin_nonce = fields.pop("nonce", None) or backup_ownership.new_marker_nonce()
        values = {
            "account": self.account,
            "uid": self.account_field(2) or "1500",
            "primary_gid": self.account_field(3) or "1500",
            "home": self.account_field(5) or str(self.home),
            "shell": self.account_field(6) or "/usr/sbin/nologin",
            "nonce": self.origin_nonce,
        }
        schema = fields.pop("schema_version", None)
        values.update(fields)
        text = backup_ownership.render_account_origin(**values)
        if schema is not None:
            text = text.replace(
                f"schema_version={backup_ownership.ACCOUNT_ORIGIN_SCHEMA_VERSION}\n",
                f"schema_version={schema}\n",
                1,
            )
        self.origin_dir.mkdir(parents=True, exist_ok=True)
        self.origin.write_text(text, encoding="utf-8")
        self.origin.chmod(0o444)
        return self.origin

    def write_home_marker(self, home, nonce, *, account=None, uid=1500, gid=1500):
        """The root-owned marker ``ensure`` leaves inside the home it created."""

        from appliance import backup_ownership

        path = Path(home) / backup_ownership.HOME_MARKER_NAME
        path.write_text(
            backup_ownership.render_home_marker(
                account=account or self.account,
                uid=uid,
                primary_gid=gid,
                home=str(home),
                installation_id="test-installation",
                nonce=nonce,
            ),
            encoding="utf-8",
        )
        path.chmod(0o400)
        return path

    def home_marker(self, home=None):
        from appliance import backup_ownership

        return Path(home or self.home) / backup_ownership.HOME_MARKER_NAME

    # --- execution --------------------------------------------------------

    def _install_stubs(self):
        self.stub_dir.mkdir(parents=True, exist_ok=True)
        (self.stub_dir / "passwd").write_text("", encoding="utf-8")
        (self.stub_dir / "group").write_text("", encoding="utf-8")
        dispatch = self.stub_dir / "dispatch.sh"
        dispatch.write_text(_DISPATCH, encoding="utf-8")
        dispatch.chmod(0o755)
        (self.stub_dir / "acl.py").write_text(ACL_STUB, encoding="utf-8")
        self.acl_db.write_text("{}", encoding="utf-8")
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
        env["EMS_STUB_ACL_DB"] = str(self.acl_db)
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
        env["EMS_APPLIANCE_QUARANTINE_DIR"] = str(self.quarantine_dir)
        env["EMS_APPLIANCE_ORIGIN_DIR"] = str(self.origin_dir)
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
