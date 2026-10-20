# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only bridge from Admin Maintenance to the installed EMS ``emsctl.py``.

Admin is orchestration/UI only; EMS-specific checks stay in EMS Core. This
helper never invents EMS logic — it runs a small allowlist of *read-only*
``emsctl.py`` commands against the installed system and returns their structured
output. The frontend only ever sends a named check id; raw command arguments are
never accepted, ``shell=True`` is never used, and nothing here mutates config,
data, containers, images or backups.

Execution mode is chosen for safety, preferring the installed version:

- ``container``: a running EMS container is present -> ``docker exec`` uses the
  installed EMS and its mounted config/data.
- ``local``: no running container, but an ``emsctl.py`` exists in the install
  root -> run it with an explicit ``--config`` pointing at the install config.
- ``unavailable``: neither is available; the overview still renders normally.
"""

import json
import subprocess
import time
from pathlib import Path

from admin.ems_tool import (
    CONTAINER_EMSCTL_PATH,
    EXEC_CLIENT_GRACE_SECONDS,
    guard_not_invocable,
    guard_stopped_command,
    guarded_container_command,
    resolve_running_ems_container,
)
from admin.install_context import detect_install_context
from ems.influx_setup import (
    INFLUX_PROBE_READY_TIMEOUT_SECONDS,
    INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS,
)

# Output caps keep a misbehaving/old EMS build from returning unbounded text.
STDOUT_CAP = 64 * 1024
STDERR_CAP = 32 * 1024

UNAVAILABLE_MESSAGE = (
    "EMS CLI diagnostics are unavailable because no running EMS container or "
    "local emsctl.py was found."
)

# A disabled subsystem is an expected config state, not a diagnostic failure.
# EMS Core stays the source of truth: Admin runs the real command and only
# reclassifies this one known result for display.
INFLUX_DISABLED_TEXT = "influxdb is disabled in config"
INFLUX_DISABLED_MESSAGE = "InfluxDB is disabled in config. Analytics is not configured."
INFLUX_SCHEMA_INCOMPLETE_MESSAGE = (
    "The analytics schema is incomplete (missing: {missing}). Repair analytics "
    "in EMS services creates it now; while influxdb.auto_sync is on, the EMS "
    "also creates it by itself."
)
FULL_SYNC_COMMAND = "docker compose exec ems python3 emsctl.py influx sync"
INFLUX_RETENTION_LONGER_MESSAGE = (
    "Retention is longer than the config: {buckets}. Restart EMS now and Repair "
    "analytics shorten a retention only when no data falls out of it; to apply "
    "it anyway, which deletes the older data, run in the installation directory: "
    + FULL_SYNC_COMMAND
)
INFLUX_RETENTION_SHORTER_MESSAGE = (
    "Retention is shorter than the config: {buckets}. Repair analytics "
    "lengthens it; nothing is deleted."
)
INFLUX_TASKS_OFF_MESSAGE = (
    "Downsampling tasks are switched off: {tasks}. Neither the EMS nor Repair "
    "analytics switches a task back on; run in the installation directory: "
    + FULL_SYNC_COMMAND
)
INFLUX_DISABLED_HINT = (
    "Enable Analytics / bundled InfluxDB in Configuration & Hardware if you want "
    "history and analytics."
)

# Named read-only checks mapped to exact emsctl argv suffixes. ``args`` is the
# emsctl subcommand only; the config/exec prefix is added per execution mode so
# the frontend can never influence the command. ``warn_on_fail`` marks checks
# (InfluxDB) that legitimately fail when an *enabled* subsystem is not reachable —
# a normal warning, not an Admin error.

# The readiness wait this check runs, plus the requests that follow it: one per
# planned bucket and one for the task list, each able to spend its budget twice
# (connect, then read), and a docker exec and interpreter start around all of
# it. The checks run one after another, so every second here is a second the
# Admin diagnostics and the guided-upgrade health gate can spend.
#
# The request count comes from the operator's `influxdb.downsampling`, not from
# this file: one lookup per planned bucket plus the task list, so a chain of L
# levels costs L+2. Eight covers six levels, twice what the template ships. A
# test counts the template's and asserts the room on top, because a check killed
# by its own ceiling is reported as a hard failure -- the opposite of the
# warning this check is marked for.
INFLUX_STATUS_CHECK_REQUEST_ALLOWANCE = 8
INFLUX_STATUS_CHECK_TIMEOUT_SECONDS = (
    INFLUX_PROBE_READY_TIMEOUT_SECONDS
    + INFLUX_STATUS_CHECK_REQUEST_ALLOWANCE * 2 * INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS
    + 25
)

CHECKS = {
    "quick_diagnose": {
        "label": "EMS diagnose",
        "args": ("diagnose", "--json"),
        "timeout": 15,
        "parse_json": True,
        "warn_on_fail": False,
    },
    "config_upgrade_dry_run": {
        "label": "Config upgrade (dry-run)",
        "args": ("config", "upgrade", "--dry-run"),
        "timeout": 15,
        "parse_json": False,
        "warn_on_fail": False,
    },
    "influx_status": {
        "label": "InfluxDB status",
        "args": ("influx", "status", "--json"),
        # A ceiling, not a delay. Too tight and a slow-but-alive InfluxDB is
        # killed and reported as a failure, where this check exists to warn.
        "timeout": INFLUX_STATUS_CHECK_TIMEOUT_SECONDS,
        "parse_json": True,
        "warn_on_fail": True,
    },
    "runtime_status": {
        "label": "Runtime status",
        "args": ("status",),
        "timeout": 15,
        "parse_json": False,
        "warn_on_fail": False,
    },
}

CHECK_ORDER = (
    "quick_diagnose",
    "config_upgrade_dry_run",
    "influx_status",
    "runtime_status",
)

_SUMMARY_BUCKET = {
    "ok": "ok",
    # A subsystem disabled by config is an expected state, not a warning. It is
    # counted separately so the summary can say "… · InfluxDB disabled".
    "disabled": "disabled",
    "warning": "warning",
    "failed": "failed",
    "timeout": "failed",
    "unavailable": "unavailable",
    "not_run": "unavailable",
}


class EmsCliDiagnostics:
    """Runs allowlisted read-only ``emsctl.py`` checks against the install."""

    def __init__(
        self,
        install_context_provider=detect_install_context,
        docker=None,
        run=None,
    ):
        self._install_context_provider = install_context_provider
        self._docker = docker
        self._run = run or subprocess.run

    def run(self, check_ids=None):
        mode = self._resolve_mode()
        if mode["mode"] == "unavailable":
            return {
                "available": False,
                "mode": "unavailable",
                "container": None,
                "checks": [],
                "summary": _summarize([]),
                "message": UNAVAILABLE_MESSAGE,
            }

        ids = [cid for cid in (check_ids or CHECK_ORDER) if cid in CHECKS]
        checks = [self._run_check(cid, mode) for cid in ids]
        return {
            "available": True,
            "mode": mode["mode"],
            "container": mode.get("container"),
            "checks": checks,
            "summary": _summarize(checks),
        }

    # --- mode resolution -------------------------------------------------

    def _resolve_mode(self):
        context = self._install_context_provider()
        container = resolve_running_ems_container(self._docker_cli(), context)
        if container is not None:
            return {"mode": "container", "container": container}
        emsctl_path = self._local_emsctl(context)
        if emsctl_path is not None:
            return {
                "mode": "local",
                "emsctl_path": emsctl_path,
                "config_path": Path(context.config_path),
                "cwd": Path(context.install_root),
            }
        return {"mode": "unavailable"}

    def _docker_cli(self):
        if self._docker is not None:
            return self._docker
        from admin.deployment import DockerCli

        return DockerCli()

    @staticmethod
    def _local_emsctl(context):
        candidate = Path(context.install_root) / "emsctl.py"
        try:
            return candidate if candidate.is_file() else None
        except OSError:
            return None

    # --- command execution ----------------------------------------------

    def _argv(self, spec, mode, bounded=True):
        if mode["mode"] == "container":
            command = ["python3", CONTAINER_EMSCTL_PATH, *spec["args"]]
            if bounded:
                # Same orphan as the EMS tool runner: killing the client here
                # leaves the check running inside the container.
                command = guarded_container_command(command, spec["timeout"])
            return ["docker", "exec", mode["container"], *command]
        return [
            "python3",
            str(mode["emsctl_path"]),
            "--config",
            str(mode["config_path"]),
            *spec["args"],
        ]

    def _run_check(self, check_id, mode):
        spec = CHECKS[check_id]
        in_container = mode["mode"] == "container"
        argv = self._argv(spec, mode)
        cwd = str(mode["cwd"]) if mode["mode"] == "local" else None
        # A local check is this process's own child, so its ceiling already ends
        # it; only the container run needs the client to wait past the guard.
        ceiling = spec["timeout"] + (EXEC_CLIENT_GRACE_SECONDS if in_container else 0)
        started = time.monotonic()
        try:
            result = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=ceiling,
                cwd=cwd,
            )
            if in_container and guard_not_invocable(result):
                in_container = False  # nothing guarded the retry
                result = self._run(
                    self._argv(spec, mode, bounded=False),
                    capture_output=True,
                    text=True,
                    timeout=spec["timeout"],
                    cwd=cwd,
                )
        except subprocess.TimeoutExpired:
            # A check that warns when the subsystem is unreachable warns when it
            # is too slow to answer as well. Reporting a hard failure there
            # fails the guided-upgrade gate closed over a slow InfluxDB, which
            # is what `warn_on_fail` exists to prevent -- and the raised ceiling
            # only makes the kill rarer, not friendlier.
            status = "warning" if spec["warn_on_fail"] else "timeout"
            return _check_result(
                check_id, spec, status, None,
                "", "Check timed out.", _elapsed_ms(started), False,
            )
        except FileNotFoundError:
            return _check_result(
                check_id, spec, "unavailable", None,
                "", "The command runner is not available.",
                _elapsed_ms(started), False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _check_result(
                check_id, spec, "failed", None,
                "", f"Could not run the check: {exc}",
                _elapsed_ms(started), False,
            )

        if in_container and guard_stopped_command(
            result.returncode, time.monotonic() - started, spec["timeout"]
        ):
            # The guard stopped it, which is the same outcome the client's own
            # ceiling reports -- including for a check marked to warn.
            status = "warning" if spec["warn_on_fail"] else "timeout"
            return _check_result(
                check_id, spec, status, None,
                "", "Check timed out.", _elapsed_ms(started), False,
            )

        exit_code = int(result.returncode)
        stdout, out_trunc = _truncate(result.stdout, STDOUT_CAP)
        stderr, err_trunc = _truncate(result.stderr, STDERR_CAP)
        parsed = _parse_json(stdout) if spec["parse_json"] and exit_code == 0 else None
        status = _status_for(exit_code, spec)
        message = None
        if check_id == "influx_status" and _influx_disabled_in_output(stdout, stderr):
            status = "disabled"
            message = INFLUX_DISABLED_MESSAGE
        elif check_id == "influx_status" and status == "ok":
            message = _influx_schema_message(parsed)
            if message:
                status = "warning"
        return _check_result(
            check_id, spec, status, exit_code, stdout, stderr,
            _elapsed_ms(started), out_trunc or err_trunc, parsed, message,
        )


def _status_for(exit_code, spec):
    if exit_code == 0:
        return "ok"
    return "warning" if spec["warn_on_fail"] else "failed"


def _influx_schema_message(parsed):
    """What `influx status` found incomplete, worded for its fix; it exits 0 either way.

    Its ``missing_tasks`` counts a task that exists but is switched off, which no
    repair creates, so those are named apart from the ones that are absent.
    """

    report = parsed.get("report") if isinstance(parsed, dict) else None
    if not isinstance(report, dict):
        return None

    def names(key):
        values = report.get(key)
        return [str(value) for value in values] if isinstance(values, list) else []

    present = {
        str(task.get("name"))
        for task in report.get("tasks") or []
        if isinstance(task, dict)
    }
    missing_tasks = names("missing_tasks")
    absent = names("missing_buckets") + [name for name in missing_tasks if name not in present]
    off = [name for name in missing_tasks if name in present]
    longer, shorter = [], []
    for bucket in report.get("buckets") or []:
        if not isinstance(bucket, dict) or not bucket.get("exists"):
            continue
        live = _seconds(bucket.get("retention_seconds"))
        wanted = _seconds(bucket.get("expected_retention_seconds"))
        if live is None or wanted is None or live == wanted:
            continue
        text = f"{bucket.get('name')} keeps {_days(live)} (config {_days(wanted)})"
        (longer if _keeps_longer(live, wanted) else shorter).append(text)
    parts = []
    if absent:
        parts.append(INFLUX_SCHEMA_INCOMPLETE_MESSAGE.format(missing=", ".join(absent)))
    if off:
        parts.append(INFLUX_TASKS_OFF_MESSAGE.format(tasks=", ".join(off)))
    if longer:
        parts.append(INFLUX_RETENTION_LONGER_MESSAGE.format(buckets="; ".join(longer)))
    if shorter:
        parts.append(INFLUX_RETENTION_SHORTER_MESSAGE.format(buckets="; ".join(shorter)))
    return " ".join(parts) or None


def _seconds(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return None


def _keeps_longer(live, wanted):
    """0 is forever, so it is longer than any finite retention."""

    if live == 0:
        return wanted != 0
    return wanted != 0 and live > wanted


def _days(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return "unknown"
    return f"{seconds // 86400} days" if seconds else "forever"


def _influx_disabled_in_output(stdout, stderr):
    haystack = f"{stdout or ''}\n{stderr or ''}".lower()
    return INFLUX_DISABLED_TEXT in haystack


def _check_result(
    check_id, spec, status, exit_code, stdout, stderr, duration_ms, truncated,
    parsed=None, message=None,
):
    return {
        "id": check_id,
        "label": spec["label"],
        "status": status,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "json": parsed,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": bool(truncated),
        "message": message,
    }


def _summarize(checks):
    counts = {"ok": 0, "disabled": 0, "warning": 0, "failed": 0, "unavailable": 0}
    for check in checks:
        counts[_SUMMARY_BUCKET.get(check["status"], "unavailable")] += 1
    if counts["failed"]:
        status = "failed"
    elif counts["warning"]:
        status = "warning"
    elif counts["ok"] or counts["disabled"]:
        status = "ok"
    else:
        status = "unavailable"
    return {"status": status, **counts}


def _truncate(text, cap):
    text = text or ""
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def _parse_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)
