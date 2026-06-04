#!/usr/bin/env python3
"""Safe runtime-state editor for ems-solarflow-api-control."""

import argparse
import getpass
import json
import math
import os
import re
import sys

from dashboard import auth as dashboard_auth


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_RUNTIME_STATE_PATH = os.path.join(BASE_DIR, "runtime-state.json")
OFFGRID_SOCKET_MODES = ("off", "eco", "standard")
TOP_LEVEL_COMMANDS = (
    "status",
    "system",
    "device",
    "ha",
    "ha-control",
    "winter",
    "dashboard",
    "interactive",
    "menu",
    "examples",
    "completion",
    "help",
)
SYSTEM_ACTIONS = (
    "enable",
    "disable",
    "max-power",
    "loop-interval",
    "min-output-limit",
)
DEVICE_ACTIONS = (
    "enable",
    "disable",
    "max-power",
    "offgrid",
    "pv-priority-factor",
)
DASHBOARD_ACTIONS = (
    "set-password",
    "change-password",
    "disable-auth",
    "auth-status",
)
COMPLETION_SHELLS = ("bash", "zsh")

TOP_LEVEL_EPILOG = """\
Command overview:
  status                         Show runtime-state.json
  system <action> [value]         Enable/disable EMS and tune global limits
  device <name> <action> [value]  Enable/disable devices and tune device values
  ha enable|disable               Toggle Home Assistant publishing
  ha-control enable|disable       Toggle Home Assistant helper control
  winter enable|disable|status    Toggle or inspect winter mode
  dashboard <command>             Manage dashboard write-mode authentication
  interactive                     Open a menu for common runtime edits
  examples                        Print a longer command cookbook
  completion bash|zsh             Generate optional shell completion

Common examples:
  python3 emsctl.py interactive
  python3 emsctl.py status
  python3 emsctl.py system disable
  python3 emsctl.py system max-power 1200
  python3 emsctl.py device WR1 max-power 600
  python3 emsctl.py device WR1 offgrid eco
  python3 emsctl.py winter enable
  python3 emsctl.py dashboard auth-status

Tip:
  Run `python3 emsctl.py` for a short start screen.
  Use `python3 emsctl.py examples` for a longer command cookbook.
  Use `python3 emsctl.py completion bash` to generate shell completion.
"""

QUICK_HELP_TEXT = """\
EMS Control CLI

Safely edits runtime-state.json only.
Does not contact Zendure hardware or Home Assistant.

Start here:
  python3 emsctl.py interactive
  python3 emsctl.py examples
  python3 emsctl.py --help

Common commands:
  python3 emsctl.py status
  python3 emsctl.py system disable
  python3 emsctl.py system max-power 1200
  python3 emsctl.py device WR1 max-power 600
  python3 emsctl.py device WR1 offgrid eco
  python3 emsctl.py dashboard auth-status
"""

EXAMPLES_TEXT = """\
EMS runtime control CLI examples

Interactive mode:
  python3 emsctl.py interactive

Status:
  python3 emsctl.py status

System runtime control:
  python3 emsctl.py system enable
  python3 emsctl.py system disable
  python3 emsctl.py system max-power 1200
  python3 emsctl.py system min-output-limit 35
  python3 emsctl.py system loop-interval 5

Device runtime control:
  python3 emsctl.py device WR1 enable
  python3 emsctl.py device WR1 disable
  python3 emsctl.py device WR1 max-power 600
  python3 emsctl.py device WR1 pv-priority-factor 1.2
  python3 emsctl.py device WR1 offgrid off
  python3 emsctl.py device WR1 offgrid eco
  python3 emsctl.py device WR1 offgrid standard

HA publishing / helper control:
  python3 emsctl.py ha enable
  python3 emsctl.py ha disable
  python3 emsctl.py ha-control enable
  python3 emsctl.py ha-control disable

Winter mode:
  python3 emsctl.py winter status
  python3 emsctl.py winter enable
  python3 emsctl.py winter disable

Dashboard authentication:
  python3 emsctl.py dashboard auth-status
  python3 emsctl.py dashboard set-password
  python3 emsctl.py dashboard change-password
  python3 emsctl.py dashboard disable-auth

Explicit config/runtime paths:
  python3 emsctl.py --config /etc/ems/config.json status
  python3 emsctl.py --runtime-state /var/lib/ems/runtime-state.json status
  python3 emsctl.py --config config.json --runtime-state runtime-state.json device WR1 max-power 600
  python3 emsctl.py --dashboard-auth config/dashboard-auth.json dashboard auth-status

Shell completion:
  source <(python3 emsctl.py completion bash)
  source <(python3 emsctl.py completion zsh)
"""


class EMSHelpFormatter(argparse.RawDescriptionHelpFormatter):
    pass


def fail(message, code=1):
    print(f"ERROR: {message}", file=sys.stderr)
    return code


def build_parser():
    parser = argparse.ArgumentParser(
        description="EMS runtime control CLI",
        epilog=TOP_LEVEL_EPILOG,
        formatter_class=EMSHelpFormatter,
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
    parser.add_argument(
        "--dashboard-auth",
        help="Path to dashboard-auth.json. Overrides dashboard auth_file config."
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "status",
        help="Print current runtime state.",
        description="Print the current runtime-state.json payload.",
        epilog="""\
Examples:
  python3 emsctl.py status
  python3 emsctl.py --config config.json status
""",
        formatter_class=EMSHelpFormatter,
    )

    system = subparsers.add_parser(
        "system",
        help="Edit system runtime state.",
        description="Edit global EMS runtime values.",
        epilog="""\
Actions:
  enable             Enable EMS runtime control
  disable            Disable EMS runtime control
  max-power VALUE    Set max_total_power in watts
  loop-interval SEC  Set loop_interval in seconds
  min-output-limit W Set min_output_limit in watts

Examples:
  python3 emsctl.py system disable
  python3 emsctl.py system max-power 1200
  python3 emsctl.py system loop-interval 5
""",
        formatter_class=EMSHelpFormatter,
    )
    system.add_argument(
        "action",
        choices=SYSTEM_ACTIONS,
        metavar="action",
        help="One of: " + ", ".join(SYSTEM_ACTIONS),
    )
    system.add_argument("value", nargs="?", help="Required for numeric actions.")

    device = subparsers.add_parser(
        "device",
        help="Edit device runtime state.",
        description="Edit per-device runtime values. Device names come from config.json.",
        epilog="""\
Actions:
  enable                         Enable a device
  disable                        Disable a device
  max-power VALUE                Set device max_power in watts
  offgrid off|eco|standard       Set offgrid socket mode
  pv-priority-factor VALUE       Set PV-first allocation weight

Examples:
  python3 emsctl.py device WR1 disable
  python3 emsctl.py device WR1 max-power 600
  python3 emsctl.py device WR1 offgrid eco
  python3 emsctl.py device WR1 pv-priority-factor 1.2
""",
        formatter_class=EMSHelpFormatter,
    )
    device.add_argument("name", help="Configured device name, for example WR1.")
    device.add_argument(
        "action",
        choices=DEVICE_ACTIONS,
        metavar="action",
        help="One of: " + ", ".join(DEVICE_ACTIONS),
    )
    device.add_argument("value", nargs="?", help="Required for max-power, offgrid, and pv-priority-factor.")

    ha = subparsers.add_parser(
        "ha",
        help="Edit HA runtime publishing state.",
        description="Enable or disable runtime Home Assistant publishing.",
        epilog="""\
Examples:
  python3 emsctl.py ha enable
  python3 emsctl.py ha disable
""",
        formatter_class=EMSHelpFormatter,
    )
    ha.add_argument("action", choices=["enable", "disable"], help="One of: enable, disable.")
    ha.add_argument("value", nargs="?", help=argparse.SUPPRESS)

    ha_control = subparsers.add_parser(
        "ha-control",
        help="Edit HA runtime helper-control state.",
        description="Enable or disable runtime Home Assistant helper control.",
        epilog="""\
Examples:
  python3 emsctl.py ha-control enable
  python3 emsctl.py ha-control disable
""",
        formatter_class=EMSHelpFormatter,
    )
    ha_control.add_argument("action", choices=["enable", "disable"], help="One of: enable, disable.")
    ha_control.add_argument("value", nargs="?", help=argparse.SUPPRESS)

    winter = subparsers.add_parser(
        "winter",
        help="Edit winter runtime state.",
        description="Enable, disable, or inspect winter runtime mode.",
        epilog="""\
Examples:
  python3 emsctl.py winter status
  python3 emsctl.py winter enable
  python3 emsctl.py winter disable
""",
        formatter_class=EMSHelpFormatter,
    )
    winter.add_argument("action", choices=["enable", "disable", "status"], help="One of: enable, disable, status.")
    winter.add_argument("value", nargs="?", help=argparse.SUPPRESS)

    dashboard = subparsers.add_parser(
        "dashboard",
        help="Manage dashboard authentication.",
        description="Manage local dashboard write-mode authentication.",
        epilog="""\
Examples:
  python3 emsctl.py dashboard auth-status
  python3 emsctl.py dashboard set-password
  python3 emsctl.py dashboard change-password
  python3 emsctl.py dashboard disable-auth

Password prompts do not echo input. Hidden automation flags are intentionally
omitted from normal help output.
""",
        formatter_class=EMSHelpFormatter,
    )
    dashboard_subparsers = dashboard.add_subparsers(
        dest="dashboard_command",
        required=True
    )

    set_password = dashboard_subparsers.add_parser(
        "set-password",
        help="Set dashboard admin password."
    )
    set_password.add_argument("--password", help=argparse.SUPPRESS)
    set_password.add_argument("--confirm-password", help=argparse.SUPPRESS)

    change_password = dashboard_subparsers.add_parser(
        "change-password",
        help="Change dashboard admin password."
    )
    change_password.add_argument("--current-password", help=argparse.SUPPRESS)
    change_password.add_argument("--new-password", help=argparse.SUPPRESS)
    change_password.add_argument("--confirm-password", help=argparse.SUPPRESS)

    dashboard_subparsers.add_parser(
        "disable-auth",
        help="Disable dashboard write-mode authentication."
    )
    dashboard_subparsers.add_parser(
        "auth-status",
        help="Show dashboard authentication status."
    )

    subparsers.add_parser(
        "interactive",
        help="Open a menu for common runtime edits.",
        description="Open a dependency-free interactive menu for common runtime edits.",
        epilog="""\
Examples:
  python3 emsctl.py interactive
  python3 emsctl.py --config config.json interactive
""",
        formatter_class=EMSHelpFormatter,
    )
    subparsers.add_parser(
        "menu",
        help="Alias for interactive.",
        description="Alias for the interactive menu.",
        formatter_class=EMSHelpFormatter,
    )

    subparsers.add_parser(
        "examples",
        help="Print a longer command cookbook.",
        description="Print practical emsctl.py examples without reading or writing runtime-state.",
        formatter_class=EMSHelpFormatter,
    )

    completion = subparsers.add_parser(
        "completion",
        help="Generate optional shell completion.",
        description="Generate shell completion code. Completion is optional and dependency-free.",
        epilog="""\
Examples:
  python3 emsctl.py completion bash
  python3 emsctl.py completion zsh
""",
        formatter_class=EMSHelpFormatter,
    )
    completion.add_argument("shell", choices=COMPLETION_SHELLS, help="Shell completion format to print.")

    subparsers.add_parser(
        "help",
        help="Show top-level help.",
        description="Show the same top-level help as --help.",
        formatter_class=EMSHelpFormatter,
    )

    return parser


def parse_args(argv=None):
    return build_parser().parse_args(argv)


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


def resolve_dashboard_auth_path(args, config):
    path = args.dashboard_auth or (
        config.get("dashboard", {})
        .get("auth_file", dashboard_auth.DEFAULT_AUTH_FILE)
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


def float_value(value, field, minimum=0.0):
    if value is None:
        raise ValueError(f"{field} requires a value")

    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc

    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")

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
            "offgrid_socket_mode": "off",
            "pv_priority_factor": float_value(
                item.get("pv_priority_factor", 1.0),
                f"devices.{name}.pv_priority_factor",
                minimum=0.01
            )
        }

    return devices


def config_device_names(config):
    names = []
    for item in config.get("devices", []):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name:
            names.append(str(name))
    return names


def safe_completion_words(words):
    safe_word = re.compile(r"^[A-Za-z0-9._@%+=:,/-]+$")
    return [str(word) for word in words if safe_word.fullmatch(str(word))]


def completion_word_list(words):
    return " ".join(safe_completion_words(words))


def completion_script_bash(config):
    commands = completion_word_list(TOP_LEVEL_COMMANDS)
    system_actions = completion_word_list(SYSTEM_ACTIONS)
    device_actions = completion_word_list(DEVICE_ACTIONS)
    ha_actions = "enable disable"
    winter_actions = "enable disable status"
    dashboard_actions = completion_word_list(DASHBOARD_ACTIONS)
    completion_shells = completion_word_list(COMPLETION_SHELLS)
    offgrid_modes = completion_word_list(OFFGRID_SOCKET_MODES)
    devices = completion_word_list(config_device_names(config))

    return f"""\
# Bash completion for emsctl.py
# Current shell:
#   source <(python3 emsctl.py completion bash)
# Persistent user install:
#   python3 emsctl.py completion bash > ~/.local/share/bash-completion/completions/emsctl

_emsctl_py_completion()
{{
  local cur prev command action
  COMPREPLY=()
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD-1]}}"

  local commands="{commands}"
  local system_actions="{system_actions}"
  local device_actions="{device_actions}"
  local ha_actions="{ha_actions}"
  local winter_actions="{winter_actions}"
  local dashboard_actions="{dashboard_actions}"
  local completion_shells="{completion_shells}"
  local offgrid_modes="{offgrid_modes}"
  local devices="{devices}"

  if [[ "$cur" == --* ]]; then
    COMPREPLY=( $(compgen -W "--config --runtime-state --dashboard-auth --help" -- "$cur") )
    return 0
  fi

  command=""
  for ((i = 1; i < COMP_CWORD; i++)); do
    case "${{COMP_WORDS[i]}}" in
      status|system|device|ha|ha-control|winter|dashboard|interactive|menu|examples|completion|help)
        command="${{COMP_WORDS[i]}}"
        break
        ;;
    esac
  done

  if [[ -z "$command" ]]; then
    COMPREPLY=( $(compgen -W "$commands --config --runtime-state --dashboard-auth" -- "$cur") )
    return 0
  fi

  case "$command" in
    system)
      COMPREPLY=( $(compgen -W "$system_actions" -- "$cur") )
      ;;
    device)
      if [[ "$prev" == "device" ]]; then
        COMPREPLY=( $(compgen -W "$devices" -- "$cur") )
      elif [[ " $device_actions " == *" $prev "* ]]; then
        if [[ "$prev" == "offgrid" ]]; then
          COMPREPLY=( $(compgen -W "$offgrid_modes" -- "$cur") )
        fi
      else
        COMPREPLY=( $(compgen -W "$device_actions" -- "$cur") )
      fi
      ;;
    ha|ha-control)
      COMPREPLY=( $(compgen -W "$ha_actions" -- "$cur") )
      ;;
    winter)
      COMPREPLY=( $(compgen -W "$winter_actions" -- "$cur") )
      ;;
    dashboard)
      COMPREPLY=( $(compgen -W "$dashboard_actions" -- "$cur") )
      ;;
    completion)
      COMPREPLY=( $(compgen -W "$completion_shells" -- "$cur") )
      ;;
  esac
}}

complete -F _emsctl_py_completion emsctl.py
complete -F _emsctl_py_completion emsctl
"""


def zsh_array(words):
    return " ".join(safe_completion_words(words))


def completion_script_zsh(config):
    commands = zsh_array(TOP_LEVEL_COMMANDS)
    system_actions = zsh_array(SYSTEM_ACTIONS)
    device_actions = zsh_array(DEVICE_ACTIONS)
    ha_actions = "enable disable"
    winter_actions = "enable disable status"
    dashboard_actions = zsh_array(DASHBOARD_ACTIONS)
    completion_shells = zsh_array(COMPLETION_SHELLS)
    offgrid_modes = zsh_array(OFFGRID_SOCKET_MODES)
    devices = zsh_array(config_device_names(config))

    return f"""\
#compdef emsctl.py emsctl
# Zsh completion for emsctl.py
# Current shell:
#   source <(python3 emsctl.py completion zsh)

_emsctl_py()
{{
  local -a commands system_actions device_actions ha_actions winter_actions dashboard_actions completion_shells offgrid_modes devices
  commands=({commands})
  system_actions=({system_actions})
  device_actions=({device_actions})
  ha_actions=({ha_actions})
  winter_actions=({winter_actions})
  dashboard_actions=({dashboard_actions})
  completion_shells=({completion_shells})
  offgrid_modes=({offgrid_modes})
  devices=({devices})

  if (( CURRENT == 2 )); then
    _describe 'command' commands
    return
  fi

  case "$words[2]" in
    system)
      _describe 'system action' system_actions
      ;;
    device)
      if (( CURRENT == 3 )); then
        _describe 'device' devices
      elif (( CURRENT == 4 )); then
        _describe 'device action' device_actions
      elif [[ "$words[4]" == "offgrid" ]]; then
        _describe 'offgrid mode' offgrid_modes
      fi
      ;;
    ha|ha-control)
      _describe 'action' ha_actions
      ;;
    winter)
      _describe 'winter action' winter_actions
      ;;
    dashboard)
      _describe 'dashboard command' dashboard_actions
      ;;
    completion)
      _describe 'shell' completion_shells
      ;;
  esac
}}

_emsctl_py "$@"
"""


def print_examples():
    print(EXAMPLES_TEXT.rstrip())


def print_quick_help():
    print(QUICK_HELP_TEXT.rstrip())


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
                    "offgrid_socket_mode": "off",
                    "pv_priority_factor": 1.0
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
        case "pv-priority-factor":
            device["pv_priority_factor"] = float_value(
                args.value,
                f"device {args.name} pv-priority-factor",
                minimum=0.01
            )
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


def prompt_new_password(args, password_attr="password"):
    password = getattr(args, password_attr, None)
    confirmation = getattr(args, "confirm_password", None)

    if password is None:
        password = getpass.getpass("New dashboard password: ")
    if confirmation is None:
        confirmation = getpass.getpass("Confirm dashboard password: ")

    if not password:
        raise ValueError("dashboard password must not be empty")
    if password != confirmation:
        raise ValueError("dashboard password confirmation does not match")
    if len(password) < 8:
        print(
            "WARNING: dashboard password is shorter than 8 characters.",
            file=sys.stderr,
        )

    return password


def handle_dashboard_command(args, config):
    auth_path = resolve_dashboard_auth_path(args, config)
    command = args.dashboard_command

    if command == "auth-status":
        configured = dashboard_auth.auth_configured(auth_path)
        if configured:
            print("Dashboard auth: configured")
            print("Dashboard write mode: available after login")
        else:
            print("Dashboard auth: not configured")
            print("Dashboard write mode: unavailable")
        return 0

    if command == "disable-auth":
        dashboard_auth.remove_auth_file(auth_path)
        print("Dashboard auth: not configured")
        print("Dashboard write mode: unavailable")
        return 0

    if command == "set-password":
        password = prompt_new_password(args)
        dashboard_auth.write_password_file(auth_path, password)
        print(f"dashboard auth configured: {auth_path}")
        return 0

    if command == "change-password":
        if not dashboard_auth.auth_configured(auth_path):
            raise ValueError("dashboard auth is not configured")

        current_password = args.current_password
        if current_password is None:
            current_password = getpass.getpass("Current dashboard password: ")

        if not dashboard_auth.verify_password_file(auth_path, current_password):
            raise ValueError("current dashboard password is incorrect")

        password = prompt_new_password(args, password_attr="new_password")
        dashboard_auth.write_password_file(auth_path, password)
        print(f"dashboard auth updated: {auth_path}")
        return 0

    raise ValueError(f"unknown dashboard command {command}")


def make_args(**kwargs):
    return argparse.Namespace(**kwargs)


def prompt_text(label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        return None
    if value == "" and default is not None:
        return str(default)
    return value


def prompt_choice(title, options):
    print()
    print(title)
    for index, (key, label) in enumerate(options, start=1):
        print(f"  {index}. {label} ({key})")

    keys = {key: key for key, _ in options}
    while True:
        raw = prompt_text("Choice")
        if raw is None:
            return "quit"
        value = raw.strip().lower().replace(" ", "-")
        if value in ("q", "quit", "exit"):
            return "quit"
        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(options):
                return options[index - 1][0]
        if value in keys:
            return keys[value]
        print("ERROR: invalid choice; enter a number, command key, or quit.")


def prompt_device_name(state):
    devices = state.get("devices", {})
    if not isinstance(devices, dict) or not devices:
        print("ERROR: no configured devices found.")
        return None

    names = sorted(devices)
    print("Known devices: " + ", ".join(names))
    default = names[0] if len(names) == 1 else None
    name = prompt_text("Device", default=default)
    if name is None:
        return None
    if name not in devices:
        print(f"ERROR: unknown device {name}; known devices: {', '.join(names)}")
        return None
    return name


def save_interactive(runtime_path, state):
    save_atomic(runtime_path, state)
    print(f"updated {runtime_path}")


def run_interactive(args, config):
    runtime_path = resolve_runtime_path(args, config)
    state, _ = load_runtime_state(runtime_path, config)

    menu_items = [
        ("status", "Show status"),
        ("system-enable", "System enable"),
        ("system-disable", "System disable"),
        ("system-max-power", "System max-power"),
        ("system-loop-interval", "System loop-interval"),
        ("system-min-output-limit", "System min-output-limit"),
        ("ha-enable", "HA publishing enable"),
        ("ha-disable", "HA publishing disable"),
        ("ha-control-enable", "HA helper-control enable"),
        ("ha-control-disable", "HA helper-control disable"),
        ("winter-status", "Winter status"),
        ("winter-enable", "Winter enable"),
        ("winter-disable", "Winter disable"),
        ("device-enable", "Device enable"),
        ("device-disable", "Device disable"),
        ("device-max-power", "Device max-power"),
        ("device-pv-priority-factor", "Device pv-priority-factor"),
        ("device-offgrid", "Device offgrid mode"),
        ("dashboard-auth-status", "Dashboard auth-status"),
        ("dashboard-set-password", "Dashboard set-password"),
        ("dashboard-change-password", "Dashboard change-password"),
        ("dashboard-disable-auth", "Dashboard disable-auth"),
        ("quit", "Quit"),
    ]

    print("EMS Control CLI interactive mode")
    print("Safely edits runtime-state.json only.")
    print("Does not contact Zendure hardware or Home Assistant.")

    while True:
        choice = prompt_choice("Select an action", menu_items)
        if choice == "quit":
            print("Bye.")
            return 0

        try:
            if choice == "status":
                print_status(runtime_path, state)
                continue

            if choice.startswith("system-"):
                action = choice.removeprefix("system-")
                value = None
                if action in ("max-power", "loop-interval", "min-output-limit"):
                    value = prompt_text(f"system {action}")
                    if value is None:
                        continue
                update_system(make_args(action=action, value=value), state)
                save_interactive(runtime_path, state)
                continue

            if choice.startswith("ha-control-"):
                action = choice.removeprefix("ha-control-")
                set_bool_section(make_args(action=action, value=None), state, "ha", "control_enabled")
                save_interactive(runtime_path, state)
                continue

            if choice.startswith("ha-"):
                action = choice.removeprefix("ha-")
                set_bool_section(make_args(action=action, value=None), state, "ha", "enabled")
                save_interactive(runtime_path, state)
                continue

            if choice == "winter-status":
                print_status(runtime_path, {"winter": state.get("winter", {})})
                continue

            if choice.startswith("winter-"):
                action = choice.removeprefix("winter-")
                set_bool_section(make_args(action=action, value=None), state, "winter", "enabled")
                save_interactive(runtime_path, state)
                continue

            if choice.startswith("device-"):
                action = choice.removeprefix("device-")
                name = prompt_device_name(state)
                if name is None:
                    continue
                value = None
                if action in ("max-power", "pv-priority-factor"):
                    value = prompt_text(f"device {name} {action}")
                    if value is None:
                        continue
                elif action == "offgrid":
                    value = prompt_text("Offgrid mode (off, eco, standard)")
                    if value is None:
                        continue
                update_device(make_args(name=name, action=action, value=value), state)
                save_interactive(runtime_path, state)
                continue

            if choice.startswith("dashboard-"):
                dashboard_command = choice.removeprefix("dashboard-")
                handle_dashboard_command(
                    make_args(
                        dashboard_auth=args.dashboard_auth,
                        dashboard_command=dashboard_command,
                        password=None,
                        confirm_password=None,
                        current_password=None,
                        new_password=None,
                    ),
                    config,
                )
                continue

            print(f"ERROR: unsupported action {choice}")

        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print_quick_help()
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)

        if args.command == "help":
            parser.print_help()
            return 0

        if args.command == "examples":
            print_examples()
            return 0

        if args.command in ("interactive", "menu"):
            return run_interactive(args, config)

        if args.command == "completion":
            if args.shell == "bash":
                print(completion_script_bash(config), end="")
            elif args.shell == "zsh":
                print(completion_script_zsh(config), end="")
            else:
                raise ValueError(f"unknown completion shell {args.shell}")
            return 0

        if args.command == "dashboard":
            return handle_dashboard_command(args, config)

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
