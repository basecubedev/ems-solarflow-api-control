# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only EMS diagnose service layer.

Extracted verbatim from emsctl.py so that both the CLI (emsctl.py) and the
dashboard can consume the same diagnosis functions without importing the CLI.
This module is import-side-effect-free and must never import emsctl.

The report shape is a versioned public contract (see docs/developer/developer.md and
tests/test_emsctl_diagnose_contract.py); do not change it incompatibly without
bumping DIAGNOSE_SCHEMA_VERSION / SUPPORT_BUNDLE_VERSION.
"""

import argparse
import json
import math
import os
import platform
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dashboard import auth as dashboard_auth
from ems import config as config_mod
from ems.health import (
    CommHealth,
    render_device_health,
    render_grid_meter_health,
)
from ems.paths import (
    BASE_DIR,
    resolve_project_path,
    resolve_runtime_path,
    resolve_dashboard_auth_path,
    resolve_template_path,
)
from ems.build_info import collect_build_info
from ems.zendure_mqtt import config_entries as zendure_mqtt_entries


BATTERY_FULL_CHARGE_ASSIST_DEFAULTS = {
    "enabled": True,
    "interval_days": 28,
    "assist_window_days": 7,
    "assist_start_soc": 80,
    "force_time": "14:00",
    "ac_charge_power": 200,
    "enable_ac_charge_mode": True,
    "state_database_path": "data/ems_state.sqlite",
}


DIAGNOSE_REDACT_KEYWORDS = (
    "password",
    "passwd",
    "password_hash",
    "token",
    "secret",
    "key",
    "auth",
    "credential",
    "credentials",
    "username",
    "mqtt",
    "hash",
    "serial",
    "sn",
    "device_id",
    "api",
    "bearer",
    "cookie",
    "session",
)

DIAGNOSE_SCHEMA_VERSION = 1
SUPPORT_BUNDLE_VERSION = 1
ROOT_CAUSE_SEVERITIES = ("info", "warning", "error")

@dataclass
class DiagnosisSection:
    id: str
    title: str
    status: str
    metrics: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@dataclass
class DiagnosisResult:
    version: int
    timestamp: str
    status: str
    sections: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    root_causes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


DIAGNOSE_LOG_PATTERNS = (
    ("permission_denied", re.compile(r"permission denied", re.I)),
    ("json_decode", re.compile(r"json decode|jsondecode|invalid json", re.I)),
    ("shelly_read_error", re.compile(r"shelly_read_error", re.I)),
    ("runtime_state_write_error", re.compile(r"runtime_state_write_error", re.I)),
    ("dashboard_auth", re.compile(r"dashboard auth|dashboard_auth", re.I)),
    ("csrf", re.compile(r"csrf", re.I)),
    ("preflight_failed", re.compile(r"preflight_failed", re.I)),
    ("preflight_abort", re.compile(r"preflight_abort", re.I)),
    ("device_unreachable", re.compile(r"device_unreachable", re.I)),
    ("traceback", re.compile(r"traceback", re.I)),
)

EXPORT_PEAK_WARNING_W = 100
EXPORT_PEAK_ERROR_W = 250
EXPORT_DURATION_WARNING_PERCENT = 20
EXPORT_AVERAGE_WARNING_W = 40
NEAR_ZERO_BAND_W = 30
SOC_SPREAD_WARNING_PERCENT = 15
SOC_SPREAD_ERROR_PERCENT = 30
LOW_SOC_PROTECTION_MARGIN_PERCENT = 3


def diagnose_add(checks, section, level, code, message, hint=None, docs=None, **details):
    check = {
        "section": section,
        "level": level,
        "code": code,
        "message": message,
        "details": details,
    }
    if hint:
        check["hint"] = hint
    if docs:
        check["docs"] = docs
    checks.append(check)


def diagnose_section_title(section_id):
    labels = {
        "environment": "Environment",
        "project": "Project structure",
        "runtime_paths": "Runtime paths",
        "config": "Config",
        "runtime_state": "Runtime state",
        "data": "Data/database",
        "dashboard": "Dashboard",
        "logs": "Logs",
        "docker": "Docker",
        "hardware": "Hardware",
        "control": "Control diagnostics",
        "control_quality": "Control quality",
    }
    return labels.get(section_id, section_id.replace("_", " ").title())


def diagnose_status_from_checks(checks):
    if any(check.get("level") == "error" for check in checks):
        return "error"
    if any(check.get("level") == "warning" for check in checks):
        return "warning"
    return "ok"


def diagnose_slug(value):
    slug = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    return slug or "unknown_root_cause"


def diagnose_standard_root_cause(value, source="diagnose"):
    if isinstance(value, dict):
        code = str(value.get("code") or diagnose_slug(value.get("title") or value.get("message") or source))
        severity = str(value.get("severity") or "warning").lower()
        if severity not in ROOT_CAUSE_SEVERITIES:
            severity = "warning"
        title = str(value.get("title") or code.replace("_", " ").title())
        message = str(value.get("message") or title)
        suggested = str(
            value.get("suggested_next_check")
            or "Review the related diagnose section for details."
        )
    else:
        title = str(value)
        code = diagnose_slug(title)
        severity = "warning"
        message = title
        suggested = "Review the related diagnose section for details."
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "message": message,
        "suggested_next_check": suggested,
    }


def diagnose_dedupe_root_causes(root_causes):
    deduped = []
    seen = set()
    for cause in root_causes:
        standard = diagnose_standard_root_cause(cause)
        key = (
            standard["code"],
            standard["severity"],
            standard["title"],
            standard["message"],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(standard)
    return deduped


def diagnose_build_sections(checks):
    sections = []
    for section_id in sorted({check.get("section", "unknown") for check in checks}):
        section_checks = [check for check in checks if check.get("section") == section_id]
        sections.append(asdict(DiagnosisSection(
            id=section_id,
            title=diagnose_section_title(section_id),
            status=diagnose_status_from_checks(section_checks),
            metrics={
                "ok": sum(1 for check in section_checks if check.get("level") == "ok"),
                "warning": sum(1 for check in section_checks if check.get("level") == "warning"),
                "error": sum(1 for check in section_checks if check.get("level") == "error"),
            },
            warnings=[
                check.get("message")
                for check in section_checks
                if check.get("level") == "warning"
            ],
            errors=[
                check.get("message")
                for check in section_checks
                if check.get("level") == "error"
            ],
        )))
    return sections


def diagnose_finalize_report(report):
    if report.get("control") and isinstance(report["control"].get("root_causes"), list):
        report["control"]["root_causes"] = diagnose_dedupe_root_causes(
            report["control"]["root_causes"]
        )
    if report.get("control_quality") and isinstance(report["control_quality"].get("root_causes"), list):
        report["control_quality"]["root_causes"] = diagnose_dedupe_root_causes(
            report["control_quality"]["root_causes"]
        )

    root_causes = []
    if report.get("control"):
        root_causes.extend(report["control"].get("root_causes", []))
    if report.get("control_quality"):
        root_causes.extend(report["control_quality"].get("root_causes", []))
    root_causes = diagnose_dedupe_root_causes(root_causes)

    warnings = [
        check.get("message")
        for check in report.get("checks", [])
        if check.get("level") == "warning"
    ]
    errors = [
        check.get("message")
        for check in report.get("checks", [])
        if check.get("level") == "error"
    ]
    sections = diagnose_build_sections(report.get("checks", []))
    model = DiagnosisResult(
        version=DIAGNOSE_SCHEMA_VERSION,
        timestamp=report.get("generated_at"),
        status=report.get("status", "unknown"),
        sections=sections,
        metrics=report.get("summary", {}),
        root_causes=root_causes,
        warnings=warnings,
        errors=errors,
    )

    build = collect_build_info()
    report["schema_version"] = DIAGNOSE_SCHEMA_VERSION
    report["ems_version"] = build["ems_version"]
    # ``build_serial`` is intentionally omitted: the diagnose output is run
    # through secret redaction, which treats any ``*serial*`` field as sensitive.
    report["build"] = {
        "release_version": build["release_version"],
        "build_label": build["build_label"],
        "git_commit_short": build["git_commit_short"],
        "git_branch": build["git_branch"],
        "git_describe": build["git_describe"],
        "git_dirty": build["git_dirty"],
        "channel": build["channel"],
        "build_id": build["build_id"],
    }
    report["diagnosis"] = asdict(model)
    report["sections"] = sections
    report["metrics"] = report.get("summary", {})
    report["root_causes"] = root_causes
    report["warnings"] = warnings
    report["errors"] = errors
    return report


def diagnose_json_file(path):
    if not os.path.exists(path):
        return None, "missing"
    try:
        with open(path) as f:
            return json.load(f), None
    except Exception as exc:
        return None, str(exc)


def diagnose_read_json_if_nonempty(path):
    if not os.path.exists(path):
        return None
    try:
        if os.path.getsize(path) == 0:
            return None
        with open(path) as f:
            json.load(f)
        return None
    except Exception as exc:
        return str(exc)


def diagnose_check_path(checks, section, code_prefix, path, label, *,
                        expect_file=False, expect_dir=False,
                        require_exists=True, check_read=False,
                        check_write=False, missing_level="error"):
    if os.path.exists(path):
        diagnose_add(
            checks,
            section,
            "ok",
            f"{code_prefix}_exists",
            f"{label} exists: {path}",
            path=path,
        )
    elif require_exists:
        diagnose_add(
            checks,
            section,
            missing_level,
            f"{code_prefix}_missing",
            f"{label} missing: {path}",
            path=path,
        )
        return
    else:
        diagnose_add(
            checks,
            section,
            "warning",
            f"{code_prefix}_missing",
            f"{label} does not exist: {path}",
            path=path,
        )
        return

    if expect_file and not os.path.isfile(path):
        diagnose_add(
            checks,
            section,
            "error",
            f"{code_prefix}_not_file",
            f"{label} is not a file: {path}",
            path=path,
        )
    if expect_dir and not os.path.isdir(path):
        diagnose_add(
            checks,
            section,
            "error",
            f"{code_prefix}_not_dir",
            f"{label} is not a directory: {path}",
            path=path,
        )
    if check_read:
        level = "ok" if os.access(path, os.R_OK) else "error"
        diagnose_add(
            checks,
            section,
            level,
            f"{code_prefix}_readable",
            f"{label} readable: {path}" if level == "ok" else f"{label} not readable: {path}",
            path=path,
        )
    if check_write:
        level = "ok" if os.access(path, os.W_OK) else "error"
        diagnose_add(
            checks,
            section,
            level,
            f"{code_prefix}_writable",
            f"{label} writable: {path}" if level == "ok" else f"{label} not writable: {path}",
            path=path,
        )


def diagnose_parent_path(checks, section, code_prefix, path, label,
                         *, check_write=False, missing_level="error"):
    parent = os.path.dirname(path) or "."
    diagnose_check_path(
        checks,
        section,
        f"{code_prefix}_parent",
        parent,
        f"{label} parent directory",
        expect_dir=True,
        check_write=check_write,
        missing_level=missing_level,
    )


def diagnose_missing_template_keys(config_data, template_data):
    missing = []

    def walk(current, template, prefix):
        if isinstance(template, dict):
            current_dict = current if isinstance(current, dict) else {}
            for key, value in template.items():
                if str(key).startswith("_comment"):
                    continue
                path = f"{prefix}.{key}" if prefix else str(key)
                if key not in current_dict:
                    missing.append(path)
                    continue
                walk(current_dict[key], value, path)
            return

        if (
            isinstance(template, list)
            and template
            and isinstance(template[0], dict)
            and isinstance(current, list)
        ):
            for index, item in enumerate(current):
                if isinstance(item, dict):
                    walk(item, template[0], f"{prefix}.{index}")

    walk(config_data, template_data, "")
    return sorted(missing)


def diagnose_container_mode():
    sources = []

    if os.path.exists("/.dockerenv"):
        sources.append({
            "source": "/.dockerenv",
            "type": "file",
            "reason": "Docker marker file exists",
        })

    for cgroup_path in ("/proc/1/cgroup", "/proc/self/cgroup"):
        try:
            with open(cgroup_path) as f:
                content = f.read()
        except OSError:
            continue
        lowered = content.lower()
        matches = [
            token
            for token in ("docker", "containerd", "kubepods", "libpod", "podman")
            if token in lowered
        ]
        if matches:
            sources.append({
                "source": cgroup_path,
                "type": "cgroup",
                "reason": "Container cgroup token found",
                "tokens": matches,
            })

    for path in ("/app/config", "/app/data"):
        if os.path.exists(path):
            sources.append({
                "source": path,
                "type": "path",
                "reason": "Container-style EMS path exists",
            })

    if sources:
        return "container", sources
    return "native", []


def diagnose_path_within(path, parent):
    if not path:
        return False
    try:
        return os.path.commonpath([
            os.path.abspath(path),
            os.path.abspath(parent),
        ]) == os.path.abspath(parent)
    except ValueError:
        return False


def diagnose_uid_gid(path):
    try:
        stat_result = os.stat(path)
    except OSError as exc:
        return None, str(exc)
    return {
        "uid": stat_result.st_uid,
        "gid": stat_result.st_gid,
    }, None


def diagnose_float(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def diagnose_int(value):
    parsed = diagnose_float(value)
    if parsed is None or int(parsed) != parsed:
        return None
    return int(parsed)


def diagnose_bool(value):
    return isinstance(value, bool)


def diagnose_clamped_int(value, default, minimum=None, maximum=None):
    parsed = diagnose_int(value)
    if parsed is None:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def diagnose_normalize_force_time(value):
    text = str(value or BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["force_time"]).strip()
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def diagnose_battery_full_charge_assist_config(config_data):
    raw = {}
    if isinstance(config_data, dict) and isinstance(
        config_data.get("battery_full_charge_assist"),
        dict
    ):
        raw = config_data.get("battery_full_charge_assist")
    merged = {
        **BATTERY_FULL_CHARGE_ASSIST_DEFAULTS,
        **raw,
    }
    return {
        "enabled": bool(merged.get("enabled", False)),
        "interval_days": diagnose_clamped_int(
            merged.get("interval_days"),
            BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["interval_days"],
            minimum=1,
        ),
        "assist_window_days": diagnose_clamped_int(
            merged.get("assist_window_days"),
            BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["assist_window_days"],
            minimum=0,
        ),
        "assist_start_soc": diagnose_clamped_int(
            merged.get("assist_start_soc"),
            BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["assist_start_soc"],
            minimum=0,
            maximum=100,
        ),
        "force_time": diagnose_normalize_force_time(merged.get("force_time")),
        "ac_charge_power": diagnose_clamped_int(
            merged.get("ac_charge_power"),
            BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["ac_charge_power"],
            minimum=0,
        ),
        "enable_ac_charge_mode": bool(merged.get("enable_ac_charge_mode", True)),
        "state_database_path": str(
            merged.get(
                "state_database_path",
                BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["state_database_path"],
            )
        ),
    }


def diagnose_config_device_names(config_data):
    devices = config_data.get("devices", []) if isinstance(config_data, dict) else []
    if not isinstance(devices, list):
        return []
    return [
        str(item.get("name"))
        for item in devices
        if isinstance(item, dict) and item.get("name")
    ]


def diagnose_path_is_clean(path):
    if not isinstance(path, str) or not path.strip():
        return False
    return "\x00" not in path and os.path.normpath(path)


def diagnose_basic_url(value):
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def diagnose_git_info(checks):
    git_dir = os.path.join(BASE_DIR, ".git")
    if not os.path.exists(git_dir):
        return

    head_path = os.path.join(git_dir, "HEAD")
    branch = None
    commit = None
    try:
        with open(head_path) as f:
            head = f.read().strip()
        if head.startswith("ref: "):
            ref = head.removeprefix("ref: ").strip()
            branch = ref.rsplit("/", 1)[-1]
            ref_path = os.path.join(git_dir, *ref.split("/"))
            if os.path.exists(ref_path):
                with open(ref_path) as f:
                    commit = f.read().strip()
        elif head:
            commit = head
    except OSError:
        pass

    if branch:
        diagnose_add(checks, "project", "ok", "git_branch", f"Git branch: {branch}", branch=branch)
    if commit:
        diagnose_add(checks, "project", "ok", "git_commit", f"Git commit: {commit[:12]}", commit=commit[:40])

    git_exe = shutil.which("git")
    if not git_exe:
        return
    try:
        result = subprocess.run(
            [git_exe, "status", "--porcelain"],
            cwd=BASE_DIR,
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode == 0 and result.stdout.strip():
        diagnose_add(checks, "project", "warning", "git_dirty", "Git working tree has local changes")
    elif result.returncode == 0:
        diagnose_add(checks, "project", "ok", "git_clean", "Git working tree is clean")


def diagnose_config_plausibility(checks, args, config_data):
    if not isinstance(config_data, dict):
        return

    system = config_data.get("system", {})
    if not isinstance(system, dict):
        diagnose_add(checks, "config", "error", "system_not_object", "system config must be an object")
        system = {}

    max_total_power = diagnose_float(system.get("max_total_power"))
    if max_total_power is None or max_total_power <= 0:
        diagnose_add(checks, "config", "error", "system_max_total_power_invalid", "system.max_total_power must be numeric and positive")
    else:
        diagnose_add(checks, "config", "ok", "system_max_total_power_valid", "system.max_total_power is numeric and positive")
        if max_total_power > 800:
            diagnose_add(
                checks,
                "config",
                "warning",
                "system_max_total_power_high",
                "system.max_total_power exceeds 800 W; BKW AC limits may be exceeded depending on setup",
                value=max_total_power,
            )

    min_output_limit = diagnose_float(system.get("min_output_limit"))
    if min_output_limit is None or min_output_limit < 0:
        diagnose_add(checks, "config", "error", "system_min_output_limit_invalid", "system.min_output_limit must be numeric and non-negative")
    else:
        diagnose_add(checks, "config", "ok", "system_min_output_limit_valid", "system.min_output_limit is numeric and non-negative")

    loop_interval = diagnose_float(system.get("loop_interval"))
    if loop_interval is None or loop_interval <= 0:
        diagnose_add(checks, "config", "error", "system_loop_interval_invalid", "system.loop_interval must be numeric and > 0")
    else:
        diagnose_add(checks, "config", "ok", "system_loop_interval_valid", "system.loop_interval is numeric and > 0")
        if loop_interval < 1:
            diagnose_add(checks, "config", "warning", "system_loop_interval_low", "system.loop_interval is below 1 second", value=loop_interval)
        if loop_interval > 60:
            diagnose_add(checks, "config", "warning", "system_loop_interval_high", "system.loop_interval is above 60 seconds", value=loop_interval)

    runtime_path_value = system.get("runtime_state_path", "runtime-state.json")
    if diagnose_path_is_clean(runtime_path_value):
        diagnose_add(checks, "config", "ok", "runtime_state_path_valid", "system.runtime_state_path resolves cleanly")
    else:
        diagnose_add(checks, "config", "error", "runtime_state_path_invalid", "system.runtime_state_path must be a non-empty clean path")

    assist = config_data.get("battery_full_charge_assist", {})
    if not isinstance(assist, dict):
        diagnose_add(checks, "config", "error", "battery_full_charge_assist_not_object", "battery_full_charge_assist must be an object")
        assist = {}
    else:
        diagnose_add(checks, "config", "ok", "battery_full_charge_assist_object", "battery_full_charge_assist config is an object")

    if "enabled" in assist and not diagnose_bool(assist.get("enabled")):
        diagnose_add(checks, "config", "error", "battery_full_charge_assist_enabled_invalid", "battery_full_charge_assist.enabled must be boolean")

    interval_days = diagnose_int(assist.get("interval_days", BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["interval_days"]))
    if interval_days is None or interval_days < 1:
        diagnose_add(checks, "config", "error", "battery_full_charge_assist_interval_invalid", "battery_full_charge_assist.interval_days must be an integer >= 1")

    window_days = diagnose_int(assist.get("assist_window_days", BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["assist_window_days"]))
    if window_days is None or window_days < 0:
        diagnose_add(checks, "config", "error", "battery_full_charge_assist_window_invalid", "battery_full_charge_assist.assist_window_days must be an integer >= 0")

    assist_start_soc = diagnose_int(assist.get("assist_start_soc", BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["assist_start_soc"]))
    if assist_start_soc is None or not (0 <= assist_start_soc <= 100):
        diagnose_add(checks, "config", "error", "battery_full_charge_assist_start_soc_invalid", "battery_full_charge_assist.assist_start_soc must be an integer from 0 to 100")

    if diagnose_normalize_force_time(assist.get("force_time", BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["force_time"])) is None:
        diagnose_add(checks, "config", "error", "battery_full_charge_assist_force_time_invalid", "battery_full_charge_assist.force_time must use HH:MM format")

    ac_charge_power = diagnose_int(assist.get("ac_charge_power", BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["ac_charge_power"]))
    if ac_charge_power is None or ac_charge_power < 0:
        diagnose_add(checks, "config", "error", "battery_full_charge_assist_ac_charge_power_invalid", "battery_full_charge_assist.ac_charge_power must be an integer >= 0")

    state_database_path = assist.get("state_database_path", BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["state_database_path"])
    if diagnose_path_is_clean(state_database_path):
        diagnose_add(checks, "config", "ok", "battery_full_charge_assist_state_database_path_valid", "battery_full_charge_assist.state_database_path resolves cleanly")
    else:
        diagnose_add(checks, "config", "error", "battery_full_charge_assist_state_database_path_invalid", "battery_full_charge_assist.state_database_path must be a non-empty clean path")

    devices = config_data.get("devices")
    if not isinstance(devices, list):
        diagnose_add(checks, "config", "error", "devices_not_list", "devices must be a list")
        devices = []
    elif not devices:
        diagnose_add(checks, "config", "error", "devices_empty", "devices must contain at least one device")
    else:
        diagnose_add(checks, "config", "ok", "devices_non_empty", "devices contains at least one device")
        if zendure_mqtt_entries.has_runtime_control_device(config_data):
            diagnose_add(checks, "config", "ok", "control_devices_present", "at least one API or MQTT output-control device can join the EMS control loop")
        else:
            diagnose_add(checks, "config", "error", "no_control_devices", "devices contains only telemetry-only or disabled MQTT entries; add an API inverter or enable output control on a supported MQTT inverter")

    broker_sources = {
        ref: view.source
        for ref, view in zendure_mqtt_entries.zendure_mqtt_broker_profile_views(
            config_data.get("zendure_mqtt")
        ).items()
    }

    for index, item in enumerate(devices):
        if not isinstance(item, dict):
            diagnose_add(checks, "config", "error", "device_not_object", f"devices.{index} must be an object", index=index)
            continue
        if zendure_mqtt_entries.is_zendure_mqtt_device_config(item):
            diagnose_zendure_mqtt_device_config(
                checks, index, item, broker_sources=broker_sources
            )
            continue
        name = item.get("name")
        path = f"devices.{index}"
        if not isinstance(name, str) or not name.strip():
            diagnose_add(checks, "config", "error", "device_name_missing", f"{path}.name must be non-empty", index=index)
        max_power = diagnose_float(item.get("max_power", 0))
        if max_power is None or max_power < 0:
            diagnose_add(checks, "config", "error", "device_max_power_invalid", f"{path}.max_power must be numeric and non-negative", index=index)
        elif max_power > 800:
            diagnose_add(checks, "config", "warning", "device_max_power_high", f"{path}.max_power exceeds 800 W", index=index)
        pv_priority_factor = item.get("pv_priority_factor")
        if pv_priority_factor is not None:
            parsed = diagnose_float(pv_priority_factor)
            if parsed is None:
                diagnose_add(checks, "config", "error", "device_pv_priority_factor_invalid", f"{path}.pv_priority_factor must be numeric", index=index)
            elif parsed <= 0:
                diagnose_add(checks, "config", "warning", "device_pv_priority_factor_non_positive", f"{path}.pv_priority_factor should be positive", index=index)

    # Name uniqueness spans every transport (API and MQTT entries alike); the
    # shared helper keeps diagnose in parity with the startup guard.
    for issue in zendure_mqtt_entries.find_duplicate_device_names(devices):
        diagnose_add(checks, "config", "error", issue["code"], issue["message"])

    for issue in zendure_mqtt_entries.find_duplicate_zendure_device_identities(devices):
        diagnose_add(checks, "config", "error", issue["code"], issue["message"])

    dashboard = config_data.get("dashboard", {})
    if isinstance(dashboard, dict) and dashboard.get("enabled", False):
        host = dashboard.get("host")
        port = diagnose_int(dashboard.get("port"))
        if not isinstance(host, str) or not host.strip():
            diagnose_add(checks, "config", "error", "dashboard_host_missing", "dashboard.host must be configured when dashboard is enabled")
        else:
            diagnose_add(checks, "config", "ok", "dashboard_host_present", "dashboard.host is configured")
        if port is None or not (1 <= port <= 65535):
            diagnose_add(checks, "config", "error", "dashboard_port_invalid", "dashboard.port must be an integer from 1 to 65535")
        else:
            diagnose_add(checks, "config", "ok", "dashboard_port_valid", "dashboard.port is valid")
        auth_path = resolve_dashboard_auth_path(args, config_data)
        auth_configured = dashboard_auth.auth_configured(auth_path)
        if host == "0.0.0.0" and not dashboard.get("ssl_enabled", False) and not auth_configured:
            diagnose_add(
                checks,
                "config",
                "warning",
                "dashboard_open_without_https_auth",
                "Dashboard binds to 0.0.0.0 without HTTPS and without configured auth",
                docs="docs/dashboard.md",
            )

    ha = config_data.get("ha", {})
    if isinstance(ha, dict):
        ha_enabled = bool(ha.get("enabled", False))
        ha_control_enabled = bool(ha.get("control_enabled", False))
        if ha_enabled:
            if diagnose_basic_url(ha.get("url", "")):
                diagnose_add(checks, "config", "ok", "ha_url_valid", "ha.url has a valid basic URL shape")
            else:
                diagnose_add(checks, "config", "error", "ha_url_invalid", "ha.url must be configured as http(s) URL when HA is enabled")
            diagnose_add(
                checks,
                "config",
                "ok" if bool(ha.get("token")) else "warning",
                "ha_token_configured",
                "Home Assistant token configured: yes" if bool(ha.get("token")) else "Home Assistant token configured: no",
                configured=bool(ha.get("token")),
            )
        if ha_control_enabled and not ha_enabled:
            diagnose_add(checks, "config", "warning", "ha_control_without_ha", "ha.control_enabled=true while ha.enabled=false")

    diagnose_grid_meter_config(checks, config_data)
    diagnose_zendure_mqtt_runtime(checks, config_data)
    diagnose_deprecated_keys(checks, config_data)


def diagnose_zendure_mqtt_device_config(checks, index, item, *, broker_sources=None):
    path = f"devices.{index}"
    name = item.get("name") if isinstance(item.get("name"), str) else f"device-{index}"
    control = zendure_mqtt_entries.is_control_zendure_mqtt_device_config(item)
    if control:
        issues = zendure_mqtt_entries.validate_zendure_mqtt_control_device_config(
            item, broker_sources=broker_sources
        )
    else:
        issues = zendure_mqtt_entries.validate_zendure_mqtt_device_config(
            item, broker_sources=broker_sources
        )
    if not issues:
        if control:
            diagnose_add(
                checks,
                "config",
                "ok",
                "zendure_mqtt_control_capable",
                f"{name} is a control-capable Zendure MQTT device; output write is enabled",
                index=index,
            )
        else:
            diagnose_add(
                checks,
                "config",
                "ok",
                "zendure_mqtt_telemetry_only",
                f"{name} is a telemetry-only Zendure MQTT device; output write is disabled",
                index=index,
            )
        return
    for issue in issues:
        diagnose_add(
            checks,
            "config",
            "error" if issue["severity"] == "error" else "warning",
            f"zendure_mqtt_{issue['code']}",
            f"{path}: {issue['message']}",
            index=index,
        )


def diagnose_zendure_mqtt_runtime(checks, config_data):
    """Read-only report on the telemetry-only Zendure MQTT runtime config.

    Reports how many valid telemetry-only devices are configured and whether the
    broker runtime is inactive, misconfigured or ready. The feature itself is
    always on; without a broker host it is simply inactive. Broker credentials
    are never echoed; only the sanitized host:port endpoint is shown.
    """

    if not isinstance(config_data, dict):
        return

    from ems.zendure_mqtt.config_entries import DEFAULT_BROKER_REF
    from ems.zendure_mqtt.runtime import (
        classify_zendure_mqtt_devices,
        load_zendure_mqtt_broker_configs,
        load_zendure_mqtt_runtime_config,
    )

    raw = config_data.get("zendure_mqtt")
    brokers, broker_errors, _stale = load_zendure_mqtt_broker_configs(raw)
    known_refs = set(brokers) | {DEFAULT_BROKER_REF}
    brokers_defined = isinstance(raw, dict) and isinstance(raw.get("brokers"), dict) and bool(raw["brokers"])
    valid, invalid = classify_zendure_mqtt_devices(
        config_data.get("devices"),
        known_broker_refs=known_refs,
        brokers_defined=brokers_defined,
    )
    runtime_config, config_error = load_zendure_mqtt_runtime_config(raw)

    feature_in_use = bool(valid) or bool(invalid) or bool(raw) or config_error
    if not feature_in_use:
        return

    diagnose_add(
        checks,
        "config",
        "ok" if valid else "info",
        "zendure_mqtt_telemetry_device_count",
        f"{len(valid)} telemetry-only Zendure MQTT device(s) configured; output write disabled",
        count=len(valid),
    )

    if invalid:
        diagnose_add(
            checks,
            "config",
            "warning",
            "zendure_mqtt_invalid_device_count",
            f"{len(invalid)} Zendure MQTT device entry(ies) failed validation and are excluded from telemetry",
            count=len(invalid),
        )
        for device in invalid:
            for issue in device.issues:
                if not isinstance(issue, dict) or not issue.get("code"):
                    continue
                diagnose_add(
                    checks,
                    "config",
                    "error" if issue.get("severity") == "error" else "warning",
                    f"zendure_mqtt_{issue['code']}",
                    f"{device.name}: {issue.get('message', issue['code'])}",
                    broker_ref=device.broker_ref,
                )

    # ``default`` is reserved for the implicit legacy top-level broker; a named
    # zendure_mqtt.brokers.default is a config error the runtime would not honour.
    for issue in zendure_mqtt_entries.find_reserved_mqtt_broker_ref_issues(
        config_data
    ):
        diagnose_add(
            checks,
            "config",
            "error",
            issue["code"],
            issue["message"],
        )

    # Enabled telemetry devices must reference a usable broker profile. Codes are
    # already sanitized (index + broker ref only, no serials/hosts/credentials).
    for issue in zendure_mqtt_entries.find_zendure_mqtt_broker_profile_issues(
        config_data
    ):
        diagnose_add(
            checks,
            "config",
            "error",
            issue["code"],
            issue["message"],
        )

    # Every configured MQTT credentials_ref (broker profile or grid meter) must
    # be canonical and belong to one credential source — the same contract
    # Admin Preview and Apply enforce, so diagnose agrees with them. Messages
    # carry only the non-secret reference/source names.
    from ems.mqtt_credentials import find_mqtt_credential_consumer_issues

    for issue in find_mqtt_credential_consumer_issues(config_data):
        diagnose_add(
            checks,
            "config",
            "error",
            issue["code"],
            issue["message"],
        )

    # Per-device snapshot status. Diagnose runs offline with no broker, so live
    # devices report "unseen"; identifiers are reduced to set/missing so no
    # serial or device id leaks into the report.
    for device in valid:
        diagnose_add(
            checks,
            "config",
            "info",
            "zendure_mqtt_telemetry_device",
            f"{device.name}: telemetry-only Zendure MQTT device "
            f"(topic_family={device.topic_family or 'unknown'}); no live telemetry observed",
            name=device.name,
            topic_family=device.topic_family,
            identifier="set" if device.identifier else "missing",
            broker_ref=device.broker_ref,
            source=device.source,
            status="unseen",
            write_output_limit=False,
        )

    if brokers_defined:
        for ref in sorted(broker_errors):
            diagnose_add(
                checks,
                "config",
                "error",
                "zendure_mqtt_broker_config_invalid",
                f"Zendure MQTT broker '{ref}' config is invalid: {broker_errors[ref]}",
                broker_ref=ref,
            )
        for ref in sorted(brokers):
            cfg = brokers[ref]
            endpoint = zendure_mqtt_entries.format_mqtt_endpoint(cfg.host, cfg.port)
            if not cfg.host:
                diagnose_add(
                    checks,
                    "config",
                    "warning",
                    "zendure_mqtt_broker_endpoint_missing",
                    f"Zendure MQTT broker '{ref}' has no broker host configured; it will not start",
                    broker_ref=ref,
                )
            elif endpoint is None:
                # Host present but not a bare hostname/IP: never echo it, a
                # credential could be smuggled through the host field.
                diagnose_add(
                    checks,
                    "config",
                    "error",
                    "zendure_mqtt_broker_host_invalid",
                    f"Zendure MQTT broker '{ref}' has an invalid broker host; it will not start",
                    broker_ref=ref,
                )
            elif not cfg.enabled:
                diagnose_add(
                    checks,
                    "config",
                    "info",
                    "zendure_mqtt_broker_disabled",
                    f"Zendure MQTT broker '{ref}' is disabled",
                    broker_ref=ref,
                    source=cfg.source,
                )
            else:
                diagnose_add(
                    checks,
                    "config",
                    "ok",
                    "zendure_mqtt_broker_configured",
                    f"Zendure MQTT broker '{ref}' configured for {endpoint} "
                    f"({cfg.source or 'unknown source'}); read-only",
                    broker_ref=ref,
                    source=cfg.source,
                    endpoint=endpoint,
                )
        return

    if config_error:
        diagnose_add(
            checks,
            "config",
            "error",
            "zendure_mqtt_runtime_config_invalid",
            f"Zendure MQTT telemetry runtime config is invalid: {config_error}",
        )
        return

    if not runtime_config.enabled:
        # The feature is always on; an active config requires a broker host, so
        # "not enabled" here means exactly "no broker host configured".
        diagnose_add(
            checks,
            "config",
            "info",
            "zendure_mqtt_runtime_inactive",
            "Zendure MQTT telemetry is inactive: no broker host is configured",
        )
        return

    endpoint = zendure_mqtt_entries.format_mqtt_endpoint(
        runtime_config.host, runtime_config.port
    )
    if endpoint is None:
        # Host present but not a bare hostname/IP: never echo it, a credential
        # could be smuggled through the host field.
        diagnose_add(
            checks,
            "config",
            "error",
            "zendure_mqtt_runtime_host_invalid",
            "Zendure MQTT telemetry runtime has an invalid broker host; it will not start",
        )
        return

    subscriptions = runtime_config.client_config().resolved_subscriptions()
    diagnose_add(
        checks,
        "config",
        "ok",
        "zendure_mqtt_runtime_configured",
        f"Zendure MQTT telemetry runtime configured for broker {endpoint} "
        f"({len(subscriptions)} subscription filter(s)); read-only",
        endpoint=endpoint,
        subscription_count=len(subscriptions),
    )


def diagnose_grid_meter_config(checks, config_data):
    grid_meter = config_data.get("grid_meter") if isinstance(config_data, dict) else None
    if not isinstance(grid_meter, dict):
        legacy_shelly = config_data.get("shelly") if isinstance(config_data, dict) else None
        if isinstance(legacy_shelly, dict):
            grid_meter = {"type": "shelly", "ip": legacy_shelly.get("ip")}
        else:
            diagnose_add(checks, "config", "warning", "grid_meter_missing", "grid_meter config is missing or not an object")
            return

    meter_type = str(grid_meter.get("type", "shelly")).strip().lower()
    model, transport = config_mod.grid_meter_model_transport(meter_type)
    type_message = f"Grid meter type: {meter_type}"
    if model and transport:
        type_message = f"{type_message} ({model} via {transport})"
    diagnose_add(
        checks,
        "config",
        "ok",
        "grid_meter_type",
        type_message,
        type=meter_type,
        model=model,
        transport=transport,
    )
    if meter_type in (
        "shelly",
        "shelly_3em_gen1",
        "ecotracker",
        config_mod.ZENDURE_GRID_METER_HTTP_GRID_METER_TYPE,
        config_mod.ZENDURE_SMARTMETER_3CT_HTTP_GRID_METER_TYPE,
        config_mod.ZENDURE_SMARTMETER_D0_HTTP_GRID_METER_TYPE,
    ):
        if grid_meter.get("ip"):
            diagnose_add(checks, "config", "ok", "grid_meter_ip_present", f"{meter_type} grid meter IP is configured")
        else:
            diagnose_add(checks, "config", "error", "grid_meter_ip_missing", f"{meter_type} grid meter requires grid_meter.ip")
    elif meter_type == "tasmota_http":
        if grid_meter.get("url") or grid_meter.get("ip"):
            diagnose_add(checks, "config", "ok", "grid_meter_endpoint_present", "Tasmota HTTP grid meter endpoint is configured")
        else:
            diagnose_add(checks, "config", "error", "grid_meter_endpoint_missing", "Tasmota HTTP grid meter requires grid_meter.url or grid_meter.ip")
        if grid_meter.get("power_path"):
            diagnose_add(checks, "config", "ok", "grid_meter_power_path_present", "Tasmota HTTP grid_meter.power_path is configured")
        else:
            diagnose_add(checks, "config", "error", "grid_meter_power_path_missing", "Tasmota HTTP grid meter requires grid_meter.power_path")
    elif meter_type in config_mod.MQTT_GRID_METER_TYPES:
        raw_settings = config_mod.grid_meter_mqtt_settings(grid_meter)
        label = (
            "Zendure SmartMeter D0"
            if meter_type == config_mod.ZENDURE_SMARTMETER_D0_GRID_METER_TYPE
            else "MQTT grid meter"
        )
        broker_ref = raw_settings.get("broker_ref")
        mqtt_settings = raw_settings
        if isinstance(broker_ref, str) and broker_ref.strip():
            try:
                # Resolving surfaces the effective connection and validates the
                # ref (unknown/disabled/incomplete/cloud source) without secrets.
                mqtt_settings = config_mod.resolve_grid_meter_mqtt_settings(config_data)
                diagnose_add(
                    checks, "config", "ok", "grid_meter_broker_ref",
                    f"{label} uses broker profile '{broker_ref.strip()}'",
                    broker_ref=broker_ref.strip(),
                    resolved_host=mqtt_settings.get("host"),
                    resolved_port=mqtt_settings.get("port"),
                )
            except ValueError as exc:
                diagnose_add(
                    checks, "config", "error", "grid_meter_broker_ref_invalid",
                    str(exc), broker_ref=broker_ref.strip(),
                )
                return
        if mqtt_settings.get("host"):
            diagnose_add(checks, "config", "ok", "grid_meter_mqtt_host_present", f"{label} broker host is configured")
        else:
            diagnose_add(checks, "config", "error", "grid_meter_mqtt_host_missing", f"{label} requires grid_meter.mqtt.host")
        if mqtt_settings.get("topic"):
            diagnose_add(checks, "config", "ok", "grid_meter_mqtt_topic_present", f"{label} topic is configured")
        else:
            diagnose_add(checks, "config", "error", "grid_meter_mqtt_topic_missing", f"{label} requires grid_meter.mqtt.topic")
        payload_format = str(mqtt_settings.get("payload_format") or "number").strip().lower()
        if payload_format in ("number", "json"):
            diagnose_add(checks, "config", "ok", "grid_meter_mqtt_payload_format", f"MQTT payload format: {payload_format}", payload_format=payload_format)
        else:
            diagnose_add(checks, "config", "error", "grid_meter_mqtt_payload_format_invalid", "MQTT grid meter payload_format must be number or json", payload_format=payload_format)
        if meter_type == config_mod.ZENDURE_SMARTMETER_D0_GRID_METER_TYPE and payload_format != "number":
            diagnose_add(checks, "config", "error", "grid_meter_mqtt_payload_format_invalid", "Zendure SmartMeter D0 requires payload_format number", payload_format=payload_format)
        if payload_format == "json" and not mqtt_settings.get("value_path"):
            diagnose_add(checks, "config", "error", "grid_meter_mqtt_value_path_missing", "MQTT JSON grid meter requires grid_meter.mqtt.value_path")
        tls_enabled = config_mod.safe_bool(mqtt_settings.get("tls"), False)
        tls_insecure = config_mod.safe_bool(mqtt_settings.get("tls_insecure"), False)
        if tls_enabled and tls_insecure:
            diagnose_add(
                checks, "config", "warning", "grid_meter_mqtt_tls_insecure",
                f"{label} uses TLS with certificate verification disabled (tls_insecure)",
                tls=True, tls_insecure=True,
            )
        else:
            diagnose_add(
                checks, "config", "ok", "grid_meter_mqtt_tls",
                f"{label} TLS: {'enabled' if tls_enabled else 'disabled'}",
                tls=tls_enabled, tls_insecure=False,
            )
    elif meter_type in ("ha", "homeassistant", "home_assistant"):
        diagnose_add(checks, "config", "ok", "grid_meter_ha_config", "Home Assistant grid meter type detected; only config completeness is checked by diagnose")
    else:
        diagnose_add(checks, "config", "warning", "grid_meter_type_unknown", f"Unknown grid meter type: {meter_type}", type=meter_type)


def diagnose_deprecated_keys(checks, config_data):
    if not isinstance(config_data, dict):
        return
    deprecated = {
        "shelly": "grid_meter",
        "system.runtime_state": "system.runtime_state_path",
        "runtime_state_path": "system.runtime_state_path",
    }
    for key_path, replacement in deprecated.items():
        current = config_data
        found = True
        for part in key_path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found:
            diagnose_add(
                checks,
                "config",
                "warning",
                "deprecated_config_key",
                f"Deprecated config key: {key_path}; use {replacement}",
                key=key_path,
                replacement=replacement,
            )


def diagnose_runtime_state_plausibility(checks, runtime_path, config_data):
    if not os.path.exists(runtime_path) or os.path.getsize(runtime_path) == 0:
        return

    runtime_data, runtime_error = diagnose_json_file(runtime_path)
    if runtime_error:
        return
    if not isinstance(runtime_data, dict):
        diagnose_add(checks, "runtime_state", "error", "runtime_state_not_object", "runtime-state.json root must be a JSON object")
        return
    diagnose_add(checks, "runtime_state", "ok", "runtime_state_object", "runtime-state.json root is a JSON object")

    system = runtime_data.get("system")
    if isinstance(system, dict):
        if "enabled" in system and not diagnose_bool(system.get("enabled")):
            diagnose_add(checks, "runtime_state", "error", "runtime_system_enabled_invalid", "runtime system.enabled must be boolean")
        elif "enabled" in system:
            diagnose_add(checks, "runtime_state", "ok", "runtime_system_enabled_valid", "runtime system.enabled is boolean")
        max_total = diagnose_float(system.get("max_total_power")) if "max_total_power" in system else None
        if "max_total_power" in system and (max_total is None or max_total < 0):
            diagnose_add(checks, "runtime_state", "error", "runtime_system_max_total_power_invalid", "runtime system.max_total_power must be numeric and non-negative")
        min_output = diagnose_float(system.get("min_output_limit")) if "min_output_limit" in system else None
        if "min_output_limit" in system and (min_output is None or min_output < 0):
            diagnose_add(checks, "runtime_state", "error", "runtime_system_min_output_limit_invalid", "runtime system.min_output_limit must be numeric and non-negative")

    config_names = set(diagnose_config_device_names(config_data))
    runtime_devices = runtime_data.get("devices")
    if isinstance(runtime_devices, dict):
        runtime_names = set(runtime_devices)
        for name in sorted(runtime_names - config_names):
            diagnose_add(checks, "runtime_state", "warning", "runtime_device_unknown", f"Runtime-state contains device not present in config: {name}", device=name)
        for name in sorted(config_names - runtime_names):
            diagnose_add(checks, "runtime_state", "warning", "runtime_device_missing", f"Config device missing from runtime-state: {name}", device=name)
        config_limits = {}
        for item in config_data.get("devices", []) if isinstance(config_data, dict) else []:
            if isinstance(item, dict) and item.get("name"):
                parsed = diagnose_float(item.get("max_power"))
                if parsed is not None:
                    config_limits[str(item["name"])] = parsed
        for name, device in runtime_devices.items():
            if not isinstance(device, dict):
                diagnose_add(checks, "runtime_state", "error", "runtime_device_not_object", f"Runtime device {name} must be an object", device=name)
                continue
            if "enabled" in device and not diagnose_bool(device.get("enabled")):
                diagnose_add(checks, "runtime_state", "error", "runtime_device_enabled_invalid", f"Runtime device {name}.enabled must be boolean", device=name)
            max_power = diagnose_float(device.get("max_power")) if "max_power" in device else None
            if "max_power" in device and (max_power is None or max_power < 0):
                diagnose_add(checks, "runtime_state", "error", "runtime_device_max_power_invalid", f"Runtime device {name}.max_power must be numeric and non-negative", device=name)
            if max_power is not None and name in config_limits and max_power > config_limits[name]:
                diagnose_add(checks, "runtime_state", "warning", "runtime_device_max_power_above_config", f"Runtime device {name}.max_power exceeds configured max_power", device=name)
    elif runtime_devices is not None:
        diagnose_add(checks, "runtime_state", "error", "runtime_devices_not_object", "runtime devices must be an object")

    timestamp = runtime_data.get("timestamp") or runtime_data.get("updated_at")
    if isinstance(timestamp, str):
        parsed = diagnose_parse_timestamp(timestamp)
        if parsed:
            age_seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
            if age_seconds > 3600:
                diagnose_add(checks, "runtime_state", "warning", "runtime_state_stale", "runtime-state timestamp is older than 1 hour", age_seconds=round(age_seconds, 1))


def diagnose_parse_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def diagnose_battery_state_database_path(config_data):
    assist = diagnose_battery_full_charge_assist_config(config_data)
    return resolve_project_path(assist["state_database_path"])


def diagnose_read_battery_full_charge_rows(database_path):
    if not database_path or not os.path.exists(database_path):
        return {}, "missing"

    try:
        uri = "file:" + os.path.abspath(database_path) + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM battery_full_charge_state
                ORDER BY device
                """
            ).fetchall()
    except sqlite3.Error as exc:
        return {}, str(exc)

    result = {}
    for row in rows:
        item = dict(row)
        for key in (
            "has_battery",
            "full_charge_assist_active",
            "restore_pending",
            "ac_mode_restore_pending",
            "max_soc_request_pending",
            "ac_input_request_pending",
        ):
            item[key] = bool(item.get(key))
        result[item["device"]] = item
    return result, None


def diagnose_battery_full_charge_status(device_state, assist_config, now):
    if not device_state:
        return "unknown"
    if not device_state.get("has_battery"):
        return "ignored"
    if device_state.get("full_charge_assist_active"):
        if not assist_config.get("enabled"):
            return "disabled, abort pending"
        return "active"
    if device_state.get("ac_mode_restore_pending"):
        if not assist_config.get("enabled"):
            return "disabled, restore pending"
        return "restoring output mode"
    if device_state.get("restore_pending"):
        if not assist_config.get("enabled"):
            return "disabled, restore pending"
        return "restore pending"

    next_due_at = diagnose_parse_timestamp(device_state.get("next_due_at"))
    if not next_due_at:
        return "due"
    if next_due_at <= now:
        return "overdue"
    if next_due_at <= now + timedelta(
        days=assist_config.get("assist_window_days", 7)
    ):
        return "due soon"
    return "ok"


def diagnose_battery_full_charge_assist_report(config_data):
    assist_config = diagnose_battery_full_charge_assist_config(config_data)
    database_path = diagnose_battery_state_database_path(config_data)
    rows, error = diagnose_read_battery_full_charge_rows(database_path)
    devices = []
    now = datetime.now(timezone.utc)

    for name in diagnose_config_device_names(config_data):
        state = rows.get(name, {})
        devices.append({
            "device": name,
            "battery": bool(state.get("has_battery", False)),
            "packNum": state.get("last_seen_pack_num"),
            "last_full_charge": state.get("last_full_charge_at"),
            "next_due": state.get("next_due_at"),
            "firmware_socLimit": state.get("last_seen_soc_limit"),
            "socStatus": state.get("last_seen_soc_status"),
            "batCalTime": state.get("last_seen_battery_calibration_time"),
            "soc": state.get("last_seen_soc"),
            "max_soc": state.get("last_seen_max_soc"),
            "acMode": state.get("last_seen_ac_mode"),
            "acStatus": state.get("last_seen_ac_status"),
            "assist_active": bool(state.get("full_charge_assist_active", False)),
            "restore_pending": bool(state.get("restore_pending", False)),
            "ac_mode_restore_pending": bool(state.get("ac_mode_restore_pending", False)),
            "max_soc_request_pending": bool(state.get("max_soc_request_pending", False)),
            "ac_input_request_pending": bool(state.get("ac_input_request_pending", False)),
            "status": diagnose_battery_full_charge_status(
                state,
                assist_config,
                now
            ),
        })

    return {
        "enabled": assist_config["enabled"],
        "interval_days": assist_config["interval_days"],
        "assist_window_days": assist_config["assist_window_days"],
        "assist_start_soc": assist_config["assist_start_soc"],
        "force_time": assist_config["force_time"],
        "ac_charge_power": assist_config["ac_charge_power"],
        "enable_ac_charge_mode": assist_config["enable_ac_charge_mode"],
        "state_database_path": database_path,
        "state_database_error": error,
        "devices": devices,
    }


def diagnose_database_deep(checks, database_path):
    if not database_path:
        return
    diagnose_check_path(
        checks,
        "data",
        "dashboard_database_file",
        database_path,
        "dashboard database",
        expect_file=True,
        require_exists=False,
        check_read=True,
    )
    if not os.path.exists(database_path):
        return

    try:
        uri = "file:" + os.path.abspath(database_path) + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as con:
            result = con.execute("PRAGMA integrity_check").fetchone()
            integrity = result[0] if result else ""
            if integrity == "ok":
                diagnose_add(checks, "data", "ok", "sqlite_integrity_ok", "SQLite integrity_check returned ok")
            else:
                diagnose_add(checks, "data", "error", "sqlite_integrity_failed", "SQLite integrity_check did not return ok", result=str(integrity)[:120])

            rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            tables = {row[0] for row in rows}
            expected = {
                "snapshots",
                "telemetry",
                "device_state",
                "rule_state",
                "daily_energy_stats",
                "energy_integration_state",
            }
            for table in sorted(expected):
                level = "ok" if table in tables else "warning"
                diagnose_add(checks, "data", level, "sqlite_table_present" if level == "ok" else "sqlite_table_missing", f"SQLite table {table}: {'present' if level == 'ok' else 'missing'}", table=table)

            interesting = {
                "snapshots": "timestamp",
                "telemetry": "timestamp",
                "daily_energy_stats": "updated_at",
                "device_state": "updated_at",
            }
            any_rows = False
            for table, timestamp_col in interesting.items():
                if table not in tables:
                    continue
                count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                any_rows = any_rows or count > 0
                latest = None
                try:
                    latest_row = con.execute(f"SELECT MAX({timestamp_col}) FROM {table}").fetchone()
                    latest = latest_row[0] if latest_row else None
                except sqlite3.Error:
                    latest = None
                diagnose_add(checks, "data", "ok", "sqlite_table_rows", f"SQLite table {table} rows: {count}", table=table, rows=count, latest=latest)
                parsed = diagnose_parse_timestamp(latest) if isinstance(latest, str) else None
                if parsed and (datetime.now(timezone.utc) - parsed).total_seconds() > 3600:
                    diagnose_add(checks, "data", "warning", "sqlite_table_stale", f"SQLite table {table} latest row is older than 1 hour", table=table)
            if not any_rows:
                diagnose_add(checks, "data", "warning", "sqlite_no_energy_rows", "Dashboard database exists but contains no snapshot/energy rows")
    except sqlite3.Error as exc:
        diagnose_add(checks, "data", "error", "sqlite_open_failed", f"Cannot open dashboard database read-only: {exc}")


def diagnose_dashboard_deep(checks, config_data):
    dashboard = config_data.get("dashboard", {}) if isinstance(config_data, dict) else {}
    if not isinstance(dashboard, dict) or not dashboard.get("enabled", False):
        return

    ssl_enabled = bool(dashboard.get("ssl_enabled", False))
    if ssl_enabled:
        for key in ("ssl_cert_file", "ssl_key_file"):
            path = resolve_project_path(str(dashboard.get(key, "")))
            diagnose_check_path(checks, "dashboard", key, path, f"dashboard {key}", expect_file=True, check_read=True)

    host = str(dashboard.get("host", "") or "")
    port = diagnose_int(dashboard.get("port"))
    if port is None or not (1 <= port <= 65535):
        return
    if host in ("0.0.0.0", "::", ""):
        probe_host = "127.0.0.1"
    elif host in ("127.0.0.1", "localhost", "::1"):
        probe_host = host
    else:
        diagnose_add(checks, "dashboard", "warning", "dashboard_probe_skipped", "Dashboard host is not loopback; local endpoint check skipped", host=host)
        return
    scheme = "https" if ssl_enabled else "http"
    url = f"{scheme}://{probe_host}:{port}/"
    try:
        with urlopen(Request(url, headers={"User-Agent": "emsctl-diagnose"}), timeout=2) as response:
            diagnose_add(
                checks,
                "dashboard",
                "ok",
                "dashboard_endpoint_reachable",
                f"Dashboard local endpoint reachable: HTTP {response.status}",
                status_code=response.status,
                content_type=response.headers.get("content-type"),
            )
    except HTTPError as exc:
        diagnose_add(checks, "dashboard", "warning", "dashboard_endpoint_http_error", f"Dashboard local endpoint returned HTTP {exc.code}", status_code=exc.code)
    except (OSError, URLError) as exc:
        diagnose_add(checks, "dashboard", "warning", "dashboard_endpoint_unreachable", f"Dashboard local endpoint not reachable: {exc.__class__.__name__}")


def diagnose_log_paths(config_data):
    paths = []
    system = config_data.get("system", {}) if isinstance(config_data, dict) else {}
    if isinstance(system, dict):
        for key in ("log_path", "log_file"):
            value = system.get(key)
            if value:
                paths.append(resolve_project_path(str(value)))
    return paths


def diagnose_tail_lines(path, max_lines=300):
    try:
        with open(path, errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return lines[-max_lines:]


def diagnose_logs_deep(checks, config_data):
    paths = diagnose_log_paths(config_data)
    if not paths:
        diagnose_add(checks, "logs", "warning", "log_path_not_configured", "No log path configured for deep log scan")
        return
    for path in paths:
        diagnose_check_path(checks, "logs", "log_file", path, "log file", expect_file=True, require_exists=False, check_read=True)
        if not os.path.exists(path):
            continue
        lines = diagnose_tail_lines(path)
        counts = {}
        last_seen = {}
        for line in lines:
            for name, pattern in DIAGNOSE_LOG_PATTERNS:
                if pattern.search(line):
                    counts[name] = counts.get(name, 0) + 1
                    last_seen[name] = diagnose_extract_timestamp(line)
        if counts:
            for name, count in sorted(counts.items()):
                diagnose_add(checks, "logs", "warning", "log_pattern_found", f"Log pattern {name}: {count} occurrence(s)", pattern=name, count=count, last_seen=last_seen.get(name))
        else:
            diagnose_add(checks, "logs", "ok", "log_scan_clean", "No common error patterns found in recent log lines", path=path, scanned_lines=len(lines))


def diagnose_extract_timestamp(line):
    match = re.search(r"\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+", line)
    return match.group(0) if match else None


def diagnose_docker_deep(checks):
    compose_files = [
        name
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", "docker-compose.example.yml")
        if os.path.exists(os.path.join(BASE_DIR, name))
    ]
    docker_exe = shutil.which("docker")
    if not docker_exe:
        diagnose_add(checks, "docker", "warning", "docker_cli_missing", "Docker CLI not found; host Docker checks skipped")
        return
    diagnose_add(checks, "docker", "ok", "docker_cli_found", "Docker CLI found")
    if not compose_files:
        diagnose_add(checks, "docker", "warning", "compose_file_missing", "No Docker Compose file found in project directory")
        return
    diagnose_add(checks, "docker", "ok", "compose_file_found", f"Compose file found: {compose_files[0]}", file=compose_files[0])
    try:
        result = subprocess.run(
            [docker_exe, "compose", "ps", "--format", "json"],
            cwd=BASE_DIR,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        diagnose_add(checks, "docker", "warning", "docker_compose_ps_failed", f"docker compose ps failed: {exc.__class__.__name__}")
        return
    if result.returncode == 0:
        output = result.stdout.strip()
        diagnose_add(checks, "docker", "ok", "docker_compose_ps", "docker compose ps completed", output_preview=output[:500])
    else:
        diagnose_add(checks, "docker", "warning", "docker_compose_ps_failed", "docker compose ps returned non-zero", stderr_preview=result.stderr[:500])


def diagnose_http_json(url, headers=None, timeout=2):
    request = Request(url, headers=headers or {"User-Agent": "emsctl-diagnose"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024)
        payload = json.loads(raw.decode("utf-8"))
        return response.status, payload


GRID_METER_PROVIDERS = {
    "shelly": "Shelly",
    "shelly_3em_gen1": "Shelly 3EM Gen1",
    "ecotracker": "EcoTracker",
    "tasmota_http": "Tasmota",
    "mqtt": "MQTT",
    "zendure_smartmeter_d0": "Zendure SmartMeter D0",
    "ha": "Home Assistant",
}


def _diagnose_record_probe(tracker, start, error=None):
    latency_ms = (time.monotonic() - start) * 1000.0
    if error is None:
        tracker.record_success(latency_ms)
    else:
        tracker.record_failure(error=error, latency_ms=latency_ms)


def diagnose_hardware(checks, config_data):
    health = {"grid_meter": None, "devices": []}
    if not isinstance(config_data, dict):
        return health
    grid_meter = config_data.get("grid_meter")
    if not isinstance(grid_meter, dict):
        grid_meter = config_data.get("shelly") if isinstance(config_data.get("shelly"), dict) else {}
        if grid_meter:
            grid_meter = {"type": "shelly", **grid_meter}
    meter_type = str(grid_meter.get("type", "shelly")).strip().lower() if isinstance(grid_meter, dict) else "unknown"

    provider = GRID_METER_PROVIDERS.get(meter_type, meter_type or "unknown")
    grid_tracker = CommHealth(provider, kind="read")

    if meter_type == "shelly" and grid_meter.get("ip"):
        url = f"http://{grid_meter['ip']}/rpc/Shelly.GetStatus"
        start = time.monotonic()
        try:
            status, payload = diagnose_http_json(url)
            from ems.clients import _parse_shelly_power
            power = _parse_shelly_power(payload, channels=grid_meter.get("channels"))
            _diagnose_record_probe(grid_tracker, start)
            diagnose_add(checks, "hardware", "ok", "shelly_read_ok", "Shelly read-only status endpoint returned parseable power", status_code=status, power_w=power)
        except Exception as exc:
            _diagnose_record_probe(grid_tracker, start, exc)
            diagnose_add(checks, "hardware", "warning", "shelly_read_failed", f"Shelly read-only probe failed: {exc.__class__.__name__}")
    elif meter_type == "shelly_3em_gen1" and grid_meter.get("ip"):
        url = f"http://{grid_meter['ip']}/status"
        start = time.monotonic()
        try:
            status, payload = diagnose_http_json(url)
            from ems.clients import _parse_shelly_3em_gen1_power
            power = _parse_shelly_3em_gen1_power(payload, channels=grid_meter.get("channels"))
            _diagnose_record_probe(grid_tracker, start)
            diagnose_add(checks, "hardware", "ok", "shelly_3em_gen1_read_ok", "Shelly 3EM Gen1 read-only status endpoint returned parseable power", status_code=status, power_w=power)
        except Exception as exc:
            _diagnose_record_probe(grid_tracker, start, exc)
            diagnose_add(checks, "hardware", "warning", "shelly_3em_gen1_read_failed", f"Shelly 3EM Gen1 read-only probe failed: {exc.__class__.__name__}")
    elif meter_type == "ecotracker" and grid_meter.get("ip"):
        url = f"http://{grid_meter['ip']}/v1/json"
        start = time.monotonic()
        try:
            status, payload = diagnose_http_json(url)
            from ems.clients import _parse_ecotracker_power
            power = _parse_ecotracker_power(payload)
            _diagnose_record_probe(grid_tracker, start)
            diagnose_add(checks, "hardware", "ok", "ecotracker_read_ok", "EcoTracker read-only endpoint returned parseable power", status_code=status, power_w=power)
        except Exception as exc:
            _diagnose_record_probe(grid_tracker, start, exc)
            diagnose_add(checks, "hardware", "warning", "ecotracker_read_failed", f"EcoTracker read-only probe failed: {exc.__class__.__name__}")
    elif meter_type in config_mod.MQTT_GRID_METER_TYPES:
        mqtt_settings = config_mod.grid_meter_mqtt_settings(grid_meter)
        host = str(mqtt_settings.get("host") or "").strip()
        if not host:
            diagnose_add(checks, "hardware", "warning", "grid_meter_probe_skipped", f"No read-only grid meter probe implemented for type: {meter_type}", type=meter_type)
        else:
            start = time.monotonic()
            try:
                port = int(float(mqtt_settings.get("port", 1883)))
                with socket.create_connection((host, port), timeout=2):
                    pass
                _diagnose_record_probe(grid_tracker, start)
                diagnose_add(
                    checks,
                    "hardware",
                    "ok",
                    "mqtt_broker_connect_ok",
                    "MQTT broker TCP connection succeeded",
                    host=host,
                    port=port,
                )
            except Exception as exc:
                _diagnose_record_probe(grid_tracker, start, exc)
                diagnose_add(
                    checks,
                    "hardware",
                    "warning",
                    "mqtt_broker_connect_failed",
                    f"MQTT broker TCP probe failed: {exc.__class__.__name__}",
                )
    else:
        diagnose_add(checks, "hardware", "warning", "grid_meter_probe_skipped", f"No read-only grid meter probe implemented for type: {meter_type}", type=meter_type)

    grid_snapshot = grid_tracker.snapshot()
    grid_snapshot["provider"] = provider
    health["grid_meter"] = grid_snapshot

    for index, device in enumerate(config_data.get("devices", [])):
        if not isinstance(device, dict):
            continue
        if zendure_mqtt_entries.is_zendure_mqtt_device_config(device):
            continue
        name = str(device.get("name") or f"device-{index}")
        read_tracker = CommHealth(name, kind="read")
        missing = [
            key
            for key in ("ip", "sn")
            if not device.get(key)
        ]
        if missing:
            diagnose_add(checks, "hardware", "warning", "zendure_device_config_incomplete", f"Zendure device {name} missing required config: {', '.join(missing)}", device=name, missing=missing)
            health["devices"].append({"name": name, "read": read_tracker.snapshot(), "write": None})
            continue
        diagnose_add(checks, "hardware", "ok", "zendure_device_config_complete", f"Zendure device {name} has required read config", device=name, serial_configured=True)
        url = f"http://{device['ip']}/properties/report"
        start = time.monotonic()
        try:
            status, payload = diagnose_http_json(url)
            from ems.clients import parse_device
            parse_device(payload)
            _diagnose_record_probe(read_tracker, start)
            diagnose_add(checks, "hardware", "ok", "zendure_read_ok", f"Zendure device {name} read-only report endpoint returned parseable payload", device=name, status_code=status)
        except Exception as exc:
            _diagnose_record_probe(read_tracker, start, exc)
            diagnose_add(checks, "hardware", "warning", "zendure_read_failed", f"Zendure device {name} read-only probe failed: {exc.__class__.__name__}", device=name)
        health["devices"].append({"name": name, "read": read_tracker.snapshot(), "write": None})

    return health


def diagnose_redact_key(key):
    lowered = str(key).lower()
    return any(token in lowered for token in DIAGNOSE_REDACT_KEYWORDS)


def diagnose_redact_value(value):
    if isinstance(value, dict):
        return {
            key: "<redacted>" if diagnose_redact_key(key) else diagnose_redact_value(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [diagnose_redact_value(item) for item in value]
    return value


def diagnose_redact_text(text):
    redacted = text
    redacted = re.sub(r"(?i)(https?://)([^/\s:@\"]+):([^@\s/\"]+)@", r"\1<redacted>:<redacted>@", redacted)
    redacted = re.sub(r"(?i)(token|password|passwd|secret|authorization|bearer|cookie|session|auth)([\"'\s:=]+)([^\"'\s,}]+)", r"\1\2<redacted>", redacted)
    redacted = re.sub(r"(?i)(sn|serial|device_id|api[_-]?key)([\"'\s:=]+)([^\"'\s,}]+)", r"\1\2<redacted>", redacted)
    return redacted


def diagnose_redact_report_for_http(report):
    return diagnose_redact_text_values(diagnose_redact_value(report))


def diagnose_redact_text_values(value):
    if isinstance(value, dict):
        return {
            key: diagnose_redact_text_values(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [diagnose_redact_text_values(item) for item in value]
    if isinstance(value, str):
        return diagnose_redact_text(value)
    return value


def diagnose_support_bundle_path(output):
    if output:
        return output
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(os.getcwd(), f"ems-diagnose-{timestamp}.zip")


def diagnose_nested_get(data, paths, default=None):
    for path in paths:
        current = data
        found = True
        for part in path:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found:
            return current
    return default


def diagnose_number_from(data, paths, default=None):
    value = diagnose_nested_get(data, paths, default=None)
    parsed = diagnose_float(value)
    return default if parsed is None else parsed


def diagnose_bool_from(data, paths, default=None):
    value = diagnose_nested_get(data, paths, default=None)
    return value if isinstance(value, bool) else default


def diagnose_sum_devices(devices, keys):
    if not isinstance(devices, dict):
        return None
    total = 0.0
    found = False
    for device in devices.values():
        if not isinstance(device, dict):
            continue
        for key in keys:
            parsed = diagnose_float(device.get(key))
            if parsed is not None:
                total += parsed
                found = True
                break
    return total if found else None


def diagnose_format_watts(value):
    if value is None:
        return "unknown"
    rounded = int(round(value))
    suffix = ""
    if rounded > 0:
        suffix = " import"
    elif rounded < 0:
        suffix = " export"
    return f"{rounded} W{suffix}"


def diagnose_control_load_runtime(runtime_path):
    if not runtime_path or not os.path.exists(runtime_path):
        return {}, "missing"
    data, error = diagnose_json_file(runtime_path)
    if error:
        return {}, error
    return data if isinstance(data, dict) else {}, None


def diagnose_first_timestamp(data, paths):
    for path in paths:
        value = diagnose_nested_get(data, [path])
        if isinstance(value, str) and diagnose_parse_timestamp(value):
            return value
    return None


def diagnose_control_live_timestamp(runtime_data):
    return diagnose_first_timestamp(
        runtime_data,
        [
            ("controller", "timestamp"),
            ("controller", "last_cycle_timestamp"),
            ("controller", "last_control_cycle_timestamp"),
            ("controller", "last_control_cycle_at"),
            ("controller", "last_update"),
            ("control", "timestamp"),
            ("control", "last_cycle_timestamp"),
            ("control", "last_control_cycle_at"),
            ("latest", "timestamp"),
            ("telemetry", "timestamp"),
        ],
    )


def diagnose_meter_timestamp(runtime_data):
    return diagnose_first_timestamp(
        runtime_data,
        [
            ("grid_meter", "timestamp"),
            ("grid_meter", "last_measurement_timestamp"),
            ("grid_meter", "last_update"),
            ("meter", "timestamp"),
            ("meter", "last_measurement_timestamp"),
            ("meter", "last_update"),
            ("controller", "grid_meter_timestamp"),
            ("controller", "meter_timestamp"),
            ("controller", "last_measurement_timestamp"),
            ("controller", "last_meter_update"),
            ("latest", "grid_meter_timestamp"),
            ("latest", "measurement_timestamp"),
        ],
    )


def diagnose_meter_failure_count(runtime_data):
    candidates = [
        ("grid_meter", "consecutive_read_failures"),
        ("grid_meter", "read_failures"),
        ("grid_meter", "failure_count"),
        ("meter", "consecutive_read_failures"),
        ("meter", "read_failures"),
        ("meter", "failure_count"),
        ("controller", "meter_read_failures"),
        ("controller", "grid_meter_read_failures"),
        ("controller", "consecutive_meter_failures"),
        ("controller", "consecutive_grid_meter_failures"),
    ]
    failures = 0
    for path in candidates:
        value = diagnose_number_from(runtime_data, [path])
        if value is not None:
            failures = max(failures, int(value))
    return failures


def diagnose_control_snapshot(config_data, runtime_data, runtime_path):
    devices = runtime_data.get("devices", {}) if isinstance(runtime_data.get("devices"), dict) else {}
    system_runtime = runtime_data.get("system", {}) if isinstance(runtime_data.get("system"), dict) else {}
    system_config = config_data.get("system", {}) if isinstance(config_data.get("system"), dict) else {}
    winter_runtime = runtime_data.get("winter", {}) if isinstance(runtime_data.get("winter"), dict) else {}
    winter_config = config_data.get("winter", {}) if isinstance(config_data.get("winter"), dict) else {}

    target_total = diagnose_number_from(
        runtime_data,
        [
            ("target_output_w",),
            ("target_w",),
            ("controller", "target_output_w"),
            ("controller", "effective_target_total_w"),
            ("controller", "allocated_target_total_w"),
        ],
    )
    if target_total is None:
        target_total = diagnose_sum_devices(devices, ("target_w", "allocated_target_w"))

    final_output = diagnose_number_from(
        runtime_data,
        [
            ("final_output_w",),
            ("inverter_output_w",),
            ("output_w",),
            ("controller", "commanded_total_w"),
        ],
    )
    if final_output is None:
        final_output = diagnose_sum_devices(devices, ("output_w", "output_limit_w"))

    grid_power = diagnose_number_from(
        runtime_data,
        [
            ("grid_power_w",),
            ("grid_power",),
            ("home_load_w",),
            ("controller", "grid_power_w"),
        ],
    )
    filtered_grid = diagnose_number_from(
        runtime_data,
        [
            ("filtered_grid_power_w",),
            ("filtered_load_w",),
            ("controller", "filtered_grid_power_w"),
            ("controller", "filtered_load_w"),
        ],
    )
    deadband_w = diagnose_float(
        diagnose_nested_get(
            config_data,
            [
                ("system", "output_control", "load_deadband_w"),
                ("system", "deadband"),
            ],
            default=10,
        )
    )
    if deadband_w is None:
        deadband_w = 10.0
    deadband_active = diagnose_bool_from(
        runtime_data,
        [
            ("deadband_active",),
            ("controller", "deadband_active"),
        ],
    )
    if deadband_active is None and filtered_grid is not None:
        deadband_active = abs(filtered_grid) <= deadband_w

    return {
        "grid_power_w": grid_power,
        "filtered_grid_power_w": filtered_grid,
        "target_output_w": target_total,
        "final_output_w": final_output,
        "deadband_active": bool(deadband_active) if deadband_active is not None else None,
        "deadband_w": deadband_w,
        "control_enabled": bool(system_runtime.get("enabled", system_config.get("enabled", True))),
        "dry_run": bool(system_config.get("dry_run", False)),
        "winter_mode": bool(winter_runtime.get("enabled", winter_config.get("enabled", False))),
        "system_limit_w": diagnose_float(system_runtime.get("max_total_power", system_config.get("max_total_power"))),
        "min_output_limit_w": diagnose_float(system_runtime.get("min_output_limit", system_config.get("min_output_limit"))),
        "loop_interval_s": diagnose_float(system_runtime.get("loop_interval", system_config.get("loop_interval"))),
        "runtime_state_path": runtime_path,
    }


def diagnose_control_samples(runtime_path, runtime_data, sample_seconds):
    embedded = diagnose_nested_get(
        runtime_data,
        [
            ("control_samples",),
            ("meter_samples",),
            ("controller", "samples"),
            ("controller", "meter_samples"),
        ],
        default=None,
    )
    samples = []
    if isinstance(embedded, list):
        for item in embedded:
            if isinstance(item, dict):
                value = diagnose_float(item.get("grid_power_w", item.get("grid_power")))
                timestamp = item.get("timestamp")
            else:
                value = diagnose_float(item)
                timestamp = None
            if value is not None:
                samples.append({"grid_power_w": value, "timestamp": timestamp})
        return samples

    if sample_seconds <= 0:
        current = diagnose_number_from(runtime_data, [("grid_power_w",), ("controller", "grid_power_w")])
        timestamp = diagnose_meter_timestamp(runtime_data)
        return [{"grid_power_w": current, "timestamp": timestamp}] if current is not None else []

    count = max(1, min(sample_seconds, 60))
    deadline = time.monotonic() + sample_seconds
    for index in range(count):
        current_data, _ = diagnose_control_load_runtime(runtime_path)
        value = diagnose_number_from(current_data, [("grid_power_w",), ("controller", "grid_power_w")])
        timestamp = diagnose_meter_timestamp(current_data)
        if value is not None:
            samples.append({"grid_power_w": value, "timestamp": timestamp})
        if index + 1 >= count or time.monotonic() >= deadline:
            break
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return samples


def diagnose_meter_quality(samples, runtime_data=None, loop_interval=None):
    values = [
        sample["grid_power_w"]
        for sample in samples
        if diagnose_float(sample.get("grid_power_w")) is not None
    ]
    threshold = max(60.0, (loop_interval or 5) * 3)
    failures = diagnose_meter_failure_count(runtime_data or {})
    if not values:
        return {
            "samples": 0,
            "warnings": ["No grid meter samples available"],
            "stale": failures >= 2,
            "noisy": False,
            "sign_changes": 0,
            "stale_reason": "read_failures" if failures >= 2 else None,
            "read_failures": failures,
        }
    sign_changes = 0
    previous_sign = 0
    for value in values:
        sign = 1 if value > 0 else -1 if value < 0 else 0
        if previous_sign and sign and sign != previous_sign:
            sign_changes += 1
        if sign:
            previous_sign = sign
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    warnings = []
    noisy = sign_changes >= max(4, len(values) // 3) or stdev > 50
    parsed_timestamps = [
        diagnose_parse_timestamp(sample.get("timestamp"))
        for sample in samples
        if isinstance(sample.get("timestamp"), str)
    ]
    parsed_timestamps = [timestamp for timestamp in parsed_timestamps if timestamp]
    unique_timestamps = {timestamp.isoformat() for timestamp in parsed_timestamps}
    value_unchanged = len(values) >= 3 and len(set(values)) == 1
    timestamp_unchanged = len(parsed_timestamps) >= 2 and len(unique_timestamps) == 1
    latest_timestamp = max(parsed_timestamps) if parsed_timestamps else None
    timestamp_age = (datetime.now(timezone.utc) - latest_timestamp).total_seconds() if latest_timestamp else None
    stale_reason = None
    if failures >= 2:
        stale_reason = "read_failures"
    elif timestamp_age is not None and timestamp_age > threshold:
        stale_reason = "measurement_timestamp_old"
    elif value_unchanged and timestamp_unchanged:
        stale_reason = "unchanged_value_and_timestamp"
    stale = stale_reason is not None
    if noisy:
        warnings.append("Meter signal appears noisy")
    if stale:
        warnings.append("Meter values appear stale")
    return {
        "samples": len(values),
        "average_w": round(sum(values) / len(values), 2),
        "min_w": min(values),
        "max_w": max(values),
        "stddev_w": round(stdev, 2),
        "sign_changes": sign_changes,
        "stale": stale,
        "noisy": noisy,
        "warnings": warnings,
        "stale_reason": stale_reason,
        "timestamp_age_seconds": round(timestamp_age, 1) if timestamp_age is not None else None,
        "read_failures": failures,
    }


def diagnose_control_distribution(config_data, runtime_data):
    devices = runtime_data.get("devices", {}) if isinstance(runtime_data.get("devices"), dict) else {}
    config_limits = {}
    config_min_soc = {}
    for item in config_data.get("devices", []) if isinstance(config_data, dict) else []:
        if isinstance(item, dict) and item.get("name"):
            name = str(item["name"])
            config_limits[name] = diagnose_float(item.get("max_power"))
            config_min_soc[name] = diagnose_float(item.get("min_soc"))

    distribution = []
    for name in sorted(set(config_limits) | set(devices)):
        device = devices.get(name, {}) if isinstance(devices.get(name), dict) else {}
        target = diagnose_float(device.get("allocated_target_w", device.get("target_w", device.get("output_w"))))
        runtime_limit = diagnose_float(device.get("max_power"))
        output_limit = diagnose_float(device.get("output_limit_w", device.get("output_limit")))
        reason = "configured allocation"
        if runtime_limit is not None and target is not None and target >= runtime_limit:
            reason = "limited by runtime max power"
        elif output_limit is not None and target is not None and target >= output_limit:
            reason = "limited by device output limit"
        distribution.append({
            "device": name,
            "target_w": target,
            "output_w": diagnose_float(device.get("output_w")),
            "runtime_max_power_w": runtime_limit,
            "configured_max_power_w": config_limits.get(name),
            "output_limit_w": output_limit,
            "online": device.get("online"),
            "reason": reason,
        })

    rules = runtime_data.get("rules", {}) if isinstance(runtime_data.get("rules"), dict) else {}
    active_rules = [
        name
        for name, value in rules.items()
        if isinstance(value, dict) and value.get("active")
    ]
    reason = "SOC balancing active" if "battery_balancing" in active_rules else "PV priority balancing active" if "pv_priority_balancing" in active_rules else "configured allocation"
    return {
        "devices": distribution,
        "reason": reason,
        "active_rules": active_rules,
    }


def diagnose_soc_analysis(config_data, runtime_data):
    devices = runtime_data.get("devices", {}) if isinstance(runtime_data.get("devices"), dict) else {}
    configured_min = {}
    for item in config_data.get("devices", []) if isinstance(config_data, dict) else []:
        if isinstance(item, dict) and item.get("name"):
            configured_min[str(item["name"])] = diagnose_float(item.get("min_soc"))

    entries = []
    soc_values = []
    warnings = []
    protected = []
    for name, device in devices.items():
        if not isinstance(device, dict):
            continue
        soc = diagnose_float(device.get("soc"))
        min_soc = diagnose_float(device.get("min_soc", configured_min.get(name, 0))) or 0
        at_min = soc is not None and min_soc > 0 and soc <= min_soc
        if soc is not None:
            soc_values.append(soc)
        if at_min:
            protected.append(name)
        entries.append({
            "device": name,
            "soc": soc,
            "min_soc": min_soc,
            "min_soc_reached": at_min,
        })
    imbalance = None
    if len(soc_values) >= 2:
        imbalance = max(soc_values) - min(soc_values)
        if imbalance > 20:
            warnings.append("SOC difference exceeds 20%")
    if protected:
        warnings.append("Minimum SOC protection active")
    winter_active = bool(diagnose_nested_get(runtime_data, [("winter", "enabled")], default=diagnose_nested_get(config_data, [("winter", "enabled")], default=False)))
    if winter_active:
        warnings.append("Winter reserve active")
    return {
        "devices": entries,
        "soc_imbalance_percent": imbalance,
        "min_soc_protected_devices": protected,
        "winter_reserve_active": winter_active,
        "warnings": warnings,
    }


def diagnose_control_stale(runtime_path, runtime_data, loop_interval):
    del runtime_path
    timestamp = diagnose_control_live_timestamp(runtime_data)
    now = datetime.now(timezone.utc)
    threshold = max(60.0, (loop_interval or 5) * 3)
    parsed = diagnose_parse_timestamp(timestamp) if timestamp else None
    if not parsed:
        return {
            "stale": False,
            "age_seconds": None,
            "stale_source": "unavailable",
            "checked": False,
            "note": "No live control timestamp available. Staleness check skipped.",
        }
    age = (now - parsed).total_seconds()
    return {
        "stale": age > threshold,
        "age_seconds": round(age, 1),
        "stale_source": "live_control_timestamp",
        "checked": True,
        "timestamp": timestamp,
        "threshold_seconds": round(threshold, 1),
    }


def diagnose_control_report(config_data, runtime_path, sample_seconds=0):
    runtime_data, runtime_error = diagnose_control_load_runtime(runtime_path)
    snapshot = diagnose_control_snapshot(config_data, runtime_data, runtime_path)
    samples = diagnose_control_samples(runtime_path, runtime_data, sample_seconds)
    meter_quality = diagnose_meter_quality(samples, runtime_data, snapshot.get("loop_interval_s"))
    distribution = diagnose_control_distribution(config_data, runtime_data)
    soc_analysis = diagnose_soc_analysis(config_data, runtime_data)
    runtime_staleness = diagnose_control_stale(runtime_path, runtime_data, snapshot.get("loop_interval_s"))

    control_path = []
    grid = snapshot.get("grid_power_w")
    filtered = snapshot.get("filtered_grid_power_w")
    target = snapshot.get("target_output_w")
    final = snapshot.get("final_output_w")
    if grid is not None:
        control_path.append(f"Grid {'import' if grid > 0 else 'export' if grid < 0 else 'neutral'} detected ({diagnose_format_watts(grid)})")
    if filtered is not None and grid is not None:
        control_path.append(f"Filter adjusted measurement to {diagnose_format_watts(filtered)}")
    if snapshot.get("deadband_active") is True:
        control_path.append(f"Deadband active within +/-{int(snapshot.get('deadband_w') or 0)} W")
    elif snapshot.get("deadband_active") is False:
        control_path.append("Deadband not active")
    if target is not None:
        control_path.append(f"Target output calculated as {diagnose_format_watts(target)}")
    if distribution["devices"]:
        limited = [item["device"] for item in distribution["devices"] if "limited" in item["reason"]]
        control_path.append("Device limits restrict output: " + ", ".join(limited) if limited else "Device limits do not restrict output")
    if final is not None:
        control_path.append(f"Final output remains {diagnose_format_watts(final)}")

    deadband = {
        "active": snapshot.get("deadband_active"),
        "threshold_w": snapshot.get("deadband_w"),
        "frequent_transitions": meter_quality.get("sign_changes", 0) >= max(4, meter_quality.get("samples", 0) // 3),
        "oscillation_around_zero": meter_quality.get("sign_changes", 0) >= 4 and abs(meter_quality.get("average_w", 0) or 0) <= (snapshot.get("deadband_w") or 10) * 2,
    }

    write_path = []
    if not snapshot.get("control_enabled", True):
        write_path.append("Control disabled")
    if snapshot.get("dry_run"):
        write_path.append("Dry run enabled")
    for item in distribution["devices"]:
        if item.get("online") is False:
            write_path.append(f"Device offline: {item['device']}")
    if not write_path:
        write_path.append("No local write-path blocker detected")

    root_causes = []
    if runtime_error == "missing":
        root_causes.append("Runtime state is missing")
    if not snapshot.get("control_enabled", True):
        root_causes.append("Control disabled")
    if snapshot.get("dry_run"):
        root_causes.append("Dry run enabled")
    if meter_quality.get("noisy"):
        root_causes.append("Grid meter signal appears noisy")
    if meter_quality.get("stale"):
        root_causes.append("Grid meter values are stale")
    if snapshot.get("deadband_active"):
        root_causes.append("Deadband currently holds output")
    if soc_analysis["min_soc_protected_devices"]:
        root_causes.append("Minimum SOC protection active")
    if soc_analysis.get("soc_imbalance_percent") is not None and soc_analysis["soc_imbalance_percent"] > 20:
        root_causes.append("SOC imbalance exceeds 20%")
    if target is not None and snapshot.get("system_limit_w") is not None and target >= snapshot["system_limit_w"]:
        root_causes.append("System output limited by runtime max_power")
    root_causes = diagnose_dedupe_root_causes(root_causes)

    return {
        "snapshot": snapshot,
        "meter_quality": meter_quality,
        "deadband": deadband,
        "control_path": control_path,
        "device_distribution": distribution,
        "soc_analysis": soc_analysis,
        "write_path": write_path,
        "root_causes": root_causes,
        "runtime_state": {
            **runtime_staleness,
            "load_error": runtime_error,
        },
    }


def diagnose_control_add_checks(checks, control):
    if not control["snapshot"].get("control_enabled", True):
        diagnose_add(checks, "control", "warning", "control_disabled", "Control disabled")
    if control["snapshot"].get("dry_run"):
        diagnose_add(checks, "control", "warning", "dry_run_enabled", "Dry run enabled")
    if control["runtime_state"].get("stale"):
        diagnose_add(checks, "control", "warning", "control_runtime_state_stale", "Live control timestamp older than expected", **control["runtime_state"])
    elif not control["runtime_state"].get("checked"):
        diagnose_add(checks, "control", "ok", "control_staleness_skipped", "INFO: No live control timestamp available. Staleness check skipped.", **control["runtime_state"])
    if control["deadband"].get("active"):
        diagnose_add(checks, "control", "ok", "deadband_active", "Deadband active")
    if control["deadband"].get("frequent_transitions"):
        diagnose_add(checks, "control", "warning", "deadband_frequent_transitions", "Frequent deadband transitions detected")
    if control["meter_quality"].get("noisy"):
        diagnose_add(checks, "control", "warning", "meter_signal_noisy", "Meter signal appears noisy")
    if control["meter_quality"].get("stale"):
        diagnose_add(checks, "control", "warning", "meter_signal_stale", "Meter values appear stale")
    if control["soc_analysis"].get("soc_imbalance_percent") is not None and control["soc_analysis"]["soc_imbalance_percent"] > 20:
        diagnose_add(checks, "control", "warning", "soc_imbalance_high", "SOC difference exceeds 20%", imbalance_percent=control["soc_analysis"]["soc_imbalance_percent"])
    for device in control["soc_analysis"].get("min_soc_protected_devices", []):
        diagnose_add(checks, "control", "ok", "min_soc_protection_active", f"Device {device} protected by minimum SOC", device=device)
    for cause in control["root_causes"]:
        cause = diagnose_standard_root_cause(cause)
        diagnose_add(
            checks,
            "control",
            "error" if cause["severity"] == "error" else "warning" if cause["severity"] == "warning" else "ok",
            "control_root_cause",
            f"Likely Cause: {cause['title']}",
            cause=cause,
            root_cause_code=cause["code"],
            suggested_next_check=cause["suggested_next_check"],
        )


def diagnose_control_text(control):
    snapshot = control["snapshot"]
    lines = [
        "Control Snapshot",
        "",
        f"Grid Power:           {diagnose_format_watts(snapshot.get('grid_power_w'))}",
        f"Filtered Grid:        {diagnose_format_watts(snapshot.get('filtered_grid_power_w'))}",
        f"Target Output:        {diagnose_format_watts(snapshot.get('target_output_w'))}",
        f"Final Output:         {diagnose_format_watts(snapshot.get('final_output_w'))}",
        "",
        f"Deadband:             {'active' if snapshot.get('deadband_active') else 'inactive' if snapshot.get('deadband_active') is False else 'unknown'}",
        f"Winter Mode:          {'enabled' if snapshot.get('winter_mode') else 'disabled'}",
        f"Control:              {'enabled' if snapshot.get('control_enabled') else 'disabled'}",
        f"Dry Run:              {'enabled' if snapshot.get('dry_run') else 'disabled'}",
        "",
        "Decision Explanation",
        "",
    ]
    for index, item in enumerate(control["control_path"], start=1):
        lines.append(f"{index}. {item}")

    quality = control["meter_quality"]
    if quality.get("samples", 0):
        lines.extend([
            "",
            "Grid Meter Quality",
            "",
            f"Samples: {quality.get('samples')}",
            f"Average: {diagnose_format_watts(quality.get('average_w'))}",
            f"Min: {diagnose_format_watts(quality.get('min_w'))}",
            f"Max: {diagnose_format_watts(quality.get('max_w'))}",
            f"Sign Changes: {quality.get('sign_changes', 0)}",
        ])
        for warning in quality.get("warnings", []):
            lines.append(f"WARNING: {warning}")

    if control["device_distribution"].get("devices"):
        lines.extend(["", "Distribution", ""])
        for item in control["device_distribution"]["devices"]:
            lines.append(f"{item['device']}: {diagnose_format_watts(item.get('target_w'))} ({item['reason']})")
        lines.append("")
        lines.append("Reason:")
        lines.append(control["device_distribution"].get("reason", "configured allocation"))

    if control["soc_analysis"].get("warnings"):
        lines.extend(["", "SOC Diagnostics", ""])
        for warning in control["soc_analysis"]["warnings"]:
            lines.append(f"WARNING: {warning}")

    runtime_state = control.get("runtime_state", {})
    if not runtime_state.get("checked"):
        lines.extend([
            "",
            "Runtime State Diagnostics",
            "",
            "INFO:",
            "No live control timestamp available.",
            "Staleness check skipped.",
        ])

    if control["write_path"]:
        lines.extend(["", "Write Path", ""])
        for item in control["write_path"]:
            lines.append(item)

    if control["root_causes"]:
        lines.extend(["", "Likely Causes", ""])
        for cause in control["root_causes"]:
            if isinstance(cause, dict):
                lines.append(f"- {cause['title']}: {cause['message']}")
                lines.append(f"  Next check: {cause['suggested_next_check']}")
            else:
                lines.append(f"- {cause}")

    return "\n".join(lines) + "\n"


def diagnose_battery_full_charge_assist_text(report):
    assist = report.get("battery_full_charge_assist") or {}
    lines = [
        "Battery full-charge assist:",
        f"  enabled: {str(bool(assist.get('enabled'))).lower()}",
        f"  interval: {assist.get('interval_days')} days",
        f"  assist window: {assist.get('assist_window_days')} days",
        f"  assist start SOC: {assist.get('assist_start_soc')} %",
        f"  force time: {assist.get('force_time') or 'invalid'}",
        f"  AC charge mode: {'enabled' if assist.get('enable_ac_charge_mode') else 'disabled'}",
        f"  AC charge power: {assist.get('ac_charge_power')} W",
        f"  state database: {assist.get('state_database_path')}",
        "",
        "Devices:",
    ]
    if assist.get("state_database_error") == "missing":
        lines.append("  state database: not initialized yet")
    elif assist.get("state_database_error"):
        lines.append(f"  state database error: {assist.get('state_database_error')}")

    for item in assist.get("devices", []):
        lines.extend([
            f"  {item['device']}:",
            f"    battery: {'yes' if item.get('battery') else 'no'}",
            f"    packNum: {item.get('packNum') if item.get('packNum') is not None else 'unknown'}",
            f"    last full charge: {item.get('last_full_charge') or 'unknown'}",
            f"    next due: {item.get('next_due') or 'unknown'}",
            f"    firmware socLimit: {item.get('firmware_socLimit') if item.get('firmware_socLimit') is not None else 'unknown'}",
            f"    socStatus: {item.get('socStatus') if item.get('socStatus') is not None else 'unknown'}",
            f"    batCalTime: {item.get('batCalTime') if item.get('batCalTime') is not None else 'unknown'}",
            f"    acMode: {item.get('acMode') if item.get('acMode') is not None else 'unknown'}",
            f"    acStatus: {item.get('acStatus') if item.get('acStatus') is not None else 'unknown'}",
            f"    assist active: {'yes' if item.get('assist_active') else 'no'}",
            f"    restore pending: {'yes' if item.get('restore_pending') else 'no'}",
            f"    ac mode restore pending: {'yes' if item.get('ac_mode_restore_pending') else 'no'}",
            f"    status: {item.get('status')}",
        ])

    return "\n".join(lines)


def diagnose_quality_root_cause(code, severity, title, message, suggested_next_check):
    severity = str(severity).lower()
    if severity not in ROOT_CAUSE_SEVERITIES:
        severity = "warning"
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "message": message,
        "suggested_next_check": suggested_next_check,
    }


def diagnose_export_import_quality(samples):
    values = [
        diagnose_float(sample.get("grid_power_w"))
        for sample in samples
        if diagnose_float(sample.get("grid_power_w")) is not None
    ]
    if not values:
        return {
            "status": "warning",
            "samples": 0,
            "message": "No grid power samples available",
        }

    imports = [value for value in values if value > NEAR_ZERO_BAND_W]
    exports = [value for value in values if value < -NEAR_ZERO_BAND_W]
    near_zero = [value for value in values if abs(value) <= NEAR_ZERO_BAND_W]
    sample_count = len(values)
    max_export = min(values)
    average_export = sum(exports) / len(exports) if exports else 0
    export_duration = round((len(exports) / sample_count) * 100, 2)
    status = "ok"
    warnings = []
    if max_export <= -EXPORT_PEAK_ERROR_W:
        status = "error"
        warnings.append(f"Export peaks up to {round(max_export)} W detected.")
    elif max_export <= -EXPORT_PEAK_WARNING_W:
        status = "warning"
        warnings.append(f"Export peaks up to {round(max_export)} W detected.")
    if export_duration > EXPORT_DURATION_WARNING_PERCENT:
        status = "warning" if status == "ok" else status
        warnings.append(f"Export duration is {export_duration}%.")
    if abs(average_export) > EXPORT_AVERAGE_WARNING_W:
        status = "warning" if status == "ok" else status
        warnings.append(f"Average export is {round(average_export)} W.")

    return {
        "status": status,
        "samples": sample_count,
        "average_grid_power_w": round(sum(values) / sample_count, 2),
        "min_grid_power_w": min(values),
        "max_grid_power_w": max(values),
        "average_import_w": round(sum(imports) / len(imports), 2) if imports else 0,
        "average_export_w": round(average_export, 2),
        "max_import_peak_w": max(values),
        "max_export_peak_w": max_export,
        "export_duration_percent": export_duration,
        "import_duration_percent": round((len(imports) / sample_count) * 100, 2),
        "near_zero_duration_percent": round((len(near_zero) / sample_count) * 100, 2),
        "warnings": warnings,
    }


def diagnose_quality_score(export_import):
    if not export_import.get("samples"):
        return {
            "score": None,
            "classification": "unknown",
            "average_absolute_deviation_w": None,
            "near_zero_percent": 0,
            "export_penalty": "unknown",
            "import_penalty": "unknown",
        }

    # Diagnostic score, not a certified measurement:
    # start at 100 and subtract bounded penalties for average grid deviation,
    # export duration, export peak severity, and import peaks. This keeps the
    # score intentionally coarse while making the dominant problem visible.
    avg_abs = abs(export_import["average_grid_power_w"])
    near_zero = export_import["near_zero_duration_percent"]
    export_peak = abs(min(0, export_import["max_export_peak_w"]))
    import_peak = max(0, export_import["max_import_peak_w"])
    export_duration = export_import["export_duration_percent"]
    deviation_penalty = min(35, avg_abs / 2)
    duration_penalty = min(25, export_duration * 0.4)
    export_peak_penalty = min(25, max(0, export_peak - NEAR_ZERO_BAND_W) / 8)
    import_peak_penalty = min(15, max(0, import_peak - 300) / 30)
    score = int(round(max(
        0,
        100 - deviation_penalty - duration_penalty - export_peak_penalty - import_peak_penalty,
    )))
    if score >= 95:
        classification = "excellent"
    elif score >= 85:
        classification = "good"
    elif score >= 70:
        classification = "acceptable"
    elif score >= 50:
        classification = "poor"
    else:
        classification = "critical"

    def penalty_label(value):
        if value < 5:
            return "low"
        if value < 15:
            return "medium"
        return "high"

    return {
        "score": score,
        "classification": classification,
        "average_absolute_deviation_w": round(avg_abs, 2),
        "near_zero_percent": near_zero,
        "export_penalty": penalty_label(duration_penalty + export_peak_penalty),
        "import_penalty": penalty_label(import_peak_penalty),
    }


def diagnose_quality_pv(config_data, runtime_data):
    devices = runtime_data.get("devices", {}) if isinstance(runtime_data.get("devices"), dict) else {}
    pv_total = diagnose_number_from(runtime_data, [("pv_total_w",), ("pv_input_w",)])
    if pv_total is None:
        pv_total = diagnose_sum_devices(devices, ("pv_input_w", "solar", "pv_power"))
    if pv_total is None:
        return {
            "status": "info",
            "available": False,
            "message": "PV diagnostics skipped because required PV telemetry is not available.",
            "root_causes": [
                diagnose_quality_root_cause(
                    "missing_pv_telemetry",
                    "info",
                    "Missing PV telemetry",
                    "PV diagnostics skipped because required PV telemetry is not available.",
                    "Check whether device telemetry includes pv_input_w or pv_total_w.",
                )
            ],
        }

    home_output = diagnose_number_from(runtime_data, [("inverter_output_w",), ("home_output_w",), ("output_w",)])
    if home_output is None:
        home_output = diagnose_sum_devices(devices, ("output_w",))
    battery_charge = diagnose_number_from(runtime_data, [("battery_charge_w",), ("pack_input_w",)])
    if battery_charge is None:
        charge_values = []
        for device in devices.values():
            if not isinstance(device, dict):
                continue
            value = diagnose_float(device.get("battery_power_w"))
            if value is not None and value < 0:
                charge_values.append(abs(value))
            else:
                pack_input = diagnose_float(device.get("pack_input_w"))
                if pack_input is not None:
                    charge_values.append(pack_input)
        battery_charge = sum(charge_values) if charge_values else None
    system_limit = diagnose_number_from(runtime_data, [("system", "max_total_power")])
    if system_limit is None:
        system_limit = diagnose_nested_get(config_data, [("system", "max_total_power")])
        system_limit = diagnose_float(system_limit)
    max_device_limit = 0
    for item in config_data.get("devices", []) if isinstance(config_data, dict) else []:
        if isinstance(item, dict):
            max_device_limit += diagnose_float(item.get("max_power")) or 0

    root_causes = []
    notes = []
    status = "ok"
    output = home_output or 0
    charge = battery_charge or 0
    if pv_total > 100 and output < min(pv_total * 0.3, 100) and charge < min(pv_total * 0.3, 100):
        status = "warning"
        notes.append("PV input is available, but home output and battery charge remain low.")
        root_causes.append(diagnose_quality_root_cause(
            "pv_available_but_not_used",
            "warning",
            "PV available but not used",
            "PV input is available, but home output and battery charge remain low.",
            "Check device output limits, SOC protection, device online state, and telemetry freshness.",
        ))
    if system_limit and output >= system_limit * 0.95 and pv_total > system_limit:
        notes.append("Output is limited by system max power.")
        root_causes.append(diagnose_quality_root_cause(
            "pv_limited_by_system_limit",
            "info",
            "PV limited by system limit",
            "PV input exceeds the configured system output limit.",
            "Check system.max_total_power and legal AC output limits.",
        ))
    if max_device_limit and output >= max_device_limit * 0.95 and pv_total > max_device_limit:
        notes.append("Output is limited by device max power.")
        root_causes.append(diagnose_quality_root_cause(
            "pv_limited_by_device_limit",
            "info",
            "PV limited by device limit",
            "PV input exceeds the sum of configured device max_power limits.",
            "Check per-device max_power and runtime max_power overrides.",
        ))
    if not notes:
        notes.append("PV usage looks plausible.")

    return {
        "status": status,
        "available": True,
        "pv_available_w": pv_total,
        "home_output_w": home_output,
        "battery_charge_w": battery_charge,
        "system_limit_w": system_limit,
        "device_limit_total_w": max_device_limit,
        "messages": notes,
        "root_causes": root_causes,
    }


def diagnose_quality_soc(config_data, runtime_data):
    devices = runtime_data.get("devices", {}) if isinstance(runtime_data.get("devices"), dict) else {}
    rows = []
    soc_values = []
    for name, device in devices.items():
        if not isinstance(device, dict):
            continue
        soc = diagnose_float(device.get("soc"))
        if soc is None:
            continue
        soc_values.append(soc)
    if not soc_values:
        return {
            "status": "info",
            "message": "SOC balancing skipped because SOC telemetry is not available.",
            "devices": [],
            "root_causes": [],
        }
    average_soc = sum(soc_values) / len(soc_values)
    highest = max(soc_values)
    lowest = min(soc_values)
    spread = highest - lowest
    root_causes = []
    status = "ok"
    if spread >= SOC_SPREAD_ERROR_PERCENT:
        status = "error"
        root_causes.append(diagnose_quality_root_cause(
            "soc_imbalance_detected",
            "error",
            "SOC imbalance detected",
            f"Battery SOC spread is {round(spread, 1)} %, above the error threshold.",
            "Check device runtime limits and SOC balancing configuration.",
        ))
    elif spread >= SOC_SPREAD_WARNING_PERCENT:
        status = "warning"
        root_causes.append(diagnose_quality_root_cause(
            "soc_imbalance_detected",
            "warning",
            "SOC imbalance detected",
            f"Battery SOC spread is {round(spread, 1)} %, above the warning threshold.",
            "Check device runtime limits and SOC balancing configuration.",
        ))

    for name, device in devices.items():
        if not isinstance(device, dict):
            continue
        soc = diagnose_float(device.get("soc"))
        if soc is None:
            continue
        output = diagnose_float(device.get("output_w")) or 0
        charge = 0
        battery_power = diagnose_float(device.get("battery_power_w"))
        if battery_power is not None and battery_power < 0:
            charge = abs(battery_power)
        min_soc = diagnose_float(device.get("min_soc")) or 0
        max_limit = diagnose_float(device.get("max_power", device.get("output_limit_w")))
        protected = min_soc > 0 and soc <= min_soc + LOW_SOC_PROTECTION_MARGIN_PERCENT
        rows.append({
            "device": name,
            "soc": soc,
            "output_w": output,
            "charge_w": charge,
            "max_output_limit_w": max_limit,
            "min_soc": min_soc,
            "difference_to_average_soc": round(soc - average_soc, 2),
            "min_soc_protected": protected,
            "runtime_max_power_w": diagnose_float(device.get("max_power")),
        })
        if protected:
            root_causes.append(diagnose_quality_root_cause(
                "min_soc_protected_device",
                "info",
                "Device protected by minimum SOC",
                f"Device {name} is close to its configured minimum SOC.",
                "Check whether minimum SOC settings intentionally protect this battery.",
            ))

    if len(rows) >= 2:
        lowest_row = min(rows, key=lambda row: row["soc"])
        highest_row = max(rows, key=lambda row: row["soc"])
        if (
            spread >= SOC_SPREAD_WARNING_PERCENT
            and lowest_row["output_w"] > highest_row["output_w"] + 50
        ):
            status = "warning" if status == "ok" else status
            root_causes.append(diagnose_quality_root_cause(
                "lower_soc_device_overused",
                "warning",
                "Lower SOC device overused",
                "A lower-SOC device is contributing more output than a higher-SOC device.",
                "Check runtime device limits, online state, and SOC balancing rules.",
            ))

    return {
        "status": status,
        "average_soc": round(average_soc, 2),
        "highest_soc": highest,
        "lowest_soc": lowest,
        "soc_spread": round(spread, 2),
        "devices": rows,
        "root_causes": root_causes,
    }


def diagnose_control_quality_report(config_data, runtime_path, sample_seconds=0):
    runtime_data, _ = diagnose_control_load_runtime(runtime_path)
    samples = diagnose_control_samples(runtime_path, runtime_data, sample_seconds)
    export_import = diagnose_export_import_quality(samples)
    quality_score = diagnose_quality_score(export_import)
    pv = diagnose_quality_pv(config_data, runtime_data)
    soc = diagnose_quality_soc(config_data, runtime_data)
    root_causes = []
    if export_import.get("samples") and export_import.get("status") in ("warning", "error"):
        root_causes.append(diagnose_quality_root_cause(
            "export_peaks_detected",
            export_import["status"],
            "Export peaks detected",
            "Grid export peaks were detected during the sample window.",
            "Check grid meter freshness and current output/device limits.",
        ))
    if quality_score.get("classification") in ("poor", "critical"):
        root_causes.append(diagnose_quality_root_cause(
            "zero_export_quality_poor",
            "warning" if quality_score["classification"] == "poor" else "error",
            "Zero-export quality poor",
            f"Regulation quality is classified as {quality_score['classification']}.",
            "Review export/import metrics and meter quality before changing settings.",
        ))
    root_causes.extend(pv.get("root_causes", []))
    root_causes.extend(soc.get("root_causes", []))
    deduped = []
    seen = set()
    for cause in root_causes:
        key = (cause["code"], cause["severity"], cause["message"])
        if key not in seen:
            seen.add(key)
            deduped.append(cause)
    status_order = {"ok": 0, "info": 0, "warning": 1, "error": 2}
    status = "ok"
    for item in (export_import, pv, soc):
        if status_order.get(item.get("status"), 0) > status_order[status]:
            status = "error" if item.get("status") == "error" else "warning"
    for cause in deduped:
        if cause["severity"] == "error":
            status = "error"
        elif cause["severity"] == "warning" and status == "ok":
            status = "warning"
    return {
        "status": status,
        "sample_seconds": int(sample_seconds or 0),
        "export_import": export_import,
        "quality_score": quality_score,
        "pv_diagnostics": pv,
        "soc_balancing": soc,
        "root_causes": deduped,
    }


def diagnose_control_quality_add_checks(checks, report):
    for cause in report.get("root_causes", []):
        diagnose_add(
            checks,
            "control_quality",
            "error" if cause["severity"] == "error" else "warning" if cause["severity"] == "warning" else "ok",
            "control_quality_root_cause",
            f"{cause['title']}: {cause['message']}",
            root_cause_code=cause["code"],
            suggested_next_check=cause["suggested_next_check"],
        )


def diagnose_control_quality_text(report):
    export_import = report["export_import"]
    score = report["quality_score"]
    lines = ["Export / Import Quality", ""]
    if export_import.get("samples"):
        lines.extend([
            f"Samples:              {export_import['samples']}",
            f"Average Grid Power:   {diagnose_format_watts(export_import['average_grid_power_w'])}",
            f"Max Import:           {diagnose_format_watts(export_import['max_import_peak_w'])}",
            f"Max Export:           {diagnose_format_watts(export_import['max_export_peak_w'])}",
            f"Export Duration:      {round(export_import['export_duration_percent'])} %",
            f"Near Zero:            {round(export_import['near_zero_duration_percent'])} %",
            "",
            f"Result: {export_import['status'].upper()}",
        ])
        for warning in export_import.get("warnings", []):
            lines.append(f"WARNING: {warning}")
    else:
        lines.append("INFO: No grid power samples available.")

    lines.extend(["", "Regulation Quality", ""])
    if score.get("score") is None:
        lines.append("Quality Score:        unknown")
    else:
        lines.extend([
            "Target:               0 W grid exchange",
            f"Average Deviation:    {diagnose_format_watts(score['average_absolute_deviation_w'])}",
            f"Near-Zero Share:      {round(score['near_zero_percent'])} %",
            f"Export Penalty:       {score['export_penalty']}",
            f"Quality Score:        {score['score']} / 100",
            f"Classification:       {score['classification']}",
        ])

    pv = report["pv_diagnostics"]
    lines.extend(["", "PV Diagnostics", ""])
    if not pv.get("available"):
        lines.append("INFO: PV diagnostics skipped because required PV telemetry is not available.")
    else:
        lines.extend([
            f"PV Available:         {diagnose_format_watts(pv.get('pv_available_w'))}",
            f"Home Output:          {diagnose_format_watts(pv.get('home_output_w'))}",
            f"Battery Charge:       {diagnose_format_watts(pv.get('battery_charge_w'))}",
            f"System Limit:         {diagnose_format_watts(pv.get('system_limit_w'))}",
            "",
            "Result:",
        ])
        lines.extend(pv.get("messages", []))

    soc = report["soc_balancing"]
    lines.extend(["", "SOC Balancing", ""])
    if not soc.get("devices"):
        lines.append("INFO: SOC balancing skipped because SOC telemetry is not available.")
    else:
        lines.extend([
            f"Average SOC:          {round(soc['average_soc'])} %",
            f"SOC Spread:           {round(soc['soc_spread'])} %",
            "",
        ])
        for row in soc["devices"]:
            lines.append(
                f"{row['device']}:                  {round(row['soc'])} %, output {round(row['output_w'])} W"
            )
        lines.append("")
        lines.append("Result:")
        lines.append("SOC balancing looks healthy." if soc["status"] == "ok" else f"SOC balancing status: {soc['status']}.")

    if report.get("root_causes"):
        lines.extend(["", "Likely Causes", ""])
        for cause in report["root_causes"]:
            lines.append(f"- {cause['title']}: {cause['message']}")
    return "\n".join(lines) + "\n"


def diagnose_write_support_bundle(report, args, config_data, runtime_path):
    output_path = diagnose_support_bundle_path(args.output)
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    runtime_data = None
    if runtime_path and os.path.exists(runtime_path):
        runtime_data, _ = diagnose_json_file(runtime_path)

    control_report = report.get("control") or {}
    control_quality_report = report.get("control_quality") or {}
    build = report.get("build") or {}
    metadata = {
        "bundle_version": SUPPORT_BUNDLE_VERSION,
        "generated_at": report.get("generated_at"),
        "ems_version": report.get("ems_version"),
        "build_label": build.get("build_label"),
        "schema_version": report.get("schema_version", DIAGNOSE_SCHEMA_VERSION),
    }

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("diagnosis.txt", diagnose_redact_text(diagnose_text(report)))
        bundle.writestr("diagnosis.json", diagnose_redact_text(json.dumps(report, indent=2, sort_keys=True)))
        bundle.writestr(
            "control-diagnostics.json",
            diagnose_redact_text(json.dumps(control_report, indent=2, sort_keys=True)),
        )
        bundle.writestr(
            "control-diagnostics.txt",
            diagnose_redact_text(
                diagnose_control_text(control_report)
                if control_report
                else "Control diagnostics not enabled.\n"
            ),
        )
        bundle.writestr(
            "control-quality.json",
            diagnose_redact_text(json.dumps(control_quality_report, indent=2, sort_keys=True)),
        )
        bundle.writestr(
            "control-quality.txt",
            diagnose_redact_text(
                diagnose_control_quality_text(control_quality_report)
                if control_quality_report
                else "Control quality diagnostics not enabled.\n"
            ),
        )
        bundle.writestr(
            "redacted-config.json",
            json.dumps(
                diagnose_redact_value(config_data if isinstance(config_data, dict) else {}),
                indent=2,
                sort_keys=True,
            ),
        )
        bundle.writestr(
            "runtime-state.json",
            json.dumps(
                diagnose_redact_value(runtime_data if isinstance(runtime_data, dict) else {}),
                indent=2,
                sort_keys=True,
            ),
        )
        bundle.writestr(
            "bundle-metadata.json",
            json.dumps(metadata, indent=2, sort_keys=True),
        )
    return output_path


def diagnose_runtime_paths(checks, args):
    # Lazy import: ems.backup imports this module, so importing it at module top
    # would create a cycle.
    from ems import backup as backup_mod

    in_container = backup_mod.running_in_container()
    backup_default = backup_mod.default_backup_dir()
    config_path = args.config
    data_dir = (
        os.path.dirname(backup_default) or "/app/data"
        if in_container
        else os.path.join(BASE_DIR, "data")
    )

    diagnose_add(
        checks, "runtime_paths", "ok", "container_mode",
        f"container mode: {'yes' if in_container else 'no'}",
        container_mode=in_container,
    )
    if in_container:
        source = backup_mod.container_detection_source()
        if source:
            diagnose_add(
                checks, "runtime_paths", "ok", "container_detection",
                f"container detection: {source}", source=source,
            )
    diagnose_add(
        checks, "runtime_paths", "ok", "config_path",
        f"config: {config_path}", path=config_path,
    )
    diagnose_add(
        checks, "runtime_paths", "ok", "data_path",
        f"data: {data_dir}", path=data_dir,
    )
    diagnose_add(
        checks, "runtime_paths", "ok", "backup_default",
        f"backup default: {backup_default}", path=backup_default,
    )

    if in_container:
        host_backup_path = "data/backups"
        diagnose_add(
            checks, "runtime_paths", "ok", "backup_host_path",
            f"backup host path: {host_backup_path}", path=host_backup_path,
        )
        persistent = diagnose_path_within(backup_default, "/app/data")
        diagnose_add(
            checks, "runtime_paths", "ok", "backup_persistent",
            f"backup persistent: {'yes' if persistent else 'no'}",
            persistent=persistent,
        )
        if not persistent:
            diagnose_add(
                checks, "runtime_paths", "warning",
                "container_backup_not_persistent",
                "container backup default is not under /app/data; "
                "backups may not survive container recreation.",
                path=backup_default,
            )


def diagnose_collect(args):
    checks = []
    mode, mode_sources = diagnose_container_mode()

    uid = os.getuid() if hasattr(os, "getuid") else None
    gid = os.getgid() if hasattr(os, "getgid") else None
    python_version = platform.python_version()
    python_info = sys.version_info
    diagnose_add(
        checks,
        "environment",
        "ok",
        "python_version",
        f"Python version: {python_version}",
        version=python_version,
    )
    if python_info >= (3, 10):
        diagnose_add(checks, "environment", "ok", "python_version_supported", "Python version is supported", version=python_version)
    else:
        diagnose_add(checks, "environment", "warning", "python_version_unsupported", "Python version is older than the supported baseline", version=python_version)
    diagnose_add(
        checks,
        "environment",
        "ok",
        "platform",
        f"Platform: {platform.platform()}",
        platform=platform.platform(),
        system=platform.system(),
        release=platform.release(),
    )
    cwd = os.getcwd()
    diagnose_add(checks, "environment", "ok", "current_working_directory", f"Current working directory: {cwd}", cwd=cwd)
    if diagnose_path_within(cwd, BASE_DIR):
        diagnose_add(checks, "environment", "ok", "cwd_inside_project", "Current working directory is inside project base")
    else:
        diagnose_add(checks, "environment", "warning", "cwd_outside_project", "Current working directory is outside project base")
    if uid is not None and gid is not None:
        diagnose_add(checks, "environment", "ok", "current_user", f"Current user: uid={uid} gid={gid}", uid=uid, gid=gid)
        root_level = "warning" if uid == 0 else "ok"
        diagnose_add(checks, "environment", root_level, "process_root", "Process runs as root" if uid == 0 else "Process does not run as root", uid=uid)

    config_path = args.config
    template_path = str(resolve_template_path(base_dir=BASE_DIR))
    data_dir = os.path.join(BASE_DIR, "data")

    for code, path, label, required in (
        ("emsctl", os.path.join(BASE_DIR, "emsctl.py"), "emsctl.py", True),
        ("main_script", os.path.join(BASE_DIR, "ems-solarflow-api-control.py"), "ems-solarflow-api-control.py", True),
        ("requirements", os.path.join(BASE_DIR, "requirements.txt"), "requirements.txt", True),
        ("requirements_dev", os.path.join(BASE_DIR, "requirements-dev.txt"), "requirements-dev.txt", False),
        ("config", config_path, "config.json", True),
        ("config_template", template_path, "config.template.json", True),
    ):
        diagnose_check_path(checks, "project", code, path, label, expect_file=True, check_read=True, missing_level="warning" if not required else "error")

    diagnose_check_path(checks, "project", "data_dir", data_dir, "data directory", expect_dir=True, check_read=True, check_write=True, missing_level="warning")
    diagnose_git_info(checks)

    config_data, config_error = diagnose_json_file(config_path)
    template_data, template_error = diagnose_json_file(template_path)

    if config_error is None:
        if isinstance(config_data, dict):
            diagnose_add(checks, "config", "ok", "config_valid_json", "config.json is valid JSON", path=config_path)
        else:
            diagnose_add(checks, "config", "error", "config_not_object", "config.json must contain a JSON object", path=config_path)
            config_data = {}
    elif config_error == "missing":
        diagnose_add(checks, "config", "error", "config_missing", f"config.json missing: {config_path}", path=config_path)
        config_data = {}
    else:
        diagnose_add(checks, "config", "error", "config_invalid_json", f"config.json is invalid JSON: {config_error}", path=config_path)
        config_data = {}

    if template_error is None:
        if isinstance(template_data, dict):
            diagnose_add(checks, "config", "ok", "template_valid_json", "config.template.json is valid JSON", path=template_path)
        else:
            diagnose_add(checks, "config", "error", "template_not_object", "config.template.json must contain a JSON object", path=template_path)
            template_data = {}
    elif template_error == "missing":
        diagnose_add(checks, "config", "error", "template_missing", f"config.template.json missing: {template_path}", path=template_path)
        template_data = {}
    else:
        diagnose_add(checks, "config", "error", "template_invalid_json", f"config.template.json is invalid JSON: {template_error}", path=template_path)
        template_data = {}

    if isinstance(config_data, dict) and isinstance(template_data, dict) and template_data:
        missing_keys = diagnose_missing_template_keys(config_data, template_data)
        if missing_keys:
            for key in missing_keys:
                diagnose_add(checks, "config", "warning", "missing_config_key", f"Missing config key: {key}", key=key)
        else:
            diagnose_add(checks, "config", "ok", "config_keys_complete", "config.json contains all template keys")

    diagnose_config_plausibility(checks, args, config_data)

    runtime_path = resolve_runtime_path(args, config_data)
    diagnose_parent_path(checks, "project", "runtime_state", runtime_path, "runtime-state path", check_write=True, missing_level="warning")
    battery_full_charge_report = diagnose_battery_full_charge_assist_report(
        config_data
    )
    battery_state_database_path = battery_full_charge_report[
        "state_database_path"
    ]
    diagnose_parent_path(
        checks,
        "project",
        "battery_full_charge_state_database",
        battery_state_database_path,
        "battery full-charge assist state database",
        check_write=True,
        missing_level="warning"
    )

    dashboard_config = config_data.get("dashboard", {}) if isinstance(config_data, dict) else {}
    database_path = None
    if isinstance(dashboard_config, dict):
        database_path = dashboard_config.get("database_path")
    if database_path:
        database_path = resolve_project_path(str(database_path))
        diagnose_parent_path(checks, "project", "dashboard_database", database_path, "dashboard database path", check_write=True, missing_level="warning")

    diagnose_parent_path(checks, "runtime_state", "runtime_state", runtime_path, "runtime-state file", check_write=True, missing_level="warning")
    diagnose_check_path(checks, "runtime_state", "runtime_state_file", runtime_path, "runtime-state.json", expect_file=True, require_exists=False, check_read=True, check_write=True)
    runtime_json_error = diagnose_read_json_if_nonempty(runtime_path)
    if runtime_json_error is None and os.path.exists(runtime_path) and os.path.getsize(runtime_path) > 0:
        diagnose_add(checks, "runtime_state", "ok", "runtime_state_valid_json", "runtime-state.json is valid JSON", path=runtime_path)
    elif runtime_json_error:
        diagnose_add(checks, "runtime_state", "error", "runtime_state_invalid_json", f"runtime-state.json is invalid JSON: {runtime_json_error}", path=runtime_path)
    diagnose_runtime_state_plausibility(checks, runtime_path, config_data)

    if database_path:
        diagnose_parent_path(checks, "data", "dashboard_database", database_path, "dashboard database", check_write=True, missing_level="warning")
    if battery_state_database_path:
        diagnose_parent_path(checks, "data", "battery_full_charge_state_database", battery_state_database_path, "battery full-charge assist state database", check_write=True, missing_level="warning")

    system_config = config_data.get("system", {}) if isinstance(config_data, dict) else {}
    if isinstance(system_config, dict):
        for key in ("log_path", "log_file"):
            log_path = system_config.get(key)
            if log_path:
                log_path = resolve_project_path(str(log_path))
                diagnose_parent_path(checks, "logs", key, log_path, f"{key}", check_write=True, missing_level="warning")

    if mode == "container":
        for source in mode_sources:
            diagnose_add(checks, "docker", "ok", "container_detected", f"Container detected via {source['source']}", **source)

        pid1_identity, pid1_error = diagnose_uid_gid("/proc/1")
        if pid1_identity:
            diagnose_add(checks, "docker", "ok", "pid1_user", f"PID 1 user: uid={pid1_identity['uid']} gid={pid1_identity['gid']}", **pid1_identity)
        else:
            diagnose_add(checks, "docker", "warning", "pid1_user_unavailable", f"PID 1 UID/GID unavailable: {pid1_error}")

        if uid is not None and gid is not None:
            diagnose_add(checks, "docker", "ok", "container_process_user", f"Current process user: uid={uid} gid={gid}", uid=uid, gid=gid)
            if uid == 0:
                diagnose_add(checks, "docker", "warning", "container_process_root", "Container process runs as root", uid=uid)

        for path, code in (("/app/config", "app_config"), ("/app/data", "app_data")):
            diagnose_check_path(checks, "docker", code, path, path, expect_dir=True, check_read=True, check_write=(path == "/app/data"), missing_level="warning")
            identity, error = diagnose_uid_gid(path)
            if identity:
                diagnose_add(checks, "docker", "ok", f"{code}_owner", f"{path} owner: uid={identity['uid']} gid={identity['gid']}", **identity)
            elif os.path.exists(path):
                diagnose_add(checks, "docker", "warning", f"{code}_owner_unavailable", f"{path} owner unavailable: {error}")

        diagnose_check_path(checks, "docker", "app_config_config", "/app/config/config.json", "/app/config/config.json", expect_file=True, check_read=True, missing_level="warning")

        for label, path in (
            ("runtime-state path", runtime_path),
            ("dashboard database path", database_path),
            ("battery full-charge state database path", battery_state_database_path),
        ):
            if not path:
                continue
            if diagnose_path_within(path, "/app/data"):
                diagnose_add(checks, "docker", "ok", "container_data_path", f"{label} resolves below /app/data: {path}", path=path)
            else:
                diagnose_add(checks, "docker", "warning", "container_data_path_outside_app_data", f"{label} does not resolve below /app/data: {path}", path=path)

    diagnose_runtime_paths(checks, args)

    if args.deep:
        diagnose_database_deep(checks, database_path)
        diagnose_dashboard_deep(checks, config_data)
        diagnose_logs_deep(checks, config_data)
        diagnose_docker_deep(checks)

    hardware_health = None
    if args.hardware:
        hardware_health = diagnose_hardware(checks, config_data)

    control_report = None
    if args.control:
        control_report = diagnose_control_report(
            config_data,
            runtime_path,
            sample_seconds=max(0, args.sample_seconds or 0),
        )
        diagnose_control_add_checks(checks, control_report)

    control_quality_report = None
    if args.control_quality or args.quality:
        control_quality_report = diagnose_control_quality_report(
            config_data,
            runtime_path,
            sample_seconds=max(0, args.sample_seconds or 0),
        )
        diagnose_control_quality_add_checks(checks, control_quality_report)

    summary = {
        "ok": sum(1 for check in checks if check["level"] == "ok"),
        "warning": sum(1 for check in checks if check["level"] == "warning"),
        "error": sum(1 for check in checks if check["level"] == "error"),
    }
    status = "error" if summary["error"] else "warning" if summary["warning"] else "ok"
    return {
        "status": status,
        "mode": mode,
        "mode_sources": mode_sources,
        "summary": summary,
        "options": {
            "deep": bool(args.deep),
            "hardware": bool(args.hardware),
            "support_bundle": bool(args.support_bundle),
            "control": bool(args.control),
            "control_quality": bool(args.control_quality or args.quality),
            "sample_seconds": int(args.sample_seconds or 0),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": {
            "base_dir": BASE_DIR,
            "config_path": config_path,
            "runtime_state_path": runtime_path,
            "battery_full_charge_state_database_path": battery_state_database_path,
        },
        "checks": checks,
        "control": control_report,
        "control_quality": control_quality_report,
        "battery_full_charge_assist": battery_full_charge_report,
        "hardware_health": hardware_health,
    }


DIAGNOSE_SERVICE_DEFAULTS = {
    "deep": False,
    "hardware": False,
    "support_bundle": False,
    "control": False,
    "control_quality": False,
    "quality": False,
    "sample_seconds": 0,
}


def diagnose_service_args(args, **overrides):
    values = {
        "config": getattr(args, "config", None),
        "runtime_state": getattr(args, "runtime_state", None),
        "dashboard_auth": getattr(args, "dashboard_auth", None),
        **DIAGNOSE_SERVICE_DEFAULTS,
    }
    values.update(vars(args))
    values.update(overrides)
    return argparse.Namespace(**values)


def run_diagnosis(args):
    return diagnose_finalize_report(diagnose_collect(diagnose_service_args(args)))


def run_install_diagnosis(args):
    return run_diagnosis(args)


def run_deep_diagnosis(args):
    return run_diagnosis(diagnose_service_args(args, deep=True))


def run_hardware_diagnosis(args):
    return run_diagnosis(diagnose_service_args(args, hardware=True))


def run_control_diagnosis(args):
    return run_diagnosis(diagnose_service_args(args, control=True))


def run_control_quality_diagnosis(args):
    return run_diagnosis(diagnose_service_args(args, control_quality=True, quality=False))


def diagnose_hardware_health_text(hardware_health):
    """Render the grid-meter and device communication health blocks."""

    lines = []
    grid = hardware_health.get("grid_meter")
    if grid:
        lines.extend(render_grid_meter_health(grid))
        lines.append("")
    lines.extend(render_device_health(hardware_health.get("devices") or []))
    return "\n".join(lines) + "\n"


def diagnose_text(report):
    mode_labels = {
        "native": "native installation",
        "container": "container",
        "unknown": "unknown",
    }
    section_labels = {
        "environment": "Environment",
        "project": "Project structure",
        "runtime_paths": "Runtime paths",
        "config": "Config",
        "runtime_state": "Runtime state",
        "data": "Data/database",
        "dashboard": "Dashboard",
        "logs": "Logs",
        "docker": "Docker",
        "hardware": "Hardware",
        "control": "Control diagnostics",
        "control_quality": "Control quality",
    }
    order = ("environment", "project", "runtime_paths", "config", "runtime_state", "data", "dashboard", "logs", "docker", "hardware", "control", "control_quality")
    level_labels = {"ok": "OK", "warning": "WARN", "error": "ERROR"}

    lines = [
        "EMS Diagnose",
        "",
        f"Mode: {mode_labels.get(report['mode'], report['mode'])}",
        f"Deep checks: {'enabled' if report['options']['deep'] else 'disabled'}",
        f"Hardware checks: {'enabled' if report['options']['hardware'] else 'disabled'}",
        f"Control diagnostics: {'enabled' if report['options'].get('control') else 'disabled'}",
        f"Control quality: {'enabled' if report['options'].get('control_quality') else 'disabled'}",
        f"Support bundle: {'enabled' if report['options']['support_bundle'] else 'disabled'}",
    ]

    if report.get("control"):
        lines.append("")
        lines.append(diagnose_control_text(report["control"]).rstrip())
    if report.get("control_quality"):
        lines.append("")
        lines.append(diagnose_control_quality_text(report["control_quality"]).rstrip())
    if report.get("battery_full_charge_assist"):
        lines.append("")
        lines.append(
            diagnose_battery_full_charge_assist_text(report).rstrip()
        )

    for section in order:
        checks = [check for check in report["checks"] if check["section"] == section]
        if not checks:
            continue
        lines.append("")
        lines.append(section_labels.get(section, section.title()))
        for check in checks:
            lines.append(f"[{level_labels.get(check['level'], check['level'].upper())}] {check['message']}")

    if report.get("hardware_health"):
        lines.append("")
        lines.append(diagnose_hardware_health_text(report["hardware_health"]).rstrip())

    lines.append("")
    lines.append(f"Result: {report['status']}")
    return "\n".join(lines) + "\n"
