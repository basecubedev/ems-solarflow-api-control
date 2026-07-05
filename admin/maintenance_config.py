# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance draft, preview and apply preparation for an existing EMS config.

Loads the real resolved ``config/config.json``, exposes a normalized draft the
Admin UI can edit, and prepares a validated merged config with a bounded diff.
This module never writes: applying the prepared payload is owned by the shared
atomic config apply service.
"""

import copy
import hashlib
import json

from admin.config_preview import _GRID_TYPE_CHOICES, _valid_host
from admin.install_context import detect_install_context
from admin.setup_config import _coerce, _set_path
from ems.config_catalog import (
    GRID_METER_VARIANTS,
    get_config_feature_field_index,
    get_config_feature_sections,
)

# Draft device fields the UI may edit. name/ip/sn are identity/host fields with
# dedicated handling; the rest are catalog-backed per-device values.
_DEVICE_NUMERIC_FIELDS = (
    "max_power",
    "min_soc",
    "max_soc",
    "pv_kwp",
    "battery_kwh",
    "pv_priority_factor",
)
_GRID_HOST_TYPES = ("shelly", "shelly_3em_gen1", "ecotracker", "tasmota_http")
_HOST_GRID_METER_TYPES = frozenset(_GRID_HOST_TYPES)

_SERIAL_PLACEHOLDER = "YOUR_SN"
_MAX_STRING_LEN = 160


def _issue(code, message):
    return {"code": code, "message": message}


def _is_maintenance_field(field):
    """True for catalog feature fields the maintenance editor may write.

    Device and grid-meter paths are owned by the dedicated hardware editors;
    deprecated, read-only and secret fields stay out so the editor can never
    surface a secret value or write outside the catalog.
    """

    if field.get("scope") not in ("maintenance", "both"):
        return False
    if field.get("level") == "deprecated":
        return False
    if field.get("editable") is False:
        return False
    if field.get("risk") == "secret" or field.get("type") == "password":
        return False
    return True


def _maintenance_field_index():
    index = {}
    for path, field in get_config_feature_field_index().items():
        if "[]" in path or path.startswith("devices") or path.startswith("grid_meter"):
            continue
        if not _is_maintenance_field(field):
            continue
        index[path] = field
    return index


def _get_path(config, path):
    cursor = config
    for part in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


def _bounded(value):
    """Bound a scalar for safe UI rendering; lists/dicts collapse to a summary."""

    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING_LEN else value[:_MAX_STRING_LEN] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return "{…}"
    text = str(value)
    return text if len(text) <= _MAX_STRING_LEN else text[:_MAX_STRING_LEN] + "…"


# --- load / draft --------------------------------------------------------


def load_maintenance_config(base_dir=None):
    """Load the resolved EMS config and return a normalized maintenance view.

    Missing or invalid config degrades to a clear status (never an exception),
    so the Admin route can return a non-500 result the UI can render.
    """

    context = detect_install_context(base_dir=base_dir)
    config_path = str(context.config_path)
    if not context.config_exists:
        return {
            "status": "missing",
            "config_path": config_path,
            "source": context.config_source,
            "message": "No config.json was found at the resolved install path.",
        }
    try:
        raw = context.config_path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return {
            "status": "invalid",
            "config_path": config_path,
            "source": context.config_source,
            "message": f"The config file could not be read: {exc}.",
        }
    try:
        config = json.loads(text)
    except ValueError:
        return {
            "status": "invalid",
            "config_path": config_path,
            "source": context.config_source,
            "message": "The config file is not valid JSON.",
        }
    if not isinstance(config, dict):
        return {
            "status": "invalid",
            "config_path": config_path,
            "source": context.config_source,
            "message": "The config file is not a JSON object.",
        }

    draft = build_maintenance_draft(config)
    return {
        "status": "ok",
        "config_path": config_path,
        "source": context.config_source,
        "revision": hashlib.sha256(raw).hexdigest(),
        "summary": _summary(config, draft),
        "draft": draft,
        "catalog": _catalog(),
        "warnings": [],
    }


def build_maintenance_draft(config):
    """Build an editable in-memory draft from the current config values."""

    return {
        "devices": [_device_draft(device) for device in _config_devices(config)],
        "grid_meter": _grid_meter_draft(config.get("grid_meter")),
        "features": _feature_draft(config),
    }


def _config_devices(config):
    devices = config.get("devices")
    if not isinstance(devices, list):
        return []
    return [device for device in devices if isinstance(device, dict)]


def _device_draft(device):
    name = str(device.get("name") or "").strip()
    draft = {
        "original_name": name,
        "name": name,
        "ip": str(device.get("ip") or "").strip(),
        "sn": str(device.get("sn") or "").strip(),
        "enabled": bool(device.get("enabled", True)),
        "has_enabled_key": "enabled" in device,
        "connection_type": str(device.get("connection_type") or "").strip() or "local API",
    }
    for key in _DEVICE_NUMERIC_FIELDS:
        if key in device:
            draft[key] = device[key]
    return draft


def _grid_meter_draft(grid_meter):
    if not isinstance(grid_meter, dict):
        return {"type": "", "ip": "", "present": False}
    draft = {
        "present": True,
        "type": str(grid_meter.get("type") or "").strip(),
        "ip": str(grid_meter.get("ip") or "").strip(),
    }
    if grid_meter.get("port") is not None:
        draft["port"] = grid_meter.get("port")
    if grid_meter.get("url"):
        draft["url"] = str(grid_meter.get("url")).strip()
    if grid_meter.get("power_path"):
        draft["power_path"] = str(grid_meter.get("power_path")).strip()
    channels = grid_meter.get("channels")
    if isinstance(channels, list):
        draft["channels"] = [str(item) for item in channels]
    return draft


def _feature_draft(config):
    draft = {}
    for path in _maintenance_field_index():
        value = _get_path(config, path)
        if value is not None:
            draft[path] = value
    return draft


def _summary(config, draft):
    devices = draft["devices"]
    grid_meter = config.get("grid_meter") if isinstance(config.get("grid_meter"), dict) else {}
    dashboard = config.get("dashboard") if isinstance(config.get("dashboard"), dict) else {}
    influx = config.get("influxdb") if isinstance(config.get("influxdb"), dict) else {}
    return {
        "device_count": len(devices),
        "enabled_device_count": sum(1 for device in devices if device.get("enabled", True)),
        "grid_meter_type": str(grid_meter.get("type") or "").strip() or None,
        "dashboard_enabled": bool(dashboard.get("enabled", False)),
        "influx_enabled": bool(influx.get("enabled", False)),
        "influx_mode": str(influx.get("mode") or "").strip() or None,
    }


def _catalog():
    """Serializable metadata the UI needs to render editors and options."""

    index = _maintenance_field_index()
    sections = []
    for section in get_config_feature_sections(mode="maintenance"):
        if section.get("setup_group") == "hardware" or section["id"] == "ha":
            continue
        fields = [field for field in section["fields"] if field["path"] in index]
        if not fields:
            continue
        section = dict(section)
        section["fields"] = fields
        sections.append(section)

    grid_meter_types = [
        {"id": key, "label": variant["label"], "description": variant["description"]}
        for key, variant in GRID_METER_VARIANTS.items()
        if variant.get("level") != "deprecated"
    ]
    return {"feature_sections": sections, "grid_meter_types": grid_meter_types}


# --- preview / merge -----------------------------------------------------


def preview_maintenance_config(draft, base_dir=None):
    """Merge a draft with the current config and return validation + diff.

    Never writes: the resolved config is only ever read, and the merged result
    stays in memory. Missing/invalid config degrades to a clear status.
    """

    context = detect_install_context(base_dir=base_dir)
    config_path = str(context.config_path)
    if not context.config_exists:
        return {
            "status": "missing",
            "config_path": config_path,
            "message": "No config.json was found at the resolved install path.",
        }
    try:
        raw = context.config_path.read_bytes()
        current = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return {
            "status": "invalid",
            "config_path": config_path,
            "message": "The current config could not be read as JSON.",
        }
    if not isinstance(current, dict):
        return {
            "status": "invalid",
            "config_path": config_path,
            "message": "The current config is not a JSON object.",
        }

    draft = draft if isinstance(draft, dict) else {}
    merged = _merge_draft(current, draft)
    validation = _validate(merged)
    diff = summarize_config_changes(current, merged)
    return {
        "status": "ok",
        "config_path": config_path,
        "revision": hashlib.sha256(raw).hexdigest(),
        "changed": diff["changed"],
        "diff": diff,
        "validation": validation,
        "preview": merged,
    }


def prepare_maintenance_config_apply(draft, expected_revision, base_dir=None):
    """Validate a reviewed draft and serialize it without writing anything."""

    result = preview_maintenance_config(draft, base_dir=base_dir)
    if result.get("status") != "ok":
        return result
    if not expected_revision or expected_revision != result["revision"]:
        return {
            "status": "conflict",
            "message": (
                "config/config.json changed after this workflow was opened. "
                "Reload the current config and review the draft again."
            ),
            "revision": result["revision"],
        }
    if not result["validation"]["ok"]:
        result["status"] = "invalid"
        result["message"] = "The draft is not valid and was not applied."
        return result
    result["payload"] = (
        json.dumps(result["preview"], indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return result


def _merge_draft(current, draft):
    merged = copy.deepcopy(current)
    _merge_devices(merged, draft.get("devices"))
    _merge_grid_meter(merged, draft.get("grid_meter"))
    _merge_features(merged, draft.get("features"))
    return merged


def _merge_devices(merged, devices):
    if not isinstance(devices, list):
        return
    originals = _config_devices(merged)
    by_name = {}
    for device in originals:
        key = str(device.get("name") or "")
        if key and key not in by_name:
            by_name[key] = device
    prototype = copy.deepcopy(originals[0]) if originals else {}

    result = []
    for item in devices:
        if not isinstance(item, dict) or item.get("removed") is True:
            continue
        original = by_name.get(str(item.get("original_name") or ""))
        device = copy.deepcopy(original) if original is not None else copy.deepcopy(prototype)
        _apply_device_fields(device, item)
        result.append(device)
    merged["devices"] = result


def _apply_device_fields(device, item):
    name = str(item.get("name") or "").strip()
    if name:
        device["name"] = name
    if "ip" in item:
        device["ip"] = str(item.get("ip") or "").strip()
    if "sn" in item:
        device["sn"] = str(item.get("sn") or "").strip()
    for key in _DEVICE_NUMERIC_FIELDS:
        if key in item:
            device[key] = _coerce_number(item[key])
    # Keep unknown device keys (comments, smart_mode) untouched; only surface an
    # explicit enabled flag so a disabled draft device reads as a real change.
    enabled = bool(item.get("enabled", True))
    if "enabled" in device or item.get("has_enabled_key") or not enabled:
        device["enabled"] = enabled


def _coerce_number(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return value
    try:
        number = float(text)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _merge_grid_meter(merged, grid_meter):
    if not isinstance(grid_meter, dict):
        return
    if grid_meter.get("present") is False:
        merged.pop("grid_meter", None)
        return
    target = merged.get("grid_meter")
    if not isinstance(target, dict):
        target = {}
        merged["grid_meter"] = target
    if grid_meter.get("type"):
        target["type"] = str(grid_meter["type"]).strip().lower()
    if "ip" in grid_meter:
        target["ip"] = str(grid_meter.get("ip") or "").strip()
    if grid_meter.get("port") is not None:
        target["port"] = _coerce_number(grid_meter["port"])
    for key in ("url", "power_path"):
        if grid_meter.get(key):
            target[key] = str(grid_meter[key]).strip()
    channels = grid_meter.get("channels")
    if isinstance(channels, list):
        target["channels"] = [str(item).strip() for item in channels if str(item).strip()]


def _merge_features(merged, features):
    if not isinstance(features, dict):
        return
    index = _maintenance_field_index()
    for path, raw_value in features.items():
        field = index.get(path)
        if field is None:
            continue
        _set_path(merged, path, _coerce(field, raw_value))


# --- validation ----------------------------------------------------------


def _validate(config):
    validation = {"errors": [], "warnings": [], "info": []}
    devices = config.get("devices")
    if not isinstance(devices, list) or not devices:
        validation["errors"].append(_issue("no_devices", "At least one inverter is required."))
    else:
        names = []
        for index, device in enumerate(devices, 1):
            if not isinstance(device, dict):
                validation["errors"].append(
                    _issue("device_invalid", f"Inverter {index} is not a valid entry.")
                )
                continue
            name = str(device.get("name") or "").strip()
            names.append(name)
            label = name or f"inverter {index}"
            if not name:
                validation["errors"].append(
                    _issue("device_name_empty", f"Inverter {index} needs a config name.")
                )
            if not _valid_host(device.get("ip")):
                validation["errors"].append(
                    _issue("device_host_invalid", f"{label} has an invalid IP address or hostname.")
                )
            serial = str(device.get("sn") or "").strip()
            if not serial or serial == _SERIAL_PLACEHOLDER:
                validation["errors"].append(
                    _issue("device_serial_missing", f"{label} requires a serial number.")
                )
        duplicates = sorted({name for name in names if name and names.count(name) > 1})
        if duplicates:
            validation["errors"].append(
                _issue(
                    "device_name_duplicate",
                    f"Config names must be unique: {', '.join(duplicates)}.",
                )
            )

    grid_meter = config.get("grid_meter")
    if isinstance(grid_meter, dict):
        meter_type = str(grid_meter.get("type") or "").strip().lower()
        if meter_type and meter_type not in _GRID_TYPE_CHOICES:
            validation["errors"].append(
                _issue("grid_meter_type_invalid", f"Unknown grid meter type: {meter_type}.")
            )
        if meter_type in _HOST_GRID_METER_TYPES and not _valid_host(grid_meter.get("ip")):
            if not (meter_type == "tasmota_http" and grid_meter.get("url")):
                validation["errors"].append(
                    _issue("grid_meter_host_invalid", "The grid meter has an invalid IP or hostname.")
                )
    else:
        validation["warnings"].append(_issue("grid_meter_missing", "No grid meter is configured."))

    try:
        json.dumps(config, allow_nan=False)
    except (TypeError, ValueError):
        validation["errors"].append(
            _issue("config_not_serializable", "The resulting config is not valid JSON data.")
        )

    validation["ok"] = not validation["errors"]
    return validation


# --- diff ----------------------------------------------------------------


def summarize_config_changes(before, after):
    """Return a compact, bounded diff of leaf-value changes between two configs.

    Dict keys flatten with dots and list indices with ``[i]`` (so per-device
    fields read as ``devices[0].max_power``). Scalar lists compare as a single
    leaf; comment keys are skipped. Values are bounded for safe UI rendering.
    """

    before_leaves = {}
    after_leaves = {}
    _flatten(before, "", before_leaves)
    _flatten(after, "", after_leaves)

    changes, added, removed = [], [], []
    for path in sorted(set(before_leaves) | set(after_leaves)):
        in_before = path in before_leaves
        in_after = path in after_leaves
        if in_before and in_after:
            if before_leaves[path] != after_leaves[path]:
                changes.append(
                    {
                        "path": path,
                        "before": _bounded(before_leaves[path]),
                        "after": _bounded(after_leaves[path]),
                    }
                )
        elif in_after:
            added.append({"path": path, "after": _bounded(after_leaves[path])})
        else:
            removed.append({"path": path, "before": _bounded(before_leaves[path])})

    return {
        "changed": bool(changes or added or removed),
        "changes": changes,
        "added": added,
        "removed": removed,
    }


def _flatten(value, prefix, out):
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.startswith("_"):
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            _flatten(child, path, out)
        return
    if isinstance(value, list) and any(isinstance(item, dict) for item in value):
        for index, child in enumerate(value):
            _flatten(child, f"{prefix}[{index}]", out)
        return
    out[prefix] = value


__all__ = [
    "load_maintenance_config",
    "build_maintenance_draft",
    "preview_maintenance_config",
    "prepare_maintenance_config_apply",
    "summarize_config_changes",
]
