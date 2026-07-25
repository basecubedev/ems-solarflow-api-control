# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config vs runtime-state overlap for Admin maintenance transparency.

A small whitelisted set of keys are writable both in static ``config.json``
(edited by the Admin console) and in ``runtime-state.json`` (edited by the
Dashboard control tab and ``emsctl``). The live EMS resolves these keys
runtime-first, so the Admin must surface the *effective* value and whether it is
a live override of the installed config value.

This module is the single, read-only source for which keys overlap, how to
resolve the runtime-state file the running EMS actually reads, and how to decide
provenance. It never writes: runtime writes go through
``dashboard.runtime_write`` (same whitelist), config writes through the atomic
config apply service.
"""

import json
import os

from dashboard.runtime_write import DEVICE_FIELDS, SECTION_FIELDS, SYSTEM_FIELDS
from ems import config as cfg

DEFAULT_RUNTIME_STATE_PATH = "runtime-state.json"

_SYSTEM_DEFAULTS = {
    "enabled": True,
    "max_total_power": 800,
    "loop_interval": 5,
    "min_output_limit": 35,
}
_SECTION_DEFAULTS = {
    "ha": {"enabled": False, "control_enabled": False},
    "winter": {"enabled": False},
}
_DEVICE_DEFAULTS = {
    "enabled": True,
    "max_power": 800,
    "offgrid_socket_mode": "off",
    "pv_priority_factor": 1.0,
}


def resolve_runtime_state_path(context, config):
    """Resolve the runtime-state file the live EMS reads for this install.

    Mirrors the EMS resolver: ``system.runtime_state_path`` joined to the real
    install root. Deliberately avoids ``ems.paths`` BASE_DIR, which resolves to
    the Admin container root rather than the mounted install.
    """

    system = config.get("system") if isinstance(config.get("system"), dict) else {}
    relative = system.get("runtime_state_path") or DEFAULT_RUNTIME_STATE_PATH
    if os.path.isabs(relative):
        return relative
    return os.path.join(str(context.install_root), relative)


def read_runtime_state(path):
    """Best-effort read of the raw runtime-state file; ``{}`` on any problem."""

    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce(value, value_type):
    if value_type == "bool":
        return cfg.safe_bool(value, False)
    if value_type == "int":
        return cfg.safe_int(value, 0)
    if value_type == "float":
        return cfg.safe_float(value, 0.0)
    return "" if value is None else str(value).strip().lower()


def _entry(config_value, runtime_section, key, value_type, default):
    config_effective = config_value if config_value is not None else default
    coerced_config = _coerce(config_effective, value_type)
    if not isinstance(runtime_section, dict) or key not in runtime_section:
        return {
            "config_value": coerced_config,
            "effective_value": coerced_config,
            "source": "config",
        }
    coerced_runtime = _coerce(runtime_section[key], value_type)
    overridden = coerced_runtime != coerced_config
    return {
        "config_value": coerced_config,
        "effective_value": coerced_runtime,
        "source": "dashboard_override" if overridden else "config",
    }


def compute_overlap_provenance(config, runtime_data):
    """Effective value + provenance for every overlapping whitelisted key.

    Returns a display-only map keyed by feature path (``system.*``, ``winter.*``,
    ``ha.*``) plus a ``devices`` sub-map keyed by device name. Provenance is a
    value comparison, never key presence: the runtime file is seeded with every
    overlapping key at creation, so presence alone would always read as an
    override. This map must never be fed into the editable maintenance draft.
    """

    if not isinstance(config, dict):
        config = {}
    if not isinstance(runtime_data, dict):
        runtime_data = {}

    result = {}

    runtime_system = runtime_data.get("system")
    config_system = config.get("system") if isinstance(config.get("system"), dict) else {}
    for key, (value_type, _rule) in SYSTEM_FIELDS.items():
        result["system." + key] = _entry(
            config_system.get(key), runtime_system, key, value_type,
            _SYSTEM_DEFAULTS.get(key),
        )

    for section, fields in SECTION_FIELDS.items():
        runtime_section = runtime_data.get(section)
        config_section = config.get(section) if isinstance(config.get(section), dict) else {}
        for key, (value_type, _rule) in fields.items():
            result[section + "." + key] = _entry(
                config_section.get(key), runtime_section, key, value_type,
                _SECTION_DEFAULTS.get(section, {}).get(key),
            )

    runtime_devices = runtime_data.get("devices")
    runtime_devices = runtime_devices if isinstance(runtime_devices, dict) else {}
    devices = {}
    config_devices = config.get("devices") if isinstance(config.get("devices"), list) else []
    for device in config_devices:
        if not isinstance(device, dict):
            continue
        name = str(device.get("name") or "").strip()
        if not name:
            continue
        runtime_device = runtime_devices.get(name)
        entries = {}
        for key, (value_type, _rule) in DEVICE_FIELDS.items():
            entries[key] = _entry(
                device.get(key), runtime_device, key, value_type,
                _DEVICE_DEFAULTS.get(key),
            )
        devices[name] = entries
    if devices:
        result["devices"] = devices

    return result


def _config_get(config, parts):
    cursor = config
    for part in parts:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


def _config_device(config, name):
    devices = config.get("devices") if isinstance(config.get("devices"), list) else []
    for device in devices:
        if isinstance(device, dict) and str(device.get("name") or "").strip() == name:
            return device
    return None


def config_effective_value(config, kind, holder, key):
    """The value the EMS uses for an overlapping key with no runtime override.

    Returns a coerced scalar (the installed config value, or the EMS default when
    the config is silent), or ``None`` when the key is not a known overlapping
    key. Reset-to-config writes this value so a cleared override cannot fall back
    to a stale in-memory config value.
    """

    if kind == "system" and key in SYSTEM_FIELDS:
        raw = _config_get(config, ["system", key])
        default = _SYSTEM_DEFAULTS.get(key)
        value_type = SYSTEM_FIELDS[key][0]
    elif kind == "section" and holder in SECTION_FIELDS and key in SECTION_FIELDS[holder]:
        raw = _config_get(config, [holder, key])
        default = _SECTION_DEFAULTS.get(holder, {}).get(key)
        value_type = SECTION_FIELDS[holder][key][0]
    elif kind == "device" and key in DEVICE_FIELDS:
        device = _config_device(config, holder)
        raw = device.get(key) if isinstance(device, dict) else None
        default = _DEVICE_DEFAULTS.get(key)
        value_type = DEVICE_FIELDS[key][0]
    else:
        return None
    effective = raw if raw is not None else default
    return _coerce(effective, value_type)


def overlap_provenance_for_context(context, config):
    """Convenience wrapper: resolve + read the runtime file, compute provenance.

    Best-effort; any resolution or read failure degrades to an all-``config``
    map so the maintenance load never breaks on a missing or unreadable runtime
    file.
    """

    try:
        path = resolve_runtime_state_path(context, config)
        runtime_data = read_runtime_state(path)
        return compute_overlap_provenance(config, runtime_data)
    except Exception:
        return compute_overlap_provenance(config, {})
