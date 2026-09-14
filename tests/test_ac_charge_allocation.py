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
from ems.clients import parse_device
from ems.models import DeviceCapabilities

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def _state(soc, max_soc=100, pack_num=2, pack_in=0, charge_max_limit_w=1000):
    return SimpleNamespace(
        soc=soc,
        max_soc=max_soc,
        pack_num=pack_num,
        pack_in=pack_in,
        charge_max_limit_w=charge_max_limit_w,
    )


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


def test_a_device_without_a_battery_is_never_allocated_a_charge():
    """The mirrored formula is not mirrored in its safety.

    On the discharge side a device with no pack reads as "below the floor" and
    is skipped by accident. On the charge side the same arithmetic reads as
    "completely empty" and would hand it the whole charge — telling hardware
    with nowhere to put the energy to draw from the grid. Battery presence is
    taken from telemetry, the same way the full-charge assist takes it.
    """

    no_pack = _state(soc=0, pack_num=0)

    assert charge_headroom_weight(no_pack, _device(), _capability()) == 0
    assert allocate_charge_targets(
        600, [no_pack], [_device()], [_capability()], [True]
    ) == [0]


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


def test_the_charge_limit_comes_from_the_device_not_from_its_output_rating():
    """Feeding out and drawing in are different paths with different ratings.

    A SolarFlow 800 Pro 2 reports an 800 W output limit and a 1000 W charge
    ceiling, and the reason they are not interchangeable is physical: an
    inverter's output adds to the house current on a circuit whose breaker sits
    upstream of the injection point, while a charge is drawn through that
    breaker and protected by it.
    """

    device = _device(max_power=800)
    reports_1000 = _state(soc=50, charge_max_limit_w=1000)

    assert resolve_max_charge_power_w(device, reports_1000) == 1000
    # The output rating is never borrowed as a charge rating.
    assert resolve_max_charge_power_w(device, _state(soc=50, charge_max_limit_w=0)) == 0


def test_an_operator_may_go_below_the_device_ceiling_but_not_above_it():
    reports_1000 = _state(soc=50, charge_max_limit_w=1000)

    assert resolve_max_charge_power_w(_device(max_charge_power_w=400), reports_1000) == 400
    # Above the ceiling the device decides: it is the one that has to accept it.
    assert resolve_max_charge_power_w(_device(max_charge_power_w=3000), reports_1000) == 1000
    # Without a reported ceiling an explicit setting still stands on its own.
    assert resolve_max_charge_power_w(
        _device(max_charge_power_w=600), _state(soc=50, charge_max_limit_w=0)
    ) == 600


def test_a_device_that_reports_no_ceiling_charges_nothing():
    """Every model that can charge reports one, so an absent value is a stranger."""

    assert resolve_max_charge_power_w(_device(max_power=800), None) == 0
    assert resolve_max_charge_power_w(_device(max_power="?"), _state(soc=50, charge_max_limit_w="?")) == 0


def test_the_surplus_export_setting_is_read_but_decides_nothing_yet():
    """gridReverse is carried so the state is visible, not acted on.

    A device with surplus export enabled and a full battery stops curtailing:
    its PV leaves whatever the EMS allocates, which makes it a source rather
    than an actuator. The EMS does not model that role yet, so the value is
    reported rather than quietly changing an allocation. The day it does change
    one, this test fails and the change is deliberate.
    """

    telemetry = {"electricLevel": 50, "socSet": 1000, "packNum": 2, "chargeMaxLimit": 1000}
    on = parse_device({"properties": dict(telemetry, gridReverse=1)})
    off = parse_device({"properties": dict(telemetry, gridReverse=2)})

    assert (on.grid_reverse, off.grid_reverse) == (1, 2)
    assert charge_headroom_weight(on, _device(), _capability()) == charge_headroom_weight(
        off, _device(), _capability()
    )
    assert resolve_max_charge_power_w(_device(), on) == resolve_max_charge_power_w(_device(), off)


def test_the_charge_catalogue_matches_the_owner_device_list():
    """The registry is the owner's AC-charging catalogue, one row per model.

    Pinned as data rather than prose: a model silently flipping here changes
    whether the EMS will ever push power into it.
    """

    from ems.mqtt_control.zendure_profiles import _HARDWARE_PROFILES

    charging = {p.display_label for p in _HARDWARE_PROFILES if p.supports_charge}

    assert charging == {
        "SolarFlow 800 Pro",
        "SolarFlow 800 Pro 2",
        "SolarFlow 1600 AC+",
        "SolarFlow 2400 AC",
        "SolarFlow 2400 AC+",
        "SolarFlow 3000 Mix AC+",
        "SolarFlow 4000 Mix AC+",
        "Hyper 2000",
    }


def test_every_charging_model_names_what_the_permission_rests_on():
    """A catalogue claim must never be indistinguishable from an observation."""

    from ems.mqtt_control.zendure_profiles import (
        CHARGE_EVIDENCE_MEASURED,
        CHARGE_EVIDENCE_NONE,
        CHARGE_EVIDENCE_VENDOR_CATALOGUE,
        _HARDWARE_PROFILES,
    )

    for profile in _HARDWARE_PROFILES:
        expected = {CHARGE_EVIDENCE_MEASURED, CHARGE_EVIDENCE_VENDOR_CATALOGUE}
        if profile.supports_charge:
            assert profile.charge_evidence in expected, profile.display_label
        else:
            assert profile.charge_evidence == CHARGE_EVIDENCE_NONE, profile.display_label

    measured = {
        p.display_label
        for p in _HARDWARE_PROFILES
        if p.charge_evidence == CHARGE_EVIDENCE_MEASURED
    }
    # Only the maintainer's own hardware was put on a probe.
    assert measured == {"SolarFlow 800 Pro 2"}


def test_a_model_the_ems_cannot_command_is_never_given_a_charge_permission():
    """The ACE 1500 does AC charge, and the EMS has no way to tell it to.

    A capability without an actuator is not a permission; the flag gates a
    command, so it follows the write path and not the datasheet.
    """

    from ems.mqtt_control.zendure_profiles import _HARDWARE_PROFILES

    for profile in _HARDWARE_PROFILES:
        if not profile.writable:
            assert not profile.supports_charge, profile.display_label


def test_the_mix_models_resolve_under_both_word_orders():
    """The product is written "4000 Mix AC+" and "Mix 4000 AC+" in the wild.

    A name that does not resolve leaves the device telemetry-only, so it is
    never controlled at all -- charge flag or no charge flag.
    """

    from ems.mqtt_control.zendure_profiles import resolve_hardware_profile

    for name, expected in (
        ("SolarFlow 4000 Mix AC+", "solarflow_4000_ac_plus"),
        ("SolarFlow Mix 4000 AC+", "solarflow_4000_ac_plus"),
        # The name this profile was created under, kept so existing configs
        # still resolve even though only the Mix variant exists.
        ("SolarFlow 4000 AC+", "solarflow_4000_ac_plus"),
        ("SolarFlow 3000 Mix AC+", "solarflow_3000_ac_plus"),
        ("SolarFlow Mix 3000 AC+", "solarflow_3000_ac_plus"),
    ):
        profile = resolve_hardware_profile(name)
        assert profile is not None, name
        assert profile.canonical_name == expected, name
