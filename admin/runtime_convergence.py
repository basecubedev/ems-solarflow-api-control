# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config -> runtime-state convergence for Admin maintenance (Tier 2).

When Admin applies a maintenance config, the whitelisted overlapping keys it
changed are mirrored into runtime-state through the same validated writers the
Dashboard uses, so the change goes live within one EMS control loop instead of
waiting for a restart. Convergence is one-directional (config/Admin -> runtime);
runtime is never merged back into config.

Best-effort: config.json is already the durable source of truth when this runs,
so any runtime write problem (a value above the runtime power ceiling, a device
not yet in runtime, a read-only file) is recorded as a per-key skip/warning and
never fails the apply. New or renamed devices are skipped on purpose: the EMS
seeds their values from config on its next start.
"""

import os
import re

from admin.config_runtime_overlap import (
    config_effective_value,
    resolve_runtime_state_path,
)
from dashboard.runtime_write import (
    DEVICE_FIELDS,
    SECTION_FIELDS,
    SYSTEM_FIELDS,
    RuntimeWriteError,
    apply_device_update,
    apply_section_update,
    apply_system_update,
    build_validation_context,
)
from ems.runtime_state import RuntimeState

_DEVICE_PATH = re.compile(r"^devices\[(\d+)\]\.([A-Za-z0-9_]+)$")
_SCALAR_PATH = re.compile(r"^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)$")

RUNTIME_ABSENT_REASON = "runtime_state_absent"


def _device_name(config, index):
    devices = config.get("devices") if isinstance(config.get("devices"), list) else []
    if index < 0 or index >= len(devices):
        return None
    device = devices[index]
    if not isinstance(device, dict):
        return None
    return str(device.get("name") or "").strip() or None


def _classify(path, config):
    device_match = _DEVICE_PATH.match(path)
    if device_match:
        key = device_match.group(2)
        if key not in DEVICE_FIELDS:
            return None
        name = _device_name(config, int(device_match.group(1)))
        if not name:
            return None
        return ("device", name, key)
    scalar_match = _SCALAR_PATH.match(path)
    if not scalar_match:
        return None
    head, key = scalar_match.group(1), scalar_match.group(2)
    if head == "system" and key in SYSTEM_FIELDS:
        return ("system", None, key)
    if head in SECTION_FIELDS and key in SECTION_FIELDS[head]:
        return ("section", head, key)
    return None


def _merged_value(config, kind, holder, key):
    if kind == "system":
        section = config.get("system") if isinstance(config.get("system"), dict) else {}
        return section.get(key)
    if kind == "section":
        section = config.get(holder) if isinstance(config.get(holder), dict) else {}
        return section.get(key)
    devices = config.get("devices") if isinstance(config.get("devices"), list) else []
    for device in devices:
        if isinstance(device, dict) and str(device.get("name") or "").strip() == holder:
            return device.get(key)
    return None


def _apply_one(runtime_state, validation_context, kind, holder, key, value):
    if kind == "system":
        apply_system_update(runtime_state, {key: value}, validation_context)
    elif kind == "section":
        apply_section_update(runtime_state, holder, {key: value}, validation_context)
    else:
        apply_device_update(runtime_state, holder, {key: value}, validation_context)


def _write_targets(context, config, targets):
    result = {"applied": [], "skipped": [], "warnings": []}
    if not targets:
        return result
    path = resolve_runtime_state_path(context, config)
    if not os.path.exists(path):
        for display_path, *_ in targets:
            result["skipped"].append({"path": display_path, "reason": RUNTIME_ABSENT_REASON})
        result["warnings"].append(
            "runtime-state file not present; the EMS will seed these values "
            "from config on its next start"
        )
        return result
    runtime_state = RuntimeState(path, {"devices": {}})
    runtime_state.load_if_changed(force=True)
    validation_context = build_validation_context(config, None)
    for display_path, kind, holder, key, value in targets:
        try:
            _apply_one(runtime_state, validation_context, kind, holder, key, value)
            result["applied"].append(display_path)
        except RuntimeWriteError as exc:
            result["skipped"].append({"path": display_path, "reason": str(exc)})
        except OSError as exc:
            result["skipped"].append(
                {"path": display_path, "reason": f"runtime_write_failed: {exc}"}
            )
    return result


def mirror_changed_keys_to_runtime(context, merged_config, changed_paths):
    """Mirror the changed overlapping whitelisted keys into runtime-state."""

    targets = []
    for path in changed_paths:
        classified = _classify(path, merged_config)
        if not classified:
            continue
        kind, holder, key = classified
        value = _merged_value(merged_config, kind, holder, key)
        if value is None:
            continue
        targets.append((path, kind, holder, key, value))
    return _write_targets(context, merged_config, targets)


def reset_targets_to_config(context, config, requests):
    """Write installed config values back into runtime-state for the given keys.

    ``requests`` is a list of ``{"scope","key",...}`` targets: ``system``/
    ``section`` (with ``section``) / ``device`` (with ``name``). Reset always
    *writes* the effective config value, never clears the override, because the
    EMS loads config once per process and a cleared key would fall back to a
    stale value.
    """

    targets = []
    for item in requests:
        if not isinstance(item, dict):
            continue
        scope = item.get("scope")
        key = item.get("key")
        if scope == "system":
            kind, holder, display = "system", None, "system." + str(key)
        elif scope == "section":
            holder = item.get("section")
            kind, display = "section", str(holder) + "." + str(key)
        elif scope == "device":
            holder = item.get("name")
            kind, display = "device", "devices." + str(holder) + "." + str(key)
        else:
            continue
        value = config_effective_value(config, kind, holder, key)
        if value is None:
            continue
        targets.append((display, kind, holder, key, value))
    return _write_targets(context, config, targets)
