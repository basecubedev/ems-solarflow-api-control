# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which full device may pass its PV to another device's AC charge, and who is in bypass."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ems import surplus_export as se
from ems.config import AC_CHARGE_CONTROL_DEFAULTS

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]

SETTINGS = se.SurplusExportSettings()


def view(name, **fields):
    base = dict(
        name=name,
        online=True,
        managed=True,
        may_output=True,
        may_take=True,
        full=False,
        chargeable=True,
        headroom_percent=40.0,
        solar_w=0.0,
        output_w=0.0,
        output_limit_w=0.0,
        grid_reverse=se.EXPORT_REFUSED,
    )
    base.update(fields)
    return se.DeviceView(**base)


def full(name, **fields):
    return view(name, full=True, chargeable=False, headroom_percent=0.0, **fields)


def plan(views, previous=None, settings=SETTINGS, grid_power_w=0.0, now=1000.0, charging=True):
    return se.plan_surplus_export(
        views, previous, settings, grid_power_w=grid_power_w, charging=charging, now=now
    )


def test_a_full_device_may_export_while_another_device_can_charge():
    result = plan([full("A", solar_w=700), view("B")])

    assert result.grid_export == {"A": "allow", "B": "refuse"}
    assert result.reason == se.REASON_PASSING


def test_export_is_refused_everywhere_when_every_device_is_full():
    result = plan([full("A", solar_w=700), full("B", solar_w=300)])

    assert result.grid_export == {"A": "refuse", "B": "refuse"}
    assert result.reason == se.REASON_NO_TAKER


def test_a_single_device_never_exports_on_its_own():
    result = plan([full("A", solar_w=700, grid_reverse=se.EXPORT_ALLOWED)])

    assert result.grid_export == {"A": "refuse"}


def test_a_device_that_may_not_charge_takes_nothing_in():
    result = plan([full("A", solar_w=700), view("B", chargeable=False)])

    assert result.grid_export == {"A": "refuse", "B": "refuse"}
    assert result.reason == se.REASON_NO_TAKER


def test_an_offline_taker_takes_nothing_in():
    result = plan([full("A", solar_w=700), view("B", online=False)])

    assert result.grid_export == {"A": "refuse"}


def test_only_as_many_devices_export_as_the_setting_allows_the_ones_with_most_pv_first():
    views = [full("A", solar_w=300), full("B", solar_w=700), full("C", solar_w=500), view("D")]

    one = plan(views)
    two = plan(views, settings=replace(SETTINGS, devices=2))

    assert [name for name, word in one.grid_export.items() if word == "allow"] == ["B"]
    assert sorted(name for name, word in two.grid_export.items() if word == "allow") == ["B", "C"]


def test_a_device_allowed_keeps_its_place_while_it_stays_full():
    previous = se.SurplusExportState(allowed=("A",))

    result = plan([full("A", solar_w=100), full("B", solar_w=900), view("C")], previous)

    assert result.state.allowed == ("A",)


def test_the_first_export_waits_for_room_in_the_taker_and_a_running_one_does_not():
    views = [full("A", solar_w=700), view("B", headroom_percent=3.0)]
    no_dwell_left = se.SurplusExportState(allowed=("A",), taker_lost_since=1000.0 - se.TAKER_LOSS_SECONDS)

    starting = plan(views)
    running = plan(views, no_dwell_left)

    assert starting.grid_export["A"] == "refuse"
    assert running.grid_export["A"] == "allow"


def test_a_taker_whose_eligibility_lapses_ends_a_running_export_once_it_stays_lapsed():
    views = [full("A", solar_w=700), view("B", chargeable=False)]

    lost = plan(views, se.SurplusExportState(allowed=("A",)), now=0)
    waiting = plan(views, lost.state, now=se.TAKER_LOSS_SECONDS - 1)
    ended = plan(views, waiting.state, now=se.TAKER_LOSS_SECONDS)

    assert lost.grid_export["A"] == "allow"
    assert waiting.grid_export["A"] == "allow"
    assert ended.grid_export["A"] == "refuse"
    assert ended.reason == se.REASON_NO_TAKER


def test_with_no_devices_set_the_ems_leaves_the_export_switch_alone():
    off = replace(SETTINGS, devices=0)

    result = plan([full("A", solar_w=700), view("B")], settings=off)

    assert result.grid_export == {}
    assert result.reason == se.REASON_OFF


def test_a_device_the_ems_does_not_write_is_given_no_value():
    result = plan([full("A", solar_w=700, managed=False), view("B")])

    assert result.grid_export == {"B": "refuse"}
    assert result.reason == se.REASON_NO_FULL_DEVICE


def test_a_device_enters_bypass_on_evidence_and_stays_while_full_and_allowed():
    entering = full("A", grid_reverse=se.EXPORT_ALLOWED, output_w=600, output_limit_w=200)
    at_its_share = replace(entering, output_w=200)

    assert se.in_bypass(entering, was_in_bypass=False)
    assert not se.in_bypass(at_its_share, was_in_bypass=False)
    assert se.in_bypass(at_its_share, was_in_bypass=True)


@pytest.mark.parametrize(
    "change",
    [
        {"full": False},
        {"grid_reverse": se.EXPORT_REFUSED},
        {"grid_reverse": None},
        {"online": False},
    ],
)
def test_bypass_ends_when_the_battery_leaves_full_or_export_is_refused(change):
    device = replace(
        full("A", grid_reverse=se.EXPORT_ALLOWED, output_w=600, output_limit_w=200), **change
    )

    assert not se.in_bypass(device, was_in_bypass=True)


def test_a_device_within_25_w_of_its_limit_is_not_taken_for_bypass():
    within = full("A", grid_reverse=se.EXPORT_ALLOWED, output_w=225, output_limit_w=200)
    beyond = replace(within, output_w=226)

    assert not se.in_bypass(within, was_in_bypass=False)
    assert se.in_bypass(beyond, was_in_bypass=False)


def test_bypass_is_seen_on_a_device_the_ems_does_not_manage():
    device = full(
        "A", managed=False, grid_reverse=se.EXPORT_ALLOWED, output_w=600, output_limit_w=35
    )

    result = plan([device], settings=replace(SETTINGS, devices=0))

    assert result.exporting == frozenset({"A"})


def test_an_export_past_the_limit_for_long_enough_refuses_the_export_for_the_hold():
    settings = replace(SETTINGS, export_limit_w=100, export_limit_seconds=120, hold_seconds=900)
    exporting = full("A", solar_w=700, grid_reverse=se.EXPORT_ALLOWED, output_w=700, output_limit_w=300)
    views = [exporting, view("B")]

    first = plan(views, se.SurplusExportState(allowed=("A",)), settings, grid_power_w=-150, now=0)
    early = plan(views, first.state, settings, grid_power_w=-150, now=119)
    tripped = plan(views, early.state, settings, grid_power_w=-150, now=120)
    holding = plan(views, tripped.state, settings, grid_power_w=0, now=1019)
    released = plan(views, holding.state, settings, grid_power_w=0, now=1020)

    assert first.grid_export["A"] == "allow"
    assert early.grid_export["A"] == "allow"
    assert tripped.grid_export["A"] == "refuse"
    assert tripped.reason == se.REASON_HELD
    assert holding.grid_export["A"] == "refuse"
    assert released.grid_export["A"] == "allow"


def test_an_export_that_dips_under_the_limit_starts_its_clock_again():
    settings = replace(SETTINGS, export_limit_w=100, export_limit_seconds=120)
    exporting = full("A", solar_w=700, grid_reverse=se.EXPORT_ALLOWED, output_w=700, output_limit_w=300)
    views = [exporting, view("B")]

    first = plan(views, se.SurplusExportState(allowed=("A",)), settings, grid_power_w=-150, now=0)
    dip = plan(views, first.state, settings, grid_power_w=-50, now=60)
    later = plan(views, dip.state, settings, grid_power_w=-150, now=150)

    assert later.grid_export["A"] == "allow"
    assert later.state.export_since == 150


def test_without_an_export_limit_the_export_stays_however_much_goes_out():
    exporting = full("A", solar_w=700, grid_reverse=se.EXPORT_ALLOWED, output_w=700, output_limit_w=300)

    result = plan(
        [exporting, view("B")], se.SurplusExportState(allowed=("A",)), grid_power_w=-5000, now=10**6
    )

    assert result.grid_export["A"] == "allow"


def test_each_device_in_bypass_is_told_its_part_of_the_house_load_split_by_what_it_delivers():
    views = [full("A", output_w=600), full("B", output_w=200), full("D", output_w=0), view("C", output_w=400)]

    assert se.house_shares(views, {"A", "B"}, 400) == {"A": 300, "B": 100}
    assert se.house_shares(views, {"A"}, 1000) == {"A": 1000}
    assert se.house_shares(views, {"A"}, -50) == {"A": 0}
    assert se.house_shares(views, {"D"}, 600) == {"D": 600}


def test_settings_come_from_the_ac_charging_section_and_a_bad_value_is_its_default():
    settings = se.settings_from_config(
        {"surplus_export_devices": "2", "surplus_export_limit_w": "lots", "surplus_export_hold_minutes": 2}
    )

    assert settings.devices == 2
    assert settings.export_limit_w == AC_CHARGE_CONTROL_DEFAULTS["surplus_export_limit_w"]
    assert settings.hold_seconds == 120
    assert se.settings_from_config(None) == se.SurplusExportSettings()


def test_every_setting_key_is_an_ac_charging_default():
    assert set(se.SETTING_KEYS) <= set(AC_CHARGE_CONTROL_DEFAULTS)
    assert se.settings_from_config(AC_CHARGE_CONTROL_DEFAULTS) == se.SurplusExportSettings()


def test_the_export_setting_is_offered_only_for_an_identified_local_model_that_reports_it():
    local = SimpleNamespace(supports_state_reconciliation=True, resolved_hardware_profile=lambda: "solarflow_800_pro")
    mqtt = SimpleNamespace(supports_state_reconciliation=False, resolved_hardware_profile=lambda: "solarflow_800_pro")
    unknown = SimpleNamespace(supports_state_reconciliation=True, resolved_hardware_profile=lambda: None)
    reported = {"gridReverse": 1}

    assert se.grid_export_offered(local, reported) == (se.GRID_EXPORT,)
    assert se.grid_export_offered(local, {}) == ()
    assert se.grid_export_offered(mqtt, reported) == ()
    assert se.grid_export_offered(unknown, reported) == ()


def test_the_report_is_read_only_when_it_carries_the_switch():
    carried = SimpleNamespace(grid_reverse=1, grid_reverse_reported=True)
    missing = SimpleNamespace(grid_reverse=0, grid_reverse_reported=False)

    assert se.reported_grid_export(carried) == {"gridReverse": 1}
    assert se.reported_grid_export(missing) == {}
    assert se.reported_grid_export(None) == {}


def test_a_device_that_may_not_supply_the_house_never_passes_its_pv():
    result = plan([full("A", solar_w=700, may_output=False), full("B", solar_w=300), view("C")])

    assert result.grid_export == {"A": "refuse", "B": "allow", "C": "refuse"}


def test_a_device_full_by_its_report_takes_nothing_in_whatever_room_its_soc_shows():
    taker = view("B", full=True, chargeable=True, headroom_percent=30.0)

    result = plan([full("A", solar_w=700), taker])

    assert result.grid_export["A"] == "refuse"
    assert result.reason == se.REASON_NO_TAKER


def test_an_export_no_charge_follows_is_refused_after_the_grace_for_the_hold():
    settings = replace(SETTINGS, hold_seconds=900)
    views = [full("A", solar_w=700), view("B")]

    allowed = plan(views, settings=settings, charging=False, grid_power_w=-100, now=0)
    waiting = plan(views, allowed.state, settings, charging=False, grid_power_w=-100, now=1)
    still = plan(views, waiting.state, settings, charging=False, grid_power_w=-100, now=1 + se.START_GRACE_SECONDS - 1)
    refused = plan(views, still.state, settings, charging=False, grid_power_w=-100, now=1 + se.START_GRACE_SECONDS)
    holding = plan(views, refused.state, settings, charging=True, now=1 + se.START_GRACE_SECONDS + 899)
    again = plan(views, holding.state, settings, charging=False, now=1 + se.START_GRACE_SECONDS + 900)

    assert allowed.grid_export["A"] == "allow"
    assert still.grid_export["A"] == "allow"
    assert refused.grid_export["A"] == "refuse"
    assert refused.reason == se.REASON_NO_CHARGE
    assert holding.grid_export["A"] == "refuse"
    assert holding.reason == se.REASON_NO_CHARGE
    assert again.grid_export["A"] == "allow"


def test_a_running_charge_keeps_the_export_and_restarts_the_grace():
    views = [full("A", solar_w=700), view("B")]
    allowed = plan(views, charging=False, grid_power_w=-100, now=0)
    waiting = plan(views, allowed.state, charging=False, grid_power_w=-100, now=100)

    charging = plan(views, waiting.state, charging=True, grid_power_w=-100, now=110)
    after = plan(views, charging.state, charging=False, grid_power_w=-100, now=110 + se.START_GRACE_SECONDS - 1)

    assert charging.state.waiting_since is None
    assert after.grid_export["A"] == "allow"


def test_the_export_limit_hold_keeps_its_own_reason_while_it_runs():
    settings = replace(SETTINGS, export_limit_w=100, export_limit_seconds=0, hold_seconds=900)
    exporting = full("A", solar_w=700, grid_reverse=se.EXPORT_ALLOWED, output_w=700, output_limit_w=300)
    views = [exporting, view("B")]

    tripped = plan(views, se.SurplusExportState(allowed=("A",)), settings, grid_power_w=-150, now=0)
    later = plan(views, tripped.state, settings, grid_power_w=0, charging=False, now=500)

    assert tripped.reason == se.REASON_HELD
    assert later.reason == se.REASON_HELD


def test_a_report_s_export_switch_and_bypass_states_are_read_and_their_absence_is_kept():
    from ems.clients import parse_device

    carried = parse_device({"properties": {"electricLevel": 100, "gridReverse": 1, "pass": 2, "reverseState": 1}})
    missing = parse_device({"properties": {"electricLevel": 100}})

    assert (carried.grid_reverse, carried.grid_reverse_reported) == (1, True)
    assert (carried.pass_state, carried.reverse_state) == (2, 1)
    assert missing.grid_reverse_reported is False
    assert (missing.pass_state, missing.reverse_state) == (None, None)
    assert se.reported_grid_export(missing) == {}


def test_an_allowed_device_keeps_the_export_near_its_ceiling_and_is_no_taker_meanwhile():
    previous = se.SurplusExportState(allowed=("A",))
    near = view("A", chargeable=True, headroom_percent=2.0, solar_w=700)
    below = replace(near, headroom_percent=3.0)

    kept = plan([near, view("B")], previous)
    dropped = plan([below, view("B")], previous)
    alone = plan([near], replace(previous, taker_lost_since=1000.0 - se.TAKER_LOSS_SECONDS))

    assert kept.grid_export["A"] == "allow"
    assert dropped.grid_export["A"] == "refuse"
    assert alone.reason == se.REASON_NO_TAKER


def test_the_export_limit_clock_runs_only_while_a_device_passes_its_pv():
    settings = replace(SETTINGS, export_limit_w=100, export_limit_seconds=0)

    result = plan([full("A", solar_w=700), view("B")], se.SurplusExportState(allowed=("A",)), settings, grid_power_w=-5000)

    assert result.grid_export["A"] == "allow"
    assert result.state.held_until is None


def test_a_switch_reported_as_true_or_false_is_no_value():
    from ems.clients import parse_device

    state = parse_device({"properties": {"electricLevel": 100, "gridReverse": True, "pass": False}})

    assert state.grid_reverse_reported is False
    assert state.pass_state is None


def test_while_the_house_takes_all_the_pv_the_export_stays_allowed_without_a_charge():
    views = [full("A", solar_w=700), view("B")]
    allowed = plan(views, charging=False, grid_power_w=0, now=0)
    within = plan(views, allowed.state, charging=False, grid_power_w=-se.NO_CHARGE_EXPORT_W, now=1)

    later = plan(views, within.state, charging=False, grid_power_w=-se.NO_CHARGE_EXPORT_W, now=10 * se.START_GRACE_SECONDS)
    unread = plan(views, later.state, charging=False, grid_power_w=None, now=10 * se.START_GRACE_SECONDS + 1)
    tripped = plan(views, unread.state, charging=False, grid_power_w=None, now=12 * se.START_GRACE_SECONDS)

    assert later.grid_export["A"] == "allow"
    assert later.state.waiting_since is None
    assert tripped.reason == se.REASON_NO_CHARGE


def test_a_full_device_without_pv_is_never_allowed_to_export():
    result = plan([full("A", solar_w=0), view("B")])

    assert result.grid_export["A"] == "refuse"
    assert result.reason == se.REASON_NO_FULL_DEVICE


def test_a_device_needs_more_than_50_w_of_pv_to_start_and_any_pv_to_keep_the_export():
    taker = view("B")
    starting_low = plan([full("A", solar_w=se.PV_START_W), taker])
    starting = plan([full("A", solar_w=se.PV_START_W + 1), taker])
    keeping = plan([full("A", solar_w=1), taker], se.SurplusExportState(allowed=("A",)))
    dark = plan([full("A", solar_w=0), taker], se.SurplusExportState(allowed=("A",)))

    assert starting_low.grid_export["A"] == "refuse"
    assert starting.grid_export["A"] == "allow"
    assert keeping.grid_export["A"] == "allow"
    assert dark.grid_export["A"] == "refuse"


def test_a_pv_reading_flickering_at_dusk_switches_the_export_once():
    taker = view("B")
    state, words = None, []
    for now, pv in enumerate([700, 6, 0, 6, 0, 6, 0]):
        result = plan([full("A", solar_w=pv), taker], state, now=float(now))
        state = result.state
        words.append(result.grid_export["A"])

    assert words == ["allow", "allow", "refuse", "refuse", "refuse", "refuse", "refuse"]


def test_a_taker_whose_eligibility_flickers_does_not_switch_the_export():
    state, words = se.SurplusExportState(allowed=("A",)), []
    for now in range(10):
        taker = view("B", chargeable=now % 2 == 0)
        result = plan([full("A", solar_w=700), taker], state, now=float(now))
        state = result.state
        words.append(result.grid_export["A"])

    assert set(words) == {"allow"}


def test_the_grace_before_an_export_no_charge_follows_is_refused_comes_from_the_settings():
    settings = replace(SETTINGS, grace_seconds=600)
    views = [full("A", solar_w=700), view("B")]
    allowed = plan(views, settings=settings, charging=False, grid_power_w=-100, now=0)
    waiting = plan(views, allowed.state, settings, charging=False, grid_power_w=-100, now=1)

    early = plan(views, waiting.state, settings, charging=False, grid_power_w=-100, now=500)
    late = plan(views, early.state, settings, charging=False, grid_power_w=-100, now=601)

    assert early.grid_export["A"] == "allow"
    assert late.reason == se.REASON_NO_CHARGE


def test_a_fractional_switch_value_is_no_value():
    from ems.clients import parse_device

    state = parse_device({"properties": {"electricLevel": 100, "gridReverse": 1.5}})

    assert state.grid_reverse_reported is False


@pytest.mark.parametrize(
    "gone",
    [
        {"may_take": False},
        {"full": True, "headroom_percent": 0.0},
        {"full": True, "headroom_percent": 30.0},
        {"online": False},
    ],
)
def test_a_taker_gone_for_good_ends_the_export_at_once(gone):
    taker = replace(view("B", chargeable=False), **gone)

    result = plan([full("A", solar_w=700), taker], se.SurplusExportState(allowed=("A",)))

    assert result.grid_export["A"] == "refuse"
    assert result.reason == se.REASON_NO_TAKER


def test_a_device_that_stops_being_a_candidate_as_its_taker_goes_ends_the_export_at_once():
    flickering = view("B", chargeable=False)

    result = plan([full("A", solar_w=0), flickering], se.SurplusExportState(allowed=("A",)))

    assert result.grid_export["A"] == "refuse"


def test_an_allowed_device_near_its_ceiling_is_no_flickering_taker_of_its_own_export():
    near = view("A", chargeable=True, headroom_percent=2.0, solar_w=700)
    gone = view("B", full=True, chargeable=False, headroom_percent=0.0)

    result = plan([near, gone], se.SurplusExportState(allowed=("A",)))

    assert result.grid_export["A"] == "refuse"
    assert result.state.taker_lost_since is None


def test_an_export_the_grid_target_asks_for_is_no_export_no_charge_follows():
    views = [full("A", solar_w=700), view("B")]
    allowed = plan(views, charging=False, grid_power_w=-100, now=0)
    waiting = plan(views, allowed.state, charging=False, grid_power_w=-100, now=1)

    on_target = se.plan_surplus_export(
        views, waiting.state, SETTINGS, grid_power_w=-100, beyond_target_w=0,
        charging=False, now=10 * se.START_GRACE_SECONDS,
    )

    assert on_target.grid_export["A"] == "allow"
    assert on_target.state.waiting_since is None


def test_an_import_below_an_import_target_is_no_export():
    views = [full("A", solar_w=700), view("B")]
    allowed = plan(views, charging=False, grid_power_w=40, now=0)
    waiting = plan(views, allowed.state, charging=False, grid_power_w=40, now=1)

    later = se.plan_surplus_export(
        views, waiting.state, SETTINGS, grid_power_w=40, beyond_target_w=-60,
        charging=False, now=10 * se.START_GRACE_SECONDS,
    )

    assert later.grid_export["A"] == "allow"
    assert later.state.waiting_since is None
