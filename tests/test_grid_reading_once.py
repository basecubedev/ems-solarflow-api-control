# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each grid reading is regulated on once, however many cycles see it.

The control loop adds every grid reading to the total it already commands. A
meter that keeps its last reading between two new ones -- an MQTT meter
publishing every 10 s under a 3 s loop, a Tibber bridge streaming every ~3 s, a
DSMR 4 smart meter -- used to hand that reading to several cycles, and each of
them added the same correction again. These tests run the real control loop
over a device that delivers what it is told and a meter that publishes only
every few cycles.
"""

import logging
import statistics
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.config import AC_CHARGE_CONTROL_DEFAULTS
from ems.controller import EMSController
from ems.grid_reading import GridReading
from ems.health import CommHealth
from tests.test_write_gates import RuntimeStateStub, device, state

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
    pytest.mark.simulation,
]


class CachedMeter:
    """A meter that keeps its last reading between two new ones.

    Each :meth:`publish` is a new measurement; between two of them every read
    returns the same one, the way a background reader of a broker or a stream
    does.
    """

    def __init__(self, value=0.0):
        self.health = CommHealth("cached", kind="read")
        self.value = value
        self.sequence = 0

    def publish(self, value):
        self.value = round(value, 1)
        self.sequence += 1

    def get_power(self):
        self.health.record_success(latency_ms=1.0)
        return self.value

    def get_reading(self):
        return GridReading(self.get_power(), self.sequence)


class ClosedLoop:
    """One device that delivers what it is told, behind a meter of its own pace.

    ``house`` maps a cycle to the house load; the meter shows the house load
    less what the device delivers, but only on the cycles ``publishes`` picks.
    """

    def __init__(self, publishes, house, max_power=1200, charge=False):
        self.dev = device("WR1")
        self.dev.max_power = max_power
        self.max_power = max_power
        self.meter = CachedMeter()
        self.publishes = publishes
        self.house = house
        self.delivered = 0
        self.outputs = []
        self.controller = EMSController(
            devices=[self.dev],
            shelly=self.meter,
            sleep_enabled=False,
            runtime_state=RuntimeStateStub(),
        )
        self.controller.set_output_limit = self.follow
        self.feature = {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": charge}

    def follow(self, dev, value, charge_exit=None):
        self.delivered = value
        return True

    def run(self, cycles):
        with patch(
            "ems.controller.fetch_all_devices",
            side_effect=lambda _devices: [
                state(solar=0, output=self.delivered, output_limit=self.delivered)
            ],
        ), patch("ems.controller.cfg.SYSTEM_ENABLED", True), patch(
            "ems.controller.cfg.MAX_TOTAL_POWER", self.max_power
        ), patch("ems.controller.cfg.MAX_DEVICE_POWER", self.max_power), patch(
            "ems.controller.cfg.MIN_OUTPUT_LIMIT", 0
        ), patch("ems.controller.cfg.DEADBAND", 10), patch(
            "ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0
        ), patch.object(cfg, "AC_CHARGE_CONTROL_CONFIG", self.feature):
            for _ in range(cycles):
                cycle = len(self.outputs)
                if self.publishes(cycle):
                    self.meter.publish(self.house(cycle) - self.delivered)
                self.controller.run_once()
                self.outputs.append(self.delivered)
        return self.outputs


def every(cycles):
    return lambda cycle: cycle % cycles == 0


def step_response(publishes, before, after, step_at, cycles, max_power=1200):
    loop = ClosedLoop(
        publishes,
        lambda cycle: before if cycle < step_at else after,
        max_power=max_power,
    )
    return loop.run(cycles)


SETTLE_READINGS = 12
READINGS = 30


@pytest.mark.parametrize("cycles_per_reading", [2, 3, 4])
def test_a_meter_slower_than_the_loop_regulates_once_per_reading(cycles_per_reading):
    """Read at each new reading, the output is what a meter read every cycle gives.

    Before the fix a reading every third cycle drove 600 W of house load to
    1182 W, back down to 10 W and up to 1070 W again: each cycle added the
    same correction once more.
    """

    every_cycle = step_response(every(1), 200, 450, SETTLE_READINGS, READINGS)
    slower = step_response(
        every(cycles_per_reading),
        200,
        450,
        SETTLE_READINGS * cycles_per_reading,
        READINGS * cycles_per_reading,
    )

    assert slower[::cycles_per_reading] == every_cycle


def test_a_meter_that_publishes_unevenly_regulates_once_per_reading():
    """A stream every ~3 s under a 3 s loop: some cycles get a reading, some not."""

    pattern = [True, False, True, True, False, False, True, False]

    def publishes(cycle):
        return pattern[cycle % len(pattern)]

    cycles = len(pattern) * 15
    step_at = len(pattern) * 5
    uneven = step_response(publishes, 200, 450, step_at, cycles)
    reading_cycles = [cycle for cycle in range(cycles) if publishes(cycle)]
    every_cycle = step_response(
        every(1),
        200,
        450,
        sum(1 for cycle in range(step_at) if publishes(cycle)),
        len(reading_cycles),
    )

    assert [uneven[cycle] for cycle in reading_cycles] == every_cycle


@pytest.mark.parametrize("cycles_per_reading", [2, 3, 4])
def test_a_ramp_limited_correction_carries_on_between_readings(cycles_per_reading):
    """The ramp finishes one reading's correction; it does not add it again."""

    every_cycle = step_response(
        every(1), 200, 1000, SETTLE_READINGS, READINGS, max_power=2000
    )
    step_at = SETTLE_READINGS * cycles_per_reading
    slower = step_response(
        every(cycles_per_reading),
        200,
        1000,
        step_at,
        READINGS * cycles_per_reading,
        max_power=2000,
    )

    assert max(slower[step_at:]) <= max(every_cycle[SETTLE_READINGS:])
    assert max(slower[step_at:]) <= 1000 + 150
    assert slower[-cycles_per_reading:] == [
        pytest.approx(1000, abs=30)
    ] * cycles_per_reading


def test_a_slower_meter_follows_a_load_drop():
    every_cycle = step_response(every(1), 900, 150, SETTLE_READINGS, READINGS)
    step_at = SETTLE_READINGS * 3
    slower = step_response(every(3), 900, 150, step_at, READINGS * 3)

    assert slower[step_at - 1] == pytest.approx(900, abs=30)
    assert min(slower[step_at:]) >= min(every_cycle[SETTLE_READINGS:])
    assert slower[-3:] == [pytest.approx(150, abs=30)] * 3


def test_a_repeated_reading_is_no_meter_failure(caplog):
    loop = ClosedLoop(every(3), lambda cycle: 400)
    with caplog.at_level(logging.WARNING):
        loop.run(12)

    assert loop.controller.grid_meter_holding is False
    assert "grid_meter_unavailable_holding_target" not in caplog.text


def test_a_repeated_reading_leaves_the_load_filter_alone():
    loop = ClosedLoop(every(3), lambda cycle: 400)
    loop.run(1)
    history = list(loop.controller.load_history)
    filtered = loop.controller.filtered_load_w

    loop.run(2)

    assert list(loop.controller.load_history) == history
    assert loop.controller.filtered_load_w == filtered


def test_a_meter_that_stops_answering_still_holds_with_a_warning(caplog):
    loop = ClosedLoop(every(1), lambda cycle: 400)
    loop.run(6)
    held = loop.controller.commanded_total_w
    loop.publishes = lambda cycle: False
    loop.meter.get_power = lambda: (
        loop.meter.health.record_failure(error=OSError("gone"), stale_used=True),
        loop.meter.value,
    )[1]

    with caplog.at_level(logging.WARNING):
        loop.run(3)

    assert loop.controller.grid_meter_holding is True
    assert "grid_meter_unavailable_holding_target" in caplog.text
    assert loop.controller.commanded_total_w == held


def test_a_meter_back_from_a_short_outage_with_its_last_reading_repeats_it():
    """A HomeWizard or DSMR client that recovers on the telegram it held adds nothing."""

    loop = ClosedLoop(every(1), lambda cycle: 400)
    loop.run(6)
    loop.publishes = lambda cycle: False
    answer = loop.meter.get_power
    loop.meter.get_power = lambda: (
        loop.meter.health.record_failure(error=OSError("gone"), stale_used=True),
        loop.meter.value,
    )[1]
    loop.run(1)
    assert loop.controller.grid_meter_holding is True
    held = loop.controller.commanded_total_w
    history = list(loop.controller.load_history)

    loop.meter.get_power = answer
    loop.run(2)

    assert loop.controller.grid_meter_holding is False
    assert loop.controller.grid_reading_repeated is True
    assert loop.controller.commanded_total_w == held
    assert list(loop.controller.load_history) == history


def test_a_meter_without_reading_identity_regulates_every_cycle():
    """A client that measures on every read keeps today's behaviour."""

    class MeasuringMeter:
        def get_power(self):
            return 500

    loop = ClosedLoop(lambda cycle: False, lambda cycle: 0)
    loop.controller.shelly = MeasuringMeter()
    totals = []
    for _ in range(3):
        loop.run(1)
        totals.append(loop.controller.commanded_total_w)

    assert totals[0] < totals[1] < totals[2]


class ChargeLoop:
    """The charge regulator's harness, behind a meter of its own pace."""

    def __init__(self, cycles_per_reading, power=-900):
        from tests.test_ac_charge_regulator import Harness, charging_device

        self.harness = Harness([charging_device()], load=power)
        self.meter = CachedMeter()
        self.harness.controller.shelly = self.meter
        self.cycles_per_reading = cycles_per_reading
        self.power = power
        self.cycle = 0

    def run(self, cycles):
        for _ in range(cycles):
            if self.cycle % self.cycles_per_reading == 0:
                self.meter.publish(self.power)
            self.harness.run(cycles=1)
            self.cycle += 1
        return self.harness.controller

    def run_until_charging(self, limit=60):
        while self.cycle < limit:
            if self.run(1).charge_direction.charging:
                return self.cycle, self.meter.sequence
        return None


def test_a_charge_is_entered_on_as_many_readings_as_with_a_meter_read_every_cycle():
    """The entry window counts readings, not cycles.

    Before the fix a reading every third cycle entered a charge on the second
    reading: five cycles of one surplus counted as five confirmations.
    """

    every_cycle = ChargeLoop(1).run_until_charging()
    slower = ChargeLoop(3).run_until_charging()

    assert every_cycle is not None and slower is not None
    assert slower[1] == every_cycle[1]
    assert slower[0] > every_cycle[0]


@pytest.mark.parametrize("cycles_per_reading", [2, 3])
def test_a_charge_entered_on_a_slower_meter_runs_on_between_readings(cycles_per_reading):
    """The cycle after an entry has no new reading, and that is no reason to leave.

    Before the fix the first repeated cycle read the total the entry cycle left
    at zero as "the surplus is gone": eight entries in 120 cycles, each one
    cycle long, and not one charge written.
    """

    loop = ChargeLoop(cycles_per_reading)
    assert loop.run_until_charging() is not None

    loop.run(10 * cycles_per_reading)

    controller = loop.harness.controller
    assert controller.charge_direction.charging is True
    assert len(controller.charge_direction.entries) == 1
    assert any(target < 0 for target in loop.harness.targets)


@pytest.mark.parametrize("cycles_per_reading", [2, 3])
@pytest.mark.parametrize("standby_floor", [0, 35], ids=["regulated", "night_idle"])
def test_a_device_charges_behind_a_slower_meter(cycles_per_reading, standby_floor):
    """A device that follows its commands, entered from the loop or from the night idle."""

    from tests.test_ac_charge_regulator import (
        Harness,
        following_http_device,
        pv_less_state,
    )

    item = pv_less_state(soc=15)
    dev, hardware = following_http_device(item)
    harness = Harness(
        [dev], load=-900, seconds_per_cycle=5, min_output_limit=standby_floor
    )
    harness.controller.set_output_limit = hardware
    meter = CachedMeter()
    harness.controller.shelly = meter
    for cycle in range(40 * cycles_per_reading):
        if cycle % cycles_per_reading == 0:
            meter.publish(-900 + item.grid_input)
        harness.run(cycles=1, states=[item])

    assert item.ac_mode == 1
    assert item.grid_input > 0
    assert len(harness.controller.charge_direction.entries) == 1


def reporting_output(double):
    """The hardware double, also reporting the output it was told in the output direction."""

    class ReportingOutput(double):
        def poll(self):
            super().poll()
            self.state.output = self.state.output_limit if self.state.ac_mode == 2 else 0

    return ReportingOutput


class FollowingChargeLoop:
    """A device that follows its commands, behind a meter of its own pace.

    The meter shows the house's surplus or deficit, plus what the device draws
    from the grid while it charges, less what it feeds out while it discharges.
    """

    def __init__(self, cycles_per_reading, house=-900, standby_floor=0, soc=50):
        from tests.test_ac_charge_regulator import (
            Harness,
            following_http_device,
            pv_less_state,
        )

        from tests.test_ac_charge_regulator import FollowingHardware

        self.item = pv_less_state(soc=soc)
        self.dev, self.hardware = following_http_device(
            self.item, double=reporting_output(FollowingHardware)
        )
        self.harness = Harness(
            [self.dev], load=house, seconds_per_cycle=5, min_output_limit=standby_floor
        )
        self.harness.controller.set_output_limit = self.hardware
        self.meter = CachedMeter()
        self.harness.controller.shelly = self.meter
        self.cycles_per_reading = cycles_per_reading
        self.house = house
        self.cycle = 0
        self.entered_on = None

    @property
    def controller(self):
        return self.harness.controller

    def run(self, cycles):
        for _ in range(cycles):
            if self.cycle % self.cycles_per_reading == 0:
                self.meter.publish(self.house + self.item.grid_input - self.item.output)
            self.harness.run(cycles=1, states=[self.item])
            if self.entered_on is None and self.controller.charge_direction.charging:
                self.entered_on = self.meter.sequence
            self.cycle += 1
        return self.controller

    def run_to_reading(self):
        """Run until the next cycle brings a new reading."""

        while self.cycle % self.cycles_per_reading != 0:
            self.run(1)


@pytest.mark.parametrize("cycles_per_reading", [3, 6])
def test_a_charge_ends_on_a_deficit_as_soon_as_behind_a_meter_read_every_cycle(cycles_per_reading):
    """The exit may reuse a reading; only the entry and the total may not.

    Judged on new readings only, a load that rose kept the charge drawing for a
    whole meter period more: the load filter, on by default then, halved the
    first reading of a deficit, and the next reading came 15 s or 30 s later,
    while about 260 W were still drawn for the charge on top of the deficit.
    """

    def cycles_to_exit(loop):
        loop.run(30 * loop.cycles_per_reading)
        assert loop.item.ac_mode == 1, "the regulator never charged the device"
        loop.run_to_reading()
        loop.house = 600
        for cycles in range(1, 10 * loop.cycles_per_reading):
            loop.run(1)
            if not loop.controller.charge_direction.charging:
                return cycles
        return None

    every_cycle = cycles_to_exit(FollowingChargeLoop(1))
    slower = cycles_to_exit(FollowingChargeLoop(cycles_per_reading))

    assert every_cycle is not None
    assert slower is not None and slower <= every_cycle


@pytest.mark.parametrize("cycles_per_reading", [3, 6])
@pytest.mark.parametrize("surplus_left", [50, 80])
def test_a_charge_ends_on_a_small_surplus_left_as_soon_as_behind_a_meter_read_every_cycle(
    cycles_per_reading, surplus_left
):
    """A load that leaves less surplus than the stop threshold ends the charge at once.

    Judged on the filtered reading once more, the filter's lag lasted a meter
    period: the charge drew from the grid until the reading after the one that
    showed the load, 30 s with a meter every sixth cycle.
    """

    def cycles_to_exit(loop):
        loop.run(30 * loop.cycles_per_reading)
        assert loop.item.ac_mode == 1, "the regulator never charged the device"
        loop.run_to_reading()
        loop.house = -surplus_left
        for cycles in range(1, 10 * loop.cycles_per_reading):
            loop.run(1)
            if not loop.controller.charge_direction.charging:
                return cycles
        return None

    every_cycle = cycles_to_exit(FollowingChargeLoop(1, house=-600))
    slower = cycles_to_exit(FollowingChargeLoop(cycles_per_reading, house=-600))

    assert every_cycle is not None
    assert slower is not None and slower <= every_cycle


@pytest.mark.parametrize("cycles_per_reading", [1, 3])
def test_after_a_charge_ends_the_device_covers_the_house_without_charging_again(cycles_per_reading):
    """Judged over the last readings: read every cycle, this device swings around the house.

    It applies a command one poll late, so a meter read every cycle shows the
    output from before the last write, the swing ``test_write_dead_time``
    names. One final value only caught the swing at a lucky moment.
    """

    loop = FollowingChargeLoop(cycles_per_reading, house=-600)
    loop.run(30 * cycles_per_reading)
    assert loop.item.ac_mode == 1, "the regulator never charged the device"
    loop.run_to_reading()
    outputs = []

    def house(cycle):
        outputs.append(loop.item.output)
        return 400

    entries = charge_entries(loop, 40 * cycles_per_reading, house)

    assert entries == 0
    assert loop.item.ac_mode == 2
    assert statistics.mean(outputs[-10 * cycles_per_reading:]) == pytest.approx(400, abs=100)


@pytest.mark.parametrize("cycles_per_reading", [2, 3, 6])
def test_a_charge_entered_behind_a_slower_meter_starts_with_the_next_reading(cycles_per_reading):
    """The floor of the entry cycle is not carried: spending waits for a reading that allows it."""

    loop = FollowingChargeLoop(cycles_per_reading)
    while not loop.controller.charge_direction.charging:
        loop.run(1)
    entered = loop.meter.sequence

    while loop.meter.sequence == entered:
        assert loop.item.grid_input == 0
        loop.run(1)
    loop.run(1)

    assert loop.item.grid_input > 0


@pytest.mark.parametrize("cycles_per_reading", [1, 2, 3, 6])
def test_a_surplus_gone_right_after_the_entry_draws_nothing_from_the_grid(cycles_per_reading):
    """Carried into the charge, the entry reading's surplus drew up to 700 W for a meter period."""

    loop = FollowingChargeLoop(cycles_per_reading, house=-700)
    while not loop.controller.charge_direction.charging:
        loop.run(1)
    loop.house = 300

    drawn = []
    for _ in range(4 * cycles_per_reading):
        loop.run(1)
        drawn.append(loop.item.grid_input)

    assert max(drawn) == 0
    assert loop.controller.charge_direction.charging is False


def charge_entries(loop, cycles, house=None):
    """Run ``cycles`` and count the charges entered; ``house`` maps a cycle to the house."""

    entries = 0
    for _ in range(cycles):
        if house is not None:
            loop.house = house(loop.cycle)
        charging = loop.controller.charge_direction.charging
        loop.run(1)
        entries += not charging and loop.controller.charge_direction.charging
    return entries


HOUR_OF_CYCLES = 3600 // 5


@pytest.mark.parametrize("cycles_per_reading", [2, 3])
@pytest.mark.parametrize("surplus", [170, 220])
def test_a_steady_small_surplus_keeps_one_charge_behind_a_slower_meter(cycles_per_reading, surplus):
    """The exit on a repeated reading must not count that reading twice.

    Judged on the total the reading had already corrected plus the reading once
    more, a steady 170 W surplus entered and left a charge twelve times an hour
    and then exported for the rest of it, the hourly limit spent.
    """

    every_cycle = charge_entries(FollowingChargeLoop(1, house=-surplus), HOUR_OF_CYCLES)
    slower = charge_entries(
        FollowingChargeLoop(cycles_per_reading, house=-surplus), HOUR_OF_CYCLES
    )

    assert every_cycle == 1
    assert slower == 1


@pytest.mark.parametrize("cycles_per_reading", [2, 3])
def test_a_noisy_surplus_switches_no_more_behind_a_slower_meter(cycles_per_reading):
    import random

    def noisy(seed):
        rng = random.Random(seed)
        return lambda cycle: -300 + rng.uniform(-100, 100)

    every_cycle = charge_entries(FollowingChargeLoop(1, house=-300), HOUR_OF_CYCLES, noisy(4))
    slower = charge_entries(
        FollowingChargeLoop(cycles_per_reading, house=-300), HOUR_OF_CYCLES, noisy(4)
    )

    assert every_cycle == 1
    assert slower == 1


@pytest.mark.parametrize("cycles_per_reading", [1, 3])
def test_a_smaller_surplus_turns_a_charge_down_and_keeps_it(cycles_per_reading):
    loop = FollowingChargeLoop(cycles_per_reading, house=-400)
    assert charge_entries(loop, 60 * cycles_per_reading) == 1
    loop.run_to_reading()

    entries = charge_entries(loop, 40 * cycles_per_reading, lambda cycle: -200)

    assert entries == 0
    assert loop.controller.charge_direction.charging is True
    assert 150 <= loop.item.grid_input <= 250


@pytest.mark.parametrize("standby_floor", [0, 35], ids=["regulated", "night_idle"])
def test_a_charge_is_entered_on_the_same_reading_behind_a_slower_meter(standby_floor):
    """Entered from the loop or from the night idle, the window counts readings."""

    def entry_reading(cycles_per_reading):
        loop = FollowingChargeLoop(cycles_per_reading, standby_floor=standby_floor, soc=15)
        loop.run(30 * cycles_per_reading)
        assert loop.entered_on is not None
        return loop.entered_on

    assert entry_reading(3) == entry_reading(1)


def advance_to(loop, repeated):
    """Run until the next cycle is one that repeats a reading, or one that brings one."""

    while (loop.cycle % loop.cycles_per_reading == 0) is repeated:
        loop.run(1)


def test_switching_the_feature_off_ends_a_charge_on_a_repeated_reading():
    from tests.test_ac_charge_regulator import leaves_the_charge

    loop = FollowingChargeLoop(3)
    loop.run(90)
    assert loop.item.ac_mode == 1, "the regulator never charged the device"
    loop.run_to_reading()
    loop.run(1)
    before = len(loop.hardware.writes)

    loop.harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": False}
    loop.run(1)

    assert loop.controller.grid_reading_repeated is True
    assert loop.controller.charge_direction.charging is False
    assert [leaves_the_charge(write) for write in loop.hardware.writes[before:]] == [True]
    assert loop.item.ac_mode == 2


def test_a_new_reading_of_a_deficit_ends_a_charge_in_its_cycle():
    from tests.test_ac_charge_regulator import leaves_the_charge

    loop = FollowingChargeLoop(3)
    loop.run(90)
    assert loop.item.ac_mode == 1, "the regulator never charged the device"
    loop.run_to_reading()
    before = len(loop.hardware.writes)

    loop.house = 1200
    loop.run(1)

    assert loop.controller.grid_reading_repeated is False
    assert loop.controller.charge_direction.charging is False
    assert [leaves_the_charge(write) for write in loop.hardware.writes[before:]] == [True]
    assert loop.item.ac_mode == 2


def test_a_charge_left_on_a_repeated_reading_sizes_the_discharge_on_the_next_one(caplog):
    """The device is noted as leaving on the cycle that ends the charge, repeated or not.

    Skipping that note on a repeated cycle lost it for good: the device had
    left by the next new reading, was no longer on the record, and the reading
    was integrated with the charge's AC input still in the filter.
    """

    from tests.test_ac_charge_regulator import (
        Harness,
        following_http_device,
        pv_less_state,
    )

    item = pv_less_state(soc=50)
    dev, hardware = following_http_device(item)
    harness = Harness([dev], load=-900, seconds_per_cycle=5)
    harness.controller.set_output_limit = hardware
    meter = CachedMeter()
    harness.controller.shelly = meter
    cycle = 0

    def run(cycles):
        nonlocal cycle
        for _ in range(cycles):
            if cycle % 3 == 0:
                meter.publish(-900 + item.grid_input)
            harness.run(cycles=1, states=[item])
            cycle += 1

    run(90)
    assert item.ac_mode == 1, "the regulator never charged the device"
    while cycle % 3 != 1:
        run(1)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": False}

    with caplog.at_level(logging.DEBUG):
        run(1)
        assert harness.controller.grid_reading_repeated is True
        assert harness.controller.charge_direction.charging is False
        run(2)

    assert item.ac_mode == 2
    sized = [
        line for line in caplog.text.splitlines()
        if "event=output_control_sized_by_house_load" in line
    ]
    assert len(sized) == 1, caplog.text
    assert "reason=left_ac_charge" in sized[0]


def test_a_repeated_reading_records_no_entry_observation():
    loop = ChargeLoop(3)
    loop.run(1)
    controller = loop.harness.controller
    window = controller.charge_direction.entry_window

    loop.run(2)

    assert controller.grid_reading_repeated is True
    assert controller.charge_direction.entry_window == window


def test_a_held_reading_still_records_a_failed_entry_observation():
    loop = ChargeLoop(1)
    loop.run(3)
    controller = loop.harness.controller
    window = controller.charge_direction.entry_window
    loop.meter.get_power = lambda: (
        loop.meter.health.record_failure(error=OSError("gone"), stale_used=True),
        loop.meter.value,
    )[1]

    loop.run(1)

    assert controller.grid_meter_holding is True
    assert controller.grid_reading_repeated is False
    assert controller.charge_direction.entry_window[-1] is False
    assert len(controller.charge_direction.entry_window) >= len(window)


def controller_for_totals(monkeypatch, **output_control):
    from tests.test_simulated_power_control_regression import controller

    return controller(
        monkeypatch,
        output_control={
            "filter_enabled": False,
            "load_deadband_w": 0,
            "target_deadband_w": 0,
            "ramp_up_w_per_cycle": 200,
            "ramp_down_w_per_cycle": 200,
            "large_import_bypass_w": 10000,
            "large_export_bypass_w": 10000,
            **output_control,
        },
    )


def total_states():
    from tests.test_simulated_power_control_regression import state as regression_state

    return [regression_state(output_limit=400)]


def test_a_repeated_reading_without_a_pending_correction_holds_the_total(monkeypatch):
    ems = controller_for_totals(monkeypatch)
    states = total_states()
    ems.stabilized_total_target(50, states, 1200)
    first = ems.commanded_total_w

    held = ems.stabilized_total_target(50, states, 1200, new_reading=False)

    assert held == first == 450


def test_a_repeated_reading_carries_a_ramp_limited_correction_on(monkeypatch):
    ems = controller_for_totals(monkeypatch)
    states = total_states()

    assert ems.stabilized_total_target(500, states, 1200) == 600
    assert ems.stabilized_total_target(500, states, 1200, new_reading=False) == 800
    assert ems.stabilized_total_target(500, states, 1200, new_reading=False) == 900
    assert ems.stabilized_total_target(500, states, 1200, new_reading=False) == 900


def test_a_total_set_elsewhere_ends_the_pending_correction(monkeypatch):
    ems = controller_for_totals(monkeypatch)
    states = total_states()
    ems.stabilized_total_target(500, states, 1200)
    ems.commanded_total_w = 300

    assert ems.stabilized_total_target(500, states, 1200, new_reading=False) == 300


def test_a_reset_ends_the_pending_correction(monkeypatch):
    ems = controller_for_totals(monkeypatch)
    states = total_states()
    ems.stabilized_total_target(500, states, 1200)
    ems.reset_output_control_state()

    assert ems.stabilized_total_target(500, states, 1200, new_reading=False) == 400


def test_a_repeated_reading_keeps_to_a_lower_ceiling(monkeypatch):
    ems = controller_for_totals(monkeypatch)
    states = total_states()
    ems.stabilized_total_target(500, states, 1200)

    assert ems.stabilized_total_target(500, states, 500, new_reading=False) == 500


def test_a_correction_the_ceiling_held_back_carries_on_once_it_rises(monkeypatch):
    ems = controller_for_totals(monkeypatch)
    states = total_states()

    assert ems.stabilized_total_target(500, states, 500) == 500
    assert ems.stabilized_total_target(500, states, 500, new_reading=False) == 500
    assert ems.stabilized_total_target(500, states, 1200, new_reading=False) == 700
    assert ems.stabilized_total_target(500, states, 1200, new_reading=False) == 900
    assert ems.stabilized_total_target(500, states, 1200, new_reading=False) == 900


def test_a_repeated_reading_pinned_at_the_ceiling_logs_no_deadband_hold(monkeypatch, caplog):
    ems = controller_for_totals(monkeypatch)
    states = total_states()
    ems.stabilized_total_target(500, states, 500)

    with caplog.at_level(logging.DEBUG):
        ems.stabilized_total_target(500, states, 500, new_reading=False)

    assert "output_control_deadband_hold" not in caplog.text


def test_the_charge_floor_of_a_reading_s_cycle_is_never_carried(monkeypatch):
    ems = controller_for_totals(monkeypatch)
    states = total_states()
    ems.stabilized_total_target(-900, states, 1200)
    assert ems.commanded_total_w == 200

    monkeypatch.setattr(ems, "commanded_total_floor_w", lambda: -800)

    assert ems.stabilized_total_target(-900, states, 1200, new_reading=False) == 0
    assert ems.stabilized_total_target(-900, states, 1200, new_reading=False) == 0


def test_a_discharge_sized_while_a_charge_is_left_ends_the_pending_correction(monkeypatch):
    """Also when the sized total is the very one the correction had reached."""

    sized = leaving_controller(monkeypatch, repeated=False).discharge_total_leaving_a_charge(
        400, total_states(), -300, 500
    )
    ems = leaving_controller(monkeypatch, repeated=False)
    ems._reading_goal = (sized, 1800)

    ems.discharge_total_leaving_a_charge(400, total_states(), -300, 500)

    assert ems.commanded_total_w == sized
    assert ems.stabilized_total_target(400, total_states(), 1200, new_reading=False) == sized


def test_a_repeated_reading_still_drops_to_standby_without_export_capacity(monkeypatch):
    ems = controller_for_totals(monkeypatch)
    states = total_states()
    ems.stabilized_total_target(500, states, 1200)

    total = ems.stabilized_total_target(
        500,
        states,
        1200,
        has_export_capacity=False,
        standby_total_w=0,
        new_reading=False,
    )

    assert total == 0


def test_a_repeated_reading_feeds_nothing_to_the_load_filter(monkeypatch):
    from tests.test_simulated_power_control_regression import controller

    ems = controller(monkeypatch)
    states = total_states()
    ems.stabilized_total_target(300, states, 1200)
    history = list(ems.load_history)
    filtered = ems.filtered_load_w

    ems.stabilized_total_target(300, states, 1200, new_reading=False)

    assert list(ems.load_history) == history
    assert ems.filtered_load_w == filtered


def leaving_controller(monkeypatch, repeated):
    ems = controller_for_totals(monkeypatch)
    ems.commanded_total_w = -300
    ems.load_history.extend([100, 120])
    ems.filtered_load_w = 110
    ems.charges_being_left.add("WR1")
    ems.grid_reading_repeated = repeated
    return ems


def test_a_new_reading_sizes_the_discharge_once_a_charge_is_left(monkeypatch):
    ems = leaving_controller(monkeypatch, repeated=False)

    total = ems.discharge_total_leaving_a_charge(400, total_states(), -300, 1200)

    assert total == ems.commanded_total_w != -300
    assert list(ems.load_history) == []


def test_a_repeated_reading_judges_a_charge_on_the_total_it_asks_for(monkeypatch):
    """The total from before the reading's correction plus the reading, unfiltered."""

    from tests.test_simulated_power_control_regression import controller

    ems = controller(monkeypatch, output_control={"ramp_down_w_per_cycle": 50})
    states = total_states()
    ems.stabilized_total_target(0, states, 1200)
    before = ems.commanded_total_w
    ems.stabilized_total_target(-300, states, 1200)
    history = list(ems.load_history)

    judged = ems.repeated_reading_charge_total_w(-300)

    assert ems.commanded_total_w != before
    assert judged == before - 300
    assert list(ems.load_history) == history


def test_a_repeated_reading_sizes_no_discharge_while_a_charge_is_left(monkeypatch):
    ems = leaving_controller(monkeypatch, repeated=True)

    total = ems.discharge_total_leaving_a_charge(400, total_states(), -300, 1200)

    assert total == -300
    assert ems.commanded_total_w == -300
    assert list(ems.load_history) == [100, 120]
    assert ems.filtered_load_w == 110
    assert ems.charges_being_left == {"WR1"}


def test_a_reading_repeated_while_the_meter_holds_is_reported_as_held_only(monkeypatch):
    ems = controller_for_totals(monkeypatch)
    meter = CachedMeter()
    meter.publish(100)
    ems.shelly = meter
    ems.grid_meter_holding = True
    ems.note_grid_reading(meter.get_reading())

    assert ems.note_grid_reading(meter.get_reading()) is False
    assert ems.grid_reading_repeated is False


def test_a_client_switched_at_runtime_starts_with_a_new_reading(monkeypatch):
    ems = controller_for_totals(monkeypatch)
    first, second = CachedMeter(), CachedMeter()
    first.publish(100)
    second.publish(100)
    ems.shelly = first
    ems.note_grid_reading(first.get_reading())

    ems.shelly = second

    assert ems.note_grid_reading(second.get_reading()) is False
