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
    resolve_running_ems_container,
)
from admin.install_context import detect_install_context

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
INFLUX_DISABLED_HINT = (
    "Enable Analytics / bundled InfluxDB in Configuration & Hardware if you want "
    "history and analytics."
)

# Named read-only checks mapped to exact emsctl argv suffixes. ``args`` is the
# emsctl subcommand only; the config/exec prefix is added per execution mode so
# the frontend can never influence the command. ``warn_on_fail`` marks checks
# (InfluxDB) that legitimately fail when an *enabled* subsystem is not reachable —
# a normal warning, not an Admin error.
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
        "timeout": 20,
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

    def _argv(self, spec, mode):
        if mode["mode"] == "container":
            return [
                "docker",
                "exec",
                mode["container"],
                "python3",
                CONTAINER_EMSCTL_PATH,
                *spec["args"],
            ]
        return [
            "python3",
            str(mode["emsctl_path"]),
            "--config",
            str(mode["config_path"]),
            *spec["args"],
        ]

    def _run_check(self, check_id, mode):
        spec = CHECKS[check_id]
        argv = self._argv(spec, mode)
        cwd = str(mode["cwd"]) if mode["mode"] == "local" else None
        started = time.monotonic()
        try:
            result = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=spec["timeout"],
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            return _check_result(
                check_id, spec, "timeout", None,
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

        exit_code = int(result.returncode)
        stdout, out_trunc = _truncate(result.stdout, STDOUT_CAP)
        stderr, err_trunc = _truncate(result.stderr, STDERR_CAP)
        parsed = _parse_json(stdout) if spec["parse_json"] and exit_code == 0 else None
        status = _status_for(exit_code, spec)
        message = None
        if check_id == "influx_status" and _influx_disabled_in_output(stdout, stderr):
            status = "disabled"
            message = INFLUX_DISABLED_MESSAGE
        return _check_result(
            check_id, spec, status, exit_code, stdout, stderr,
            _elapsed_ms(started), out_trunc or err_trunc, parsed, message,
        )


def _status_for(exit_code, spec):
    if exit_code == 0:
        return "ok"
    return "warning" if spec["warn_on_fail"] else "failed"


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
