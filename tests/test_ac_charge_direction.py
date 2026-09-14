# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direction stability for AC charging.

An AC mode change moves relays inside the device and the wear is cumulative, so
the question these tests answer is not "does it settle" but "how often can it
possibly switch, in the worst load the installation can produce".

The loop is simulated closed: the integrator's floor follows the direction, the
direction follows the integrator, and an adversarial load drives both.
"""

import pytest

from ems.ac_charge_control import (
    REASON_ENTERED,
    REASON_ENTRY_RATE_LIMITED,
    REASON_LEFT_BELOW_STOP,
    REASON_NOT_POSSIBLE,
    ChargeDirectionSettings,
    ChargeDirectionState,
    decide_charge_direction,
)

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]

LOOP_SECONDS = 5.0
CYCLES_PER_HOUR = int(3600 / LOOP_SECONDS)
SETTINGS = ChargeDirectionSettings(
    start_w=150,
    stop_w=100,
    entry_confirm_cycles=5,
    entry_window_cycles=7,
    max_entries_per_hour=12,
)


def run_closed_loop(
    loads,
    settings=SETTINGS,
    *,
    max_power=800,
    max_charge=1200,
    ramp_up=500,
    ramp_down=300,
):
    """Drive the integrator and the direction decision against each other.

    The ramp is modelled, not skipped: it is what separates the desired total
    from the commanded one, and measuring the exit on the wrong one of those two
    made a "leave immediately" rule take several cycles.

    ``loads`` are filtered grid readings: positive imports, negative exports.
    Returns the switch count, the entry times and the direction trace.
    """

    state = ChargeDirectionState()
    commanded = 0.0
    charging = False
    switches = 0
    entries = []
    trace = []

    for index, load in enumerate(loads):
        now = index * LOOP_SECONDS
        floor = -max_charge if state.charging else 0
        desired = max(floor, min(max_power, commanded + load))

        delta = desired - commanded
        limit = ramp_up if delta > 0 else ramp_down
        if limit > 0 and abs(delta) > limit:
            commanded += limit if delta > 0 else -limit
        else:
            commanded = desired
        commanded = max(floor, min(max_power, commanded))

        decision = decide_charge_direction(
            state,
            charging_possible=True,
            commanded_total_w=commanded,
            desired_total_w=desired,
            filtered_load_w=load,
            now=now,
            settings=settings,
        )
        if decision.charging != charging:
            switches += 1
            charging = decision.charging
            if charging:
                entries.append(now)
        state = decision.state
        trace.append(charging)

    return switches, entries, trace


def test_a_load_oscillating_around_zero_never_switches_direction():
    """The worst intuitive case is in fact the quietest one.

    Idle and discharge are the same AC mode, so a target crossing zero moves no
    relay at all. The mode boundary sits at the start threshold, far from zero,
    and a load that never reaches it never causes a switch.
    """

    loads = [40 if index % 2 else -40 for index in range(CYCLES_PER_HOUR)]

    switches, entries, trace = run_closed_loop(loads)

    assert switches == 0
    assert entries == []
    assert not any(trace)


def test_an_adversarial_oscillation_cannot_exceed_the_hourly_entry_limit():
    """Hysteresis alone cannot bound this; the rate limit is what does.

    The load is built to defeat the band: it holds a deep surplus exactly long
    enough to confirm an entry, then swings to import to force an immediate
    exit, and repeats for an hour. Every mechanism is being worked against at
    once.
    """

    pattern = [-400, -400, -400, -400, 900]
    loads = (pattern * (CYCLES_PER_HOUR // len(pattern) + 1))[:CYCLES_PER_HOUR]

    switches, entries, _trace = run_closed_loop(loads)

    assert len(entries) <= SETTINGS.max_entries_per_hour
    # Each entry costs at most one exit, so the relay count is bounded with it.
    assert switches <= 2 * SETTINGS.max_entries_per_hour


def test_a_surplus_that_merely_brushes_the_band_never_enters():
    """Between stop and start, a device that is not charging must not start."""

    loads = [-120] * CYCLES_PER_HOUR

    switches, entries, _trace = run_closed_loop(loads)

    assert switches == 0
    assert entries == []


def test_a_real_surplus_enters_once_and_stays():
    loads = [-500] * 200

    switches, entries, trace = run_closed_loop(loads)

    assert switches == 1
    assert len(entries) == 1
    # Five observations, the fifth of which enters: four loop intervals of
    # waiting, not five.
    assert trace[:4] == [False] * 4
    assert all(trace[4:])


def _observe(state, load, settings=SETTINGS, now=0.0):
    return decide_charge_direction(
        state, charging_possible=True, commanded_total_w=0,
        desired_total_w=load, filtered_load_w=load, now=now, settings=settings,
    )


def test_a_brief_dip_no_longer_discards_the_confirmation():
    """Counting in a row made one dip throw away everything gathered so far.

    Counting within a window keeps it: the dip costs one observation, not five.
    """

    state = ChargeDirectionState()
    for load in (-500, -500, -100, -500, -500):
        decision = _observe(state, load)
        state = decision.state
    assert decision.charging is False

    # Five surplus observations are now inside the seven-cycle window.
    decision = _observe(state, -500)
    assert decision.charging is True
    assert decision.reason == REASON_ENTERED


def test_height_can_never_substitute_for_duration():
    """The reason this counts observations instead of averaging them.

    A mean over the window would read a single spike ten times the threshold as
    a sustained surplus and move a relay for something already over. Here it is
    one observation, and four alternating spikes never reach five.
    """

    state = ChargeDirectionState()
    for index in range(40):
        load = -1600 if index % 2 == 0 else 0
        decision = _observe(state, load)
        state = decision.state
        assert decision.charging is False, index


def test_leaving_charge_takes_one_cycle_and_no_confirmation():
    charging = ChargeDirectionState(charging=True, entries=(0.0,))

    decision = decide_charge_direction(
        charging,
        charging_possible=True,
        # The ramp still holds the commanded charge deep in charge territory;
        # the unramped desired total has already crossed the band's lower edge.
        # Measuring the exit on the commanded value made this take five cycles
        # of drawing from the grid.
        commanded_total_w=-900,
        desired_total_w=-90,
        filtered_load_w=810,
        now=10.0,
        settings=SETTINGS,
    )

    assert decision.charging is False
    assert decision.reason == REASON_LEFT_BELOW_STOP


def test_the_way_back_is_never_blocked_by_a_threshold_or_a_counter():
    """Failing closed on the return path would leave hardware on the grid."""

    deep_in_charge = ChargeDirectionState(
        charging=True, entries=tuple(float(i) for i in range(50))
    )

    decision = decide_charge_direction(
        deep_in_charge,
        charging_possible=False,
        # Every signal still says "keep charging"; the permission says otherwise.
        commanded_total_w=-900,
        desired_total_w=-900,
        filtered_load_w=-900,
        now=100.0,
        settings=SETTINGS,
    )

    assert decision.charging is False
    assert decision.reason == REASON_NOT_POSSIBLE


def test_the_rate_limit_refuses_visibly_rather_than_silently():
    used_up = ChargeDirectionState(
        charging=False,
        entry_window=(True,) * (SETTINGS.entry_confirm_cycles - 1),
        entries=tuple(float(i) for i in range(SETTINGS.max_entries_per_hour)),
    )

    decision = decide_charge_direction(
        used_up, charging_possible=True, commanded_total_w=0,
        desired_total_w=-900, filtered_load_w=-900, now=10.0, settings=SETTINGS,
    )

    assert decision.charging is False
    assert decision.rate_limited is True
    assert decision.reason == REASON_ENTRY_RATE_LIMITED


def test_entries_older_than_an_hour_stop_counting():
    aged = ChargeDirectionState(
        charging=False,
        entry_window=(True,) * (SETTINGS.entry_confirm_cycles - 1),
        entries=tuple(float(i) for i in range(SETTINGS.max_entries_per_hour)),
    )

    decision = decide_charge_direction(
        aged, charging_possible=True, commanded_total_w=0,
        desired_total_w=-900, filtered_load_w=-900, now=4000.0, settings=SETTINGS,
    )

    assert decision.charging is True
    assert decision.state.entries == (4000.0,)


def test_charging_does_not_exit_on_its_own_balanced_meter():
    """The trap this module exists to avoid.

    While charging, the surplus has been consumed by the charge, so the meter
    reads balanced. Judging the exit on that reading would leave immediately and
    re-enter three cycles later, forever.
    """

    state = ChargeDirectionState(charging=True, entries=(0.0,))
    commanded = -600.0

    for index in range(100):
        decision = decide_charge_direction(
            state,
            charging_possible=True,
            commanded_total_w=commanded,
            # A perfectly regulated grid: the meter shows nothing, so the
            # desired total is exactly the charge already being held.
            desired_total_w=commanded,
            filtered_load_w=0,
            now=index * LOOP_SECONDS,
            settings=SETTINGS,
        )
        assert decision.charging is True, index
        state = decision.state
