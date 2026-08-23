# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the packaged export-root script without root and without real mounts.

``setup-export-root.sh`` is the one component that turns host paths into
kernel mounts, so its refusals have to be observable on a normal developer
machine. Every privileged or mutating tool it calls is replaced by a recording
stub on ``PATH``; ``readlink``, ``stat`` and ``mkdir`` stay real, because path
canonicalisation is exactly what is under test.

The stubs simulate a bind mount by replacing the target directory with a
symlink to the source, which is close enough for ``stat``-based identity checks
and nothing else. The real kernel behaviour is proven by the Docker/systemd
tests in ``test_appliance_sftp_confinement.py``.
"""

import json
import shutil
import os
import subprocess
from pathlib import Path

from tests.helpers.appliance_acl import ACL_STUB

ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCRIPT = ROOT / "packaging" / "appliance" / "bin" / "setup-export-root.sh"

BACKUP_USER = "ems-backup"
EXPORT_NAMES = ("config", "backups", "data")

# Every tool the script may call that needs root or changes the kernel state.
STUBBED_TOOLS = (
    "setfacl",
    "getfacl",
    "mount",
    "umount",
    "mountpoint",
    "findmnt",
    "getent",
    "chown",
    "chmod",
    "sync",
    "mv",
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
state="$EMS_STUB_DIR/mounts"
hook="$EMS_STUB_DIR/hooks/$tool"
[ -x "$hook" ] && "$hook" "$@"

mounted() {
    [ -f "$state" ] || return 1
    grep -Fxq "target=$1" "$state"
}

case "$tool" in
    getent)
        [ "$1" = passwd ] && [ "$2" = "$EMS_STUB_ACCOUNT" ] || exit 2
        printf '%s:x:1500:1500::/var/lib/%s:/usr/sbin/nologin\n' "$2" "$2"
        exit 0
        ;;
    chown)
        exit "${EMS_STUB_CHOWN_RC:-0}"
        ;;
    chmod)
        # Modes are real: the script verifies them, so the stub must not lie.
        exec "$EMS_STUB_REAL_CHMOD" "$@"
        ;;
    setfacl|getfacl)
        [ "${EMS_STUB_SETFACL_RC:-0}" = 0 ] || exit "$EMS_STUB_SETFACL_RC"
        # Failing one exact call is what separates "the pre-state could not be
        # captured" from "the read-back could not", which end differently.
        upper=$(echo "$tool" | tr 'a-z' 'A-Z')
        eval "fail_at=\${EMS_STUB_${upper}_FAIL_AT:-}"
        eval "fail_from=\${EMS_STUB_${upper}_FAIL_FROM:-}"
        if [ -n "$fail_at" ] || [ -n "$fail_from" ]; then
            counter="$EMS_STUB_DIR/$tool.count"
            seen=$(cat "$counter" 2>/dev/null || echo 0)
            seen=$((seen + 1))
            printf '%s' "$seen" > "$counter"
            [ -n "$fail_at" ] && [ "$seen" = "$fail_at" ] && exit 1
            [ -n "$fail_from" ] && [ "$seen" -ge "$fail_from" ] && exit 1
        fi
        exec python3 "$EMS_STUB_DIR/acl.py" "$tool" "$@"
        ;;
    sync)
        [ "${EMS_STUB_SYNC_RC:-0}" = 0 ] || exit "$EMS_STUB_SYNC_RC"
        # Which flush fails decides what the run has to put back, so one exact
        # path can be failed without disturbing the others. The manifest and the
        # transaction state share a parent directory, so a nth-match counter is
        # what separates their flushes.
        if [ -n "${EMS_STUB_SYNC_FAIL_PATH:-}" ] && [ "$1" = "$EMS_STUB_SYNC_FAIL_PATH" ]; then
            counter="$EMS_STUB_DIR/sync.path.count"
            seen=$(cat "$counter" 2>/dev/null || echo 0)
            seen=$((seen + 1))
            printf '%s' "$seen" > "$counter"
            [ -z "${EMS_STUB_SYNC_FAIL_PATH_AT:-}" ] && exit 1
            [ "$seen" = "$EMS_STUB_SYNC_FAIL_PATH_AT" ] && exit 1
        fi
        exit 0
        ;;
    mv)
        for argument in "$@"; do target=$argument; done
        [ -n "${EMS_STUB_MV_FAIL_PATH:-}" ] && [ "$target" = "$EMS_STUB_MV_FAIL_PATH" ] && exit 1
        exec "$EMS_STUB_REAL_MV" "$@"
        ;;
    mountpoint)
        target=$2
        [ "$1" = "-q" ] || target=$1
        mounted "$target" && exit 0
        exit 1
        ;;
    findmnt)
        target=""
        source_only=0
        for argument in "$@"; do
            case "$argument" in
                --mountpoint|--target) source_only=1 ;;
                -*) ;;
                *) [ "$source_only" = 1 ] && target=$argument && source_only=0 ;;
            esac
        done
        mounted "$target" || exit 1
        options=$(sed -n "s|^options=$target=||p" "$state" | tail -n 1)
        printf '%s\n' "${options:-rw,relatime}"
        exit 0
        ;;
    mount)
        rc=${EMS_STUB_MOUNT_RC:-0}
        [ "$rc" != 0 ] && exit "$rc"
        case "$1" in
            --bind)
                source_dir=$2
                target=$3
                case "$source_dir" in
                    /proc/self/fd/*) source_dir=$(readlink -f "$source_dir") ;;
                esac
                printf 'target=%s\n' "$target" >> "$state"
                printf 'options=%s=rw,relatime\n' "$target" >> "$state"
                # Simulated bind: the target becomes the source for stat(1).
                if rmdir "$target" 2>/dev/null; then
                    ln -s "${EMS_STUB_BIND_SOURCE:-$source_dir}" "$target"
                    printf 'simulated=%s\n' "$target" >> "$state"
                fi
                exit 0
                ;;
            -o)
                target=$3
                case "$2" in
                    *ro*)
                        [ "${EMS_STUB_REMOUNT_RC:-0}" != 0 ] && exit "${EMS_STUB_REMOUNT_RC}"
                        mounted "$target" || exit 1
                        printf 'options=%s=ro,relatime\n' "$target" >> "$state"
                        exit 0
                        ;;
                esac
                exit 1
                ;;
        esac
        exit 1
        ;;
    umount)
        target=$1
        simulated=0
        grep -Fxq "simulated=$target" "$state" 2>/dev/null && simulated=1
        grep -Fxv "target=$target" "$state" > "$state.next" 2>/dev/null || : > "$state.next"
        grep -Fv "options=$target=" "$state.next" > "$state.next2" 2>/dev/null || : > "$state.next2"
        grep -Fxv "simulated=$target" "$state.next2" > "$state" 2>/dev/null || : > "$state"
        rm -f "$state.next" "$state.next2"
        # Only a bind this stub simulated leaves a symlink behind; anything
        # else at the target was already on the host and must survive.
        if [ "$simulated" = 1 ] && [ -L "$target" ]; then
            rm -f "$target"
            mkdir -p "$target"
        fi
        exit 0
        ;;
esac
exit 0
"""


class ExportScriptHarness:
    """One temporary host layout plus a recording stub for every mutating tool."""

    def __init__(self, tmp_path, *, account=BACKUP_USER):
        self.root = Path(tmp_path)
        self.install_root = self.root / "opt" / "ems-solarflow"
        self.export_root = self.root / "srv" / "ems-appliance-export"
        self.status_file = self.root / "var" / "agent" / "export-access.json"
        self.stub_dir = self.root / "stubs"
        self.calls_file = self.stub_dir / "calls.log"
        self.acl_db = self.stub_dir / "acl.json"
        self.state_dir = self.root / "var" / "lib" / "ems-appliance-manager"
        self.acl_manifest = self.state_dir / "agent" / "package-state" / "acl-manifest.tsv"
        self.acl_recovery = self.acl_manifest.with_name("acl-recovery.tsv")
        self.acl_state_file = self.acl_manifest.with_name("acl-transaction.state")
        self.account = account
        self.environment = {}
        self._install_stubs()
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.acl_manifest.parent.mkdir(parents=True, exist_ok=True)

    # --- layout ----------------------------------------------------------

    def seed_installation(self, *, names=EXPORT_NAMES):
        for name in names:
            (self.install_root / name).mkdir(parents=True, exist_ok=True)
            (self.install_root / name / "marker").write_text("ems\n", encoding="utf-8")
        (self.install_root / "secrets").mkdir(parents=True, exist_ok=True)
        return self.install_root

    def seed_export_root(self, *, names=EXPORT_NAMES):
        self.export_root.mkdir(parents=True, exist_ok=True)
        for name in names:
            (self.export_root / name).mkdir(parents=True, exist_ok=True)
        return self.export_root

    def outside(self, name="etc"):
        target = self.root / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "passwd").write_text("root:x:0:0::/root:/bin/sh\n", encoding="utf-8")
        return target

    def replace_with_symlink(self, path, destination):
        path = Path(path)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            for entry in sorted(path.rglob("*"), reverse=True):
                entry.unlink() if not entry.is_dir() else entry.rmdir()
            path.rmdir()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(destination)
        return path

    def mark_mounted(self, target, *, options="rw,relatime"):
        state = self.stub_dir / "mounts"
        with state.open("a", encoding="utf-8") as handle:
            handle.write(f"target={target}\n")
            handle.write(f"options={target}={options}\n")
        return target

    def hook(self, tool, script):
        """Run ``script`` whenever the stub for ``tool`` is invoked."""

        hooks = self.stub_dir / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        path = hooks / tool
        path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    # --- execution -------------------------------------------------------

    def _install_stubs(self):
        self.stub_dir.mkdir(parents=True, exist_ok=True)
        (self.stub_dir / "hooks").mkdir(parents=True, exist_ok=True)
        (self.stub_dir / "mounts").write_text("", encoding="utf-8")
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

    def run(self, *arguments, **environment):
        env = dict(os.environ)
        env["PATH"] = f"{self.stub_dir}:{env.get('PATH', '')}"
        env["EMS_STUB_DIR"] = str(self.stub_dir)
        env["EMS_STUB_CALLS"] = str(self.calls_file)
        env["EMS_STUB_ACL_DB"] = str(self.acl_db)
        env["EMS_STUB_ACCOUNT"] = self.account
        env["EMS_APPLIANCE_STATE_DIR"] = str(self.state_dir)
        env["EMS_STUB_REAL_CHMOD"] = shutil.which("chmod", path="/usr/bin:/bin") or "/bin/chmod"
        env["EMS_STUB_REAL_MV"] = shutil.which("mv", path="/usr/bin:/bin") or "/bin/mv"
        env["EMS_APPLIANCE_ACL_RECOVERY"] = str(self.acl_recovery)
        env["EMS_APPLIANCE_BACKUP_USER"] = self.account
        env["EMS_APPLIANCE_HOST_PATHS"] = str(self.root / "absent-host-paths.env")
        env["EMS_APPLIANCE_INSTALL_ROOT"] = str(self.install_root)
        env["EMS_APPLIANCE_EXPORT_ROOT"] = str(self.export_root)
        env["EMS_APPLIANCE_EXPORT_STATUS_FILE"] = str(self.status_file)
        env["EMS_APPLIANCE_EXPORT_LOCK"] = str(self.stub_dir / "export.lock")
        env.update({key: str(value) for key, value in self.environment.items()})
        env.update({key: str(value) for key, value in environment.items()})
        return subprocess.run(
            [str(EXPORT_SCRIPT), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=env,
        )

    # --- observation -----------------------------------------------------

    def calls(self):
        return [
            line.strip()
            for line in self.calls_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def called(self, tool):
        return [line for line in self.calls() if line.split(" ", 1)[0] == tool]

    def status(self):
        try:
            return json.loads(self.status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

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

    def transaction_state(self):
        try:
            return self.acl_state_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def manifest_lines(self):
        try:
            text = self.acl_manifest.read_text(encoding="utf-8")
        except OSError:
            return []
        return [line for line in text.splitlines() if line and not line.startswith("#")]
