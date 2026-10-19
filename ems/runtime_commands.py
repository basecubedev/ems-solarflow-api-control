# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one path every runtime-state edit takes.

emsctl, the dashboard, the Admin's config->runtime mirror and the scheduler
change operator values only through these commands and writers: one whitelist,
one set of validators, one writer that re-reads the file before it saves.
"""

import copy
import logging
import math
from dataclasses import dataclass

from ems import device_settings
from ems.logging_utils import log_event


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
    # Stopping a device drawing from the grid must not need a restart.
    "ac_charge_enabled": ("bool", None),
}

DEVICE_AC_FIELDS = {
    "runtime_role": ("enum", ("ac_output", "ac_input")),
    "ac_charge_power_w": ("int", (0, GENERIC_MAX_POWER_W)),
}

DEVICE_SETTING_FIELDS = {
    setting.key: ("setting", setting.words) for setting in device_settings.SETTINGS
}

WRITABLE_DEVICE_FIELDS = {**DEVICE_FIELDS, **DEVICE_AC_FIELDS, **DEVICE_SETTING_FIELDS}

SECTION_FIELDS = {
    "winter": {
        "enabled": ("bool", None),
    },
    "ac_charge_control": {
        "enabled": ("bool", None),
    },
}


class RuntimeWriteError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeCommand:
    """An emsctl-named edit: the key it sets and the value, fixed or given."""

    key: str
    value: object = None
    choices: tuple = ()


SYSTEM_COMMANDS = {
    "enable": RuntimeCommand("enabled", True),
    "disable": RuntimeCommand("enabled", False),
    "max-power": RuntimeCommand("max_total_power"),
    "loop-interval": RuntimeCommand("loop_interval"),
    "min-output-limit": RuntimeCommand("min_output_limit"),
}

DEVICE_COMMANDS = {
    "enable": RuntimeCommand("enabled", True),
    "disable": RuntimeCommand("enabled", False),
    "max-power": RuntimeCommand("max_power"),
    "offgrid": RuntimeCommand("offgrid_socket_mode"),
    "pv-priority-factor": RuntimeCommand("pv_priority_factor"),
    "ac-mode": RuntimeCommand(
        "runtime_role", choices=(("output", "ac_output"), ("input", "ac_input"))
    ),
    "ac-charge": RuntimeCommand("ac_charge_enabled", choices=(("on", True), ("off", False))),
    "ac-charge-power": RuntimeCommand("ac_charge_power_w"),
    **{
        setting.command: RuntimeCommand(setting.key)
        for setting in device_settings.SETTINGS
    },
}

SECTION_COMMANDS = {
    "enable": RuntimeCommand("enabled", True),
    "disable": RuntimeCommand("enabled", False),
}

EMSCTL_SCOPES = {
    "system": "system",
    "device": "device",
    "winter": "winter",
    "ac-charge": "ac_charge_control",
}


def emsctl_line(scope, action, value=None, *, device=None):
    """The emsctl command line that runs ``action`` in ``scope``."""

    command = next((name for name, named in EMSCTL_SCOPES.items() if named == scope), None)
    if command is None:
        raise RuntimeWriteError(f"unsupported runtime scope {scope}")
    words = ["emsctl", command, *([device] if device else []), action]
    if value is not None:
        words.append(str(value))
    return " ".join(words)


def runtime_command(scope, action):
    """The command ``action`` names in ``scope``: system, device or a section."""

    if scope == "system":
        table = SYSTEM_COMMANDS
    elif scope == "device":
        table = DEVICE_COMMANDS
    elif scope in SECTION_FIELDS:
        table = SECTION_COMMANDS
    else:
        raise RuntimeWriteError(f"unsupported runtime scope {scope}")
    if action not in table:
        raise RuntimeWriteError(f"unknown {scope} command {action}")
    return table[action]


def command_values(scope, action, value=None):
    """The runtime values a command sets, before validation."""

    command = runtime_command(scope, action)
    if command.value is not None:
        if value is not None:
            raise RuntimeWriteError("does not take a value")
        return {command.key: command.value}
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RuntimeWriteError("requires a value")
    if not command.choices:
        return {command.key: value}
    choices = dict(command.choices)
    word = str(value).strip().lower()
    if word not in choices:
        allowed = " or ".join(repr(choice) for choice in choices)
        raise RuntimeWriteError(f"value must be {allowed}")
    return {command.key: choices[word]}


def validate_command(scope, action, value=None, *, device=None, validation_context=None):
    """The values a command would save, validated, without saving them."""

    payload = command_values(scope, action, value)
    if scope == "system":
        return validate_system_values(payload, validation_context)
    if scope == "device":
        return validate_device_values(device, payload, validation_context)
    return validate_section_values(scope, payload)


def run_command(
    runtime_state, scope, action, value=None, *, device=None, validation_context=None, source
):
    """Validate and save one command; ``source`` names the caller."""

    return run_commands(
        runtime_state, scope, ((action, value),), device=device,
        validation_context=validation_context, source=source,
    )


def run_commands(runtime_state, scope, commands, *, device=None, validation_context=None, source):
    """Validate several commands of one scope together and save them in one write.

    All or nothing: a refused value leaves every other one unwritten.
    """

    payload = {}
    for action, value in commands:
        payload.update(command_values(scope, action, value))
    if scope == "system":
        return apply_system_update(runtime_state, payload, validation_context, source=source)
    if scope == "device":
        return apply_device_update(
            runtime_state, device, payload, validation_context, source=source
        )
    return apply_section_update(
        runtime_state, scope, payload, validation_context, source=source
    )


def runtime_payload(runtime_state):
    if runtime_state is None:
        return {}
    snapshot = getattr(runtime_state, "snapshot", None)
    if callable(snapshot):
        return snapshot()
    data = getattr(runtime_state, "data", None)
    return copy.deepcopy(data) if isinstance(data, dict) else {}


def apply_system_update(runtime_state, payload, validation_context=None, *, source):
    values = _validate_payload(
        payload,
        _system_fields(validation_context),
    )
    section = _update_section(runtime_state, "system", values)
    _log_update(source, "system", values)
    return {"system": section}


def apply_section_update(
    runtime_state, section_name, payload, validation_context=None, *, source
):
    if section_name not in SECTION_FIELDS:
        raise RuntimeWriteError(f"unsupported runtime section {section_name}")
    values = _validate_payload(payload, SECTION_FIELDS[section_name])
    section = _update_section(runtime_state, section_name, values)
    _log_update(source, section_name, values)
    return {section_name: section}


def apply_device_update(
    runtime_state, device_name, payload, validation_context=None, *, source
):
    values = validate_device_values(device_name, payload, validation_context)
    if "runtime_role" in values:
        values["runtime_role_reason"] = source
    try:
        device = _update_device(runtime_state, device_name, values)
    except KeyError as exc:
        raise RuntimeWriteError(str(exc).strip("'")) from exc
    except ValueError as exc:
        raise RuntimeWriteError(str(exc)) from exc
    _log_update(source, "device", values, device=device_name)
    return {"device": device_name, "state": device}


def _log_update(source, scope, values, device=None):
    where = {"device": device} if device else {}
    log_event(
        logging.INFO,
        "runtime_values_updated",
        source=source,
        scope=scope,
        changed=",".join(f"{key}={values[key]}" for key in sorted(values)),
        **where,
    )


def validate_system_values(payload, validation_context=None):
    """Validated ``system`` values; the one rule set every runtime writer uses."""

    return _validate_payload(payload, _system_fields(validation_context))


def validate_section_values(section_name, payload):
    """Validated values for a runtime section such as ``winter``."""

    if section_name not in SECTION_FIELDS:
        raise RuntimeWriteError(f"unsupported runtime section {section_name}")
    return _validate_payload(payload, SECTION_FIELDS[section_name])


def validate_device_values(device_name, payload, validation_context=None):
    """Validated per-device values, including the AC role, charge power and settings.

    AC charging is refused for a device the EMS cannot switch: over MQTT the
    acMode is not reconciled, so ``ac_input`` would only stop output
    regulation and leave the device feeding in at its last limit. A device
    setting is refused there too: over MQTT the EMS writes none.
    """

    if isinstance(payload, dict) and not ac_role_supported(
        device_name, validation_context
    ):
        refused = sorted(
            key
            for key in set(payload) & set(DEVICE_AC_FIELDS)
            if not _back_to_ac_output(key, payload[key])
        )
        if refused:
            raise RuntimeWriteError(
                f"{refused[0]} is not available for {device_name}: it is "
                "connected over MQTT, where the EMS cannot switch the AC mode"
            )
        refused = sorted(
            key
            for key in set(payload) & set(DEVICE_SETTING_FIELDS)
            if not _unmanaged(payload[key])
        )
        if refused:
            raise RuntimeWriteError(
                f"{refused[0]} is not available for {device_name}: it is "
                "connected over MQTT, where the EMS writes no device settings"
            )
    fields = {
        **_device_fields(device_name, validation_context),
        **DEVICE_AC_FIELDS,
        **DEVICE_SETTING_FIELDS,
    }
    return _validate_payload(payload, fields)


def _unmanaged(value):
    """Handing a setting back to the device writes nothing, so it is never refused."""

    return value is None or str(value).strip().lower() == device_settings.UNMANAGED


def _back_to_ac_output(key, value):
    """``ac_output`` is the default and stops nothing, so it is never refused:
    a role left at ``ac_input`` from before a move to MQTT must be clearable."""

    return key == "runtime_role" and str(value or "").strip().lower() == "ac_output"


def ac_role_supported(device_name, validation_context=None):
    context = validation_context if isinstance(validation_context, dict) else {}
    return device_name not in set(context.get("ac_role_unsupported") or ())


def build_validation_context(config=None, runtime_state=None):
    from ems.zendure_mqtt.config_entries import is_mqtt_telemetry_device_config

    config = config if isinstance(config, dict) else {}
    system = config.get("system", {}) if isinstance(config.get("system"), dict) else {}
    devices = config.get("devices", []) if isinstance(config.get("devices"), list) else []
    defaults = getattr(runtime_state, "defaults", None)
    defaults = defaults if isinstance(defaults, dict) else {}
    default_devices = defaults.get("devices", {}) if isinstance(defaults.get("devices"), dict) else {}

    configured_system_max = _safe_int(
        system.get("max_total_power"),
        GENERIC_MAX_POWER_W,
        minimum=0,
    )
    system_max = _safe_int(
        system.get("max_total_power_limit", configured_system_max),
        configured_system_max,
        minimum=0,
    )
    min_output_max = _safe_int(
        system.get("min_output_limit_max", system.get("max_total_power_limit")),
        system_max,
        minimum=0,
    )

    device_limits = {}
    for name, device in default_devices.items():
        if isinstance(device, dict):
            device_limits[name] = _safe_int(
                device.get("max_power"),
                GENERIC_MAX_POWER_W,
                minimum=0,
            )

    max_device_fallback = _safe_int(
        system.get("max_device_power"),
        GENERIC_MAX_POWER_W,
        minimum=0,
    )
    for device in devices:
        if not isinstance(device, dict) or not device.get("name"):
            continue
        name = device["name"]
        device_limits[name] = _safe_int(
            device.get("max_power", max_device_fallback),
            max_device_fallback,
            minimum=0,
        )

    return {
        "system_max_total_power": system_max,
        "min_output_limit_max": min_output_max,
        "device_max_power": device_limits,
        "fallback_device_max_power": max_device_fallback,
        "ac_role_unsupported": sorted(
            device["name"]
            for device in devices
            if isinstance(device, dict)
            and device.get("name")
            and is_mqtt_telemetry_device_config(device)
        ),
    }


def attach_limits(payload, validation_context, offered_settings=None):
    """The runtime payload with its bounds, and the device settings each device has."""

    result = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    result["_limits"] = effective_limits(validation_context)
    result["_limits"]["device_settings_offered"] = dict(offered_settings or {})
    return result


def effective_limits(validation_context=None):
    context = validation_context if isinstance(validation_context, dict) else {}
    return {
        "system": {
            "max_total_power": _safe_int(
                context.get("system_max_total_power"),
                GENERIC_MAX_POWER_W,
                minimum=0,
            ),
            "min_output_limit": _safe_int(
                context.get("min_output_limit_max"),
                GENERIC_MAX_POWER_W,
                minimum=0,
            ),
        },
        "devices": dict(context.get("device_max_power") or {}),
        "fallback_device_max_power": _safe_int(
            context.get("fallback_device_max_power"),
            GENERIC_MAX_POWER_W,
            minimum=0,
        ),
        "ac_charge_power_max": DEVICE_AC_FIELDS["ac_charge_power_w"][1][1],
        "ac_role_unsupported": list(context.get("ac_role_unsupported") or ()),
        "device_settings": device_settings.catalogue(),
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

    if value_type == "setting":
        if _unmanaged(value):
            return None
        normalized = str(value).strip().lower() if not isinstance(value, bool) else ""
        if normalized not in rule:
            allowed = ", ".join((*rule, device_settings.UNMANAGED))
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
