# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for the emsctl CLI and diagnose test modules.

Underscore-prefixed so pytest does not collect it as a test module. Imported by
both tests/test_emsctl_cli.py and tests/test_diagnostics.py.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import emsctl
from ems import paths as ems_paths

ROOT = Path(__file__).resolve().parents[1]
EMSCTL = ROOT / "emsctl.py"


def write_config(path):
    path.write_text(json.dumps({
        "system": {
            "enabled": True,
            "max_total_power": 900,
            "max_device_power": 800,
            "loop_interval": 5,
            "min_output_limit": 35,
            "runtime_state_path": "runtime-state.json",
        },
        "ha": {
            "enabled": True,
            "control_enabled": True,
            "url": "http://homeassistant.local:8123",
            "token": "test-token",
        },
        "winter": {
            "enabled": False,
        },
        "devices": [
            {
                "name": "WR1",
                "max_power": 800,
                "pv_priority_factor": 1.1,
            }
        ],
        "grid_meter": {
            "type": "shelly",
            "ip": "192.0.2.10",
        },
    }))


def run_emsctl(tmp_path, *args, input_text=None):
    config_path = tmp_path / "config.json"
    runtime_path = tmp_path / "runtime-state.json"
    if not config_path.exists():
        write_config(config_path)

    return subprocess.run(
        [
            sys.executable,
            str(EMSCTL),
            "--config",
            str(config_path),
            "--runtime-state",
            str(runtime_path),
            "--dashboard-auth",
            str(tmp_path / "dashboard-auth.json"),
            *args,
        ],
        cwd=ROOT,
        text=True,
        input=input_text,
        capture_output=True,
        check=False,
    )


def run_emsctl_no_args(tmp_path):
    return subprocess.run(
        [sys.executable, str(EMSCTL)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_diagnose_help_discovery(output):
    for expected in (
        "diagnose --deep",
        "diagnose --hardware",
        "diagnose --control",
        "diagnose --control-quality",
        "diagnose --support-bundle",
    ):
        assert expected in output


def assert_diagnose_option_flags(output):
    for expected in (
        "--sample-seconds",
        "--json",
        "--output",
    ):
        assert expected in output


def runtime_state(tmp_path):
    return json.loads((tmp_path / "runtime-state.json").read_text())


def write_control_runtime(tmp_path, **overrides):
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "grid_power_w": 142,
        "filtered_load_w": 131,
        "inverter_output_w": 130,
        "controller": {
            "enabled": True,
            "effective_target_total_w": 130,
            "commanded_total_w": 130,
            "filtered_load_w": 131,
        },
        "system": {
            "enabled": True,
            "max_total_power": 900,
            "min_output_limit": 35,
            "loop_interval": 5,
        },
        "winter": {
            "enabled": False,
        },
        "devices": {
            "WR1": {
                "online": True,
                "enabled": True,
                "soc": 55,
                "min_soc": 15,
                "allocated_target_w": 130,
                "target_w": 130,
                "output_w": 130,
                "max_power": 800,
                "output_limit_w": 800,
            }
        },
    }
    for key, value in overrides.items():
        payload[key] = value
    (tmp_path / "runtime-state.json").write_text(json.dumps(payload))
    return payload


def write_two_device_config(path):
    write_config(path)
    config = json.loads(path.read_text())
    config["devices"] = [
        {"name": "WR1", "max_power": 800, "pv_priority_factor": 1.0, "min_soc": 15},
        {"name": "WR2", "max_power": 800, "pv_priority_factor": 1.0, "min_soc": 15},
    ]
    path.write_text(json.dumps(config))
    return config


def write_discovery_config(path, runtime_state_path=None, auth_file=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "system": {},
        "dashboard": {},
        "devices": [],
    }
    if runtime_state_path is not None:
        payload["system"]["runtime_state_path"] = runtime_state_path
    if auth_file is not None:
        payload["dashboard"]["auth_file"] = auth_file
    path.write_text(json.dumps(payload))


def patch_emsctl_base(monkeypatch, base_dir):
    monkeypatch.setattr(emsctl, "BASE_DIR", str(base_dir))
    # resolve_runtime_path / resolve_dashboard_auth_path now live in ems.paths
    # and read its module-level BASE_DIR.
    monkeypatch.setattr(ems_paths, "BASE_DIR", str(base_dir))
    monkeypatch.setattr(
        emsctl,
        "DEFAULT_CONFIG_PATH",
        str(base_dir / "config.json"),
    )
    monkeypatch.setattr(
        emsctl,
        "DOCKER_CONFIG_PATH",
        str(base_dir / "config" / "config.json"),
    )


def config_args(config=None, runtime_state=None, dashboard_auth=None):
    return SimpleNamespace(
        config=config,
        runtime_state=runtime_state,
        dashboard_auth=dashboard_auth,
    )


def diagnose_args(tmp_path, **overrides):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["grid_meter"] = {"type": "ha"}
    config_path.write_text(json.dumps(config))
    write_control_runtime(tmp_path)
    values = {
        "config": str(config_path),
        "runtime_state": str(tmp_path / "runtime-state.json"),
        "dashboard_auth": str(tmp_path / "dashboard-auth.json"),
        "deep": False,
        "hardware": False,
        "support_bundle": False,
        "control": False,
        "control_quality": False,
        "quality": False,
        "sample_seconds": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)
