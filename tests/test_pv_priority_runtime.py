# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import emsctl
import pytest

from ems.controller import EMSController
from ems.models import DeviceState
from ems.runtime_state import build_runtime_defaults

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
EMSCTL = ROOT / "emsctl.py"


class RuntimeStateStub:
    def __init__(self, system=None, devices=None):
        self.system = system or {}
        self.devices = devices or {}

    def load_if_changed(self):
        return None

    def get_system(self, key, default=None):
        return self.system.get(key, default)

    def get_device(self, device_name, key, default=None):
        return self.devices.get(device_name, {}).get(key, default)


class ShellyStub:
    def get_power(self):
        return 0


def device(name="WR1", pv_priority_factor=1.0):
    return SimpleNamespace(
        name=name,
        max_power=800,
        pv_kwp=1.0,
        pv_priority_factor=pv_priority_factor,
        battery_kwh=1.0,
        min_soc=15,
        max_soc=100,
        smart_mode=1,
        grid_off_mode=None,
    )


def state():
    return DeviceState(
        soc=50,
        min_soc=15,
        max_soc=100,
        solar=400,
        output=0,
        pack_in=0,
        pack_out=0,
        temp=20,
        voltage=48,
        rssi=0,
        remain_minutes=0,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
        output_limit=0,
        soc_limit=0,
        pack_state=2,
        fault_level=0,
        smart_mode=1,
        grid_off_mode=0,
        ac_mode=2,
        ac_status=1,
        dc_status=1,
        grid_state=1,
    )


def write_config(path):
    path.write_text(json.dumps({
        "system": {
            "runtime_state_path": "runtime-state.json",
            "max_total_power": 800,
            "loop_interval": 5,
            "min_output_limit": 0,
        },
        "devices": [
            {
                "name": "WR1",
                "max_power": 800,
                "pv_priority_factor": 1.2,
            }
        ],
    }))


def run_emsctl(tmp_path, *args):
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
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_runtime_defaults_include_pv_priority_factor_from_device():
    with patch("ems.runtime_state.cfg.CONFIG", {"ha": {}}):
        defaults = build_runtime_defaults([
            device("WR1", pv_priority_factor=1.4)
        ])

    assert defaults["devices"]["WR1"]["pv_priority_factor"] == 1.4


def test_emsctl_defaults_include_pv_priority_factor_from_config():
    defaults = emsctl.config_device_defaults({
        "devices": [
            {
                "name": "WR1",
                "max_power": 800,
                "pv_priority_factor": 1.25,
            }
        ]
    })

    assert defaults["WR1"]["pv_priority_factor"] == 1.25


def test_emsctl_device_pv_priority_factor_writes_runtime_state(tmp_path):
    result = run_emsctl(
        tmp_path,
        "device",
        "WR1",
        "pv-priority-factor",
        "1.3",
    )

    assert result.returncode == 0, result.stderr

    runtime_state = json.loads((tmp_path / "runtime-state.json").read_text())
    assert runtime_state["devices"]["WR1"]["pv_priority_factor"] == 1.3


def test_emsctl_rejects_invalid_pv_priority_factor_without_writing(tmp_path):
    result = run_emsctl(
        tmp_path,
        "device",
        "WR1",
        "pv-priority-factor",
        "1.3",
    )
    assert result.returncode == 0, result.stderr

    runtime_path = tmp_path / "runtime-state.json"
    before = runtime_path.read_text()

    for value in (None, "abc", "0", "-1"):
        args = ["device", "WR1", "pv-priority-factor"]
        if value is not None:
            args.append(value)

        result = run_emsctl(tmp_path, *args)

        assert result.returncode != 0
        assert runtime_path.read_text() == before


def test_controller_applies_runtime_pv_priority_factor_before_fetch():
    dev = device("WR1", pv_priority_factor=1.0)
    runtime_state = RuntimeStateStub(
        devices={"WR1": {"pv_priority_factor": 1.5}}
    )
    controller = EMSController(
        devices=[dev],
        shelly=ShellyStub(),
        sleep_enabled=False,
        runtime_state=runtime_state,
    )
    controller.run_startup_ac_mode_reconcile_once = Mock()
    controller.set_output_limit = Mock()

    def fetch(devices):
        assert devices[0].pv_priority_factor == 1.5
        return [state()]

    with patch(
        "ems.controller.fetch_all_devices",
        side_effect=fetch,
    ), patch(
        "ems.controller.cfg.SYSTEM_ENABLED",
        False,
    ), patch(
        "ems.controller.cfg.SOC_RECONCILE_INTERVAL",
        0,
    ):
        controller.run_once()

    assert dev.pv_priority_factor == 1.5


def test_controller_keeps_static_pv_priority_factor_without_runtime_override():
    dev = device("WR1", pv_priority_factor=0.8)
    runtime_state = RuntimeStateStub(devices={"WR1": {}})
    controller = EMSController(
        devices=[dev],
        shelly=ShellyStub(),
        sleep_enabled=False,
        runtime_state=runtime_state,
    )
    controller.run_startup_ac_mode_reconcile_once = Mock()
    controller.set_output_limit = Mock()

    with patch(
        "ems.controller.fetch_all_devices",
        return_value=[state()],
    ), patch(
        "ems.controller.cfg.SYSTEM_ENABLED",
        False,
    ), patch(
        "ems.controller.cfg.SOC_RECONCILE_INTERVAL",
        0,
    ):
        controller.run_once()

    assert dev.pv_priority_factor == 0.8
