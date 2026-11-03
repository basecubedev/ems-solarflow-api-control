# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which parts of a device the dashboard draws: its PV input and its battery.

The answer is the device's energy class, the one the winter plan reads: a
battery is gone only once the device reports no packs, and PV is gone only
when the configuration says ``pv_kwp: 0``, because a dark array reads exactly
like none. A device that has not said either way keeps both.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dashboard.telemetry import build_dashboard_snapshot
from ems import config as cfg
from ems.e3dc_runtime import e3dc_device_state

pytestmark = [
    pytest.mark.contract,
]


def _state(**over):
    values = {"solar": 400, "output": 300, "pack_out": 110, "pack_in": 10, "soc": 60}
    values.update(over)
    return SimpleNamespace(**values)


def _snapshot(states, *, entries=(), read_only_devices=None, profiles=()):
    controller = SimpleNamespace(
        devices=[
            SimpleNamespace(name=f"WR{index + 1}", hardware_profile=(profiles[index] if index < len(profiles) else None))
            for index in range(len(states))
        ],
        runtime_state=None,
        device_online={f"WR{index + 1}": True for index in range(len(states))},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=None,
        read_only_devices=read_only_devices,
    )
    with patch.object(cfg, "ZENDURE_CONFIG", list(entries)):
        return build_dashboard_snapshot(
            controller,
            load_w=0,
            states=states,
            targets=[0] * len(states),
            effective_targets=[0] * len(states),
            allocated_total_w=0,
            effective_total_w=0,
            enabled=True,
            max_total_power=1600,
            min_output_limit=35,
        )


def test_a_device_reporting_packs_and_pv_has_both():
    snapshot = _snapshot([_state(pack_num=2)], entries=[{"name": "WR1", "pv_kwp": 1.6}])

    assert snapshot["devices"]["WR1"]["hardware"] == {"pv": True, "battery": True}


def test_a_device_reporting_no_packs_has_no_battery():
    snapshot = _snapshot([_state(pack_num=0, pack_out=0, pack_in=0)], entries=[{"name": "WR1"}])

    assert snapshot["devices"]["WR1"]["hardware"] == {"pv": True, "battery": False}


def test_a_device_configured_without_pv_has_no_pv():
    snapshot = _snapshot([_state(pack_num=1, solar=0)], entries=[{"name": "WR1", "pv_kwp": 0}])

    assert snapshot["devices"]["WR1"]["hardware"] == {"pv": False, "battery": True}


def test_a_device_that_has_not_said_keeps_its_battery_and_pv():
    """No pack count yet, no config entry: silence takes nothing away."""

    snapshot = _snapshot([_state(solar=0)])

    assert snapshot["devices"]["WR1"]["hardware"] == {"pv": True, "battery": True}


def test_each_device_answers_for_itself():
    snapshot = _snapshot(
        [_state(pack_num=0), _state(pack_num=2, solar=0)],
        entries=[{"name": "WR1", "pv_kwp": 0.8}, {"name": "WR2", "pv_kwp": 0}],
    )

    assert snapshot["devices"]["WR1"]["hardware"] == {"pv": True, "battery": False}
    assert snapshot["devices"]["WR2"]["hardware"] == {"pv": False, "battery": True}


def test_a_battery_only_model_has_no_pv_without_pv_kwp_0():
    snapshot = _snapshot([_state(pack_num=1, solar=0)], entries=[{"name": "WR1", "pv_kwp": 1.0}], profiles=["solarflow_2400_ac"])

    assert snapshot["devices"]["WR1"]["hardware"] == {"pv": False, "battery": True}


def test_a_model_with_pv_still_loses_the_battery_it_reports_no_packs_for():
    snapshot = _snapshot([_state(pack_num=0, pack_out=0, pack_in=0)], profiles=["solarflow_800_pro"])

    assert snapshot["devices"]["WR1"]["hardware"] == {"pv": True, "battery": False}


def test_a_local_device_is_known_by_the_product_its_report_names():
    controller_device = SimpleNamespace(name="WR1", resolved_hardware_profile=lambda: "solarflow_2400_ac_plus")
    controller = SimpleNamespace(
        devices=[controller_device],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=None,
        read_only_devices=None,
    )
    with patch.object(cfg, "ZENDURE_CONFIG", []):
        snapshot = build_dashboard_snapshot(
            controller, load_w=0, states=[_state(pack_num=1)], targets=[0], effective_targets=[0],
            allocated_total_w=0, effective_total_w=0, enabled=True, max_total_power=1600, min_output_limit=35,
        )

    assert snapshot["devices"]["WR1"]["hardware"] == {"pv": False, "battery": True}


def test_an_e3dc_tile_has_pv_and_a_battery():
    class Runtime:
        def tiles(self):
            return [{"name": "E3DC", "source": "e3dc", "online": False, "state": e3dc_device_state(None), "has_reading": False}]

    snapshot = _snapshot([_state(pack_num=1)], read_only_devices=Runtime())

    assert snapshot["devices"]["E3DC"]["hardware"] == {"pv": True, "battery": True}
