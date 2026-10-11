# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for the emsctl CLI and diagnose test modules.

Underscore-prefixed so pytest does not collect it as a test module. Imported by
both tests/test_emsctl_cli.py and tests/test_diagnostics.py.
"""
import json
import math
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import emsctl
from ems import paths as ems_paths
from ems.clients import zero_device_state
from ems.control_status import ControlStatusWriter
from ems.target_control import ControlExplanation, DeviceControlExplanation

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
                "ip": "192.0.2.20",
                "sn": "SN-TEST-0001",
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
    """Write runtime-state.json as the EMS keeps it: operator state only."""

    payload = {
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
                "enabled": True,
                "max_power": 800,
                "pv_priority_factor": 1.0,
            }
        },
    }
    for key, value in overrides.items():
        payload[key] = value
    (tmp_path / "runtime-state.json").write_text(json.dumps(payload))
    return payload


DEFAULT_LIVE_DEVICE = {
    "soc": 55,
    "min_soc": 15,
    "output_w": 130,
    "output_limit_w": 800,
    "allocated_target_w": 130,
}

_LIVE_STATE_FIELDS = {
    "soc": "soc",
    "min_soc": "min_soc",
    "pv_input_w": "solar",
    "output_w": "output",
    "output_limit_w": "output_limit",
    "battery_charge_w": "pack_out",
    "battery_discharge_w": "pack_in",
}


class LiveEms:
    """A stand-in for the running EMS that publishes through the real writer.

    ``devices`` maps a name to its live fields; ``None`` is a device that never
    answered. ``measured_at`` dates the meter's readings: ``None`` for a meter
    without a health record, ``"per_cycle"`` for one read fresh every cycle,
    ``"frozen"`` for one that kept serving its first reading.
    """

    def __init__(
        self,
        directory,
        *,
        devices=None,
        filtered_load_w=131,
        effective_target_total_w=130,
        measured_at=None,
        read_failures=0,
        loop_interval=5,
    ):
        devices = {"WR1": DEFAULT_LIVE_DEVICE} if devices is None else devices
        self.filtered_load_w = filtered_load_w
        self.effective_target_total_w = effective_target_total_w
        self.measured_at = measured_at
        self.health = None
        if measured_at is not None:
            self.health = SimpleNamespace(consecutive_failures=read_failures, measured=None)
            self.health.age_seconds = lambda now: now - self.health.measured
        self.decided = {}
        last_states = {}
        for name, fields in devices.items():
            if fields is None:
                continue
            last_states[name] = replace(
                zero_device_state(),
                **{
                    state_field: fields[key]
                    for key, state_field in _LIVE_STATE_FIELDS.items()
                    if key in fields
                },
            )
            self.decided[name] = DeviceControlExplanation(
                device=name,
                online=fields.get("online", True),
                pv_input_w=fields.get("pv_input_w", 0),
                output_w=fields.get("output_w", 0),
                soc=fields.get("soc"),
                min_soc=fields.get("min_soc"),
                max_soc=100,
                allocated_target_w=fields.get("allocated_target_w"),
                effective_target_w=fields.get("effective_target_w", fields.get("allocated_target_w")),
                limiting_reason=fields.get("limiting_reason"),
            )
        self.controller = SimpleNamespace(
            devices=[
                SimpleNamespace(name=name, max_power=(fields or {}).get("max_power_w", 800))
                for name, fields in devices.items()
            ],
            last_states=last_states,
            device_online={
                name: bool(fields) and fields.get("online", True)
                for name, fields in devices.items()
            },
            last_seen={},
            cycle_failures=0,
            shelly=SimpleNamespace(health=self.health),
            runtime_system_int=lambda key, default, minimum=0: loop_interval,
        )
        self.writer = ControlStatusWriter(
            ems_paths.resolve_control_status_path(Path(directory) / "runtime-state.json")
        )

    def cycle(self, grid_power_w, cycle_time):
        """Run one cycle at ``cycle_time`` (epoch seconds) and publish it."""

        if self.health is not None and (
            self.measured_at == "per_cycle" or self.health.measured is None
        ):
            self.health.measured = cycle_time
        controller = self.controller
        controller.last_fetch_at = cycle_time
        controller.last_seen = {name: cycle_time for name in controller.last_states}
        controller.last_control_explanation = ControlExplanation(
            mode="pv_first",
            requested_total_w=self.effective_target_total_w,
            effective_target_total_w=self.effective_target_total_w,
            allocated_target_total_w=self.effective_target_total_w,
            commanded_total_w=self.effective_target_total_w,
            devices=self.decided,
            load_w=grid_power_w,
            filtered_load_w=self.filtered_load_w,
        )
        assert self.writer.write(controller, now=cycle_time, now_monotonic=cycle_time)
        return json.loads(self.writer.path.read_text())


def write_live_control_status(directory, *, grid=(142,), age_seconds=0, loop_interval=5, **options):
    """Write control-status.json beside runtime-state.json as a running EMS does.

    Each entry of ``grid`` is one control cycle, ``loop_interval`` apart, the
    last one ``age_seconds`` ago. The other options are :class:`LiveEms`'s.
    """

    ems = LiveEms(directory, loop_interval=loop_interval, **options)
    last_cycle = math.floor(time.time() - age_seconds) + 0.5
    status = None
    for index, value in enumerate(grid):
        status = ems.cycle(value, last_cycle - loop_interval * (len(grid) - 1 - index))
    return status


def write_two_device_config(path):
    write_config(path)
    config = json.loads(path.read_text())
    config["devices"] = [
        {"name": "WR1", "ip": "192.0.2.20", "sn": "SN-TEST-0001", "max_power": 800, "pv_priority_factor": 1.0, "min_soc": 15},
        {"name": "WR2", "ip": "192.0.2.21", "sn": "SN-TEST-0002", "max_power": 800, "pv_priority_factor": 1.0, "min_soc": 15},
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
