# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided first-run config assistant."""

import copy
import json
import os

from ems import config as config_mod


GRID_METER_CHOICES = (
    ("shelly", "Shelly Pro 3EM / Shelly Plus/Pro Gen2/Gen3"),
    ("shelly_3em_gen1", "Shelly 3EM Gen1"),
    ("ecotracker", "EcoTracker"),
    ("tasmota_http", "Tasmota HTTP / SmartMeter reader"),
)
SUPPORTED_GRID_METER_TYPES = tuple(value for value, _ in GRID_METER_CHOICES)

SECRET_KEYS = {"token"}


class ConfigInitError(ValueError):
    """Raised for setup input or planning errors."""


def _load_template(base_dir):
    path = os.path.join(base_dir, "config.template.json")
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


def _print(prompt):
    print(prompt, end="", flush=True)


def _read_line(prompt):
    _print(prompt)
    try:
        return input("").strip()
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
        default_index = SUPPORTED_GRID_METER_TYPES.index(current_type) + 1
        while True:
            raw = ask_text("Choice", str(default_index), required=True)
            if raw.isdigit() and 1 <= int(raw) <= len(GRID_METER_CHOICES):
                meter_type = GRID_METER_CHOICES[int(raw) - 1][0]
                break
            print("Please enter one of the listed numbers.")

    result = copy.deepcopy(existing)
    result["type"] = meter_type
    if meter_type == "tasmota_http":
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
            f"PV size connected to this device in kWp",
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
        ha["token"] = ask_text(
            "Home Assistant token",
            ha.get("token") or "",
            required=True,
            noninteractive=noninteractive,
            allow_placeholder_default=allow_placeholder_defaults,
        )


def apply_answers(
    base_config,
    template_config,
    *,
    noninteractive=False,
    allow_placeholder_defaults=False,
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
    return updated


def _meter_summary(grid_meter):
    meter_type = grid_meter.get("type", "shelly")
    if meter_type == "tasmota_http":
        target = grid_meter.get("url") or grid_meter.get("ip") or "(not set)"
        return f"Tasmota HTTP at {target}"
    label = {
        "shelly": "Shelly",
        "shelly_3em_gen1": "Shelly 3EM Gen1",
        "ecotracker": "EcoTracker",
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
):
    plan = build_plan(config, config_exists, config_path, base_dir)
    print_intro(plan)

    prompt_enabled = not dry_run and not yes
    if prompt_enabled and not ask_confirm("Continue?", True):
        print("Aborted.")
        return None, plan

    updated = apply_answers(
        plan["base_config"],
        plan["template_config"],
        noninteractive=(dry_run or yes),
        allow_placeholder_defaults=dry_run,
    )
    print_summary(updated, config_path)

    if dry_run:
        print()
        print("Dry run: config preview follows. No files were written.")
        print(
            config_mod.render_config_json(
                redacted_config(updated),
                plan.get("layout"),
            ),
            end="",
        )
        return None, plan

    if prompt_enabled and not ask_confirm("Continue?", True):
        print("Aborted.")
        return None, plan

    return updated, plan
