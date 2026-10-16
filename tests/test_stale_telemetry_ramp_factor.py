# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stale-telemetry ramp factor of 0 holds the total, it does not unlimit it (K-low).

``stale_telemetry_ramp_factor`` multiplies the total ramp while device
telemetry is stale, and the docs say a lower value is more conservative. The
product of a factor of 0 was a ramp limit of 0, which the ramp read as "no
limit": the most conservative setting let the total jump straight to the
desired value on telemetry it had just called stale. A configured ramp of 0
still means no limit, and a factor does not create one from it.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.ac_charge_control import ChargeDirectionState
from ems.controller import EMSController
from tests.test_failed_cycles_return_the_charge import Loop
from tests.test_write_gates import ShellyStub

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]

STATES = [SimpleNamespace(output_limit=0, output=0)]


def _total(factor, *, stale=True, ramp_up=300):
    output_control = {
        **cfg.OUTPUT_CONTROL_DEFAULTS,
        "filter_enabled": False,
        "load_deadband_w": 0,
        "target_deadband_w": 0,
        "ramp_enabled": True,
        "ramp_up_w_per_cycle": ramp_up,
        "large_import_bypass_w": 10_000,
        "large_export_bypass_w": 10_000,
        "stale_telemetry_ramp_factor": factor,
    }
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    controller.commanded_total_w = 100
    with patch.object(cfg, "OUTPUT_CONTROL_CONFIG", output_control), patch.object(
        controller, "telemetry_stale", return_value=stale
    ):
        return controller.stabilized_total_target(
            raw_load=500, states=STATES, max_power=2400
        )


def test_a_factor_of_zero_holds_the_total_while_telemetry_is_stale():
    assert _total(0) == 100


@pytest.mark.parametrize(("factor", "expected"), [(0.5, 250), (1.0, 400), (2.0, 600)])
def test_a_positive_factor_scales_the_ramp(factor, expected):
    assert _total(factor) == expected


def test_fresh_telemetry_ignores_the_factor():
    assert _total(0, stale=False) == 400


def test_an_unlimited_ramp_stays_unlimited_whatever_the_factor():
    assert _total(0, ramp_up=0) == 600
    assert _total(0.5, ramp_up=0) == 600


def _charging_total(factor, *, commanded, raw_load, ramp_up=300):
    """The total of a running AC charge one cycle on, telemetry stale."""

    output_control = {
        **cfg.OUTPUT_CONTROL_DEFAULTS,
        "filter_enabled": False,
        "load_deadband_w": 0,
        "target_deadband_w": 0,
        "ramp_enabled": True,
        "ramp_up_w_per_cycle": ramp_up,
        "ramp_down_w_per_cycle": 500,
        "large_import_bypass_w": 10_000,
        "large_export_bypass_w": 10_000,
        "stale_telemetry_ramp_factor": factor,
    }
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    controller.charge_direction = ChargeDirectionState(charging=True)
    controller.charge_capacity_w = 1200
    controller.commanded_total_w = commanded
    with patch.object(cfg, "OUTPUT_CONTROL_CONFIG", output_control), patch.object(
        controller, "telemetry_stale", return_value=True
    ):
        return controller.stabilized_total_target(
            raw_load=raw_load, states=STATES, max_power=2400
        )


@pytest.mark.parametrize("factor", [0, 0.5])
def test_the_factor_never_holds_or_slows_the_way_back_from_a_charge(factor):
    """A charge drawing from the grid is walked down at the configured ramp (R1b).

    One unreachable device keeps the telemetry stale for the whole outage, and a
    factor of 0 held a running AC charge with it: 300 W kept coming from the
    grid until the device answered again.
    """

    assert _charging_total(factor, commanded=-1000, raw_load=300) == -700


def test_the_way_back_from_a_charge_stops_at_idle_under_a_holding_factor():
    """Past idle the move is a discharge, which a factor of 0 holds."""

    assert _charging_total(0, commanded=-100, raw_load=500) == 0


def test_a_factor_of_zero_still_holds_a_charge_from_growing():
    assert _charging_total(0, commanded=-500, raw_load=-300) == -500


def test_a_charge_on_stale_telemetry_walks_down_in_the_loop_under_a_factor_of_zero():
    """The reported run: the surplus shrinks to import while one device keeps telemetry stale."""

    loop = Loop(load=-900)
    loop.run(cycles=20)
    item = loop.items["WR1"]
    assert item.ac_mode == 1
    charging = item.grid_input
    loop.load = 300
    loop.controller.shelly = ShellyStub(300)
    output_control = {**cfg.OUTPUT_CONTROL_CONFIG, "stale_telemetry_ramp_factor": 0}

    with patch.object(cfg, "OUTPUT_CONTROL_CONFIG", output_control), patch.object(
        loop.controller, "telemetry_stale", return_value=True
    ):
        loop.run(cycles=12)

    assert item.grid_input < charging
