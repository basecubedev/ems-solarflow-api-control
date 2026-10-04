# SPDX-License-Identifier: AGPL-3.0-or-later
"""AC charging on a device with no PV input, through the real control loop.

The devices the feature exists for — a SolarFlow 2400 AC charging from a roof
plant's surplus — report no PV, and an empty one reports exactly what
night/min-SoC idle waits for. Every earlier regulator test used a device with
900 W of its own PV and a standby floor of zero, which is the one combination
in which neither the idle nor the standby floor can intervene. These tests
use the other combinations, which is what an Admin-created installation runs.
"""

import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ems.ac_charge_control import resolve_max_charge_power_w
from ems.clients import parse_device
from ems.mqtt_control.zendure_profiles import (
    HARDWARE_PROFILES,
    OPERATION_CHARGE,
)
from tests.test_ac_charge_regulator import DyingMeter, Harness, charging_device
from tests.test_write_gates import state

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]

ADMIN_STANDBY_FLOOR_W = 35


def ac_device(name="AC2400", **kwargs):
    return charging_device(name, hardware_profile="solarflow_2400_ac", **kwargs)


def no_pv_state(soc, output=0, pack_in=0, charge_max_limit_w=2400):
    return state(
        soc=soc,
        min_soc=15,
        solar=0,
        output=output,
        output_limit=output,
        pack_in=pack_in,
        soc_limit=0,
        pack_num=1,
        charge_max_limit_w=charge_max_limit_w,
    )


def test_an_empty_device_without_pv_charges_from_an_exported_surplus():
    """The idle's conditions hold, and the surplus is still there to take."""

    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    harness.run(cycles=15, states=[no_pv_state(soc=15)])

    assert harness.controller.night_min_soc_idle_active is False
    assert harness.controller.charge_direction.charging is True
    assert harness.targets[-1] < 0, harness.targets


def test_the_standby_floor_s_own_drain_does_not_refuse_a_charge():
    """Feeding out under the EMS's outputLimit is the EMS's drain, not a foreign one."""

    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    feeding_the_floor = no_pv_state(
        soc=50, output=ADMIN_STANDBY_FLOOR_W, pack_in=42
    )
    harness.run(cycles=15, states=[feeding_the_floor])

    assert harness.controller.charge_direction.charging is True
    assert harness.targets[-1] < 0, harness.targets


def test_a_drain_the_output_does_not_account_for_is_still_not_charged():
    """Feeding 35 W out while the pack delivers 800 W is somebody else's load."""

    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    off_grid_load = no_pv_state(soc=50, output=ADMIN_STANDBY_FLOOR_W, pack_in=800)
    harness.run(cycles=15, states=[off_grid_load])

    assert harness.controller.charge_direction.charging is False


def test_a_pack_drained_with_nothing_fed_out_is_still_not_charged():
    """The drain guard keeps its purpose: output 0 while the pack empties."""

    harness = Harness([ac_device()], load=-900)
    drained = no_pv_state(soc=50, output=0, pack_in=300)
    harness.run(cycles=15, states=[drained])

    assert harness.controller.charge_direction.charging is False
    assert all(target >= 0 for target in harness.targets)


def test_night_idle_still_parks_an_empty_device_when_nothing_is_exported():
    harness = Harness(
        [ac_device()], load=120, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    harness.run(cycles=5, states=[no_pv_state(soc=15)])

    assert harness.controller.night_min_soc_idle_active is True
    assert harness.controller.charge_direction.charging is False


def test_an_active_night_idle_is_left_when_a_surplus_appears(caplog):
    harness = Harness(
        [ac_device()], load=120, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    harness.run(cycles=3, states=[no_pv_state(soc=15)])
    assert harness.controller.night_min_soc_idle_active is True
    harness.controller.load_history.append(120)

    exit_cycle_history = []
    original_reset = harness.controller.reset_output_control_state

    def reset_and_record():
        original_reset()
        exit_cycle_history.append(len(harness.controller.load_history))

    harness.controller.reset_output_control_state = reset_and_record
    with caplog.at_level(logging.INFO):
        harness.run(cycles=15, states=[no_pv_state(soc=15)], load=-900)

    exits = [
        record.getMessage()
        for record in caplog.records
        if "night_min_soc_idle_exit" in record.getMessage()
    ]
    assert len(exits) == 1 and "reason=ac_charge_surplus" in exits[0], exits
    assert exit_cycle_history == [0]
    assert harness.controller.night_min_soc_idle_active is False
    assert harness.controller.charge_direction.charging is True


def test_one_dip_below_the_start_threshold_does_not_discard_the_evidence():
    """The idle feeds the entry window, so a dip costs one observation."""

    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    empty = [no_pv_state(soc=15)]
    harness.run(cycles=4, states=empty)
    harness.run(cycles=1, states=empty, load=-50)
    harness.run(cycles=2, states=empty, load=-900)

    assert harness.controller.night_min_soc_idle_active is False
    assert harness.controller.charge_direction.charging is True


def test_a_single_export_spike_does_not_take_the_plant_out_of_idle(caplog):
    """One sample is not a surplus; leaving the idle resets the control state."""

    harness = Harness(
        [ac_device()], load=120, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    empty = [no_pv_state(soc=15)]
    harness.run(cycles=2, states=empty)

    with caplog.at_level(logging.INFO):
        for _ in range(5):
            harness.run(cycles=1, states=empty, load=-400)
            harness.run(cycles=6, states=empty, load=-50)

    exits = [r for r in caplog.records if "night_min_soc_idle_exit" in r.getMessage()]
    assert exits == []
    assert harness.controller.night_min_soc_idle_active is True
    assert harness.controller.charge_direction.charging is False


def test_a_held_meter_reading_does_not_keep_the_plant_out_of_idle(caplog):
    """A stale "still exporting" is not a surplus, for the idle either."""

    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    harness.controller.grid_meter_reading_is_fresh = lambda: False
    with caplog.at_level(logging.INFO):
        harness.run(cycles=20, states=[no_pv_state(soc=15)])

    assert not [r for r in caplog.records if "night_min_soc_idle_exit" in r.getMessage()]
    assert harness.controller.night_min_soc_idle_active is True
    assert harness.controller.charge_direction.charging is False


def test_a_young_held_reading_is_no_entry_observation_in_the_idle():
    """Young enough to be tolerated is still not a measurement of a surplus.

    The meter fails and serves its last reading, -900 W, while the surplus may
    already be gone. The tolerance exists so a running charge survives a missed
    read; it is not evidence that a new one is worth starting.
    """

    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    meter = DyingMeter(-900)
    harness.controller.shelly = meter
    empty = [no_pv_state(soc=15)]
    harness.run(cycles=2, states=empty)
    assert harness.controller.night_min_soc_idle_active is True

    meter.failing = True
    harness.run(cycles=10, states=empty)

    assert harness.controller.grid_meter_holding is True
    assert harness.controller.charge_direction.charging is False
    assert harness.controller.night_min_soc_idle_active is True
    assert all(target >= 0 for target in harness.targets), harness.targets


def test_the_cycle_out_of_the_idle_sizes_no_charge_from_a_held_reading():
    """Leaving the idle resets the commanded total, and a hold must hold anyway.

    The charge is entered on measurements; the next cycle the meter fails. With
    no commanded total left to hold, the loop integrated the held reading into a
    fresh one and sized a charge from a number nobody had measured.
    """

    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    meter = DyingMeter(-900)
    harness.controller.shelly = meter
    empty = [no_pv_state(soc=15)]
    for _ in range(10):
        harness.run(cycles=1, states=empty)
        if harness.controller.charge_direction.charging:
            break
    assert harness.controller.night_min_soc_idle_active is True
    assert harness.controller.charge_direction.charging is True

    meter.failing = True
    written = len(harness.targets)
    harness.run(cycles=1, states=empty)

    assert harness.controller.night_min_soc_idle_active is False
    assert all(target >= 0 for target in harness.targets[written:]), (
        harness.targets[written:]
    )
    assert harness.controller.commanded_total_w >= 0


def test_night_idle_is_not_held_off_by_a_device_that_may_not_charge(caplog):
    harness = Harness(
        [ac_device(ac_charge_enabled=False)],
        load=-900,
        min_output_limit=ADMIN_STANDBY_FLOOR_W,
    )
    with caplog.at_level(logging.INFO):
        harness.run(cycles=20, states=[no_pv_state(soc=15)])

    assert not [r for r in caplog.records if "night_min_soc_idle_exit" in r.getMessage()]
    assert harness.controller.night_min_soc_idle_active is True


def test_a_device_reporting_no_ceiling_charges_at_its_model_s_rating():
    harness = Harness([ac_device()], load=-900)
    harness.run(cycles=15, states=[no_pv_state(soc=50, charge_max_limit_w=None)])

    assert harness.controller.device_charge_limits["AC2400"] == 2400
    assert harness.controller.charge_direction.charging is True


def test_a_device_with_no_ceiling_at_all_is_named_once_and_never_enters(
    caplog, monkeypatch
):
    """Entering for it would leave the floor at zero and exit the next cycle.

    That loop is what used to happen: twelve entries an hour, the rate limit,
    and no charge, with nothing in the log naming the missing number.
    """

    from ems.mqtt_control import zendure_profiles

    unrated = replace(
        HARDWARE_PROFILES["solarflow_2400_ac"], rated_charge_power_w=0
    )
    monkeypatch.setattr(
        zendure_profiles, "hardware_profile_by_name", lambda name: unrated
    )
    harness = Harness([ac_device()], load=-900)

    with caplog.at_level(logging.WARNING):
        harness.run(
            cycles=15, states=[no_pv_state(soc=50, charge_max_limit_w=None)]
        )

    unknown = [
        record
        for record in caplog.records
        if "ac_charge_ceiling_unknown" in record.getMessage()
    ]
    assert len(unknown) == 1
    assert harness.controller.charge_direction.charging is False
    assert harness.controller.charge_direction.entries == ()
    assert all(target >= 0 for target in harness.targets)


def refusals(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if "event=ac_charge_refused" in record.getMessage()
    ]


@pytest.mark.parametrize(
    "silent_field, reason",
    [
        ({"pack_num": None}, "reason=pack_count_unreported"),
        ({"max_soc": 0}, "reason=max_soc_unreported"),
    ],
)
def test_a_device_that_leaves_out_what_charging_needs_is_named_once(
    caplog, silent_field, reason
):
    """Silence is not a refusal by the device, and it must not look like one.

    A report without ``packNum`` -- even beside a populated ``packData`` -- or
    without ``socSet`` leaves the device without headroom to weigh, so it is
    never charged, and nothing said why.
    """

    silent = replace(no_pv_state(soc=50), **silent_field)
    harness = Harness([ac_device()], load=-900)

    with caplog.at_level(logging.WARNING):
        harness.run(cycles=15, states=[silent])

    said = refusals(caplog)
    assert len(said) == 1, said
    assert "device=AC2400" in said[0] and reason in said[0]
    assert harness.controller.charge_direction.charging is False


@pytest.mark.parametrize(
    "pinned, product",
    [(None, None), (None, "solarFlowSomethingNew"), ("solarflow_9000", None)],
)
def test_a_device_whose_model_cannot_be_identified_is_named_once(
    caplog, pinned, product
):
    device = charging_device("AC2400", hardware_profile=pinned)
    device.observed_product = product
    device.resolved_hardware_profile = lambda: None
    harness = Harness([device], load=-900)

    with caplog.at_level(logging.WARNING):
        harness.run(cycles=15, states=[no_pv_state(soc=50)])

    said = refusals(caplog)
    assert len(said) == 1, said
    assert "reason=model_unidentified" in said[0]
    assert f"pinned_profile={pinned}" in said[0]
    assert f"reported_product={product}" in said[0]
    assert harness.controller.charge_direction.charging is False


def test_a_refusal_is_said_again_only_after_it_had_cleared(caplog):
    harness = Harness([ac_device()], load=120)
    silent = replace(no_pv_state(soc=50), pack_num=None)
    reported = no_pv_state(soc=50)

    with caplog.at_level(logging.WARNING):
        harness.run(cycles=3, states=[silent])
        harness.run(cycles=1, states=[reported])
        harness.run(cycles=3, states=[silent])

    assert len(refusals(caplog)) == 2


@pytest.mark.parametrize(
    "silenced",
    [
        {"feature": False},
        {"device": {"ac_charge_enabled": False}},
    ],
)
def test_nothing_is_said_for_a_device_the_operator_does_not_let_charge(
    caplog, silenced
):
    harness = Harness(
        [ac_device(**silenced.get("device", {}))],
        load=-900,
        feature=silenced.get("feature", True),
    )

    with caplog.at_level(logging.WARNING):
        harness.run(cycles=5, states=[replace(no_pv_state(soc=50), pack_num=None)])

    assert refusals(caplog) == []


def test_a_device_reporting_a_zero_ceiling_is_not_charged_at_its_rating():
    """Zero is the device refusing a charge, not the device saying nothing."""

    harness = Harness([ac_device()], load=-900)
    harness.run(cycles=15, states=[no_pv_state(soc=50, charge_max_limit_w=0)])

    assert harness.controller.device_charge_limits["AC2400"] == 0
    assert harness.controller.charge_direction.charging is False
    assert all(target >= 0 for target in harness.targets)


def test_the_ceiling_warning_is_not_repeated_when_eligibility_flaps(caplog):
    harness = Harness([ac_device()], load=-900)
    zero = no_pv_state(soc=50, charge_max_limit_w=0)
    drained = no_pv_state(soc=50, pack_in=300, charge_max_limit_w=0)

    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            harness.run(cycles=1, states=[zero])
            harness.run(cycles=1, states=[drained])

    unknown = [
        record
        for record in caplog.records
        if "ac_charge_ceiling_unknown" in record.getMessage()
    ]
    assert len(unknown) == 1


def test_a_transport_that_bounds_a_charge_by_max_power_is_never_allocated_more():
    """MQTT refuses a charge above max_power; allocating it would be dropped."""

    dev = ac_device(charge_bounded_by_max_power=True)
    harness = Harness([dev], load=-2000)
    harness.feature = {**harness.feature, "max_total_charge_power_w": 2400}
    harness.run(cycles=20, states=[no_pv_state(soc=50, charge_max_limit_w=None)])

    assert harness.controller.device_charge_limits["AC2400"] == 800
    assert harness.controller.charge_direction.charging is True
    assert min(harness.targets) >= -800, harness.targets


def test_the_control_view_names_a_missing_ceiling_rather_than_a_refusal():
    charging = ac_device("CHARGES")
    no_ceiling = ac_device("NO_CEILING")
    harness = Harness([charging, no_ceiling], load=-900)
    harness.run(
        cycles=15,
        states=[
            no_pv_state(soc=50),
            no_pv_state(soc=50, charge_max_limit_w=0),
        ],
    )

    assert harness.controller.charge_direction.charging is True
    devices = harness.controller.last_control_explanation.to_dict()["devices"]
    assert devices["CHARGES"]["decision_reason"] == "ac_charge_allocation"
    assert devices["NO_CEILING"]["decision_reason"] == "ac_charge_no_ceiling"


def test_every_model_that_charges_carries_a_rated_charge_power():
    """A model enabled to charge with no rating depends on one telemetry field."""

    missing = [
        name
        for name, profile in HARDWARE_PROFILES.items()
        if profile.supports_operation(OPERATION_CHARGE)
        and profile.rated_charge_power_w <= 0
    ]
    assert missing == []


def test_a_model_that_cannot_charge_carries_no_rating():
    rated = [
        name
        for name, profile in HARDWARE_PROFILES.items()
        if not profile.supports_operation(OPERATION_CHARGE)
        and profile.rated_charge_power_w > 0
    ]
    assert rated == []


def test_the_reported_ceiling_wins_over_the_rating_and_the_setting_is_capped():
    silent = SimpleNamespace(charge_max_limit_w=None)
    refuses = SimpleNamespace(charge_max_limit_w=0)
    reports = SimpleNamespace(charge_max_limit_w=1800)
    unset = SimpleNamespace(max_charge_power_w=0)
    capped = SimpleNamespace(max_charge_power_w=5000)

    assert resolve_max_charge_power_w(unset, reports, rated_w=2400) == 1800
    assert resolve_max_charge_power_w(unset, silent, rated_w=2400) == 2400
    assert resolve_max_charge_power_w(capped, silent, rated_w=2400) == 2400
    assert resolve_max_charge_power_w(unset, silent) == 0
    assert resolve_max_charge_power_w(unset, refuses, rated_w=2400) == 0


@pytest.mark.parametrize("field", ["chargeMaxLimit", "chargeLimit"])
def test_either_ceiling_field_is_read(field):
    telemetry = parse_device(
        {"properties": {"electricLevel": 50, "packNum": 1, field: 2400}}
    )

    assert telemetry.charge_max_limit_w == 2400


def test_a_ceiling_the_device_does_not_report_is_absent_not_zero():
    silent = parse_device({"properties": {"electricLevel": 50, "packNum": 1}})
    refuses = parse_device(
        {"properties": {"electricLevel": 50, "packNum": 1, "chargeMaxLimit": 0}}
    )

    assert silent.charge_max_limit_w is None
    assert refuses.charge_max_limit_w == 0


def test_evidence_from_before_a_stale_meter_does_not_count_afterwards():
    """The window carries no timestamps, so it is emptied rather than frozen."""

    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    empty = [no_pv_state(soc=15)]
    harness.run(cycles=4, states=empty)

    harness.controller.grid_meter_reading_is_fresh = lambda: False
    harness.run(cycles=10, states=empty)
    harness.controller.grid_meter_reading_is_fresh = lambda: True

    harness.run(cycles=1, states=empty, load=-400)
    harness.run(cycles=1, states=empty, load=-50)

    assert harness.controller.night_min_soc_idle_active is True
    assert harness.controller.charge_direction.charging is False


def test_evidence_from_before_the_feature_was_disabled_does_not_count():
    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    empty = [no_pv_state(soc=15)]
    harness.run(cycles=4, states=empty)

    enabled = harness.feature
    harness.feature = {**enabled, "enabled": False}
    harness.run(cycles=5, states=empty)
    harness.feature = enabled

    harness.run(cycles=1, states=empty, load=-400)
    harness.run(cycles=1, states=empty, load=-50)

    assert harness.controller.charge_direction.charging is False


def test_a_running_charge_is_stopped_by_its_own_decision_when_the_meter_goes_stale(
    caplog,
):
    """Idle must not swallow the way back: the decision stops it and says why.

    The telemetry still looks idle -- the device has not started drawing yet --
    which is exactly the state in which the idle would otherwise take over.
    """

    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    empty = [no_pv_state(soc=15)]
    harness.run(cycles=10, states=empty)
    assert harness.controller.charge_direction.charging is True

    harness.controller.grid_meter_reading_is_fresh = lambda: False
    with caplog.at_level(logging.WARNING):
        harness.run(cycles=1, states=empty)

    assert harness.controller.charge_direction.charging is False
    assert harness.controller.night_min_soc_idle_active is False
    assert any(
        "ac_charge_stopped_stale_meter" in record.getMessage()
        for record in caplog.records
    )
    assert harness.targets[-1] >= 0, harness.targets


def test_the_cycle_that_leaves_the_idle_is_counted_once():
    """Three surplus samples in idle and one on the way out are four, not five."""

    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    harness.run(cycles=3, states=[no_pv_state(soc=15)])
    assert harness.controller.night_min_soc_idle_active is True

    pv_returns = state(
        soc=15, min_soc=15, solar=80, output=0, soc_limit=0, pack_num=1,
        charge_max_limit_w=2400,
    )
    harness.run(cycles=1, states=[pv_returns])

    assert harness.controller.night_min_soc_idle_active is False
    assert harness.controller.charge_direction.charging is False

    harness.run(cycles=1, states=[pv_returns])

    assert harness.controller.charge_direction.charging is True


def test_a_replayed_device_keeps_the_ceiling_its_trace_recorded():
    from ems.simulation import state_from_trace_device

    assert state_from_trace_device({"electricLevel": 50}).charge_max_limit_w is None
    assert state_from_trace_device({"chargeMaxLimit": 0}).charge_max_limit_w == 0
    assert state_from_trace_device({"chargeLimit": 900}).charge_max_limit_w == 900


@pytest.mark.parametrize(
    "device_kwargs, feature_overrides",
    [
        ({"max_charge_power_w": 80}, {}),
        ({}, {"max_total_charge_power_w": 90}),
    ],
)
def test_a_capacity_the_exit_would_undercut_never_enters(
    device_kwargs, feature_overrides, caplog
):
    """At or below stop_w every entry would leave on the next cycle."""

    harness = Harness(
        [ac_device(**device_kwargs)], load=-600, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    harness.feature = {**harness.feature, **feature_overrides}
    with caplog.at_level(logging.WARNING):
        harness.run(cycles=60, states=[no_pv_state(soc=15)])

    messages = [record.getMessage() for record in caplog.records]
    assert sum("ac_charge_capacity_below_stop" in m for m in messages) == 1
    assert not any("ac_charge_entry_rate_limited" in m for m in messages)
    assert harness.controller.charge_direction.entries == ()
    assert harness.controller.night_min_soc_idle_active is True


def test_a_capacity_just_above_the_exit_holds_a_charge():
    harness = Harness([ac_device(max_charge_power_w=150)], load=-600)
    harness.run(cycles=30, states=[no_pv_state(soc=50)])

    assert harness.controller.charge_direction.charging is True
    assert len(harness.controller.charge_direction.entries) == 1


def test_a_used_up_entry_limit_does_not_hold_the_idle_off():
    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    harness.feature = {**harness.feature, "max_charge_entries_per_hour": 1}
    empty = [no_pv_state(soc=15)]
    harness.run(cycles=10, states=empty)
    assert harness.controller.charge_direction.charging is True

    harness.run(cycles=3, states=empty, load=400)
    assert harness.controller.charge_direction.charging is False

    harness.run(cycles=20, states=empty, load=-900)

    assert harness.controller.charge_direction.charging is False
    assert harness.controller.night_min_soc_idle_active is True


def test_a_flickering_surplus_never_leaves_the_idle_without_entering(caplog):
    """The idle is left because a charge was entered, never in anticipation.

    With a separate observer feeding the window, the decision on the exit cycle
    added one more sample, pushed the oldest export out, fell short, and the
    plant re-parked on the next cycle -- a reset and a park write per flicker.
    """

    harness = Harness(
        [ac_device()], load=120, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    empty = [no_pv_state(soc=15)]
    harness.run(cycles=2, states=empty)

    exits_without_charge = 0
    pattern = [-900, 0, 0, -900, -900, -900, -900, 0, -900, -900, 0, -900] * 3
    with caplog.at_level(logging.INFO):
        for load in pattern:
            caplog.clear()
            was_charging = harness.controller.charge_direction.charging
            harness.run(cycles=1, states=empty, load=load)
            left = any(
                "night_min_soc_idle_exit" in record.getMessage()
                for record in caplog.records
            )
            if left and not was_charging:
                exits_without_charge += 1

    assert exits_without_charge == 0


def test_a_charge_entered_in_idle_survives_the_parked_floor_on_a_marginal_surplus():
    """The parked devices still report the floor; seeding from it ends the charge.

    Two devices at 35 W add 70 W to the first desired total, and a 160 W
    surplus then reads as 90 W -- inside the exit band -- one cycle after the
    charge was entered.
    """

    parked = state(
        soc=15, min_soc=15, solar=0, output=0,
        output_limit=ADMIN_STANDBY_FLOOR_W, soc_limit=0, pack_num=1,
        charge_max_limit_w=2400,
    )
    harness = Harness(
        [ac_device("A"), ac_device("B")],
        load=-160,
        min_output_limit=ADMIN_STANDBY_FLOOR_W,
    )
    harness.run(cycles=40, states=[parked, parked])

    assert harness.controller.charge_direction.charging is True
    assert len(harness.controller.charge_direction.entries) == 1


def test_the_rate_limit_warning_is_not_rearmed_by_a_flickering_surplus(caplog):
    harness = Harness(
        [ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W
    )
    harness.feature = {**harness.feature, "max_charge_entries_per_hour": 1}
    empty = [no_pv_state(soc=15)]
    harness.run(cycles=10, states=empty)
    harness.run(cycles=3, states=empty, load=400)
    assert harness.controller.charge_direction.charging is False

    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            harness.run(cycles=5, states=empty, load=-900)
            harness.run(cycles=3, states=empty, load=0)

    warnings = [
        record
        for record in caplog.records
        if "ac_charge_entry_rate_limited" in record.getMessage()
    ]
    assert len(warnings) == 1


@pytest.mark.parametrize("devices, real_surplus_w", [(1, 120), (3, 60)])
def test_the_standby_floor_s_own_export_is_not_counted_as_surplus(
    devices, real_surplus_w
):
    """The floor stops the moment a device switches to charging.

    The meter reads the real surplus plus every floor. Counting the floors
    lowered the entry threshold by 35 W per device, and with three devices a
    60 W surplus entered, overshot into import and left -- twelve times an
    hour.
    """

    feeding_the_floor = no_pv_state(soc=60, output=ADMIN_STANDBY_FLOOR_W, pack_in=45)
    meter = -(real_surplus_w + devices * ADMIN_STANDBY_FLOOR_W)
    harness = Harness(
        [ac_device(f"AC{index}") for index in range(devices)],
        load=meter,
        min_output_limit=ADMIN_STANDBY_FLOOR_W,
    )
    harness.run(cycles=30, states=[feeding_the_floor] * devices)

    assert harness.controller.charge_direction.entries == ()
    assert all(target >= 0 for target in harness.targets)


def test_a_real_surplus_above_the_threshold_still_enters_past_the_floor():
    feeding_the_floor = no_pv_state(soc=60, output=ADMIN_STANDBY_FLOOR_W, pack_in=45)
    harness = Harness(
        [ac_device()],
        load=-(300 + ADMIN_STANDBY_FLOOR_W),
        min_output_limit=ADMIN_STANDBY_FLOOR_W,
    )
    harness.run(cycles=15, states=[feeding_the_floor])

    assert harness.controller.charge_direction.charging is True
