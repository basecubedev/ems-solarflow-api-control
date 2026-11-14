# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
""""PV surplus available" is reported only while there is PV to speak of.

A Tibber Pulse tester (issue #341) ran a SolarFlow without panels in dry-run at
night: the battery stood idle at 75 %, so the EMS planned nothing, and 0 W of PV
"covered" the 0 W it planned -- the Control page named "PV surplus available"
as the active limit. The allocation itself is right and stays as it is; only the
limit no longer claims a surplus that does not exist.
"""

from types import SimpleNamespace

import pytest

from ems.clients import parse_device
from ems.target_control import calculate_targets, detect_capabilities

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.simulation,
    pytest.mark.unit,
]


def _device(name="INV_1"):
    return SimpleNamespace(
        name=name, max_power=800, pv_kwp=1.0, pv_priority_factor=1.0, battery_kwh=1.0,
        min_soc=15, max_soc=100, smart_mode=1, grid_off_mode=None,
    )


def _state(**properties):
    base = {"electricLevel": 75, "packNum": 1, "minSoc": 150, "socSet": 1000}
    return parse_device({"properties": {**base, **properties}})


def _limit(state, requested_total):
    targets, _current, new_total, explanation = calculate_targets(
        2200, [state], 800, [_device()], [detect_capabilities(state)],
        requested_total=requested_total, explain=True,
    )
    limit = next(item for item in explanation.limits if item.name == "pv_surplus_available")
    return targets, new_total, limit


def test_no_pv_and_nothing_planned_is_no_pv_surplus():
    idle = _state(solarInputPower=0, outputHomePower=0, dcStatus=0, acStatus=0, outputLimit=0)

    targets, new_total, limit = _limit(idle, 0)

    assert targets == [0] and new_total == 0
    assert limit.active is False
    assert limit.value == 0
    assert limit.reason == "no PV output"


def test_an_idle_battery_without_pv_plans_nothing_and_claims_no_surplus():
    idle = _state(solarInputPower=0, outputHomePower=0, dcStatus=0, acStatus=0, outputLimit=0)

    targets, _new_total, limit = _limit(idle, 800)

    assert detect_capabilities(idle).can_discharge is False
    assert targets == [0]
    assert limit.active is False
    assert limit.reason == "no PV output"


def test_pv_that_covers_the_planned_output_is_a_surplus():
    sunny = _state(solarInputPower=400, outputHomePower=300, dcStatus=1, acStatus=1, outputLimit=300)

    targets, _new_total, limit = _limit(sunny, 300)

    assert targets == [300]
    assert limit.active is True
    assert limit.value == 400
    assert limit.reason == "total PV can cover the requested output"


def test_pv_short_of_the_planned_output_needs_the_battery():
    weak = _state(solarInputPower=100, outputHomePower=300, packInputPower=200, dcStatus=2, acStatus=1, outputLimit=300)

    _targets, _new_total, limit = _limit(weak, 300)

    assert limit.active is False
    assert limit.reason == "battery discharge is needed beyond PV output"
