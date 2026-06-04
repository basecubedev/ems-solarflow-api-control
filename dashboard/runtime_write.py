import math
import copy


OFFGRID_SOCKET_MODES = ("off", "eco", "standard")
GENERIC_MAX_POWER_W = 5000

SYSTEM_FIELDS = {
    "enabled": ("bool", None),
    "max_total_power": ("int", (0, GENERIC_MAX_POWER_W)),
    "loop_interval": ("int", (1, 3600)),
    "min_output_limit": ("int", (0, GENERIC_MAX_POWER_W)),
}

DEVICE_FIELDS = {
    "enabled": ("bool", None),
    "max_power": ("int", (0, GENERIC_MAX_POWER_W)),
    "offgrid_socket_mode": ("enum", OFFGRID_SOCKET_MODES),
    "pv_priority_factor": ("float", (0.01, 100.0)),
}

SECTION_FIELDS = {
    "ha": {
        "enabled": ("bool", None),
        "control_enabled": ("bool", None),
    },
    "winter": {
        "enabled": ("bool", None),
    },
}


class RuntimeWriteError(ValueError):
    pass


def runtime_payload(runtime_state):
    if runtime_state is None:
        return {}
    snapshot = getattr(runtime_state, "snapshot", None)
    if callable(snapshot):
        return snapshot()
    data = getattr(runtime_state, "data", None)
    return copy.deepcopy(data) if isinstance(data, dict) else {}


def apply_system_update(runtime_state, payload, validation_context=None):
    values = _validate_payload(
        payload,
        _system_fields(validation_context),
    )
    section = _update_section(runtime_state, "system", values)
    return {"system": section}


def apply_section_update(runtime_state, section_name, payload, validation_context=None):
    if section_name not in SECTION_FIELDS:
        raise RuntimeWriteError(f"unsupported runtime section {section_name}")
    values = _validate_payload(payload, SECTION_FIELDS[section_name])
    section = _update_section(runtime_state, section_name, values)
    return {section_name: section}


def apply_device_update(runtime_state, device_name, payload, validation_context=None):
    values = _validate_payload(
        payload,
        _device_fields(device_name, validation_context),
    )
    try:
        device = _update_device(runtime_state, device_name, values)
    except KeyError as exc:
        raise RuntimeWriteError(str(exc).strip("'")) from exc
    except ValueError as exc:
        raise RuntimeWriteError(str(exc)) from exc
    return {"device": device_name, "state": device}


def build_validation_context(config=None, runtime_state=None):
    config = config if isinstance(config, dict) else {}
    system = config.get("system", {}) if isinstance(config.get("system"), dict) else {}
    devices = config.get("devices", []) if isinstance(config.get("devices"), list) else []
    defaults = getattr(runtime_state, "defaults", None)
    defaults = defaults if isinstance(defaults, dict) else {}
    default_system = defaults.get("system", {}) if isinstance(defaults.get("system"), dict) else {}
    default_devices = defaults.get("devices", {}) if isinstance(defaults.get("devices"), dict) else {}

    system_max = _safe_int(
        system.get("max_total_power", default_system.get("max_total_power")),
        GENERIC_MAX_POWER_W,
        minimum=0,
        maximum=GENERIC_MAX_POWER_W,
    )
    min_output_max = _safe_int(
        system.get("max_total_power", default_system.get("max_total_power")),
        system_max,
        minimum=0,
        maximum=GENERIC_MAX_POWER_W,
    )

    device_limits = {}
    for name, device in default_devices.items():
        if isinstance(device, dict):
            device_limits[name] = _safe_int(
                device.get("max_power"),
                GENERIC_MAX_POWER_W,
                minimum=0,
                maximum=GENERIC_MAX_POWER_W,
            )

    max_device_fallback = _safe_int(
        system.get("max_device_power"),
        GENERIC_MAX_POWER_W,
        minimum=0,
        maximum=GENERIC_MAX_POWER_W,
    )
    for device in devices:
        if not isinstance(device, dict) or not device.get("name"):
            continue
        name = device["name"]
        device_limits[name] = _safe_int(
            device.get("max_power", max_device_fallback),
            max_device_fallback,
            minimum=0,
            maximum=GENERIC_MAX_POWER_W,
        )

    return {
        "system_max_total_power": system_max,
        "min_output_limit_max": min_output_max,
        "device_max_power": device_limits,
        "fallback_device_max_power": max_device_fallback,
    }


def attach_limits(payload, validation_context):
    result = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    result["_limits"] = effective_limits(validation_context)
    return result


def effective_limits(validation_context=None):
    context = validation_context if isinstance(validation_context, dict) else {}
    return {
        "system": {
            "max_total_power": _safe_int(
                context.get("system_max_total_power"),
                GENERIC_MAX_POWER_W,
                minimum=0,
                maximum=GENERIC_MAX_POWER_W,
            ),
            "min_output_limit": _safe_int(
                context.get("min_output_limit_max"),
                GENERIC_MAX_POWER_W,
                minimum=0,
                maximum=GENERIC_MAX_POWER_W,
            ),
        },
        "devices": dict(context.get("device_max_power") or {}),
        "fallback_device_max_power": _safe_int(
            context.get("fallback_device_max_power"),
            GENERIC_MAX_POWER_W,
            minimum=0,
            maximum=GENERIC_MAX_POWER_W,
        ),
    }


def _runtime_section(runtime_state, section_name):
    if runtime_state is None:
        raise RuntimeWriteError("runtime state is not available")

    data = getattr(runtime_state, "data", None)
    if not isinstance(data, dict):
        raise RuntimeWriteError("runtime state is not available")

    section = data.setdefault(section_name, {})
    if not isinstance(section, dict):
        raise RuntimeWriteError(f"runtime section {section_name} must be an object")
    return section


def _update_section(runtime_state, section_name, values):
    updater = getattr(runtime_state, "update_section", None)
    if callable(updater):
        try:
            return updater(section_name, values)
        except ValueError as exc:
            raise RuntimeWriteError(str(exc)) from exc

    lock = getattr(runtime_state, "lock", None)
    if lock is None:
        return _update_section_unlocked(runtime_state, section_name, values)
    with lock:
        return _update_section_unlocked(runtime_state, section_name, values)


def _update_section_unlocked(runtime_state, section_name, values):
    section = _runtime_section(runtime_state, section_name)
    section.update(values)
    result = copy.deepcopy(section)
    _save(runtime_state)
    return result


def _update_device(runtime_state, device_name, values):
    updater = getattr(runtime_state, "update_device", None)
    if callable(updater):
        return updater(device_name, values)

    lock = getattr(runtime_state, "lock", None)
    if lock is None:
        return _update_device_unlocked(runtime_state, device_name, values)
    with lock:
        return _update_device_unlocked(runtime_state, device_name, values)


def _update_device_unlocked(runtime_state, device_name, values):
    devices = _runtime_section(runtime_state, "devices")
    if device_name not in devices:
        known = ", ".join(sorted(devices)) or "(none)"
        raise RuntimeWriteError(f"unknown device {device_name}; known devices: {known}")

    device = devices[device_name]
    if not isinstance(device, dict):
        raise RuntimeWriteError(f"device {device_name} runtime state must be an object")

    device.update(values)
    result = copy.deepcopy(device)
    _save(runtime_state)
    return result


def _validate_payload(payload, fields):
    if not isinstance(payload, dict):
        raise RuntimeWriteError("request body must be a JSON object")

    if not payload:
        raise RuntimeWriteError("request body must contain at least one field")

    unknown = sorted(set(payload) - set(fields))
    if unknown:
        raise RuntimeWriteError(f"unknown field: {unknown[0]}")

    return {
        key: _validate_value(key, payload[key], *fields[key])
        for key in payload
    }


def _system_fields(validation_context):
    limits = effective_limits(validation_context)
    fields = dict(SYSTEM_FIELDS)
    fields["max_total_power"] = ("int", (0, limits["system"]["max_total_power"]))
    fields["min_output_limit"] = ("int", (0, limits["system"]["min_output_limit"]))
    return fields


def _device_fields(device_name, validation_context):
    limits = effective_limits(validation_context)
    fields = dict(DEVICE_FIELDS)
    device_limits = limits["devices"]
    max_power = _safe_int(
        device_limits.get(device_name),
        limits["fallback_device_max_power"],
        minimum=0,
        maximum=GENERIC_MAX_POWER_W,
    )
    fields["max_power"] = ("int", (0, max_power))
    return fields


def _validate_value(key, value, value_type, rule):
    if value_type == "bool":
        if not isinstance(value, bool):
            raise RuntimeWriteError(f"{key} must be a boolean")
        return value

    if value_type == "int":
        if isinstance(value, bool):
            raise RuntimeWriteError(f"{key} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeWriteError(f"{key} must be an integer") from exc
        if parsed != value and not (isinstance(value, str) and value.strip() == str(parsed)):
            raise RuntimeWriteError(f"{key} must be an integer")
        minimum, maximum = rule
        if parsed < minimum or parsed > maximum:
            raise RuntimeWriteError(f"{key} must be between {minimum} and {maximum}")
        return parsed

    if value_type == "float":
        if isinstance(value, bool):
            raise RuntimeWriteError(f"{key} must be numeric")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeWriteError(f"{key} must be numeric") from exc
        if not math.isfinite(parsed):
            raise RuntimeWriteError(f"{key} must be finite")
        minimum, maximum = rule
        if parsed < minimum or parsed > maximum:
            raise RuntimeWriteError(f"{key} must be between {minimum} and {maximum}")
        return parsed

    if value_type == "enum":
        normalized = str(value or "").strip().lower()
        if normalized not in rule:
            allowed = ", ".join(rule)
            raise RuntimeWriteError(f"{key} must be one of: {allowed}")
        return normalized

    raise RuntimeWriteError(f"unsupported validator for {key}")


def _save(runtime_state):
    save = getattr(runtime_state, "save_atomic", None)
    if not callable(save):
        raise RuntimeWriteError("runtime state cannot be saved")
    save()


def _safe_int(value, default, minimum=None, maximum=None):
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = int(default)
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed
