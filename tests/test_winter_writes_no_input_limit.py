# SPDX-License-Identifier: AGPL-3.0-or-later
"""Winter mode writes no ``inputLimit`` (K16).

Each daily winter step also wrote ``inputLimit = winter.ac_charge_power``
(200 W), meant to keep the grid charge a raised ``minSoc`` sets off small. In
output mode the firmware ignores it: on 2026-10-03 both inverters charged at
1.2 to 1.4 kW with it in place. In AC input mode the runtime intent reconciler
is the only owner of ``inputLimit``, and winter's 200 W replaced the charge
power the operator set until the reconciler wrote it back. The write is gone,
and with it ``winter.ac_charge_power``, which nothing else read but a sensor
that echoed it.
"""

import json
import logging
import shutil
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.clients import parse_device
from ems.config import AC_CHARGE_CONTROL_DEFAULTS
from ems.controller import EMSController
from tests.test_ac_charge_regulator import FollowingHardware, following_http_device
from tests.test_off_means_off import Runtime
from tests.test_write_gates import ShellyStub

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

BEFORE_NOON = datetime(2026, 11, 3, 11, 0)
NOON = datetime(2026, 11, 3, 12, 5)
RETIRED = "ac_charge_power"


class WinterClock(datetime):
    """The loop's wall clock, moved by the test."""

    at = BEFORE_NOON

    @classmethod
    def now(cls, tz=None):
        at = cls.at
        return cls(at.year, at.month, at.day, at.hour, at.minute, tzinfo=tz)


class SocWindowHardware(FollowingHardware):
    """The following double, which also takes its SoC window from the writes."""

    def apply(self, properties):
        super().apply(properties)
        if "minSoc" in properties:
            self.state.min_soc = properties["minSoc"] / 10
        if "socSet" in properties:
            self.state.max_soc = properties["socSet"] / 10


def battery_only(ac_mode, input_limit):
    item = parse_device(
        {
            "properties": {
                "electricLevel": 20,
                "minSoc": 200,
                "socSet": 1000,
                "solarInputPower": 0,
                "outputLimit": 0,
                "inputLimit": input_limit,
                "packNum": 1,
                "acMode": ac_mode,
                "acStatus": 2 if ac_mode == 1 and input_limit else 1,
                "smartMode": 1,
            }
        }
    )
    item.grid_input = input_limit if ac_mode == 1 else 0
    return item


@contextmanager
def winter_loop():
    with (
        patch("ems.controller.datetime", WinterClock),
        patch.object(cfg, "WINTER_CONFIG", {**cfg.WINTER_DEFAULTS, "enabled": True}),
        patch.object(cfg, "ZENDURE_CONFIG", [{"name": "WR1", "pv_kwp": 0}]),
        patch.object(cfg, "DRY_RUN", False),
        patch.object(cfg, "SIMULATION_MODE", False),
        patch.object(cfg, "ARGS", SimpleNamespace(replay=None)),
        patch.object(cfg, "ALLOW_HARDWARE_WRITES", True),
        patch.object(cfg, "ALLOW_STATE_RECONCILIATION_WRITES", True),
        patch.object(cfg, "SYSTEM_ENABLED", True),
        patch.object(cfg, "MAX_TOTAL_POWER", 800),
        patch.object(cfg, "MAX_DEVICE_POWER", 800),
        patch.object(cfg, "MIN_OUTPUT_LIMIT", 0),
        patch.object(cfg, "DEADBAND", 10),
        patch.object(cfg, "SOC_RECONCILE_INTERVAL", 1),
        patch.object(cfg, "BATTERY_FULL_CHARGE_ASSIST_CONFIG", {
            **cfg.BATTERY_FULL_CHARGE_ASSIST_DEFAULTS, "enabled": False,
        }),
        patch.object(cfg, "AC_CHARGE_CONTROL_CONFIG", {
            **AC_CHARGE_CONTROL_DEFAULTS, "enabled": False,
        }),
    ):
        yield


def run_day(item, runtime):
    """One cycle before the step hour, then two in it; return the writes."""

    dev, hardware = following_http_device(
        item, hardware_profile="solarflow_800_pro_2", double=SocWindowHardware
    )
    controller = EMSController(
        devices=[dev],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=runtime,
    )

    def fetch(_devices):
        dev.fetch()
        return [item]

    with winter_loop(), patch("ems.controller.fetch_all_devices", side_effect=fetch):
        WinterClock.at = BEFORE_NOON
        controller.run_once()
        WinterClock.at = NOON
        controller.run_once()
        controller.run_once()
    return hardware.writes


def test_the_daily_step_leaves_the_operator_s_charge_power_to_its_owner():
    """AC input at 600 W from runtime state: the step wrote 200, the reconciler 600 back."""

    item = battery_only(ac_mode=1, input_limit=600)
    runtime = Runtime(devices={"WR1": {"runtime_role": "ac_input", "ac_charge_power_w": 600}})

    writes = run_day(item, runtime)

    assert [write["minSoc"] for write in writes if "minSoc" in write] == [230]
    assert [write["inputLimit"] for write in writes if "inputLimit" in write] == []
    assert item.input_limit_w == 600


def test_the_daily_step_writes_no_input_limit_in_output_mode():
    item = battery_only(ac_mode=2, input_limit=0)

    writes = run_day(item, Runtime())

    assert [write["minSoc"] for write in writes if "minSoc" in write] == [230]
    assert [write for write in writes if "inputLimit" in write] == []


def test_the_winter_charge_power_is_retired_everywhere():
    from ems.config_catalog import get_config_feature_field_index

    template = json.loads(open("config/config.template.json", encoding="utf-8").read())

    assert f"winter.{RETIRED}" not in get_config_feature_field_index()
    assert RETIRED not in template["winter"]
    assert RETIRED not in cfg.WINTER_DEFAULTS
    assert RETIRED not in cfg.default_runtime_config()["winter"]
    assert not hasattr(cfg, "build_winter_ac_charge_limit_payload")
    assert not hasattr(EMSController, "apply_winter_ac_charge_limit")


@pytest.mark.parametrize("value", [200, 0, "yes", None])
def test_a_config_that_still_carries_the_key_loads_and_starts_unchanged(
    tmp_path, capsys, caplog, value
):
    def loaded(with_key):
        config = cfg.default_runtime_config()
        config["winter"].pop(RETIRED, None)
        if with_key:
            config["winter"][RETIRED] = value
        config["config_upgrade"] = {"on_startup": "check"}
        (tmp_path / "config").mkdir(exist_ok=True)
        shutil.copy("config/config.template.json", tmp_path / "config" / "config.template.json")
        path = tmp_path / ("with.json" if with_key else "without.json")
        path.write_text(json.dumps(config))
        before = path.read_bytes()
        args = SimpleNamespace(
            config=str(path), dry_run=False, simulate=False, replay=None,
            self_test=False,
        )
        snapshot = {name: getattr(cfg, name) for name in dir(cfg) if name.isupper()}
        try:
            cfg.initialize(args, str(tmp_path))
            result = {
                key: item for key, item in cfg.WINTER_CONFIG.items() if key != RETIRED
            }
        finally:
            for name, saved in snapshot.items():
                setattr(cfg, name, saved)
        assert path.read_bytes() == before
        return result

    with caplog.at_level(logging.DEBUG):
        assert loaded(True) == loaded(False)

    assert RETIRED not in capsys.readouterr().err
    assert f"winter.{RETIRED}" not in caplog.text
