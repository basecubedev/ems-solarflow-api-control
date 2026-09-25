# SPDX-License-Identifier: AGPL-3.0-or-later
"""Battery presence is a three-valued fact, and absence is never inferred.

``packNum`` is the only telemetry field that says anything about a battery. The
parser used to fold a missing field into ``0``, which made "this device has no
battery" indistinguishable from "this device never told us" and from "we have
never reached this device at all". Nothing could act on absence, because absence
could not be expressed.

These tests pin the three values and, just as importantly, the two directions
that must never be confused: an unobserved field is ``unknown`` and behaves
exactly as it did before, and a device that was never reached is ``unknown``
rather than battery-less.
"""

import pytest

from ems.clients import parse_device, zero_device_state
from ems.models import DeviceState
from ems.target_control import (
    BATTERY_ABSENT,
    BATTERY_PRESENT,
    BATTERY_UNKNOWN,
    battery_presence,
    detect_capabilities,
)

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]


def state(**overrides):
    values = dict(
        soc=50,
        min_soc=15,
        max_soc=100,
        solar=0,
        output=0,
        pack_in=0,
        pack_out=0,
        temp=25,
        voltage=48,
        rssi=-50,
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
    values.update(overrides)
    return DeviceState(**values)


# --- parsing: the four cases the old `or 0` collapsed into one ---------------


def test_reported_pack_count_is_preserved():
    parsed = parse_device({"properties": {"packNum": 2}})

    assert parsed.pack_num == 2
    assert battery_presence(parsed) == BATTERY_PRESENT


def test_reported_zero_means_no_battery():
    parsed = parse_device({"properties": {"packNum": 0}})

    assert parsed.pack_num == 0
    assert battery_presence(parsed) == BATTERY_ABSENT


def test_absent_field_is_unknown_not_zero():
    """The whole point: a field nobody reported is not a report of zero."""

    parsed = parse_device({"properties": {"electricLevel": 74}})

    assert parsed.pack_num is None
    assert battery_presence(parsed) == BATTERY_UNKNOWN


def test_null_field_is_unknown():
    parsed = parse_device({"properties": {"packNum": None}})

    assert battery_presence(parsed) == BATTERY_UNKNOWN


def test_unreachable_device_is_unknown_not_battery_less():
    """A device we have never read must not read as "no battery".

    ``zero_device_state`` stands in for a device that never answered. Treating
    its silence as an observed zero would let an unreachable device qualify for
    behaviour that is only safe once absence is actually confirmed.
    """

    assert zero_device_state().pack_num is None
    assert battery_presence(zero_device_state()) == BATTERY_UNKNOWN


# --- the helper itself ------------------------------------------------------


@pytest.mark.parametrize(
    "pack_num,expected",
    [
        (None, BATTERY_UNKNOWN),
        (0, BATTERY_ABSENT),
        (1, BATTERY_PRESENT),
        (8, BATTERY_PRESENT),
        ("2", BATTERY_PRESENT),
        ("0", BATTERY_ABSENT),
        # Neither of these is an observation of zero packs, so neither may
        # unlock behaviour that only a confirmed absence is allowed to unlock.
        (-1, BATTERY_UNKNOWN),
        ("nonsense", BATTERY_UNKNOWN),
        ("", BATTERY_UNKNOWN),
        (1.5, BATTERY_UNKNOWN),
        # JSON has no integers; a pack count may well arrive as 2.0.
        (2.0, BATTERY_PRESENT),
        (0.0, BATTERY_ABSENT),
        ("2.0", BATTERY_PRESENT),
    ],
)
def test_presence_values(pack_num, expected):
    assert battery_presence(state(pack_num=pack_num)) == expected


def test_presence_of_a_state_without_the_field_is_unknown():
    """Defensive: a state object from an older shape must not read as absent."""

    class Legacy:
        pass

    assert battery_presence(Legacy()) == BATTERY_UNKNOWN


# --- presence is new information, not a new gate ----------------------------


def test_battery_presence_does_not_change_the_other_capabilities():
    """Presence is new information, not a new gate on existing decisions."""

    with_battery = detect_capabilities(state(pack_num=2, solar=400))
    without = detect_capabilities(state(pack_num=0, solar=400))

    assert (with_battery.can_charge, with_battery.can_discharge) == (
        without.can_charge,
        without.can_discharge,
    )
    assert with_battery.can_export == without.can_export
    assert with_battery.can_ac_charge == without.can_ac_charge


def test_the_state_store_records_an_unreported_pack_count_as_null(tmp_path):
    """`packNum: 0` in a support bundle means "confirmed none", so it may not
    stand in for a device that never reported the field."""

    from datetime import datetime, timezone

    from ems.state_store import BatteryFullChargeStateStore

    store = BatteryFullChargeStateStore(str(tmp_path / "state.sqlite"))
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    unknown = store.record_observation(
        "WR1", state(pack_num=None), False, now, interval_days=28
    )
    absent = store.record_observation(
        "WR2", state(pack_num=0), False, now, interval_days=28
    )
    present = store.record_observation(
        "WR3", state(pack_num=2), True, now, interval_days=28
    )

    assert unknown["last_seen_pack_num"] is None
    assert absent["last_seen_pack_num"] == 0
    assert present["last_seen_pack_num"] == 2


@pytest.mark.parametrize("pack_num", [2.9, -1, True, "nonsense", ""])
def test_the_store_does_not_record_a_count_the_controller_rejects(pack_num, tmp_path):
    """A support bundle must not print a pack count the EMS does not believe."""

    from datetime import datetime, timezone

    from ems.state_store import BatteryFullChargeStateStore

    store = BatteryFullChargeStateStore(str(tmp_path / "state.sqlite"))
    item = state(pack_num=pack_num)

    record = store.record_observation(
        "WR1", item, False, datetime(2026, 6, 1, tzinfo=timezone.utc), interval_days=28
    )

    assert battery_presence(item) == BATTERY_UNKNOWN
    assert record["last_seen_pack_num"] is None
