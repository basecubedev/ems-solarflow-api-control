# SPDX-License-Identifier: AGPL-3.0-or-later
"""A device that cannot store its PV is not competing to charge.

PV-first allocation biases export toward the fuller battery, on the reasoning
that the emptier one should keep its PV and charge instead. A device with no
battery reports ``electricLevel 0``, so it is permanently the emptiest device in
the plant and collects the full penalty -- for a question it has no stake in.
It cannot charge, so PV it is not allowed to export is not stored, it is lost.

Measured on the shipped defaults with A (battery, 400 W PV, SoC 50) and B (no
battery, 800 W PV): at a 600 W request B received 200 W and A received 400 W,
although B carried two thirds of the plant's PV. At the equally valid
``pv_charge_balance_strength = 1.0`` B received exactly nothing, always.

Two corrections follow, and they are the same sentence twice: the charge
balance has no question for a device that cannot charge, and PV-first priority
belongs to any device that cannot absorb its own PV -- which a full battery
already is, and a missing battery permanently is.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems.models import DeviceState
from ems.target_control import calculate_targets, detect_capabilities

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

# The values the template ships, not the module fallbacks.
SHIPPED_DEADBAND_PERCENT = 1.0
SHIPPED_FULL_BIAS_PERCENT = 15.0
SHIPPED_STRENGTH = 0.7


def device(name, max_power=800, pv_kwp=1.0, pv_priority_factor=1.0, battery_kwh=1.0):
    return SimpleNamespace(
        name=name,
        max_power=max_power,
        pv_kwp=pv_kwp,
        pv_priority_factor=pv_priority_factor,
        battery_kwh=battery_kwh,
        min_soc=15,
        max_soc=100,
    )


def state(soc, solar, pack_num, min_soc=15, max_soc=100, **overrides):
    values = dict(
        soc=soc,
        min_soc=min_soc,
        max_soc=max_soc,
        solar=solar,
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
        pack_num=pack_num,
    )
    values.update(overrides)
    return DeviceState(**values)


def allocate(
    states, requested_total, devices=None, strength=SHIPPED_STRENGTH, capabilities=None
):
    devices = devices or [device(f"WR{i + 1}") for i in range(len(states))]
    capabilities = capabilities or [detect_capabilities(item) for item in states]

    with patch.multiple(
        "ems.target_control.cfg",
        REDISTRIBUTE_CLAMPED_POWER=True,
        PV_KWP_WEIGHTING=True,
        BATTERY_KWH_WEIGHTING=True,
        PV_CHARGE_BALANCE_ENABLED=True,
        PV_CHARGE_BALANCE_DEADBAND_PERCENT=SHIPPED_DEADBAND_PERCENT,
        PV_CHARGE_BALANCE_FULL_BIAS_PERCENT=SHIPPED_FULL_BIAS_PERCENT,
        PV_CHARGE_BALANCE_STRENGTH=strength,
    ):
        targets, _, _ = calculate_targets(
            load=0,
            devices=states,
            max_power=1600,
            device_configs=devices,
            capabilities=capabilities,
            requested_total=requested_total,
        )

    return [round(value) for value in targets]


def mixed_plant():
    """A battery device with less PV beside a battery-less one with more."""

    return [state(soc=50, solar=400, pack_num=1), state(soc=0, solar=800, pack_num=0)]


# --- the battery-less device is served first, up to its own PV ---------------


@pytest.mark.parametrize(
    "requested,expected",
    [
        (200, [0, 200]),
        (600, [0, 600]),
        (1000, [200, 800]),
        (1200, [400, 800]),
    ],
)
def test_battery_less_device_is_served_before_the_battery(requested, expected):
    """Its surplus is lost if withheld; the battery's is merely stored later."""

    assert allocate(mixed_plant(), requested) == expected


def test_full_strength_no_longer_starves_the_battery_less_device():
    """At strength 1.0 the penalty was total: B received exactly zero."""

    assert allocate(mixed_plant(), 600, strength=1.0) == [0, 600]


def test_allocation_never_exceeds_the_devices_own_pv():
    """Priority is a claim on the requested total, not a licence to over-commit."""

    targets = allocate(mixed_plant(), 1200)

    assert targets[1] <= 800


# --- nothing changes for plants made only of batteries -----------------------


@pytest.mark.parametrize(
    "requested,expected",
    [(600, [200, 400]), (1000, [333, 667])],
)
def test_battery_only_plant_is_unchanged(requested, expected):
    both = [state(soc=50, solar=400, pack_num=1), state(soc=50, solar=800, pack_num=2)]

    assert allocate(both, requested) == expected


def test_an_empty_battery_still_keeps_its_pv():
    """The penalty is right for a real battery: it should charge, not export.

    This is the boundary the change must not cross. Same telemetry as the
    battery-less device apart from the pack count, and the opposite outcome.
    """

    plant = [state(soc=50, solar=400, pack_num=1), state(soc=0, solar=800, pack_num=1)]

    assert allocate(plant, 600) == [400, 200]


def test_unknown_battery_presence_behaves_as_before():
    """Silence must not buy the new treatment."""

    plant = [
        state(soc=50, solar=400, pack_num=1),
        state(soc=0, solar=800, pack_num=None),
    ]

    assert allocate(plant, 600) == [400, 200]


def test_a_lone_battery_less_device_is_unaffected():
    assert allocate([state(soc=0, solar=800, pack_num=0)], 600) == [600]


# --- the device's own limits still bind --------------------------------------


def test_device_max_power_still_caps_the_priority_claim():
    plant = mixed_plant()
    devices = [device("WR1"), device("WR2", max_power=500)]

    assert allocate(plant, 1000, devices=devices) == [500, 500]


def test_pv_priority_factor_still_orders_two_battery_less_devices():
    plant = [state(soc=0, solar=400, pack_num=0), state(soc=0, solar=400, pack_num=0)]
    devices = [device("WR1", pv_priority_factor=1.0), device("WR2", pv_priority_factor=3.0)]

    first, second = allocate(plant, 400, devices=devices)

    assert second > first


def test_priority_still_honours_a_blocked_export_capability():
    """Priority is granted inside the export gate, never around it.

    A runtime reservation blocks output by clearing ``can_export``; a
    battery-less device must not slip past that on the strength of its new
    priority. Telemetry alone cannot produce the state -- a device reporting PV
    always reads as export-capable -- so the capability is passed explicitly,
    exactly as the controller does for a reserved device.
    """

    from dataclasses import replace

    plant = mixed_plant()
    capabilities = [detect_capabilities(item) for item in plant]
    capabilities[1] = replace(capabilities[1], can_export=False, can_discharge=False)

    assert allocate(plant, 600, capabilities=capabilities)[1] == 0


# --- night idle: there is no battery to be blocked by ------------------------


def _strict_idle(item):
    from ems.controller import EMSController

    return EMSController.state_is_strict_night_min_soc_idle(None, item)


def test_a_battery_less_device_at_rest_is_idle_whatever_soc_it_reports():
    """The plant's idle decision is an ``all()`` over its controlled devices.

    A battery-less device reporting any SoC above ``minSoc`` -- a default, an
    artefact, anything -- used to hold the whole plant out of night idle for
    good. It has no battery that could still deliver, so it never had a reason
    to.
    """

    for reported_soc in (0, 50, 100):
        assert _strict_idle(state(soc=reported_soc, solar=0, pack_num=0)) is True


def test_a_battery_device_with_charge_left_still_blocks_idle():
    assert _strict_idle(state(soc=55, solar=0, pack_num=1)) is False


def test_a_battery_device_at_its_floor_is_idle():
    assert _strict_idle(state(soc=10, solar=0, pack_num=1)) is True


def test_unknown_presence_still_decides_on_soc():
    assert _strict_idle(state(soc=55, solar=0, pack_num=None)) is False


def test_pv_still_keeps_a_battery_less_device_out_of_idle():
    """Idle is about having nothing to give, not about lacking a battery."""

    assert _strict_idle(state(soc=0, solar=300, pack_num=0)) is False
