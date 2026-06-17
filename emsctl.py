#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Safe runtime-state editor for ems-solarflow-api-control."""

import argparse
import shutil
import sqlite3
import subprocess
import statistics
import getpass
import json
import math
import os
import platform
import re
import sys
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dashboard import auth as dashboard_auth


from ems.paths import (
    BASE_DIR,
    resolve_project_path,
    resolve_runtime_path,
    resolve_dashboard_auth_path,
)

from ems.diagnostics import (
    run_diagnosis,
    run_install_diagnosis,
    run_deep_diagnosis,
    run_hardware_diagnosis,
    run_control_diagnosis,
    run_control_quality_diagnosis,
    diagnose_text,
    diagnose_json_file,
    diagnose_write_support_bundle,
)

DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DOCKER_CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
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
    "influx",
    "stack",
    "diagnose",
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
    "ac-mode",
    "ac-charge-power",
)
DEVICE_AC_MODE_VALUES = ("output", "input")
DEVICE_AC_MODE_RUNTIME_ROLES = {
    "output": "ac_output",
    "input": "ac_input",
}
DEVICE_AC_MODE_ROLE_ALIASES = {
    "normal_output": "ac_output",
    "ac_output": "ac_output",
    "ac_input_charge": "ac_input",
    "reserved": "ac_input",
    "ac_input": "ac_input",
}
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
  diagnose [--json]               Run read-only local diagnostics
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
  python3 emsctl.py device WR1 ac-mode output
  python3 emsctl.py device WR1 ac-charge-power 200
  python3 emsctl.py winter enable
  python3 emsctl.py dashboard auth-status
  python3 emsctl.py diagnose
  python3 emsctl.py diagnose --deep
  python3 emsctl.py diagnose --hardware
  python3 emsctl.py diagnose --control
  python3 emsctl.py diagnose --control-quality --sample-seconds 60
  python3 emsctl.py diagnose --support-bundle

Tip:
  Run `python3 emsctl.py` for a short start screen.
  Use `python3 emsctl.py examples` for a longer command cookbook.
  Use `python3 emsctl.py completion bash` to generate shell completion.
"""

QUICK_HELP_TEXT = """\
EMS Control CLI

Usage:
  python3 emsctl.py <command> [options]

Start here:
  python3 emsctl.py status
  python3 emsctl.py diagnose
  python3 emsctl.py examples

Common commands:
  python3 emsctl.py status
  python3 emsctl.py interactive
  python3 emsctl.py examples

Diagnostics:
  python3 emsctl.py diagnose
      Run basic installation and runtime checks.
  python3 emsctl.py diagnose --deep
      Run extended runtime, database, log, and dashboard checks.
  python3 emsctl.py diagnose --hardware
      Run read-only hardware connectivity checks.
  python3 emsctl.py diagnose --control
      Explain current EMS control decisions.
  python3 emsctl.py diagnose --control-quality --sample-seconds 60
      Measure export/import quality, PV usage, and SOC balancing.
  python3 emsctl.py diagnose --support-bundle
      Create a redacted support bundle for GitHub issues.

  Use --json for machine-readable output.
  Use --output <path> together with --support-bundle.

Runtime control:
  python3 emsctl.py system disable
  python3 emsctl.py system max-power 1200
  python3 emsctl.py device WR1 max-power 600
  python3 emsctl.py device WR1 ac-mode input
  python3 emsctl.py device WR1 ac-charge-power 200
  python3 emsctl.py device WR1 offgrid eco

Dashboard:
  python3 emsctl.py dashboard auth-status
  python3 emsctl.py dashboard set-password

More help:
  python3 emsctl.py --help
  python3 emsctl.py examples
  python3 emsctl.py diagnose --help
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
  python3 emsctl.py device WR1 ac-mode output
  python3 emsctl.py device WR1 ac-mode input
  python3 emsctl.py device WR1 ac-charge-power 200
  python3 emsctl.py device WR1 ac-charge-power 0
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

Diagnostics:
  python3 emsctl.py diagnose
  python3 emsctl.py diagnose --deep
  python3 emsctl.py diagnose --hardware
  python3 emsctl.py diagnose --control
  python3 emsctl.py diagnose --control --sample-seconds 30
  python3 emsctl.py diagnose --control-quality --sample-seconds 60
  python3 emsctl.py diagnose --quality --json
  python3 emsctl.py diagnose --json
  python3 emsctl.py diagnose --support-bundle
  python3 emsctl.py diagnose --support-bundle --output /tmp/ems-support.zip

Docker diagnostics:
  docker compose exec ems python3 emsctl.py diagnose
  docker compose exec ems python3 emsctl.py diagnose --control
  docker compose exec ems python3 emsctl.py diagnose --control-quality --sample-seconds 60
  docker compose exec ems python3 emsctl.py diagnose --support-bundle

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
        help=(
            "Path to config.json. Default discovery: EMS_CONFIG_FILE, "
            "config.json next to emsctl.py, then config/config.json."
        )
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
  ac-mode [output|input]         Set or show runtime AC mode role
  ac-charge-power WATTS          Set runtime AC charge inputLimit in watts

Examples:
  python3 emsctl.py device WR1 disable
  python3 emsctl.py device WR1 max-power 600
  python3 emsctl.py device WR1 ac-mode output
  python3 emsctl.py device WR1 ac-mode input
  python3 emsctl.py device WR1 ac-charge-power 200
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
    device.add_argument("value", nargs="?", help="Required for max-power, offgrid, pv-priority-factor, ac-mode writes, and ac-charge-power.")

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

    diagnose = subparsers.add_parser(
        "diagnose",
        help="Run read-only local diagnostics.",
        description=(
            "Run read-only diagnostics for config, runtime-state, data paths, "
            "and container mounts without contacting external services."
        ),
        epilog="""\
Examples:
  python3 emsctl.py diagnose
  python3 emsctl.py diagnose --json
  python3 emsctl.py diagnose --deep
  python3 emsctl.py diagnose --hardware
  python3 emsctl.py diagnose --control
  python3 emsctl.py diagnose --control --sample-seconds 30
  python3 emsctl.py diagnose --control-quality --sample-seconds 60
  python3 emsctl.py diagnose --quality --json
  python3 emsctl.py diagnose --support-bundle
  python3 emsctl.py diagnose --support-bundle --output /tmp/ems-support.zip
""",
        formatter_class=EMSHelpFormatter,
    )
    diagnose.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable diagnostic output.",
    )
    diagnose.add_argument(
        "--deep",
        action="store_true",
        help="Run deeper local read-only operational checks.",
    )
    diagnose.add_argument(
        "--hardware",
        action="store_true",
        help="Run optional short-timeout read-only hardware/network probes.",
    )
    diagnose.add_argument(
        "--support-bundle",
        action="store_true",
        help="Create a redacted ZIP support bundle.",
    )
    diagnose.add_argument(
        "--control",
        action="store_true",
        help="Explain current EMS control/regulation behavior from local state.",
    )
    diagnose.add_argument(
        "--control-quality",
        action="store_true",
        help="Evaluate zero-export, PV usage, and SOC balancing quality.",
    )
    diagnose.add_argument(
        "--quality",
        action="store_true",
        help="Alias for --control-quality.",
    )
    diagnose.add_argument(
        "--sample-seconds",
        type=int,
        default=0,
        help="Collect local runtime-state meter samples for control diagnostics.",
    )
    diagnose.add_argument(
        "--output",
        help="ZIP output path. Only valid with --support-bundle.",
    )

    influx = subparsers.add_parser(
        "influx",
        help="Set up, inspect or reconcile the InfluxDB history backend.",
        description=(
            "Manage the optional InfluxDB 2.x history backend. Config is the "
            "source of truth: 'init' sets up the bundled docker-compose "
            "InfluxDB zero-config (generate local secrets, start it, sync "
            "schema); 'sync' reconciles buckets, retention and downsampling "
            "tasks to match config.json; 'status' reports the live buckets, "
            "tasks and task health."
        ),
        epilog="""\
Examples:
  python3 emsctl.py influx init
  python3 emsctl.py influx init --no-start
  python3 emsctl.py influx init --no-sync --json
  python3 emsctl.py influx status
  python3 emsctl.py influx status --json
  python3 emsctl.py influx sync
  python3 emsctl.py influx sync --json
""",
        formatter_class=EMSHelpFormatter,
    )
    influx.add_argument(
        "action",
        choices=("init", "status", "sync"),
        help=(
            "init: complete Analytics setup (bundled: secrets, start, wait, "
            "sync; external: validate, check connectivity, sync). "
            "status: read live schema. sync: reconcile schema to config."
        ),
    )
    influx.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable output.",
    )
    influx.add_argument(
        "--no-start",
        action="store_true",
        help="init: create/merge secrets only; do not start the container.",
    )
    influx.add_argument(
        "--no-sync",
        action="store_true",
        help="init: skip the schema sync step after InfluxDB is ready.",
    )
    influx.add_argument(
        "--force-disabled",
        action="store_true",
        help="init: proceed even when influxdb.enabled is false in config.",
    )

    stack = subparsers.add_parser(
        "stack",
        help="Start the EMS docker-compose stack (with bundled InfluxDB).",
        description=(
            "Beginner-friendly host-side helper that starts the EMS "
            "docker-compose stack. With bundled InfluxDB enabled it also "
            "creates local secrets, includes the InfluxDB compose files and "
            "runs the schema sync, so Analytics works from one command. Runs "
            "docker on the host; the EMS controller never manages Docker."
        ),
        epilog="""\
Examples:
  python3 emsctl.py stack up
  python3 emsctl.py stack up --no-sync
  python3 emsctl.py stack up --json
""",
        formatter_class=EMSHelpFormatter,
    )
    stack.add_argument(
        "stack_action",
        choices=("up",),
        help="up: create secrets if needed, start containers, sync schema.",
    )
    stack.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable output.",
    )
    stack.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip the InfluxDB schema sync step.",
    )
    stack.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the docker compose command without running it.",
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


def resolve_config_path(args):
    if args.config:
        return args.config

    env_path = os.environ.get("EMS_CONFIG_FILE")
    if env_path:
        return env_path

    if os.path.exists(DEFAULT_CONFIG_PATH):
        return DEFAULT_CONFIG_PATH

    if os.path.exists(DOCKER_CONFIG_PATH):
        return DOCKER_CONFIG_PATH

    return DEFAULT_CONFIG_PATH


def print_diagnose_text(report):
    print(diagnose_text(report), end="")


def handle_diagnose_command(args):
    if args.output and not args.support_bundle:
        return fail("--output is only valid together with --support-bundle", code=2)
    if args.sample_seconds < 0:
        return fail("--sample-seconds must be >= 0", code=2)

    report = run_diagnosis(args)
    bundle_path = None
    if args.support_bundle:
        config_data, _ = diagnose_json_file(report["project"]["config_path"])
        bundle_path = diagnose_write_support_bundle(
            report,
            args,
            config_data if isinstance(config_data, dict) else {},
            report["project"]["runtime_state_path"],
        )
        report["support_bundle_path"] = bundle_path

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_diagnose_text(report)
        if bundle_path:
            print(f"Support bundle: {bundle_path}")
    return 1 if report["status"] == "error" else 0


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


def strict_int_value(value, field, minimum=0):
    if value is None:
        raise ValueError(f"{field} requires a value")

    text = str(value).strip()

    try:
        parsed = int(text, 10)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc

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
    ac_modes = completion_word_list(DEVICE_AC_MODE_VALUES)
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
  local ac_modes="{ac_modes}"
  local devices="{devices}"

  if [[ "$cur" == --* ]]; then
    COMPREPLY=( $(compgen -W "--config --runtime-state --dashboard-auth --help" -- "$cur") )
    return 0
  fi

  command=""
  for ((i = 1; i < COMP_CWORD; i++)); do
    case "${{COMP_WORDS[i]}}" in
      status|system|device|ha|ha-control|winter|dashboard|diagnose|interactive|menu|examples|completion|help)
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
        elif [[ "$prev" == "ac-mode" ]]; then
          COMPREPLY=( $(compgen -W "$ac_modes" -- "$cur") )
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
    ac_modes = zsh_array(DEVICE_AC_MODE_VALUES)
    devices = zsh_array(config_device_names(config))

    return f"""\
#compdef emsctl.py emsctl
# Zsh completion for emsctl.py
# Current shell:
#   source <(python3 emsctl.py completion zsh)

_emsctl_py()
{{
  local -a commands system_actions device_actions ha_actions winter_actions dashboard_actions completion_shells offgrid_modes ac_modes devices
  commands=({commands})
  system_actions=({system_actions})
  device_actions=({device_actions})
  ha_actions=({ha_actions})
  winter_actions=({winter_actions})
  dashboard_actions=({dashboard_actions})
  completion_shells=({completion_shells})
  offgrid_modes=({offgrid_modes})
  ac_modes=({ac_modes})
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
      elif [[ "$words[4]" == "ac-mode" ]]; then
        _describe 'AC mode' ac_modes
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


def normalize_runtime_role(value):
    role = str(value or "ac_output").strip().lower()
    return DEVICE_AC_MODE_ROLE_ALIASES.get(role)


def runtime_role_ac_mode_summary(role):
    if role == "ac_input":
        return {
            "role": "ac_input",
            "command": "input",
            "desired_ac_mode": 1,
            "output_control_allowed": False,
            "description": (
                "AC input/charge runtime role; normal EMS output allocation "
                "is blocked"
            ),
        }

    return {
        "role": "ac_output",
        "command": "output",
        "desired_ac_mode": 2,
        "output_control_allowed": True,
        "description": (
            "AC output/home-output runtime role; normal EMS output allocation "
            "is allowed"
        ),
    }


def print_device_ac_mode_status(device_name, device):
    role = normalize_runtime_role(device.get("runtime_role"))
    if role is None:
        role = "ac_output"
    summary = runtime_role_ac_mode_summary(role)
    print(json.dumps({
        "device": device_name,
        "runtime_role": summary["role"],
        "command": summary["command"],
        "desired_ac_mode": summary["desired_ac_mode"],
        "output_control_allowed": summary["output_control_allowed"],
        "description": summary["description"],
    }, indent=2, sort_keys=True))


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
        case "ac-mode":
            value = str(args.value or "").strip().lower()
            if value not in DEVICE_AC_MODE_RUNTIME_ROLES:
                raise ValueError("device ac-mode value must be 'output' or 'input'")
            device["runtime_role"] = DEVICE_AC_MODE_RUNTIME_ROLES[value]
            device["runtime_role_reason"] = "emsctl"
        case "ac-charge-power":
            device["ac_charge_power_w"] = strict_int_value(
                args.value,
                f"device {args.name} ac-charge-power",
                minimum=0
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


def resolve_influx_token_with_secret_file(influx_config):
    """Resolve a token from config/env, falling back to the bundled secret file.

    Running the bundled stack on the host means the ``token_env`` variable is
    usually only present inside the secret file, not the host environment, so
    bundled mode reads it from there when config/env do not provide one.
    """
    from ems.config import resolve_influx_token
    from ems import influx_setup

    token = resolve_influx_token(influx_config)
    if token:
        return token
    if influx_config.get("mode") == "bundled":
        return influx_setup.read_secret_file_token(influx_config)
    return ""


def run_docker_compose(command, cwd, dry_run=False, stdout_to_stderr=False):
    """Run a docker compose command on the host. Returns the exit code.

    Kept thin and side-effect-only so it is easy to mock in unit tests, which
    never execute Docker.

    ``stdout_to_stderr`` redirects the child's stdout (and stderr) to this
    process's stderr. Docker Compose prints progress/status lines like
    ``Container ems-influxdb Running`` to stdout; in ``--json`` mode those would
    corrupt the single JSON document we emit, so callers pass
    ``stdout_to_stderr=args.json`` to keep stdout a clean JSON channel while the
    human-readable Docker output is still shown (on stderr).
    """
    import subprocess

    # Trace goes to stderr so stdout stays a clean single JSON object for
    # --json consumers (and piping into jq).
    print("+ " + " ".join(command), file=sys.stderr)
    if dry_run:
        return 0
    stdout_target = sys.stderr if stdout_to_stderr else None
    stderr_target = sys.stderr if stdout_to_stderr else None
    try:
        completed = subprocess.run(
            command, cwd=cwd, stdout=stdout_target, stderr=stderr_target
        )
    except FileNotFoundError:
        print(
            "ERROR: 'docker' not found. Install Docker (with the compose "
            "plugin) or start the stack manually.",
            file=sys.stderr,
        )
        return 1
    return completed.returncode


def execute_influx_schema_op(influx_config, action):
    """Run a schema 'sync'/'status' op without printing. Returns (code, result).

    ``result`` is a single dict — ``{"action", "ok", "url", "report"|"error"}`` —
    so callers (the standalone command, ``influx init``, ``stack up``) embed the
    data in their own single JSON object instead of this function emitting a
    second JSON fragment to stdout.

    Host-side ops connect via the bundled ``host_url`` so the Docker compose
    service name in ``influxdb.url`` does not need to resolve on the host.
    """
    from ems.history import schema
    from ems.history.influx_client import HistoryInfluxClient, wait_for_influx_ready
    from ems import influx_setup

    url = influx_setup.host_cli_url(influx_config)
    token = resolve_influx_token_with_secret_file(influx_config)
    if not token:
        return 2, {
            "action": action,
            "ok": False,
            "url": url,
            "error": (
                "no InfluxDB token: set influxdb.token, export the env var "
                f"named by influxdb.token_env ({influx_config['token_env']}), "
                "or run 'emsctl.py influx init' for the bundled backend"
            ),
        }

    client = HistoryInfluxClient(url, influx_config["org"], token)

    try:
        wait_for_influx_ready(client, timeout_s=15)
    except TimeoutError as exc:
        return 1, {"action": action, "ok": False, "url": url, "error": str(exc)}

    try:
        if action == "sync":
            report = schema.sync(client, influx_config)
        else:
            report = schema.status(client, influx_config)
    except Exception as exc:  # network/HTTP errors surface as a clean failure
        return 1, {
            "action": action,
            "ok": False,
            "url": url,
            "error": f"influx {action} failed: {exc}",
        }

    return 0, {"action": action, "ok": True, "url": url, "report": report}


def run_influx_schema_command(influx_config, action, json_output):
    """Standalone 'influx sync'/'influx status' handler (owns its own output)."""
    from ems import influx_setup

    code, result = execute_influx_schema_op(influx_config, action)

    # Surface the local data directory for the bundled backend so operators know
    # where history is persisted on disk (and what to include in backups).
    bundled = influx_config.get("mode") == "bundled"
    if action == "status" and bundled:
        result["data_dir"] = influx_setup.DEFAULT_DATA_DIR

    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return code

    if not result["ok"]:
        return fail(result["error"], code=code)

    if action == "sync":
        print_influx_sync(result["report"])
    else:
        print_influx_status(result["report"])
        if bundled:
            print(f"  data directory: {influx_setup.DEFAULT_DATA_DIR}")
    return code


def describe_token_status(influx_config):
    """Redacted token provenance for JSON output (never the raw value)."""
    from ems.config import resolve_influx_token
    from ems import influx_setup

    if str(influx_config.get("token", "")).strip():
        source = "config"
    elif resolve_influx_token(influx_config):
        source = "token_env"
    elif (
        influx_config.get("mode") == "bundled"
        and influx_setup.read_secret_file_token(influx_config)
    ):
        source = "secret_file"
    else:
        source = None

    present = source is not None
    return {
        "source": source,
        "present": present,
        "redacted": "********" if present else None,
    }


def handle_influx_command(args, config):
    from ems.config import normalize_influxdb_config

    influx_config = normalize_influxdb_config(config.get("influxdb"))

    if args.action == "init":
        return handle_influx_init(args, influx_config)

    if not influx_config["enabled"]:
        return fail(
            "influxdb is disabled in config (set influxdb.enabled = true)",
            code=2,
        )

    return run_influx_schema_command(influx_config, args.action, args.json)


# Shown when 'influx init' runs against a disabled config. Matches the dashboard
# hint so the operator sees one consistent instruction everywhere.
INFLUX_DISABLED_MESSAGE = (
    "InfluxDB is disabled in config.json. "
    "Enable influxdb.enabled to use Analytics history."
)


def handle_influx_init(args, influx_config):
    """Complete end-to-end setup for the configured InfluxDB backend.

    This is the one command an operator runs to make Analytics history work:

    - **bundled** mode generates local secrets, starts the container, waits for
      readiness and (when ``auto_sync``) reconciles the schema;
    - **external** mode never touches Docker — it validates the connection
      settings, checks connectivity and reconciles the schema when ``auto_sync``;
    - **disabled** config exits with a clear, actionable message.
    """
    if not influx_config["enabled"]:
        return handle_influx_init_disabled(args, influx_config)

    if influx_config["mode"] == "bundled":
        return handle_influx_init_bundled(args, influx_config)

    return handle_influx_init_external(args, influx_config)


def _influx_init_fail(args, influx_config, message, code=2):
    """Emit an ``influx init`` failure as text (stderr) or a single JSON object.

    Keeping the JSON-mode error inside one object preserves the "exactly one
    JSON document on stdout" contract for ``influx init --json``.
    """
    if args.json:
        print(json.dumps(
            {
                "ok": False,
                "command": "influx init",
                "enabled": influx_config["enabled"],
                "mode": influx_config["mode"],
                "started": False,
                "ready": False,
                "synced": False,
                "errors": [message],
            },
            indent=2,
            sort_keys=True,
        ))
        return code
    return fail(message, code=code)


def handle_influx_init_disabled(args, influx_config):
    """Disabled InfluxDB: print a helpful message; do not start Docker or sync.

    The only meaningful action while disabled is the secret-file-only bootstrap
    (``--force-disabled --no-start``) so an operator can pre-stage bundled
    secrets before flipping ``influxdb.enabled`` to true.
    """
    if args.force_disabled and influx_config["mode"] == "bundled":
        if not args.no_start:
            return _influx_init_fail(
                args,
                influx_config,
                "InfluxDB is disabled. Use --force-disabled --no-start to only "
                "create the secret file, or enable influxdb.enabled=true before "
                "starting bundled InfluxDB.",
            )
        # Secret-file-only path: reuse the bundled flow, which honours
        # --no-start and therefore never starts Docker or runs a sync.
        return handle_influx_init_bundled(args, influx_config)

    return _influx_init_fail(args, influx_config, INFLUX_DISABLED_MESSAGE)


def handle_influx_init_bundled(args, influx_config):
    """Bundled docker-compose InfluxDB: secrets -> start -> wait -> sync."""
    from ems import influx_setup
    from ems.paths import BASE_DIR

    # The bundled Compose overlays read a fixed env_file path, so a custom
    # secret_file would write secrets the container never sees. Refuse to start
    # in that case; --no-start (secret file only) is still allowed.
    if not args.no_start and not influx_setup.uses_default_secret_file(influx_config):
        return _influx_init_fail(
            args,
            influx_config,
            "influxdb.secret_file is not the default "
            f"'{influx_setup.DEFAULT_SECRET_FILE}'. The bundled Docker Compose "
            "overlays currently only read that path, so a custom secret_file "
            "would not reach the containers. Use --no-start to only write the "
            f"secret file, or set secret_file to '{influx_setup.DEFAULT_SECRET_FILE}'.",
        )

    # 1. Create/merge the local secret env file (idempotent, never overwrites).
    secret_report = influx_setup.ensure_secret_file(influx_config)

    # 2. Start bundled InfluxDB unless --no-start. Use the full compose file
    # set (base first) so env_file paths resolve against the repo root, but
    # bring up only the influxdb service.
    files = influx_setup.compose_files(influx_config)
    started = False
    if not args.no_start:
        # Pre-create the local bind-mount target so the container writes its DB
        # state under ./data/influxdb (idempotent).
        influx_setup.ensure_data_dir()
        command = influx_setup.build_compose_command(
            files, action=("up", "-d", "influxdb")
        )
        code = run_docker_compose(
            command, cwd=BASE_DIR, stdout_to_stderr=args.json
        )
        if code != 0:
            return _influx_init_fail(
                args,
                influx_config,
                "failed to start bundled InfluxDB via docker compose",
                code=1,
            )
        started = True

    # 3 + 4. Wait for readiness and reconcile the schema (when auto_sync). When
    # the schema is not synced we still run a readiness probe so 'ready' is
    # truthful and the operator gets a final status check. The nested op returns
    # its data so it stays inside this command's single JSON object.
    sync_ran = False
    ready = False
    sync_result = None
    errors = []
    do_sync = started and not args.no_sync and influx_config["auto_sync"]
    if do_sync:
        rc, sync_result = execute_influx_schema_op(influx_config, "sync")
        if rc == 0:
            sync_ran = True
            ready = True
        else:
            errors.append(sync_result["error"])
    elif started:
        # Final readiness check without changing the schema (auto_sync off or
        # --no-sync). A failed probe is not fatal: the container may still be
        # coming up and the operator is told to run 'influx sync' next.
        rc, _ = execute_influx_schema_op(influx_config, "status")
        ready = rc == 0

    ok = not errors
    payload = {
        "ok": ok,
        "command": "influx init",
        "enabled": influx_config["enabled"],
        "mode": influx_config["mode"],
        "secret_file": secret_report["relative_path"],
        "secret_file_created": secret_report["created"],
        "generated_keys": secret_report["generated_keys"],
        "summary": secret_report["summary"],
        "compose_files": files,
        "data_dir": influx_setup.DEFAULT_DATA_DIR,
        "started": started,
        "start_skipped": args.no_start,
        "ready": ready,
        "synced": sync_ran,
        "sync_skipped": not do_sync,
        "token": describe_token_status(influx_config),
        "errors": errors,
    }
    if sync_result is not None and sync_result.get("ok"):
        payload["sync_result"] = sync_result["report"]

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ok else 1

    if not ok:
        return fail(errors[0])

    print_influx_init(secret_report, started, sync_ran, ready, args)
    return 0


def handle_influx_init_external(args, influx_config):
    """External InfluxDB: validate settings, check connectivity, sync if enabled.

    External InfluxDB is user-managed, so this never starts or stops Docker. It
    confirms the connection settings are present, verifies the server is
    reachable and (when ``auto_sync``) reconciles buckets/retention/tasks.
    """
    from ems import influx_setup

    token_status = describe_token_status(influx_config)
    missing = []
    if not influx_config["url"]:
        missing.append("influxdb.url")
    if not influx_config["org"]:
        missing.append("influxdb.org")
    if not influx_config["bucket_prefix"]:
        missing.append("influxdb.bucket_prefix")
    if not token_status["present"]:
        missing.append("influxdb.token or the influxdb.token_env env var")

    errors = []
    ready = False
    synced = False
    sync_result = None
    do_sync = not args.no_sync and influx_config["auto_sync"]

    if missing:
        errors.append(
            "external InfluxDB is not fully configured; set: " + ", ".join(missing)
        )
    else:
        # 'sync' reconciles the schema; 'status' is a connectivity check only.
        # Both wait for readiness first, so either confirms reachability.
        action = "sync" if do_sync else "status"
        rc, result = execute_influx_schema_op(influx_config, action)
        if rc == 0:
            ready = True
            sync_result = result
            synced = do_sync
        else:
            errors.append(result["error"])

    ok = not errors
    payload = {
        "ok": ok,
        "command": "influx init",
        "enabled": influx_config["enabled"],
        "mode": "external",
        "user_managed": True,
        "url": influx_setup.host_cli_url(influx_config),
        "started": False,
        "start_skipped": True,
        "ready": ready,
        "synced": synced,
        "sync_skipped": not do_sync,
        "token": token_status,
        "errors": errors,
    }
    if do_sync and sync_result is not None and sync_result.get("ok"):
        payload["sync_result"] = sync_result["report"]

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ok else 1

    if not ok:
        return fail(errors[0])

    print_influx_init_external(influx_config, ready, synced, do_sync)
    return 0


def print_influx_init(secret_report, started, sync_ran, ready, args):
    from ems import influx_setup

    print("InfluxDB bundled setup")
    verb = "created" if secret_report["created"] else "updated"
    print(f"  secret file: {secret_report['relative_path']} ({verb}, gitignored)")
    print(f"  data directory: {influx_setup.DEFAULT_DATA_DIR} (gitignored)")
    if secret_report["generated_keys"]:
        print(
            "  generated secrets: "
            + ", ".join(secret_report["generated_keys"])
            + " (redacted)"
        )
    else:
        print("  generated secrets: none (existing values preserved)")
    print("  values:")
    for key, value in secret_report["summary"].items():
        print(f"    {key}={value}")

    if started:
        print("  bundled InfluxDB: started (docker compose up -d influxdb)")
        print(f"  reachable: {'yes' if ready else 'not yet'}")
    else:
        print("  bundled InfluxDB: not started (--no-start)")

    if sync_ran:
        print("  schema sync: done")
    else:
        print("  schema sync: skipped")

    print("\nNext steps:")
    if not started:
        print("  - start the stack:   python3 emsctl.py stack up")
    elif not sync_ran:
        print("  - reconcile schema:  python3 emsctl.py influx sync")
    print("  - check status:      python3 emsctl.py influx status")
    if started and sync_ran:
        print("\nAnalytics history is ready in the dashboard.")


def print_influx_init_external(influx_config, ready, synced, do_sync):
    print("InfluxDB external setup")
    print(f"  url: {influx_config['url']}")
    print(f"  reachable: {'yes' if ready else 'no'}")
    if synced:
        print("  schema sync: done")
    elif do_sync:
        print("  schema sync: skipped")
    else:
        print("  schema sync: skipped (auto_sync=false)")
    print(
        "\nExternal InfluxDB is user-managed: emsctl does not start or stop it."
    )
    print("Next steps:")
    if not synced:
        print("  - reconcile schema:  python3 emsctl.py influx sync")
    print("  - check status:      python3 emsctl.py influx status")


def handle_stack_command(args, config):
    from ems.config import normalize_influxdb_config
    from ems import influx_setup
    from ems.paths import BASE_DIR

    influx_config = normalize_influxdb_config(config.get("influxdb"))
    bundled = (
        influx_config["enabled"] and influx_config["mode"] == "bundled"
    )

    # The bundled Compose overlays read a fixed env_file path, so a custom
    # secret_file would write secrets the container never sees. Refuse clearly.
    if bundled and not influx_setup.uses_default_secret_file(influx_config):
        return fail(
            "influxdb.secret_file is not the default "
            f"'{influx_setup.DEFAULT_SECRET_FILE}'. The bundled Docker Compose "
            "overlays currently only read that path. Set secret_file to "
            f"'{influx_setup.DEFAULT_SECRET_FILE}' to start the bundled stack.",
            code=2,
        )

    # 1 + 2. For bundled mode with auto_init, create local secrets first.
    # --dry-run only previews the docker command, so it stays side-effect-free.
    secret_report = None
    if bundled and influx_config["auto_init"] and not args.dry_run:
        secret_report = influx_setup.ensure_secret_file(influx_config)

    # Pre-create the local InfluxDB bind-mount target (idempotent). --dry-run
    # only previews the command, so it stays side-effect-free.
    if bundled and not args.dry_run:
        influx_setup.ensure_data_dir()

    # 3 + 4. Build and run the compose command with the right files.
    files = influx_setup.compose_files(influx_config)
    command = influx_setup.build_compose_command(files, action=("up", "-d"))
    code = run_docker_compose(
        command, cwd=BASE_DIR, dry_run=args.dry_run, stdout_to_stderr=args.json
    )
    if code != 0:
        return fail("docker compose up failed")
    if args.dry_run:
        if args.json:
            print(json.dumps(
                {
                    "ok": True,
                    "command": "stack up",
                    "bundled_influxdb": bundled,
                    "compose_files": files,
                    "dry_run": True,
                },
                indent=2,
                sort_keys=True,
            ))
        return 0

    # 5. Sync schema once InfluxDB is ready (bundled mode only). The nested op
    # returns its data so it stays inside this command's single JSON object.
    sync_ran = False
    sync_result = None
    errors = []
    do_sync = bundled and not args.no_sync and influx_config["auto_sync"]
    if do_sync:
        rc, sync_result = execute_influx_schema_op(influx_config, "sync")
        if rc == 0:
            sync_ran = True
        else:
            errors.append(sync_result["error"])

    ok = not errors
    if args.json:
        payload = {
            "ok": ok,
            "command": "stack up",
            "bundled_influxdb": bundled,
            "compose_files": files,
            "secret_file": (
                secret_report["relative_path"] if secret_report else None
            ),
            "secret_file_created": (
                secret_report["created"] if secret_report else None
            ),
            "data_dir": influx_setup.DEFAULT_DATA_DIR if bundled else None,
            "synced": sync_ran,
            "sync_skipped": not do_sync,
            "errors": errors,
        }
        if bundled:
            payload["token"] = describe_token_status(influx_config)
        if sync_result is not None and sync_result.get("ok"):
            payload["sync_result"] = sync_result["report"]
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ok else 1

    if not ok:
        return fail(errors[0])

    print("\nEMS stack started.")
    print("  dashboard: http://localhost:8080")
    if bundled:
        print("  influxdb:  http://localhost:8086")
        print(f"  data dir:  {influx_setup.DEFAULT_DATA_DIR} (gitignored)")
        if sync_ran:
            print("  analytics: ready (schema synced)")
        else:
            print("  analytics: run 'python3 emsctl.py influx sync' when ready")
    return 0


def print_influx_sync(report):
    print("InfluxDB schema sync")
    for bucket in report["buckets"]:
        retention = bucket["retention_seconds"]
        retention_label = (
            f"{retention // 86400}d" if retention else "infinite"
        )
        print(f"  bucket {bucket['name']}: {bucket['action']} (retention {retention_label})")
    for task in report["tasks"]:
        print(f"  task {task['name']}: {task['action']}")
    for task in report["disabled_tasks"]:
        print(f"  task {task['name']}: {task['action']}")
    if not report["tasks"] and not report["disabled_tasks"]:
        print("  no downsampling tasks configured")


def print_influx_status(report):
    print(f"InfluxDB schema status (prefix '{report['bucket_prefix']}')")
    print(f"  healthy: {'yes' if report['healthy'] else 'no'}")
    print("  buckets:")
    for bucket in report["buckets"]:
        if not bucket["exists"]:
            print(f"    {bucket['name']}: MISSING")
            continue
        retention = bucket["retention_seconds"] or 0
        retention_label = f"{retention // 86400}d" if retention else "infinite"
        print(f"    {bucket['name']}: ok (retention {retention_label})")
    print("  tasks:")
    if not report["tasks"]:
        print("    (none)")
    for task in report["tasks"]:
        print(
            f"    {task['name']}: status={task['status']} "
            f"last_run={task['last_run_status']} "
            f"latest_completed={task['latest_completed']}"
        )


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
        args.config = resolve_config_path(args)

        if args.command == "diagnose":
            return handle_diagnose_command(args)

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

        if args.command == "influx":
            return handle_influx_command(args, config)

        if args.command == "stack":
            return handle_stack_command(args, config)

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
            devices = state.get("devices", {})
            if args.action == "ac-mode" and args.value is None:
                if not isinstance(devices, dict) or args.name not in devices:
                    known = (
                        ", ".join(sorted(devices))
                        if isinstance(devices, dict)
                        else ""
                    )
                    raise ValueError(
                        f"unknown device {args.name}; known devices: {known or '(none)'}"
                    )
                if created:
                    save_atomic(runtime_path, state)
                print_device_ac_mode_status(args.name, devices[args.name])
                return 0
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
        if args.command == "device" and args.action == "ac-charge-power":
            watts = state["devices"][args.name]["ac_charge_power_w"]
            print(f"Set {args.name} AC charge power to {watts} W.")
        else:
            print(f"updated {runtime_path}")
        return 0

    except ValueError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
