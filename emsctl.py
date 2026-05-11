#!/usr/bin/env python3
"""Safe runtime-state editor for ems-solarflow-api-control."""

import argparse
import json
import os
import sys


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_RUNTIME_STATE_PATH = os.path.join(BASE_DIR, "runtime-state.json")
OFFGRID_SOCKET_MODES = ("off", "eco", "standard")


def fail(message, code=1):
    print(f"ERROR: {message}", file=sys.stderr)
    return code


def parse_args():
    parser = argparse.ArgumentParser(
        description="Edit EMS runtime-state.json without HA or hardware access."
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.json. Default: config.json next to emsctl.py."
    )
    parser.add_argument(
        "--runtime-state",
        help="Path to runtime-state.json. Overrides config runtime_state_path."
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Print current runtime state.")

    system = subparsers.add_parser("system", help="Edit system runtime state.")
    system.add_argument(
        "action",
        choices=[
            "enable",
            "disable",
            "max-power",
            "loop-interval",
            "min-output-limit"
        ]
    )
    system.add_argument("value", nargs="?")

    device = subparsers.add_parser("device", help="Edit device runtime state.")
    device.add_argument("name")
    device.add_argument(
        "action",
        choices=["enable", "disable", "max-power", "offgrid"]
    )
    device.add_argument("value", nargs="?")

    ha = subparsers.add_parser("ha", help="Edit HA runtime publishing state.")
    ha.add_argument("action", choices=["enable", "disable"])
    ha.add_argument("value", nargs="?")

    ha_control = subparsers.add_parser(
        "ha-control",
        help="Edit HA runtime helper-control state."
    )
    ha_control.add_argument("action", choices=["enable", "disable"])
    ha_control.add_argument("value", nargs="?")

    winter = subparsers.add_parser("winter", help="Edit winter runtime state.")
    winter.add_argument("action", choices=["enable", "disable", "status"])
    winter.add_argument("value", nargs="?")

    return parser.parse_args()


def load_config(path):
    if not path or not os.path.exists(path):
        return {}

    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as exc:
        raise ValueError(f"cannot read config {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"config {path} must contain a JSON object")

    return data


def resolve_runtime_path(args, config):
    if args.runtime_state:
        path = args.runtime_state
    else:
        path = (
            config.get("system", {})
            .get("runtime_state_path", "runtime-state.json")
        )

    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)

    return path


def int_value(value, field, minimum=0):
    if value is None:
        raise ValueError(f"{field} requires a value")

    try:
        parsed = int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc

    if parsed < minimum:
        raise ValueError(f"{field} must be >= {minimum}")

    return parsed


def config_device_defaults(config):
    devices = {}
    max_device_power = (
        config.get("system", {})
        .get("max_device_power", 800)
    )

    for item in config.get("devices", []):
        if not isinstance(item, dict):
            continue

        name = item.get("name")
        if not name:
            continue

        devices[name] = {
            "enabled": True,
            "max_power": int_value(
                item.get("max_power", max_device_power),
                f"devices.{name}.max_power",
                minimum=0
            ),
            "offgrid_socket_mode": "off"
        }

    return devices


def runtime_defaults(config, existing=None):
    existing = existing if isinstance(existing, dict) else {}
    system = config.get("system", {})

    devices = config_device_defaults(config)
    existing_devices = existing.get("devices", {})

    if not devices and isinstance(existing_devices, dict):
        for name, value in existing_devices.items():
            if isinstance(value, dict):
                devices[name] = {
                    "enabled": True,
                    "max_power": 800,
                    "offgrid_socket_mode": "off"
                }

    return {
        "system": {
            "enabled": system.get("enabled", True),
            "max_total_power": int_value(
                system.get("max_total_power", 800),
                "system.max_total_power",
                minimum=0
            ),
            "loop_interval": int_value(
                system.get("loop_interval", 5),
                "system.loop_interval",
                minimum=1
            ),
            "min_output_limit": int_value(
                system.get("min_output_limit", 0),
                "system.min_output_limit",
                minimum=0
            )
        },
        "ha": {
            "enabled": config.get("ha", {}).get("enabled", True),
            "control_enabled": config.get("ha", {}).get(
                "control_enabled",
                True
            )
        },
        "winter": {
            "enabled": config.get("winter", {}).get("enabled", False)
        },
        "devices": devices
    }


def merge_defaults(data, defaults):
    if not isinstance(data, dict):
        data = {}

    merged = dict(data)

    system = merged.get("system")
    if not isinstance(system, dict):
        system = {}
    merged["system"] = {
        **defaults.get("system", {}),
        **system
    }

    for section_name in ("ha", "winter"):
        section = merged.get(section_name)
        if not isinstance(section, dict):
            section = {}
        merged[section_name] = {
            **defaults.get(section_name, {}),
            **section
        }

    devices = merged.get("devices")
    if not isinstance(devices, dict):
        devices = {}

    merged_devices = {}
    for name, default_device in defaults.get("devices", {}).items():
        device = devices.get(name)
        if not isinstance(device, dict):
            device = {}
        merged_devices[name] = {
            **default_device,
            **device
        }
        merged_devices[name].pop("offgrid_socket", None)

    for name, device in devices.items():
        if name not in merged_devices:
            merged_devices[name] = device
            if isinstance(merged_devices[name], dict):
                merged_devices[name].pop("offgrid_socket", None)

    merged["devices"] = merged_devices
    return merged


def load_runtime_state(path, config):
    if not os.path.exists(path):
        data = {}
        state = merge_defaults(data, runtime_defaults(config, data))
        return state, True

    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as exc:
        raise ValueError(f"cannot read runtime state {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"runtime state {path} must contain a JSON object")

    return merge_defaults(data, runtime_defaults(config, data)), False


def save_atomic(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, path)


def ensure_no_value(args):
    if args.value is not None:
        raise ValueError(f"{args.action} does not take a value")


def update_system(args, state):
    system = state.setdefault("system", {})

    match args.action:
        case "enable":
            ensure_no_value(args)
            system["enabled"] = True
        case "disable":
            ensure_no_value(args)
            system["enabled"] = False
        case "max-power":
            system["max_total_power"] = int_value(
                args.value,
                "system max-power",
                minimum=0
            )
        case "loop-interval":
            system["loop_interval"] = int_value(
                args.value,
                "system loop-interval",
                minimum=1
            )
        case "min-output-limit":
            system["min_output_limit"] = int_value(
                args.value,
                "system min-output-limit",
                minimum=0
            )
        case _:
            raise ValueError(f"unknown system action {args.action}")


def update_device(args, state):
    devices = state.setdefault("devices", {})
    if args.name not in devices:
        known = ", ".join(sorted(devices)) or "(none)"
        raise ValueError(f"unknown device {args.name}; known devices: {known}")

    device = devices[args.name]
    if not isinstance(device, dict):
        raise ValueError(f"device {args.name} runtime state must be an object")

    match args.action:
        case "enable":
            ensure_no_value(args)
            device["enabled"] = True
        case "disable":
            ensure_no_value(args)
            device["enabled"] = False
        case "max-power":
            device["max_power"] = int_value(
                args.value,
                f"device {args.name} max-power",
                minimum=0
            )
        case "offgrid":
            value = str(args.value or "").strip().lower()
            if value not in OFFGRID_SOCKET_MODES:
                raise ValueError(
                    "device offgrid value must be 'off', 'eco', or 'standard'"
                )
            device["offgrid_socket_mode"] = value
        case _:
            raise ValueError(f"unknown device action {args.action}")


def set_bool_section(args, state, section_name, key):
    ensure_no_value(args)
    section = state.setdefault(section_name, {})

    match args.action:
        case "enable":
            section[key] = True
        case "disable":
            section[key] = False
        case _:
            raise ValueError(f"unknown {section_name} action {args.action}")


def print_status(path, state):
    payload = {
        "runtime_state_path": path,
        "state": state
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def main():
    args = parse_args()

    try:
        config = load_config(args.config)
        runtime_path = resolve_runtime_path(args, config)
        state, created = load_runtime_state(runtime_path, config)

        if args.command == "status":
            if created:
                save_atomic(runtime_path, state)
            print_status(runtime_path, state)
            return 0

        if args.command == "winter" and args.action == "status":
            ensure_no_value(args)
            if created:
                save_atomic(runtime_path, state)
            print_status(runtime_path, {
                "winter": state.get("winter", {})
            })
            return 0

        if args.command == "system":
            update_system(args, state)
        elif args.command == "device":
            update_device(args, state)
        elif args.command == "ha":
            set_bool_section(args, state, "ha", "enabled")
        elif args.command == "ha-control":
            set_bool_section(args, state, "ha", "control_enabled")
        elif args.command == "winter":
            set_bool_section(args, state, "winter", "enabled")
        else:
            raise ValueError(f"unknown command {args.command}")

        save_atomic(runtime_path, state)
        print(f"updated {runtime_path}")
        return 0

    except ValueError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
