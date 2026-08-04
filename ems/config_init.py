# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided first-run config assistant."""

import copy
import getpass
import json
import os

from ems import config as config_mod
from ems.influx_setup import DOCKER_FIRST_SECRET_FILE
from ems.paths import resolve_template_path


GRID_METER_CHOICES = (
    ("shelly", "Shelly Pro 3EM / Shelly Plus/Pro Gen2/Gen3"),
    ("shelly_3em_gen1", "Shelly 3EM Gen1"),
    ("ecotracker", "EcoTracker"),
    (
        config_mod.ZENDURE_GRID_METER_HTTP_GRID_METER_TYPE,
        "Zendure Grid Meter via local HTTP (D0 / Smart Meter 3CT)",
    ),
    ("tasmota_http", "Tasmota HTTP / SmartMeter reader"),
    (
        config_mod.ZENDURE_SMARTMETER_D0_GRID_METER_TYPE,
        "Zendure SmartMeter D0 via MQTT",
    ),
    ("mqtt", "Generic MQTT grid meter"),
)
# Backward-compatible grid-meter types accepted from an existing config but not
# offered as a new interactive menu choice. The 3CT and D0 HTTP types share the
# generic local-HTTP client; the generic menu choice covers new setups while
# these keep an existing typed config round-tripping instead of resetting to
# Shelly.
_LEGACY_GRID_METER_TYPES = (
    config_mod.ZENDURE_SMARTMETER_3CT_HTTP_GRID_METER_TYPE,
    config_mod.ZENDURE_SMARTMETER_D0_HTTP_GRID_METER_TYPE,
)
SUPPORTED_GRID_METER_TYPES = (
    tuple(value for value, _ in GRID_METER_CHOICES) + _LEGACY_GRID_METER_TYPES
)

SECRET_KEYS = {"token", "password"}


class ConfigInitError(ValueError):
    """Raised for setup input or planning errors."""


def _zendure_d0_serial_from_topic(topic):
    return config_mod.zendure_smartmeter_d0_serial_from_topic(topic)


def _load_template(base_dir):
    path = resolve_template_path(base_dir=base_dir)
    with open(path) as f:
        return json.load(f)


def _is_template_config(config, base_dir):
    if not isinstance(config, dict):
        return False
    try:
        return config == _load_template(base_dir)
    except (OSError, json.JSONDecodeError):
        return False


def classify_config(config, config_exists, base_dir):
    if not config_exists:
        return "missing"
    if _is_template_config(config, base_dir):
        return "template"
    return "edited"


def _read_line(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return None


def _format_default(default):
    if default is None:
        return ""
    if isinstance(default, bool):
        return "Y/n" if default else "y/N"
    return str(default)


def _usable_default(default, *, required, allow_placeholder_default):
    if (
        required
        and not allow_placeholder_default
        and config_mod.is_template_placeholder_value(default)
    ):
        return None
    return default


def ask_text(
    label,
    default=None,
    *,
    required=False,
    noninteractive=False,
    allow_placeholder_default=False,
):
    default = _usable_default(
        default,
        required=required,
        allow_placeholder_default=allow_placeholder_default,
    )
    if noninteractive:
        if required and (default is None or str(default).strip() == ""):
            raise ConfigInitError(f"{label} is required")
        return "" if default is None else str(default)

    suffix = f" [{_format_default(default)}]" if default is not None else ""
    while True:
        value = _read_line(f"{label}{suffix}: ")
        if value is None:
            raise ConfigInitError("input aborted")
        if value == "" and default is not None:
            value = str(default)
        if not required or value.strip():
            return value.strip()
        print("Please enter a value.")


def ask_secret_text(
    label,
    default=None,
    *,
    required=False,
    noninteractive=False,
    allow_placeholder_default=False,
):
    default = _usable_default(
        default,
        required=required,
        allow_placeholder_default=allow_placeholder_default,
    )
    if noninteractive:
        if required and (default is None or str(default).strip() == ""):
            raise ConfigInitError(f"{label} is required")
        return "" if default is None else str(default)

    configured = default is not None and str(default).strip() != ""
    state = "configured, press Enter to keep" if configured else "not set"
    prompt = f"{label} [{state}]: "
    while True:
        try:
            value = getpass.getpass(prompt)
        except EOFError:
            raise ConfigInitError("input aborted") from None
        if value == "" and configured:
            value = str(default)
        if not required or value.strip():
            return value.strip()
        print("Please enter a value.")


def ask_confirm(label, default=True, *, noninteractive=False):
    if noninteractive:
        return bool(default)

    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        value = _read_line(f"{label}{suffix} ")
        if value is None:
            raise ConfigInitError("input aborted")
        value = value.strip().lower()
        if not value:
            return bool(default)
        if value in ("y", "yes", "j", "ja"):
            return True
        if value in ("n", "no", "nein"):
            return False
        print("Please answer yes or no.")


def ask_int(
    label,
    default,
    *,
    minimum=0,
    noninteractive=False,
    allow_placeholder_default=False,
):
    while True:
        raw = ask_text(
            label,
            default,
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_default,
        )
        try:
            value = int(raw, 10)
        except (TypeError, ValueError):
            if noninteractive:
                raise ConfigInitError(f"{label} must be an integer")
            print("Please enter a whole number.")
            continue
        if value < minimum:
            if noninteractive:
                raise ConfigInitError(f"{label} must be >= {minimum}")
            print(f"Please enter a value >= {minimum}.")
            continue
        return value


def ask_float(
    label,
    default,
    *,
    minimum=0.0,
    noninteractive=False,
    allow_placeholder_default=False,
):
    while True:
        raw = ask_text(
            label,
            default,
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_default,
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            if noninteractive:
                raise ConfigInitError(f"{label} must be numeric")
            print("Please enter a number.")
            continue
        if value < minimum:
            if noninteractive:
                raise ConfigInitError(f"{label} must be >= {minimum}")
            print(f"Please enter a value >= {minimum}.")
            continue
        return value


def ask_grid_meter(
    existing,
    *,
    noninteractive=False,
    allow_placeholder_defaults=False,
):
    existing = existing if isinstance(existing, dict) else {}
    current_type = str(existing.get("type") or "shelly").strip().lower()
    if current_type not in SUPPORTED_GRID_METER_TYPES:
        current_type = "shelly"

    if noninteractive:
        meter_type = current_type
    else:
        print()
        print("Which grid meter do you use?")
        for index, (_, label) in enumerate(GRID_METER_CHOICES, start=1):
            print(f"{index}) {label}")
        # Legacy types (e.g. the 3CT HTTP alias) are not menu entries, so their
        # default points at the generic local-HTTP choice instead.
        menu_types = [value for value, _ in GRID_METER_CHOICES]
        default_type = (
            current_type
            if current_type in menu_types
            else config_mod.ZENDURE_GRID_METER_HTTP_GRID_METER_TYPE
        )
        default_index = menu_types.index(default_type) + 1
        while True:
            raw = ask_text("Choice", str(default_index), required=True)
            if raw.isdigit() and 1 <= int(raw) <= len(GRID_METER_CHOICES):
                meter_type = GRID_METER_CHOICES[int(raw) - 1][0]
                break
            print("Please enter one of the listed numbers.")

    result = copy.deepcopy(existing)
    result["type"] = meter_type
    if meter_type in config_mod.MQTT_GRID_METER_TYPES:
        mqtt_settings = config_mod.grid_meter_mqtt_settings(result)
        existing_password = mqtt_settings.pop("password", None)
        mqtt_settings["host"] = ask_text(
            "MQTT broker host",
            mqtt_settings.get("host") or "",
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_defaults,
        )
        mqtt_settings["port"] = ask_int(
            "MQTT broker port",
            mqtt_settings.get("port", 1883),
            minimum=1,
            noninteractive=noninteractive,
        )
        mqtt_settings["username"] = ask_text(
            "MQTT username (optional)",
            mqtt_settings.get("username") or "",
            noninteractive=noninteractive,
        )
        mqtt_settings["password"] = ask_secret_text(
            "MQTT password (optional)",
            existing_password,
            noninteractive=noninteractive,
        )

        if meter_type == config_mod.ZENDURE_SMARTMETER_D0_GRID_METER_TYPE:
            serial_default = _zendure_d0_serial_from_topic(
                mqtt_settings.get("topic")
            )
            serial = ask_text(
                "Zendure SmartMeter D0 serial number",
                serial_default,
                required=not mqtt_settings.get("topic"),
                noninteractive=noninteractive,
                allow_placeholder_default=allow_placeholder_defaults,
            )
            generated_topic = (
                config_mod.zendure_smartmeter_d0_topic(serial)
                if serial
                else mqtt_settings.get("topic", "")
            )
            if noninteractive:
                topic = mqtt_settings.get("topic") or generated_topic
            elif ask_confirm(
                f"Use generated MQTT topic {generated_topic}?",
                True,
                noninteractive=noninteractive,
            ):
                topic = generated_topic
            else:
                topic = ask_text(
                    "Custom MQTT power topic",
                    mqtt_settings.get("topic") or generated_topic,
                    required=True,
                    noninteractive=noninteractive,
                    allow_placeholder_default=allow_placeholder_defaults,
                )
            mqtt_settings["topic"] = topic
            payload_format = "number"
            mqtt_settings.pop("value_path", None)
        else:
            mqtt_settings["topic"] = ask_text(
                "MQTT power topic",
                mqtt_settings.get("topic") or "",
                required=True,
                noninteractive=noninteractive,
                allow_placeholder_default=allow_placeholder_defaults,
            )
            payload_format = ask_text(
                "MQTT payload format (number/json)",
                mqtt_settings.get("payload_format") or "number",
                required=True,
                noninteractive=noninteractive,
            ).strip().lower()
            if payload_format not in ("number", "json"):
                if noninteractive:
                    raise ConfigInitError("MQTT payload format must be number or json")
                print("Unknown payload format, using number.")
                payload_format = "number"
            mqtt_settings["payload_format"] = payload_format
            if payload_format == "json":
                mqtt_settings["value_path"] = ask_text(
                    "MQTT JSON value path",
                    mqtt_settings.get("value_path") or "",
                    required=True,
                    noninteractive=noninteractive,
                    allow_placeholder_default=allow_placeholder_defaults,
                )
            else:
                mqtt_settings.pop("value_path", None)
        mqtt_settings["payload_format"] = payload_format
        mqtt_settings["max_age_seconds"] = ask_int(
            "MQTT max value age in seconds",
            mqtt_settings.get("max_age_seconds", 15),
            minimum=1,
            noninteractive=noninteractive,
        )
        result["mqtt"] = mqtt_settings
        for key in (
            "ip",
            "url",
            "power_path",
            "channels",
            "host",
            "port",
            "username",
            "password",
            "topic",
            "payload_format",
            "value_path",
            "max_age_seconds",
        ):
            result.pop(key, None)
    elif meter_type == "tasmota_http":
        current_url = result.get("url") or result.get("ip") or ""
        value = ask_text(
            "Tasmota HTTP URL or IP",
            current_url,
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_defaults,
        )
        if value.startswith("http://") or value.startswith("https://"):
            result["url"] = value
            result.pop("ip", None)
        else:
            result["ip"] = value
            result.pop("url", None)
        result["power_path"] = ask_text(
            "Tasmota power path",
            result.get("power_path") or "StatusSNS.SML.Power_curr",
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_defaults,
        )
        for key in (
            "host",
            "port",
            "username",
            "password",
            "topic",
            "payload_format",
            "value_path",
            "max_age_seconds",
            "channels",
            "mqtt",
        ):
            result.pop(key, None)
    else:
        result["ip"] = ask_text(
            "Grid meter IP address",
            result.get("ip") or "",
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_defaults,
        )
        result.pop("url", None)
        result.pop("power_path", None)
        for key in (
            "host",
            "port",
            "username",
            "password",
            "topic",
            "payload_format",
            "value_path",
            "max_age_seconds",
            "mqtt",
        ):
            result.pop(key, None)
    return result


def _device_defaults(template_config):
    devices = template_config.get("devices")
    if isinstance(devices, list) and devices and isinstance(devices[0], dict):
        return copy.deepcopy(devices[0])
    return {
        "name": "WR1",
        "ip": "",
        "sn": "",
        "smart_mode": 1,
        "max_power": 800,
        "pv_kwp": 1.0,
        "pv_priority_factor": 1.0,
        "battery_kwh": 1.92,
        "min_soc": 15,
        "max_soc": 100,
    }


def ask_devices(
    existing_devices,
    template_config,
    *,
    noninteractive=False,
    allow_placeholder_defaults=False,
):
    existing_devices = [
        item for item in existing_devices
        if isinstance(item, dict)
    ] if isinstance(existing_devices, list) else []
    default_count = len(existing_devices) if existing_devices else 1
    count = ask_int(
        "How many Zendure inverters do you have?",
        default_count,
        minimum=1,
        noninteractive=noninteractive,
    )
    template_device = _device_defaults(template_config)
    devices = []

    for index in range(count):
        current = copy.deepcopy(template_device)
        current["name"] = f"WR{index + 1}"
        if index < len(existing_devices):
            current.update(copy.deepcopy(existing_devices[index]))
        current.setdefault("name", f"WR{index + 1}")
        current.setdefault("max_power", 800)
        current.setdefault("pv_kwp", 1.0)
        current.setdefault("battery_kwh", 1.92)
        current.setdefault("min_soc", 15)
        current.setdefault("max_soc", 100)

        display = index + 1
        current["name"] = ask_text(
            f"Device {display} name",
            current.get("name") or f"WR{display}",
            required=True,
            noninteractive=noninteractive,
        )
        current["ip"] = ask_text(
            f"Device {display} IP",
            current.get("ip") or "",
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_defaults,
        )
        current["sn"] = ask_text(
            f"Device {display} serial number",
            current.get("sn") or "",
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_defaults,
        )
        current["max_power"] = ask_int(
            f"Device {display} max output power",
            current.get("max_power", 800),
            minimum=0,
            noninteractive=noninteractive,
        )
        current["pv_kwp"] = ask_float(
            "PV size connected to this device in kWp",
            current.get("pv_kwp", 1.0),
            minimum=0.0,
            noninteractive=noninteractive,
        )
        current["battery_kwh"] = ask_float(
            "Battery size in kWh",
            current.get("battery_kwh", 1.92),
            minimum=0.0,
            noninteractive=noninteractive,
        )
        current["min_soc"] = ask_int(
            "Minimum SOC",
            current.get("min_soc", 15),
            minimum=0,
            noninteractive=noninteractive,
        )
        current["max_soc"] = ask_int(
            "Maximum SOC",
            current.get("max_soc", 100),
            minimum=0,
            noninteractive=noninteractive,
        )
        devices.append(current)

    return devices


def apply_system_basics(
    config,
    *,
    noninteractive=False,
    allow_placeholder_defaults=False,
):
    system = config.setdefault("system", {})
    dashboard = config.setdefault("dashboard", {})
    winter = config.setdefault("winter", {})
    assist = config.setdefault("battery_full_charge_assist", {})
    influxdb = config.setdefault("influxdb", {})
    ha = config.setdefault("ha", {})

    system["max_total_power"] = ask_int(
        "Maximum total output power",
        system.get("max_total_power", 800),
        minimum=0,
        noninteractive=noninteractive,
    )
    system["min_output_limit"] = ask_int(
        "Minimum output limit",
        system.get("min_output_limit", 35),
        minimum=0,
        noninteractive=noninteractive,
    )
    system["enabled"] = ask_confirm(
        "EMS control enabled on startup?",
        bool(system.get("enabled", True)),
        noninteractive=noninteractive,
    )
    dashboard["enabled"] = ask_confirm(
        "Dashboard enabled?",
        bool(dashboard.get("enabled", True)),
        noninteractive=noninteractive,
    )
    winter["enabled"] = ask_confirm(
        "Winter mode enabled?",
        bool(winter.get("enabled", False)),
        noninteractive=noninteractive,
    )
    assist["enabled"] = ask_confirm(
        "Battery full-charge assist enabled?",
        bool(assist.get("enabled", False)),
        noninteractive=noninteractive,
    )
    influxdb["enabled"] = ask_confirm(
        "InfluxDB analytics enabled?",
        bool(influxdb.get("enabled", False)),
        noninteractive=noninteractive,
    )
    ha["enabled"] = ask_confirm(
        "Home Assistant publishing enabled?",
        bool(ha.get("enabled", False)),
        noninteractive=noninteractive,
    )
    if ha["enabled"]:
        ha["url"] = ask_text(
            "Home Assistant URL",
            ha.get("url") or "http://homeassistant.local:8123",
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_defaults,
        )
        ha["token"] = ask_secret_text(
            "Home Assistant token",
            ha.get("token"),
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_defaults,
        )


def apply_analytics(config):
    """Enable bundled InfluxDB analytics for the Docker-first setup.

    Sets the supported zero-config bundled defaults and points secrets at
    ``config/influxdb.env`` so a Docker-first install needs no repo checkout.
    Other influxdb keys (url/host_url/org/...) keep their template values.
    """
    influxdb = config.setdefault("influxdb", {})
    influxdb["enabled"] = True
    influxdb["mode"] = "bundled"
    influxdb["auto_init"] = True
    influxdb["auto_sync"] = True
    influxdb["secret_file"] = DOCKER_FIRST_SECRET_FILE
    return config


def apply_answers(
    base_config,
    template_config,
    *,
    noninteractive=False,
    allow_placeholder_defaults=False,
    analytics=False,
):
    updated = copy.deepcopy(base_config)
    updated["grid_meter"] = ask_grid_meter(
        updated.get("grid_meter", {}),
        noninteractive=noninteractive,
        allow_placeholder_defaults=allow_placeholder_defaults,
    )
    updated["devices"] = ask_devices(
        updated.get("devices", []),
        template_config,
        noninteractive=noninteractive,
        allow_placeholder_defaults=allow_placeholder_defaults,
    )
    apply_system_basics(
        updated,
        noninteractive=noninteractive,
        allow_placeholder_defaults=allow_placeholder_defaults,
    )
    if analytics:
        apply_analytics(updated)
    return updated


def _meter_summary(grid_meter):
    meter_type = grid_meter.get("type", "shelly")
    if meter_type in config_mod.MQTT_GRID_METER_TYPES:
        mqtt_settings = config_mod.grid_meter_mqtt_settings(grid_meter)
        target = mqtt_settings.get("host") or "(not set)"
        topic = mqtt_settings.get("topic") or "(no topic)"
        label = (
            "Zendure SmartMeter D0"
            if meter_type == config_mod.ZENDURE_SMARTMETER_D0_GRID_METER_TYPE
            else "Generic MQTT"
        )
        return f"{label} at {target}:{mqtt_settings.get('port', 1883)} {topic}"
    if meter_type == "tasmota_http":
        target = grid_meter.get("url") or grid_meter.get("ip") or "(not set)"
        return f"Tasmota HTTP at {target}"
    label = {
        "shelly": "Shelly",
        "shelly_3em_gen1": "Shelly 3EM Gen1",
        "ecotracker": "EcoTracker",
        config_mod.ZENDURE_GRID_METER_HTTP_GRID_METER_TYPE:
            "Zendure Grid Meter via local HTTP",
        config_mod.ZENDURE_SMARTMETER_3CT_HTTP_GRID_METER_TYPE:
            "Zendure Smart Meter 3CT HTTP",
    }.get(meter_type, meter_type)
    return f"{label} at {grid_meter.get('ip') or '(not set)'}"


def print_summary(config, config_path):
    print()
    print("Setup summary:")
    print(f"  config: {config_path}")
    print(f"  grid meter: {_meter_summary(config.get('grid_meter', {}))}")
    devices = config.get("devices", [])
    print(f"  devices: {len(devices) if isinstance(devices, list) else 0}")
    if isinstance(devices, list):
        for device in devices:
            if not isinstance(device, dict):
                continue
            print(
                f"    {device.get('name', '(unnamed)')}: "
                f"{device.get('ip') or '(no IP)'}, "
                f"max {device.get('max_power', 0)} W, "
                f"battery {device.get('battery_kwh', 0)} kWh"
            )
    system = config.get("system", {})
    dashboard = config.get("dashboard", {})
    winter = config.get("winter", {})
    assist = config.get("battery_full_charge_assist", {})
    influxdb = config.get("influxdb", {})
    ha = config.get("ha", {})
    print(f"  max total power: {system.get('max_total_power', 0)} W")
    print(f"  dashboard: {'enabled' if dashboard.get('enabled') else 'disabled'}")
    print(f"  winter mode: {'enabled' if winter.get('enabled') else 'disabled'}")
    print(
        "  battery full-charge assist: "
        f"{'enabled' if assist.get('enabled') else 'disabled'}"
    )
    print(
        "  InfluxDB analytics: "
        f"{'enabled' if influxdb.get('enabled') else 'disabled'}"
    )
    print(
        "  Home Assistant publishing: "
        f"{'enabled' if ha.get('enabled') else 'disabled'}"
    )
    if ha.get("enabled"):
        print(f"  Home Assistant URL: {ha.get('url') or '(not set)'}")


def redacted_config(config):
    if isinstance(config, dict):
        result = {}
        for key, value in config.items():
            if str(key).lower() in SECRET_KEYS and value:
                result[key] = "<redacted>"
            else:
                result[key] = redacted_config(value)
        return result
    if isinstance(config, list):
        return [redacted_config(item) for item in config]
    return config


def render_redacted_config_json(config, layout=None):
    return config_mod.render_config_json(redacted_config(config), layout)


def print_next_steps():
    print()
    print("Next steps:")
    in_container = str(os.environ.get("EMS_IN_CONTAINER", "")).lower() in (
        "1",
        "true",
        "yes",
    ) or os.path.exists("/.dockerenv")
    if in_container:
        print("  docker compose restart")
        print("  docker compose exec ems python3 emsctl.py diagnose")
        print("  docker compose exec ems python3 emsctl.py diagnose --hardware")
    else:
        print("  python3 emsctl.py diagnose")
        print("  python3 emsctl.py diagnose --hardware")


def build_plan(config, config_exists, config_path, base_dir):
    kind = classify_config(config, config_exists, base_dir)
    if kind == "edited":
        plan = config_mod.build_config_upgrade_plan(config, base_dir)
        base_config = plan["upgraded_config"]
        layout = plan.get("template_layout")
    else:
        plan = config_mod.build_config_upgrade_plan({}, base_dir)
        base_config = plan["upgraded_config"]
        layout = plan.get("template_layout")

    return {
        "kind": kind,
        "config_path": config_path,
        "base_config": base_config,
        "layout": layout,
        "template_config": _load_template(base_dir),
    }


def print_intro(plan):
    print("Welcome to EMS setup.")
    print()
    print("Using config file:")
    print(f"  {plan['config_path']}")
    print()

    if plan["kind"] == "missing":
        print("No config.json found.")
        print("The setup assistant will create one from config.template.json.")
    elif plan["kind"] == "template":
        print("This config still looks like the default template.")
        print("The setup assistant will fill it with your answers.")
    else:
        print("Existing config detected.")
        print("The setup assistant can update selected setup fields.")
        print("Unknown/custom values will be preserved.")
    print()


def run_config_init(
    *,
    config,
    config_exists,
    config_path,
    base_dir,
    dry_run=False,
    yes=False,
    analytics=False,
):
    plan = build_plan(config, config_exists, config_path, base_dir)
    print_intro(plan)
    if analytics:
        print("Analytics (bundled InfluxDB) will be enabled.")
        print()

    prompt_enabled = not dry_run and not yes
    if prompt_enabled and not ask_confirm("Continue?", True):
        print("Aborted.")
        return None, plan

    # --analytics is a Docker-first bootstrap flag: it must seed a fresh/template
    # config unattended (enabling Analytics) without forcing real device values,
    # so it tolerates template placeholders the same way --dry-run does. Plain
    # `config init --yes` still rejects placeholders.
    allow_placeholder_defaults = dry_run or (yes and analytics)
    updated = apply_answers(
        plan["base_config"],
        plan["template_config"],
        noninteractive=(dry_run or yes),
        allow_placeholder_defaults=allow_placeholder_defaults,
        analytics=analytics,
    )
    print_summary(updated, config_path)

    if dry_run:
        print()
        print("Dry run: config preview follows. No files were written.")
        safe_preview = render_redacted_config_json(
            updated,
            plan.get("layout"),
        )
        os.write(1, safe_preview.encode())
        return None, plan

    if prompt_enabled and not ask_confirm("Continue?", True):
        print("Aborted.")
        return None, plan

    return updated, plan
