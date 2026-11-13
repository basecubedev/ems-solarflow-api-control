# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config vs runtime-state overlap for Admin maintenance transparency.

A small whitelisted set of keys are writable both in static ``config.json``
(edited by the Admin console) and in ``runtime-state.json`` (edited by the
Dashboard control tab and ``emsctl``). The live EMS resolves these keys
runtime-first, so the Admin must surface the *effective* value and whether it is
a live override of the installed config value.

This module is the single, read-only source for which keys overlap and how to
decide provenance; where the runtime-state file lives it takes from
``ems.runtime_state.runtime_state_path_under``. It never writes: runtime writes
go through ``ems.runtime_commands`` (same whitelist), config writes through
the atomic config apply service.
"""

import json
import re

from ems.runtime_commands import (
    DEVICE_FIELDS,
    SECTION_FIELDS,
    SYSTEM_FIELDS,
    RuntimeWriteError,
    build_validation_context,
    validate_device_values,
)
from ems import config as cfg
from ems.config_catalog import device_common_defaults
from ems.read_only_devices import is_read_only_device_config
from ems.runtime_state import owned_runtime_device, runtime_state_path_under

_EMS_DEFAULTS = cfg.default_runtime_config()
_SYSTEM_DEFAULTS = {key: _EMS_DEFAULTS["system"].get(key) for key in SYSTEM_FIELDS}
_SECTION_DEFAULTS = {
    section: {key: _EMS_DEFAULTS.get(section, {}).get(key) for key in fields}
    for section, fields in SECTION_FIELDS.items()
}
_DEVICE_COMMON_DEFAULTS = device_common_defaults()


def _device_defaults(config, entry=None):
    """What the EMS applies to a whitelisted device key the config omits.

    A device without ``max_power`` gets the installation's
    ``system.max_device_power``, not a fixed figure, and the off-grid socket
    mode the EMS derives from the entry's legacy ``grid_off_mode``.
    """

    max_device_power = _config_get(config, ["system", "max_device_power"])
    if max_device_power is None:
        max_device_power = _EMS_DEFAULTS["system"]["max_device_power"]
    grid_off_mode = entry.get("grid_off_mode") if isinstance(entry, dict) else None
    return {
        "enabled": True,
        "max_power": max_device_power,
        "offgrid_socket_mode": cfg.offgrid_socket_mode_for(grid_off_mode),
        "pv_priority_factor": _DEVICE_COMMON_DEFAULTS["pv_priority_factor"],
        "ac_charge_enabled": _DEVICE_COMMON_DEFAULTS["ac_charge_enabled"],
    }


_DEVICE_LEAF_RE = re.compile(r"^devices\[\d+\]\.([A-Za-z0-9_]+)$")


def applies_live(path, config=None, live_devices=None):
    """Does changing this config path take effect without an EMS restart?

    Only the whitelisted overlap keys are mirrored into runtime-state when the
    Admin applies a config, and only those are re-read by the running EMS.
    Everything else — every write gate, ``dry_run`` and ``simulation_mode``
    included — stays inert until the container is recreated. A read-only
    device has no runtime-state entry at all, so nothing about it is live.
    ``live_devices``, the device paths a save makes live (live_device_paths),
    narrows a device key further, when given, to one of them.
    """

    text = str(path or "")
    if text.startswith("system."):
        return text[len("system.") :] in SYSTEM_FIELDS
    device = _DEVICE_LEAF_RE.match(text)
    if device is not None:
        entry = _device_at_path(config, text) if config is not None else None
        if config is not None and is_read_only_device_config(entry):
            return False
        if live_devices is not None and text not in live_devices:
            return False
        return device.group(1) in DEVICE_FIELDS
    section, _, key = text.partition(".")
    return bool(key) and key in SECTION_FIELDS.get(section, {})


_DEVICE_INDEX_RE = re.compile(r"^devices\[(\d+)\]\.")


def _device_name(entry):
    return str(entry.get("name") or "").strip() if isinstance(entry, dict) else ""


def runtime_device_entries(context, config):
    """The ``devices`` map of the runtime-state file of this install now; empty when it cannot be read.

    EMS seeds one entry for each device it builds to control when it starts,
    over HTTP or MQTT, and prunes the rest then: a device switched off, a
    read-only one, and one read over MQTT without control, which it builds from
    ``config.json`` alone. These are the entries the mirror can write.
    """

    try:
        devices = read_runtime_state(resolve_runtime_state_path(context, config)).get("devices")
    except (OSError, TypeError, ValueError):
        return {}
    return devices if isinstance(devices, dict) else {}


def device_group(entry):
    """The device a config entry stands for: ``("serial", serial)``, or ``("name", name)`` for an entry naming no serial.

    The serial is read as the EMS stamps it: ``sn``, else ``serial_number``,
    else ``mqtt.device_id``, as text. None for a read-only entry, a nameless
    one without a serial, and one whose ``sn`` and ``serial_number`` name
    different serials: the EMS stamps an HTTP device with the one and an MQTT
    device with the other, so neither is safe to follow.
    """

    if not isinstance(entry, dict) or is_read_only_device_config(entry):
        return None
    serials = [_text(entry.get(key)) for key in ("sn", "serial_number")]
    serials = [serial for serial in serials if serial]
    if len(set(serials)) > 1:
        return None
    mqtt = entry.get("mqtt") if isinstance(entry.get("mqtt"), dict) else {}
    identity = (serials or [_text(mqtt.get("device_id"))])[0]
    if identity:
        return ("serial", identity)
    name = _device_name(entry)
    return ("name", name) if name else None


def _text(value):
    return "" if value is None or isinstance(value, (bool, dict, list)) else str(value).strip()


_ABSENT = "absent"
_AMBIGUOUS = "ambiguous"


def _device_state(config, group):
    """``(voice, built)`` for the device ``group`` names in ``config``; _ABSENT or _AMBIGUOUS when it cannot be said.

    ``voice`` is the entry that speaks for the device: the one the EMS builds
    to control (switched on, over HTTP or MQTT), else the first one it would
    build if it were switched on, else the first; ``built`` is whether the EMS
    builds one. Only the config decides, so every save, the preview and the
    provenance view pick the same entry. _ABSENT when ``config`` holds no entry
    for the device, _AMBIGUOUS when it holds several the EMS builds.
    """

    carriers = [entry for entry in _devices(config) if device_group(entry) == group]
    if not carriers:
        return _ABSENT
    built = _built(carriers)
    if len(built) > 1:
        return _AMBIGUOUS
    if built:
        return built[0], True
    controllable = [entry for entry in carriers if _built([dict(entry, enabled=True)])]
    return (controllable or carriers)[0], False


def _built(entries):
    return cfg.http_control_device_configs(entries) + cfg.mqtt_control_device_configs(entries)


def device_state(config, group):
    """``(voice, built)`` for the device ``group`` names in ``config`` (_device_state); None when that cannot be said."""

    state = _device_state(config, group)
    return state if isinstance(state, tuple) else None


def values_differ(before, after):
    """Whether a saved value changed: 1.0 and 1 are one JSON number, a switch and a number are not alike."""

    return before != after or isinstance(before, bool) is not isinstance(after, bool)


def device_runtime_plan(previous, merged):
    """``(path, name, identity, key, value)`` for each runtime value a save changes.

    The runtime entry of a device holds the effective values (the configured
    one, else the EMS default) of the entry that speaks for it, except where
    the Dashboard set a value the config has not changed since. A save keeps
    that so: per device (device_group) it writes each effective value that
    differs from the one the speaking entry of ``previous`` had, a removed key
    and a changed system default included, and ``enabled`` when whether the
    EMS builds the device changed. So a device is compared with itself, never
    with whatever stood at its list position; switching it off or turning it
    telemetry-only takes it out of control at once; a telemetry entry never
    switches control on; and switching a device back on leaves its live
    overrides alone. A switch-off is written before the values that come with
    it and a switch-on after them. A device ``previous`` did not hold brings
    its values to a runtime entry its serial still owns, since the EMS would
    keep that entry's values at its next start, but never switches it on; one
    that only gained its serial in this save, at the same address, is the
    entry of its name. A
    device ``previous`` held several built entries for, and an entry that
    changed or lost its serial in this save, are left alone: neither is tied
    to a running device by its name. ``path`` names the key on the speaking
    entry, ``name`` that entry's name and ``identity`` its serial.
    """

    devices = merged.get("devices") if isinstance(merged, dict) else None
    devices = devices if isinstance(devices, list) else []
    seen = set()
    for entry in devices:
        group = device_group(entry)
        if group is None or group in seen:
            continue
        seen.add(group)
        state = _device_state(merged, group)
        if not isinstance(state, tuple):
            continue
        voice, built = state
        name = _device_name(voice)
        before = _device_state(previous, group)
        if before == _ABSENT and group[0] == "serial":
            named = _device_state(previous, ("name", name))
            if isinstance(named, tuple) and _address(named[0]) == _address(voice):
                before = named
        if not name or before == _AMBIGUOUS:
            continue
        if before == _ABSENT and any(_device_name(item) == name for item in _devices(previous)):
            continue
        prior, was_built = before if isinstance(before, tuple) else (None, None)
        now = _effective_values(voice, merged)
        then = _effective_values(prior, previous) if prior is not None else {}
        changes = [(key, value) for key, value in now.items() if key not in then or values_differ(then[key], value)]
        if was_built is not built and not (built and was_built is None):
            changes = changes + [("enabled", True)] if built else [("enabled", False)] + changes
        index = next(position for position, item in enumerate(devices) if item is voice)
        identity = group[1] if group[0] == "serial" else None
        for key, value in changes:
            yield f"devices[{index}].{key}", name, identity, key, value


def _address(entry):
    """Where the EMS reaches a device: its IP, or its MQTT broker and route."""

    mqtt = entry.get("mqtt") if isinstance(entry.get("mqtt"), dict) else {}
    return _text(entry.get("ip")), _text(mqtt.get("broker_ref")), _text(mqtt.get("device_id"))


def _effective_values(entry, config):
    """The whitelisted values the EMS seeds a device's runtime entry with from ``entry`` (ems.runtime_state.build_runtime_defaults).

    The device clients' coercions and the seed's are applied in turn, so a
    ``max_power`` of 0 is the installation's device ceiling and one of 799.5
    is 799, as at the EMS's start.
    """

    ceiling = _device_defaults(config)["max_power"]
    return {
        "max_power": cfg.safe_int(cfg.device_power_ceiling(entry.get("max_power", ceiling), ceiling), ceiling, minimum=0),
        "offgrid_socket_mode": cfg.offgrid_socket_mode_for(entry.get("grid_off_mode")),
        "pv_priority_factor": cfg.safe_float(entry.get("pv_priority_factor", 1.0) or 1.0, 1.0, minimum=0.01),
        "ac_charge_enabled": bool(entry.get("ac_charge_enabled", True)),
    }


def live_device_paths(previous, merged, runtime_devices):
    """The device paths a save makes live: those device_runtime_plan changes, validated as the mirror validates them, for a device that owns an entry in ``runtime_devices``."""

    if not isinstance(runtime_devices, dict):
        return frozenset()
    context = build_validation_context(merged, None)
    live = set()
    for path, name, identity, key, value in device_runtime_plan(previous, merged):
        if owned_runtime_device(runtime_devices, name, identity) is None:
            continue
        try:
            validate_device_values(name, {key: value}, context)
        except RuntimeWriteError:
            continue
        live.add(path)
    return frozenset(live)


def runtime_entry_for(entry, config, runtime_devices):
    """The name of the runtime-state entry whose values the config device ``entry`` shows; None when it shows none.

    That is the entry of the device it stands for, found by the EMS's own rule
    (owned_runtime_device), when ``entry`` speaks for that device
    (device_state): so a device renamed, or two devices that swapped names,
    since the EMS started still show what the running EMS holds for them.
    """

    group = device_group(entry)
    state = device_state(config, group) if group is not None else None
    if state is None or not isinstance(runtime_devices, dict) or state[0] is not entry:
        return None
    return owned_runtime_device(runtime_devices, _device_name(entry), group[1] if group[0] == "serial" else None)


def _devices(config):
    devices = config.get("devices") if isinstance(config, dict) else None
    return [device for device in devices if isinstance(device, dict)] if isinstance(devices, list) else []


def _device_at_path(config, path):
    match = _DEVICE_INDEX_RE.match(str(path or ""))
    devices = config.get("devices") if isinstance(config, dict) else None
    if match is None or not isinstance(devices, list):
        return None
    index = int(match.group(1))
    return devices[index] if index < len(devices) else None


def resolve_runtime_state_path(context, config):
    """Resolve the runtime-state file the live EMS reads for this install.

    Mirrors the EMS resolver: ``system.runtime_state_path`` joined to the real
    install root. Deliberately avoids ``ems.paths`` BASE_DIR, which resolves to
    the Admin container root rather than the mounted install.
    """

    return runtime_state_path_under(context.install_root, config)


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


_EMS_SECTION_READS = {
    **{
        ("battery_care", key): (
            lambda value, key=key: cfg.battery_care_points(value, cfg.BATTERY_CARE_DEFAULTS[key])
        )
        for key in cfg.BATTERY_CARE_POINT_KEYS
    },
    ("battery_care", "min_discharge_w"): (
        lambda value: cfg.battery_care_watts(value, cfg.BATTERY_CARE_DEFAULTS["min_discharge_w"])
    ),
}


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
    ``ac_charge_control.*``) plus a ``devices`` sub-map keyed by the config's device name,
    each compared with the runtime entry the device owns (runtime_entry_for). Provenance is a
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
            config_value = config_section.get(key)
            if config_value is not None and (section, key) in _EMS_SECTION_READS:
                config_value = _EMS_SECTION_READS[(section, key)](config_value)
            result[section + "." + key] = _entry(
                config_value, runtime_section, key, value_type,
                _SECTION_DEFAULTS.get(section, {}).get(key),
            )

    runtime_devices = runtime_data.get("devices")
    runtime_devices = runtime_devices if isinstance(runtime_devices, dict) else {}
    devices = {}
    config_devices = config.get("devices") if isinstance(config.get("devices"), list) else []
    for device in config_devices:
        if not isinstance(device, dict) or is_read_only_device_config(device):
            continue
        name = str(device.get("name") or "").strip()
        if not name:
            continue
        entry_name = runtime_entry_for(device, config, runtime_devices)
        runtime_device = runtime_devices.get(entry_name)
        built = device_state(config, device_group(device))[1] if entry_name is not None else None
        entries = {}
        effective = _effective_values(device, config)
        for key, (value_type, _rule) in DEVICE_FIELDS.items():
            configured = (built if built is False else device.get(key)) if key == "enabled" else effective[key]
            entries[key] = _entry(
                configured, runtime_device, key, value_type,
                _device_defaults(config, device).get(key),
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
        if isinstance(device, dict) and key != "enabled":
            raw = _effective_values(device, config)[key]
        default = _device_defaults(config, device).get(key)
        value_type = DEVICE_FIELDS[key][0]
    else:
        return None
    if kind == "section" and raw is not None and (holder, key) in _EMS_SECTION_READS:
        raw = _EMS_SECTION_READS[(holder, key)](raw)
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
