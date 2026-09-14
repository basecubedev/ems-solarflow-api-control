# SPDX-License-Identifier: AGPL-3.0-or-later
"""Charge allocation: one primitive, a second weight function.

The discharge side already weights by usable energy above the floor and splits
the total with weighted_limited_allocation. Charging weights by the energy still
missing below the ceiling and splits it with the same primitive, so a change to
the allocation rules reaches both directions.
"""

from types import SimpleNamespace

import pytest

from ems.ac_charge_control import (
    allocate_charge_targets,
    charge_headroom_weight,
    resolve_max_charge_power_w,
)
from ems.models import DeviceCapabilities

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def _state(soc, max_soc=100):
    return SimpleNamespace(soc=soc, max_soc=max_soc)


def _device(max_power=800, max_charge_power_w=0, battery_kwh=2.0):
    return SimpleNamespace(
        max_power=max_power,
        max_charge_power_w=max_charge_power_w,
        battery_kwh=battery_kwh,
    )


def _capability(can_charge=True):
    return DeviceCapabilities(
        can_charge=can_charge,
        can_discharge=True,
        can_export=True,
        can_ac_charge=can_charge,
        reason="test",
    )


def test_weight_is_the_mirror_of_the_discharge_weight():
    # 40 percentage points of headroom on a 2 kWh battery.
    assert charge_headroom_weight(_state(60), _device(), _capability()) == pytest.approx(0.8)
    # A full device absorbs nothing.
    assert charge_headroom_weight(_state(100), _device(), _capability()) == 0
    # A firmware charge inhibit removes it regardless of headroom.
    assert charge_headroom_weight(_state(20), _device(), _capability(False)) == 0
    # An unknown ceiling is not an invitation to guess one.
    assert charge_headroom_weight(_state(20, max_soc=0), _device(), _capability()) == 0


def test_the_emptier_battery_takes_the_larger_share():
    states = [_state(20), _state(80)]
    devices = [_device(), _device()]
    capabilities = [_capability(), _capability()]

    targets = allocate_charge_targets(800, states, devices, capabilities, [True, True])

    assert sum(targets) == -800
    # 80 vs 20 points of headroom: four times the share.
    assert targets == [-640, -160]


def test_a_device_that_may_not_charge_is_skipped_entirely():
    states = [_state(20), _state(20)]
    devices = [_device(), _device()]
    capabilities = [_capability(), _capability()]

    targets = allocate_charge_targets(600, states, devices, capabilities, [True, False])

    assert targets == [-600, 0]


def test_a_per_device_limit_caps_its_share_and_the_rest_moves_on():
    states = [_state(20), _state(20)]
    devices = [_device(max_charge_power_w=200), _device()]
    capabilities = [_capability(), _capability()]

    targets = allocate_charge_targets(800, states, devices, capabilities, [True, True])

    assert targets[0] == -200
    assert targets[1] == -600


def test_nothing_is_allocated_when_no_device_can_take_it():
    states = [_state(100)]
    devices = [_device()]

    targets = allocate_charge_targets(500, states, devices, [_capability()], [True])

    assert targets == [0]


def test_an_explicit_limit_always_outranks_the_derived_one():
    """The precedence is the contract a later model catalogue must not break."""

    assert resolve_max_charge_power_w(_device(max_power=800)) == 800
    assert resolve_max_charge_power_w(_device(max_power=800, max_charge_power_w=300)) == 300
    # Zero means "derive", not "no charging".
    assert resolve_max_charge_power_w(_device(max_power=800, max_charge_power_w=0)) == 800
    # Unreadable values never invent a limit.
    assert resolve_max_charge_power_w(_device(max_power="?", max_charge_power_w="?")) == 0
