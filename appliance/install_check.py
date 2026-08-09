# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post-install verification.

A package that reports success while the agent never started, the socket is
missing or the web account cannot reach it leaves an appliance that looks
installed and is not. This module answers one question — is this installation
actually usable — and separates two things that must never be confused: a
critical failure, and a host feature that is simply not installed.

Optional host features (Docker, NetworkManager, OpenSSH) are reported as
unavailable. They never fail the package.
"""

import os
import socket
import stat

from appliance.paths import AGENT_DIR_MODE, resolve_paths

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_DEFERRED = "deferred"
STATUS_UNAVAILABLE = "unavailable"

AGENT_UNIT = "ems-appliance-agent.service"
WEB_UNIT = "ems-appliance-web.service"

WEB_USER = "ems-appliance-web"
APPLIANCE_GROUP = "ems-appliance"

OPTIONAL_FEATURES = (
    ("docker", "docker", "Admin container management"),
    ("network_manager", "nmcli", "WLAN and hostname management"),
    ("openssh", "sshd", "SSH key management and backup export"),
    ("acl", "setfacl", "read-only export permissions"),
)

# systemd is not running inside an image-build chroot. Everything that does not
# need a running manager must still be correct there.
SYSTEMD_RUNTIME_MARKER = "/run/systemd/system"


def systemd_running(marker=SYSTEMD_RUNTIME_MARKER):
    return os.path.isdir(marker)


def _which(tool):
    for directory in (os.environ.get("PATH") or "/usr/sbin:/usr/bin:/sbin:/bin").split(":"):
        candidate = os.path.join(directory, tool)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _check(name, status, detail, *, critical=True):
    return {"check": name, "status": status, "detail": detail, "critical": bool(critical)}


def _unit_active(runner, unit):
    if runner is None or not runner.available("systemctl"):
        return None
    result = runner.run("systemctl", ["is-active", unit], timeout=30)
    return (result.stdout or "").strip()


def check_units(runner, *, live):
    checks = []
    for unit in (AGENT_UNIT, WEB_UNIT):
        if not live:
            checks.append(
                _check(unit, STATUS_DEFERRED, "no running systemd; the unit starts on first boot")
            )
            continue
        state = _unit_active(runner, unit)
        if state == "active":
            checks.append(_check(unit, STATUS_OK, "active"))
        else:
            checks.append(
                _check(unit, STATUS_FAILED, f"is-active reports {state or 'unknown'}")
            )
    return checks


def check_socket(paths, *, live):
    path = paths.agent_socket
    if not live:
        return [_check("agent_socket", STATUS_DEFERRED, "created when the agent starts")]
    if not path.exists():
        return [_check("agent_socket", STATUS_FAILED, f"{path} does not exist")]
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o007:
        return [_check("agent_socket", STATUS_FAILED, f"{path} is world-accessible ({mode:o})")]
    return [_check("agent_socket", STATUS_OK, f"{path} mode {mode:o}")]


def check_directories(paths):
    checks = []
    for directory in (paths.state_dir, paths.log_dir, *paths.web_directories()):
        if directory.is_dir():
            continue
        return [_check("directories", STATUS_FAILED, f"{directory} is missing")]
    for directory in paths.agent_directories():
        if not directory.is_dir():
            return [_check("directories", STATUS_FAILED, f"{directory} is missing")]
    checks.append(_check("directories", STATUS_OK, "the appliance layout is complete"))
    return checks


def check_ownership(paths):
    """Agent state must belong to root alone; web state to the web account."""

    problems = []
    for directory in paths.agent_directories():
        try:
            entry = directory.stat()
        except OSError as exc:
            problems.append(f"{directory}: {exc.__class__.__name__}")
            continue
        if entry.st_uid != 0 or entry.st_gid != 0:
            problems.append(f"{directory} is {entry.st_uid}:{entry.st_gid}, expected 0:0")
        if stat.S_IMODE(entry.st_mode) != AGENT_DIR_MODE:
            problems.append(
                f"{directory} is {stat.S_IMODE(entry.st_mode):o}, expected {AGENT_DIR_MODE:o}"
            )
    if problems:
        return [_check("state_ownership", STATUS_FAILED, "; ".join(problems[:4]))]

    web_uid = _account_uid(WEB_USER)
    if web_uid is None:
        return [_check("state_ownership", STATUS_FAILED, f"the {WEB_USER} account does not exist")]
    for directory in paths.web_directories():
        try:
            if directory.stat().st_uid != web_uid:
                return [
                    _check(
                        "state_ownership",
                        STATUS_FAILED,
                        f"{directory} does not belong to {WEB_USER}",
                    )
                ]
        except OSError as exc:
            return [_check("state_ownership", STATUS_FAILED, f"{directory}: {exc.__class__.__name__}")]
    return [_check("state_ownership", STATUS_OK, "agent state is root-only, web state is web-owned")]


def _account_uid(name):
    import pwd

    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def check_web_reaches_agent(paths, *, live, user=WEB_USER):
    """Connect to the socket as the web account, the way the service will."""

    if not live:
        return [_check("web_to_agent", STATUS_DEFERRED, "the agent is not running yet")]
    import pwd

    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        return [_check("web_to_agent", STATUS_FAILED, f"the {user} account does not exist")]
    if os.geteuid() != 0:
        return [
            _check("web_to_agent", STATUS_DEFERRED, "run as root to test the web account's access")
        ]

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: drop to the web account and try the real socket
        os.close(read_fd)
        code = b"fork_failed"
        try:
            os.initgroups(user, entry.pw_gid)
            os.setgid(entry.pw_gid)
            os.setuid(entry.pw_uid)
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(15)
            connection.connect(str(paths.agent_socket))
            connection.sendall(b'{"operation": "status.get"}\n')
            code = b"ok" if connection.recv(64) else b"empty_reply"
            connection.close()
        except OSError as exc:
            code = str(exc.__class__.__name__).encode("ascii", "replace")
        finally:
            try:
                os.write(write_fd, code)
            finally:
                os._exit(0)

    os.close(write_fd)
    try:
        result = os.read(read_fd, 64).decode("ascii", "replace")
    finally:
        os.close(read_fd)
        os.waitpid(pid, 0)

    if result == "ok":
        return [_check("web_to_agent", STATUS_OK, f"{user} reached the agent socket")]
    return [
        _check("web_to_agent", STATUS_FAILED, f"{user} cannot use the agent socket: {result}")
    ]


EXPORT_SERVICE_UNIT = "ems-appliance-export.service"
EXPORT_PATH_UNIT = "ems-appliance-export.path"

# What the setup script reported, and what that means for the installation.
# "pending" and "unavailable" are states an operator can resolve later; the
# rest are the feature reporting success while it is broken.
EXPORT_STATUS_VERDICTS = {
    "configured": (STATUS_OK, False),
    "pending": (STATUS_OK, False),
    "unavailable": (STATUS_UNAVAILABLE, False),
    "degraded": (STATUS_FAILED, True),
    "failed": (STATUS_FAILED, True),
}


def _unit_enabled(runner, unit):
    if runner is None or not runner.available("systemctl"):
        return None
    return (runner.run("systemctl", ["is-enabled", unit], timeout=30).stdout or "").strip()


def _unit_failed(runner, unit):
    return _unit_active(runner, unit) == "failed"


def _recorded_export(paths):
    import json

    try:
        payload = json.loads(paths.export_status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def check_export(paths, runner, *, live, mounts=None, sshd=None):
    """Is the read-only SFTP export what the package says it is?

    OpenSSH may be absent and the EMS directories may not exist yet — both are
    reported and neither fails the package. A watcher that did not start, a
    refused export path or a bind that is mounted read-write are failures: they
    would otherwise be invisible until a backup was needed.
    """

    if not live:
        return [
            _check(name, STATUS_DEFERRED, "started on first boot", critical=False)
            for name in ("export_service", "export_path_unit", "export_setup")
        ]

    checks = []
    enabled = _unit_enabled(runner, EXPORT_SERVICE_UNIT)
    if _unit_failed(runner, EXPORT_SERVICE_UNIT):
        checks.append(
            _check(
                "export_service",
                STATUS_FAILED,
                f"{EXPORT_SERVICE_UNIT} is failed; backup authentication was disabled with it",
            )
        )
    elif enabled in ("enabled", "static", "enabled-runtime"):
        checks.append(_check("export_service", STATUS_OK, f"{EXPORT_SERVICE_UNIT} is {enabled}"))
    else:
        checks.append(
            _check(
                "export_service",
                STATUS_FAILED,
                f"{EXPORT_SERVICE_UNIT} is {enabled or 'unknown'}, expected enabled",
            )
        )

    active = _unit_active(runner, EXPORT_PATH_UNIT)
    if active in ("active", "waiting"):
        checks.append(_check("export_path_unit", STATUS_OK, f"{EXPORT_PATH_UNIT} is {active}"))
    else:
        checks.append(
            _check(
                "export_path_unit",
                STATUS_FAILED,
                f"{EXPORT_PATH_UNIT} is {active or 'unknown'}; a new EMS directory would "
                "never be published",
            )
        )

    checks.append(_check_export_setup(paths, mounts=mounts, sshd=sshd))
    return checks


def _check_export_setup(paths, *, mounts=None, sshd=None):
    """The status file is diagnostic input; the export root is the evidence."""

    from appliance.export_state import inspect_exports, verify_reported_exports

    recorded = _recorded_export(paths)
    if recorded is None:
        if sshd is False or (sshd is None and not _which("sshd")):
            return _check(
                "export_setup",
                STATUS_UNAVAILABLE,
                "openssh-server is not installed; SFTP backup access is unavailable",
                critical=False,
            )
        return _check("export_setup", STATUS_FAILED, "the export setup never reported a state")

    status = str(recorded.get("status") or "")
    verdict, critical = EXPORT_STATUS_VERDICTS.get(status, (STATUS_FAILED, True))
    detail = str(recorded.get("detail") or "").strip()
    summary = f"the export setup reports {status or 'nothing'}" + (f": {detail}" if detail else "")

    state = inspect_exports(paths, mounts=_mount_table(paths) if mounts is None else mounts)
    if state["unmanaged"] and status != "unavailable":
        return _check(
            "export_setup",
            STATUS_FAILED,
            "the SFTP chroot is not exclusive: " + ", ".join(state["unmanaged"]),
        )
    if verdict != STATUS_OK or status != "configured":
        return _check("export_setup", verdict, summary, critical=critical)

    problems = verify_reported_exports(recorded, state, paths)
    if problems:
        return _check("export_setup", STATUS_FAILED, "; ".join(problems[:4]))
    return _check("export_setup", STATUS_OK, summary)


def _mount_table(paths):
    from appliance.hostprobe import HostProbe

    try:
        return HostProbe(None).mount_records()
    except Exception:
        return {}


def check_host_paths(paths, config=None, runner=None):
    """The generated host-path files must still agree with the configuration."""

    from appliance.config import load_config
    from appliance.host_config import describe

    try:
        report = describe(paths, config or load_config(paths), runner=runner)
    except Exception as exc:
        return [_check("host_paths", STATUS_FAILED, f"{exc.__class__.__name__}: {exc}")]
    if not report["environment_present"]:
        return [
            _check(
                "host_paths",
                STATUS_FAILED,
                f"{report['environment_file']} is missing; run 'ems-appliance host-config --apply'",
            )
        ]
    if not report["consistent"]:
        return [
            _check(
                "host_paths",
                STATUS_FAILED,
                "the generated host configuration disagrees with appliance.conf: "
                + ", ".join(report["drift"] or ["the watched path"]),
            )
        ]
    return [
        _check(
            "host_paths",
            STATUS_OK,
            f"install root {report['install_root']}, export root {report['export_root']}",
        )
    ]


BACKUP_ACCOUNT_RECORD = "backup-account.json"
NOLOGIN_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false")


def backup_account_state(paths, config):
    """What the host says about the package-owned backup account."""

    import json
    import pwd

    record = {}
    try:
        payload = json.loads(
            (paths.package_state_dir / BACKUP_ACCOUNT_RECORD).read_text(encoding="utf-8")
        )
        record = payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        record = {}

    name = config.backup_user
    try:
        entry = pwd.getpwnam(name)
        home, shell, exists = entry.pw_dir, entry.pw_shell, True
    except KeyError:
        home, shell, exists = str(record.get("home") or ""), "", False

    keys_dir = os.path.join(home, ".ssh") if home else ""
    conflicts = []
    if keys_dir and os.path.isdir(keys_dir):
        conflicts = sorted(
            item
            for item in os.listdir(keys_dir)
            if item.startswith("authorized_keys.disabled-by-appliance.conflict")
        )
    return {
        "account": name,
        "exists": exists,
        "package_owned": bool(record.get("created_by_package")) and record.get("account") == name,
        "home": home,
        "shell": shell,
        "expected_shell": shell in NOLOGIN_SHELLS if exists else None,
        "keys_active": bool(keys_dir and os.path.isfile(os.path.join(keys_dir, "authorized_keys"))),
        "keys_disabled": bool(
            keys_dir
            and os.path.isfile(os.path.join(keys_dir, "authorized_keys.disabled-by-appliance"))
        ),
        "keys_conflicted": conflicts,
    }


def check_backup_account(paths, config=None):
    """A backup account this package does not own cannot be removed safely."""

    from appliance.config import load_config

    try:
        state = backup_account_state(paths, config or load_config(paths))
    except Exception as exc:
        return [_check("backup_account", STATUS_FAILED, f"{exc.__class__.__name__}: {exc}")]

    if not state["exists"]:
        return [
            _check(
                "backup_account",
                STATUS_UNAVAILABLE,
                f"the {state['account']} account does not exist; SFTP backup access is unavailable",
                critical=False,
            )
        ]
    if not state["package_owned"]:
        return [
            _check(
                "backup_account",
                STATUS_FAILED,
                f"{state['account']} exists but this package has no ownership record for it; "
                "removal would not be able to withdraw it safely",
            )
        ]
    if state["keys_conflicted"]:
        return [
            _check(
                "backup_account",
                STATUS_FAILED,
                f"{state['account']} has unresolved key files ("
                + ", ".join(state["keys_conflicted"])
                + "); authentication stays disabled until they are resolved",
            )
        ]
    if not state["expected_shell"]:
        return [
            _check(
                "backup_account",
                STATUS_FAILED,
                f"{state['account']} has the shell {state['shell'] or 'unknown'}, expected a nologin shell",
            )
        ]
    return [
        _check(
            "backup_account",
            STATUS_OK,
            f"{state['account']} is package-owned, has no shell and its key is "
            + ("active" if state["keys_active"] else "disabled"),
        )
    ]


def check_optional_features():
    checks = []
    for name, tool, purpose in OPTIONAL_FEATURES:
        if _which(tool):
            checks.append(_check(name, STATUS_OK, f"{tool} is available", critical=False))
        else:
            checks.append(
                _check(
                    name,
                    STATUS_UNAVAILABLE,
                    f"{tool} is not installed; {purpose} is unavailable",
                    critical=False,
                )
            )
    return checks


def verify_installation(paths=None, *, runner=None, live=None):
    """Return a report; ``ok`` is false only when a critical check failed."""

    paths = paths or resolve_paths()
    if runner is None:
        from appliance.commands import CommandRunner

        runner = CommandRunner()
    live = systemd_running() if live is None else bool(live)

    checks = []
    checks.extend(check_directories(paths))
    checks.extend(check_ownership(paths))
    checks.extend(check_host_paths(paths, runner=runner))
    checks.extend(check_units(runner, live=live))
    checks.extend(check_socket(paths, live=live))
    checks.extend(check_web_reaches_agent(paths, live=live))
    checks.extend(check_export(paths, runner, live=live))
    checks.extend(check_backup_account(paths))
    checks.extend(check_optional_features())

    failures = [item for item in checks if item["critical"] and item["status"] == STATUS_FAILED]
    return {
        "ok": not failures,
        "live_system": live,
        "checks": checks,
        "failures": [f"{item['check']}: {item['detail']}" for item in failures],
        "unavailable": [
            item["check"] for item in checks if item["status"] == STATUS_UNAVAILABLE
        ],
    }
