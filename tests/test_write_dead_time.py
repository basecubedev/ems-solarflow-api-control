# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A grid reading counts no write it cannot show yet.

The control loop adds each grid reading to the total it already commands, on the
assumption that the reading shows that total. A reading the EMS received before
its last write -- a pushed meter's frame that arrived between the cycle's read
and its write -- still shows the total from before, and added to the new one it
counted that write's correction twice; it is corrected by the targets that went
out after it arrived. A device that follows its targets more slowly than the
next reading comes needs a dead time: a reading that arrives within it after a
write is held. These tests run the real control loop on one clock over a device
that delivers each target some time after it was sent, behind a meter read every
cycle or one that pushes its readings.
"""

import time
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.config import OUTPUT_CONTROL_DEFAULTS
from ems.controller import EMSController
from ems.grid_reading import GridReading
from ems.health import CommHealth
from ems.mqtt_control.dispatch import coalesced, failed, published, queued, rejected, superseded
from ems.write_dead_time import MAX_DEAD_TIME_S, MAX_HELD_IN_A_ROW, PENDING_GIVE_UP_S, WriteDeadTime
from tests.test_write_gates import RuntimeStateStub, device, state

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
    pytest.mark.simulation,
]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class LaggingDevice:
    """Delivers each target ``lag_s`` after it was sent."""

    def __init__(self, clock, lag_s):
        self.clock = clock
        self.lag_s = lag_s
        self.sent = []

    def send(self, value):
        self.sent.append((self.clock(), value))

    def limit(self):
        return self.sent[-1][1] if self.sent else 0

    def output_at(self, moment):
        delivered = 0
        for sent_at, value in self.sent:
            if sent_at + self.lag_s <= moment:
                delivered = value
        return delivered


class PolledMeter:
    """Measures on every read: the house less what the device delivers right then."""

    def __init__(self, loop):
        self.loop = loop
        self.health = CommHealth("polled", kind="read")

    def get_power(self):
        self.health.record_success(latency_ms=1.0)
        return round(self.loop.grid_at(self.loop.clock()), 1)


class PushedMeter:
    """Keeps the last reading pushed to it, dated when it arrived."""

    def __init__(self, loop, push_offsets):
        self.loop = loop
        self.push_offsets = push_offsets
        self.health = CommHealth("pushed", kind="read")
        self.reading = GridReading(0.0, 0, received_at=None)

    def push(self, moment):
        value = round(self.loop.grid_at(moment), 1)
        self.reading = GridReading(value, self.reading.sequence + 1, received_at=moment)

    def deliver(self, since, until):
        for offset in self.push_offsets:
            cycle = int(since // self.loop.interval)
            while True:
                moment = cycle * self.loop.interval + offset
                if moment > until:
                    break
                if moment > since:
                    self.push(moment)
                cycle += 1

    def get_power(self):
        return self.get_reading().power_w

    def get_reading(self):
        self.health.record_success(latency_ms=1.0)
        return self.reading


class Loop:
    """One device behind one meter, on one clock.

    A cycle starts every ``interval`` seconds, reads the meter at its start and
    writes ``write_delay`` seconds later, the time the device telemetry and the
    calculation take. ``house`` maps a moment to the house load.
    """

    def __init__(
        self,
        house,
        *,
        interval=3.0,
        write_delay=0.5,
        lag=0.0,
        dead_time=None,
        push_offsets=None,
        max_power=1200,
        output_control=None,
        gate_open=True,
    ):
        self.clock = Clock()
        self.gate_open = gate_open
        self.interval = interval
        self.write_delay = write_delay
        self.house = house
        self.device = LaggingDevice(self.clock, lag)
        self.dev = device("WR1")
        self.dev.max_power = max_power
        self.max_power = max_power
        self.meter = PolledMeter(self) if push_offsets is None else PushedMeter(self, push_offsets)
        self.output_control = {**OUTPUT_CONTROL_DEFAULTS, **(output_control or {})}
        if dead_time is not None:
            self.output_control["write_dead_time_seconds"] = dead_time
        self.controller = EMSController(
            devices=[self.dev],
            shelly=self.meter,
            sleep_enabled=False,
            runtime_state=RuntimeStateStub(),
        )
        self.cycle_start = 0.0
        self.outputs = []
        self.grids = []
        self.unused = []
        self.filtered = []

    def grid_at(self, moment):
        return self.house(moment) - self.device.output_at(moment)

    def dispatch(self, dev, value, charge_exit=None):
        self.clock.now = self.cycle_start + self.write_delay
        self.device.send(value)
        return published(value)

    def states(self, _devices):
        return [
            state(
                solar=0,
                output=self.device.output_at(self.clock()),
                output_limit=self.device.limit(),
            )
        ]

    def run(self, cycles):
        with patch("time.monotonic", self.clock), patch(
            "ems.controller.dispatch_device_write", side_effect=self.dispatch
        ), patch("ems.controller.fetch_all_devices", side_effect=self.states), patch(
            "ems.controller.cfg.SYSTEM_ENABLED", True
        ), patch("ems.controller.cfg.MAX_TOTAL_POWER", self.max_power), patch(
            "ems.controller.cfg.MAX_DEVICE_POWER", self.max_power
        ), patch("ems.controller.cfg.MIN_OUTPUT_LIMIT", 0), patch(
            "ems.controller.cfg.DEADBAND", 2
        ), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0), patch.object(
            cfg, "DRY_RUN", not self.gate_open
        ), patch.object(cfg, "SIMULATION_MODE", False), patch.object(
            cfg, "ALLOW_HARDWARE_WRITES", True
        ), patch.object(
            cfg, "OUTPUT_CONTROL_CONFIG", self.output_control
        ), patch.object(
            cfg, "AC_CHARGE_CONTROL_CONFIG", {**cfg.AC_CHARGE_CONTROL_DEFAULTS, "enabled": False}
        ):
            for _ in range(cycles):
                previous = self.cycle_start
                self.cycle_start = len(self.outputs) * self.interval
                if isinstance(self.meter, PushedMeter):
                    self.meter.deliver(previous if self.outputs else -1.0, self.cycle_start)
                self.clock.now = self.cycle_start
                self.controller.run_once()
                self.outputs.append(self.device.limit())
                self.grids.append(round(self.grid_at(self.cycle_start + self.write_delay + 1e-6), 1))
                self.unused.append(getattr(self.controller, "grid_reading_in_dead_time", None))
                self.filtered.append(self.controller.filtered_load_w)
        return self.outputs


def step(before, after, at):
    return lambda moment: before if moment < at else after


STEP_AT_S = 30.0


def settles(outputs, target, tolerance=40, tail=6):
    return all(abs(value - target) <= tolerance for value in outputs[-tail:])


def held_runs(unused):
    longest = run = 0
    for held in unused:
        run = run + 1 if held else 0
        longest = max(longest, run)
    return longest


@pytest.mark.parametrize("interval", [3.0, 5.0])
def test_a_meter_read_every_cycle_at_a_usual_loop_is_never_held(interval):
    """A one-second dead time leaves a meter read at the start of each cycle alone.

    A device that follows at once, a cycle that writes half a second after its
    read: the next read comes long after the dead time, and the loop regulates
    exactly as it did without one.
    """

    house = step(200, 700, STEP_AT_S)
    with_dead_time = Loop(house, interval=interval, dead_time=1.0)
    without = Loop(house, interval=interval)
    cycles = int(2 * STEP_AT_S / interval)

    assert with_dead_time.run(cycles) == without.run(cycles)
    assert not any(with_dead_time.unused)


@pytest.mark.parametrize("push_offset", [0.2, 0.5, 0.9])
def test_a_pushed_reading_that_arrived_before_the_write_is_corrected_for_it(push_offset):
    """A frame between a cycle's read and its write shows the output before the write.

    Added as it was, it counted that write's correction twice, and a 500 W load
    step swung the output between nothing and the device maximum for good. It
    is corrected for the write instead, and no reading is held for it.
    """

    loop = Loop(step(200, 700, STEP_AT_S), write_delay=1.0, push_offsets=[push_offset])

    outputs = loop.run(40)

    assert settles(outputs, 700)
    assert max(outputs) < 900
    assert not any(loop.unused)


@pytest.mark.parametrize("push_offset", [0.2, 0.5, 0.9])
def test_a_pushed_reading_before_the_write_is_held_inside_a_set_dead_time(push_offset):
    loop = Loop(step(200, 700, STEP_AT_S), write_delay=1.0, push_offsets=[push_offset], dead_time=1.0)

    outputs = loop.run(40)

    assert settles(outputs, 700)
    assert max(outputs) < 900
    assert any(loop.unused)


@pytest.mark.parametrize("interval", [3.0, 5.0])
def test_a_meter_read_every_cycle_needs_no_correction(interval):
    """A read at the start of a cycle comes after every write before it."""

    loop = Loop(step(200, 700, STEP_AT_S), interval=interval)
    corrections = []
    original = loop.controller.note_grid_reading

    def note(reading):
        result = original(reading)
        corrections.append(loop.controller.grid_reading_correction_w)
        return result

    loop.controller.note_grid_reading = note
    loop.run(int(2 * STEP_AT_S / interval))

    assert set(corrections) == {0}


@pytest.mark.parametrize(
    "interval, write_delay, lag, dead_time",
    [(1.0, 0.2, 1.5, 1.0), (1.0, 0.2, 1.5, 2.0), (3.0, 0.5, 2.8, 3.0)],
)
def test_a_device_slower_than_the_loop_settles_behind_a_dead_time_that_covers_it(
    interval, write_delay, lag, dead_time
):
    """A device that delivers its target after the next read keeps the loop swinging.

    Each reading still showed the output from before the last write, and the
    loop added that write's correction again: the output swung by several
    hundred watts for good, with the load filter on from nothing to the device
    maximum and back. Readings that come before the device can have followed
    are left alone.
    """

    house = step(200, 700, STEP_AT_S)
    cycles = int(4 * STEP_AT_S / interval)
    held = Loop(house, interval=interval, write_delay=write_delay, lag=lag, dead_time=dead_time)
    unheld = Loop(house, interval=interval, write_delay=write_delay, lag=lag, dead_time=0)

    outputs = held.run(cycles)
    swinging = unheld.run(cycles)

    assert settles(outputs, 700)
    assert max(outputs) < 1000
    assert max(swinging[cycles // 2:]) - min(swinging[cycles // 2:]) >= 500


@pytest.mark.parametrize("push_offsets", [[2.9], [0.1, 1.1, 2.1]])
def test_a_pushed_reading_that_arrived_after_the_dead_time_is_regulated_on(push_offsets):
    house = step(200, 700, STEP_AT_S)
    with_dead_time = Loop(house, write_delay=1.0, push_offsets=push_offsets, dead_time=1.0)
    without = Loop(house, write_delay=1.0, push_offsets=push_offsets)

    assert with_dead_time.run(30) == without.run(30)
    assert not any(with_dead_time.unused)


def test_no_more_than_three_readings_in_a_row_are_held():
    """A total that keeps moving cannot keep the loop from its readings."""

    loads = [200, 900, 300, 1100, 150, 800, 400, 1000]
    loop = Loop(
        lambda moment: loads[int(moment) % len(loads)],
        interval=1.0,
        write_delay=0.2,
        dead_time=10.0,
    )

    loop.run(60)

    assert held_runs(loop.unused) == MAX_HELD_IN_A_ROW
    assert loop.unused.count(False) >= 60 // 4


def test_a_dead_time_of_zero_regulates_on_every_reading():
    loop = Loop(step(200, 700, STEP_AT_S), write_delay=1.0, push_offsets=[0.5], dead_time=0)

    loop.run(30)

    assert not any(loop.unused)


def test_a_closed_write_gate_starts_no_dead_time():
    loop = Loop(
        step(200, 700, STEP_AT_S), interval=1.0, write_delay=0.2, gate_open=False, dead_time=1.0
    )

    loop.run(30)

    assert loop.device.sent == []
    assert loop.controller.write_dead_time.moved_at is None
    assert not any(loop.unused)


def test_a_reading_in_the_dead_time_feeds_nothing_to_the_load_filter():
    loop = Loop(step(200, 700, STEP_AT_S), write_delay=1.0, push_offsets=[0.5], dead_time=1.0)

    loop.run(30)

    held = [index for index, unused in enumerate(loop.unused) if unused and index > 0]
    assert held
    for index in held:
        assert loop.filtered[index] == loop.filtered[index - 1]


def test_the_dead_time_is_dated_by_the_last_write_that_moved_the_total():
    loop = Loop(step(200, 700, STEP_AT_S), interval=3.0, write_delay=0.5)

    loop.run(12)

    closed = [sent_at for sent_at, _value in loop.device.sent if sent_at < loop.cycle_start]
    assert loop.controller.write_dead_time.moved_at == closed[-1]


def reading(received_at, sequence=None):
    return GridReading(100.0, sequence, received_at=received_at)


def moved(sends, threshold=5, start=None):
    dead_time = WriteDeadTime()
    for cycle in ([start] if start else []) + [sends]:
        for name, value, at in cycle:
            dead_time.note_published(name, value, at)
        dead_time.close_cycle(threshold)
    return dead_time


def test_a_first_target_moves_the_power():
    assert moved([("WR1", 300, 1.0)]).moved_at == 1.0


def test_a_target_sent_again_unchanged_moves_nothing():
    dead_time = moved([("WR1", 300, 4.0)], start=[("WR1", 300, 1.0)])

    assert dead_time.moved_at == 1.0


def test_a_target_moved_from_one_device_to_another_moves_nothing():
    start = [("WR1", 300, 1.0), ("WR2", 300, 1.1)]
    dead_time = moved([("WR1", 400, 4.0), ("WR2", 200, 4.1)], start=start)

    assert dead_time.moved_at == 1.1


def test_a_move_inside_the_threshold_moves_nothing():
    dead_time = moved([("WR1", 305, 4.0)], start=[("WR1", 300, 1.0)], threshold=5)

    assert dead_time.moved_at == 1.0


def test_a_move_beyond_the_threshold_is_dated_by_the_last_target_that_changed():
    start = [("WR1", 300, 1.0), ("WR2", 300, 1.1)]
    dead_time = moved([("WR2", 350, 4.0), ("WR1", 400, 4.1), ("WR2", 350, 4.2)], start=start)

    assert dead_time.moved_at == 4.1


def test_a_cycle_without_a_send_keeps_the_last_move():
    dead_time = moved([("WR1", 300, 1.0)])
    dead_time.close_cycle(5)

    assert dead_time.moved_at == 1.0


def test_a_reading_before_the_move_settled_is_held_and_one_after_is_not():
    dead_time = moved([("WR1", 300, 10.0)])

    assert dead_time.holds(reading(9.5), 1.0) is True
    assert dead_time.holds(reading(10.9), 1.0) is True
    assert dead_time.holds(reading(11.0), 1.0) is False


def test_nothing_is_held_without_a_move_a_dead_time_or_an_arrival_time():
    assert WriteDeadTime().holds(reading(0.0), 1.0) is False
    dead_time = moved([("WR1", 300, 10.0)])
    assert dead_time.holds(reading(10.1), 0.0) is False
    assert dead_time.holds(reading(None), 1.0) is False


def test_the_fourth_reading_in_a_row_is_used_and_starts_the_count_again():
    dead_time = moved([("WR1", 300, 10.0)])

    held = [dead_time.holds(reading(10.0 + index * 0.1), 5.0) for index in range(6)]

    assert held == [True, True, True, False, True, True]
    assert dead_time.held_in_a_row == 2


def test_a_used_reading_starts_the_count_again():
    dead_time = moved([("WR1", 300, 10.0)])
    dead_time.holds(reading(10.1), 1.0)
    dead_time.holds(reading(11.5), 1.0)

    assert dead_time.held_in_a_row == 0


def test_a_queued_target_dates_nothing_until_it_goes_out():
    dead_time = moved([], start=[("WR1", 300, 1.0)])

    dead_time.note_queued("WR1", 400, 4.0)
    dead_time.close_cycle(5)
    assert dead_time.moved_at == 1.0

    dead_time.note_published("WR1", 400, 5.5)
    dead_time.close_cycle(5)
    assert dead_time.moved_at == 5.5


def test_a_reading_cannot_show_the_targets_that_went_out_after_it_arrived():
    dead_time = sent(("WR1", 300, 1.0), ("WR2", -200, 1.0), ("WR1", 450, 5.0), ("WR2", -100, 5.2))

    assert dead_time.unseen_w(4.9, 6.0) == 250
    assert dead_time.unseen_w(5.1, 6.0) == 100
    assert dead_time.unseen_w(5.3, 6.0) == 0
    assert dead_time.unseen_w(None, 6.0) == 0


def test_a_reading_cannot_show_a_target_that_waits_in_a_queue():
    dead_time = sent(("WR1", 300, 1.0))
    dead_time.note_queued("WR1", 400, 3.0)

    assert dead_time.unseen_w(2.0, 4.0) == 100

    dead_time.note_dropped("WR1", 350)
    assert dead_time.unseen_w(2.0, 4.0) == 100
    dead_time.note_dropped("WR1", 400)
    assert dead_time.unseen_w(2.0, 4.0) == 0


def test_a_change_from_before_a_device_s_first_target_is_not_known():
    dead_time = sent(("WR1", 300, 5.0), ("WR1", 350, 8.0))
    dead_time.note_queued("WR2", 200, 8.0)

    assert dead_time.unseen_w(4.0, 9.0) == 50


def test_a_target_that_waits_is_no_longer_counted_after_a_minute():
    dead_time = sent(("WR1", 300, 1.0))
    dead_time.note_queued("WR1", 400, 100.0)

    assert dead_time.unseen_w(50.0, 100.0 + PENDING_GIVE_UP_S - 0.1) == 100
    assert dead_time.unseen_w(50.0, 100.0 + PENDING_GIVE_UP_S) == 0


def test_a_target_queued_again_keeps_the_time_it_first_waited_from():
    dead_time = sent(("WR1", 300, 1.0))
    dead_time.note_queued("WR1", 400, 100.0)
    dead_time.note_queued("WR1", 400, 150.0)

    assert dead_time.unseen_w(50.0, 100.0 + PENDING_GIVE_UP_S) == 0

    dead_time.note_queued("WR1", 450, 170.0)
    assert dead_time.unseen_w(50.0, 175.0) == 150


def test_a_target_queued_twice_unchanged_is_dated_when_it_goes_out():
    dead_time = moved([], start=[("WR1", 300, 1.0)])
    dead_time.note_queued("WR1", 400, 4.0)
    dead_time.close_cycle(5)
    dead_time.note_queued("WR1", 400, 5.0)

    dead_time.note_published("WR1", 400, 6.5)
    dead_time.close_cycle(5)

    assert dead_time.moved_at == 6.5


def sent(*changes):
    dead_time = WriteDeadTime()
    for name, value, at in changes:
        dead_time.note_published(name, value, at)
    return dead_time


def test_a_charge_is_judged_against_no_target_that_went_out_after_the_reading():
    dead_time = sent(("WR1", -200, 1.0), ("WR1", -600, 10.6))

    assert dead_time.deepest_charge_w(["WR1"], 10.5, 1.0, 11.0) == -200


def test_a_charge_is_judged_against_the_deepest_target_in_the_dead_time_before_the_reading():
    dead_time = sent(("WR1", -900, 1.0), ("WR1", -600, 8.0), ("WR1", -300, 9.5))

    assert dead_time.deepest_charge_w(["WR1"], 10.0, 1.0, 11.0) == -600
    assert dead_time.deepest_charge_w(["WR1"], 10.0, 3.0, 11.0) == -900
    assert dead_time.deepest_charge_w(["WR1"], 20.0, 1.0, 21.0) == -300


def test_only_the_named_devices_and_only_their_charges_count_towards_the_judged_total():
    dead_time = sent(("WR1", -400, 1.0), ("WR2", 300, 1.0), ("WR3", -50, 9.8))

    assert dead_time.deepest_charge_w(["WR1"], 10.0, 1.0, 10.0) == -400
    assert dead_time.deepest_charge_w(["WR1", "WR3"], 10.0, 1.0, 10.0) == -450
    assert dead_time.deepest_charge_w(["WR2"], 10.0, 1.0, 10.0) == 0
    assert dead_time.deepest_charge_w(["WR1", "WR2"], 10.0, 1.0, 10.0) == -400
    assert dead_time.deepest_charge_w(["WR9"], 10.0, 1.0, 10.0) is None


def test_a_reading_without_an_arrival_time_is_judged_up_to_now():
    dead_time = sent(("WR1", -200, 1.0), ("WR1", -600, 10.6))

    assert dead_time.deepest_charge_w(["WR1"], None, 1.0, 11.0) == -600


def test_the_history_keeps_what_a_device_had_when_a_dead_time_began():
    dead_time = sent(*[("WR1", -100 * (80 - index), float(index)) for index in range(1, 80)])

    assert dead_time.deepest_charge_w(["WR1"], 79.5, MAX_DEAD_TIME_S, 79.5) == -1100
    assert dead_time.deepest_charge_w(["WR1"], 79.5, 0.2, 79.5) == -100
    assert len(dead_time._changes["WR1"]) <= 3 * MAX_DEAD_TIME_S + 2

    dead_time.note_published("WR2", -50, 79.0)
    assert dead_time.deepest_charge_w(["WR1"], 200.0, MAX_DEAD_TIME_S, 200.0) == -100


def test_a_target_that_went_out_since_the_cycle_began_dates_the_next_reading():
    """A queued target published while the meter is read counts before the next close."""

    dead_time = moved([], start=[("WR1", 300, 1.0)])
    dead_time.note_queued("WR1", 400, 9.0)
    dead_time.close_cycle(5)

    dead_time.note_published("WR1", 400, 10.1)

    assert dead_time.holds(reading(10.0), 1.0) is True
    assert dead_time.holds(reading(11.2), 1.0) is False


def test_a_target_that_is_no_number_is_ignored():
    dead_time = WriteDeadTime()
    dead_time.note_published("WR1", None, 1.0)
    dead_time.close_cycle(5)

    assert dead_time.moved_at is None


def test_how_long_after_the_move_a_reading_arrived():
    dead_time = moved([("WR1", 300, 10.0)])

    assert dead_time.since_move_s(reading(10.25)) == pytest.approx(0.25)
    assert dead_time.since_move_s(reading(None)) is None
    assert WriteDeadTime().since_move_s(reading(1.0)) is None


def gate_open():
    return patch.multiple(cfg, DRY_RUN=False, SIMULATION_MODE=False, ALLOW_HARDWARE_WRITES=True)


def plain_controller():
    return EMSController(
        devices=[device("WR1")],
        shelly=PolledMeter(None),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
    )


@pytest.mark.parametrize(
    "result, noted, waiting",
    [
        (published(300), True, False),
        (queued(300), False, True),
        (coalesced(300), False, False),
        (rejected(300, reason="limit"), False, False),
        (failed(300), False, False),
    ],
    ids=["published", "queued", "coalesced", "rejected", "failed"],
)
def test_only_a_target_that_went_out_dates_the_dead_time(result, noted, waiting):
    """``waiting`` says a queued 300 W is still counted as what a reading cannot show."""

    ems = plain_controller()
    ems.write_dead_time.note_published("WR1", 0, 1.0)
    ems.write_dead_time.close_cycle(5)
    clock = Clock()
    clock.now = 7.0
    with gate_open(), patch("time.monotonic", clock), patch(
        "ems.controller.dispatch_device_write", return_value=result
    ):
        ems.set_output_limit(ems.devices[0], 300)
    ems.write_dead_time.close_cycle(5)

    assert ems.write_dead_time.moved_at == (7.0 if noted else None)
    assert ems.write_dead_time.unseen_w(2.0, 8.0) == (300 if noted or waiting else 0)


def test_a_target_the_write_gate_kept_back_dates_nothing():
    ems = plain_controller()
    with patch.object(cfg, "DRY_RUN", True), patch("ems.controller.dispatch_device_write") as dispatch:
        ems.set_output_limit(ems.devices[0], 300)
    ems.write_dead_time.close_cycle(5)

    dispatch.assert_not_called()
    assert ems.write_dead_time.moved_at is None


def test_a_queued_target_published_later_dates_the_dead_time_from_then():
    ems = plain_controller()
    ems.write_dead_time.note_published("WR1", 0, 1.0)
    clock = Clock()
    clock.now = 7.0
    with gate_open(), patch("time.monotonic", clock), patch(
        "ems.controller.dispatch_device_write", return_value=queued(300)
    ):
        ems.set_output_limit(ems.devices[0], 300)
        ems.write_dead_time.close_cycle(5)
        clock.now = 8.5
        ems._observe_deferred_write_dispatch(ems.devices[0], published(300))
        ems.write_dead_time.close_cycle(5)

    assert ems.write_dead_time.moved_at == 8.5
    assert ems.write_dead_time.unseen_w(8.0, 9.0) == 300
    assert ems.write_dead_time.unseen_w(8.6, 9.0) == 0


@pytest.mark.parametrize("outcome", [failed(300), rejected(300, reason="limit")], ids=["failed", "rejected"])
def test_a_queued_target_that_failed_to_go_out_is_no_longer_counted(outcome):
    """A transport that clears its queue and publishes the target itself can still fail."""

    ems = plain_controller()
    ems.write_dead_time.note_published("WR1", 0, time.monotonic() - 10)
    with gate_open(), patch(
        "ems.controller.dispatch_device_write", side_effect=[queued(300), outcome]
    ):
        ems.set_output_limit(ems.devices[0], 300)
        assert ems.write_dead_time.unseen_w(time.monotonic() - 5, time.monotonic()) == 300
        ems.set_output_limit(ems.devices[0], 300)

    assert ems.write_dead_time.unseen_w(time.monotonic() - 5, time.monotonic()) == 0


def test_a_queued_target_superseded_later_is_no_longer_counted():
    ems = plain_controller()
    ems.write_dead_time.note_published("WR1", 0, time.monotonic() - 10)
    with gate_open(), patch(
        "ems.controller.dispatch_device_write", return_value=queued(300)
    ):
        ems.set_output_limit(ems.devices[0], 300)
    assert ems.write_dead_time.unseen_w(time.monotonic() - 5, time.monotonic()) == 300

    ems._observe_deferred_write_dispatch(
        ems.devices[0], superseded(300, correlation_id=None, reason="superseded_by_new_target")
    )

    assert ems.write_dead_time.unseen_w(time.monotonic() - 5, time.monotonic()) == 0


def early_reading_controller(monkeypatch):
    from tests.test_grid_reading_once import controller_for_totals

    ems = controller_for_totals(monkeypatch, write_dead_time_seconds=1.0)
    ems.write_dead_time.note_published("WR1", 300, 10.0)
    ems.write_dead_time.close_cycle(5)
    return ems


def test_a_new_reading_in_the_dead_time_is_left_unused(monkeypatch):
    ems = early_reading_controller(monkeypatch)

    assert ems.note_grid_reading(reading(10.5)) is True
    assert ems.grid_reading_in_dead_time is True
    assert ems.grid_reading_repeated is False
    assert ems.grid_reading_unused is True


def test_a_reading_from_a_meter_that_holds_is_never_in_the_dead_time(monkeypatch):
    ems = early_reading_controller(monkeypatch)
    ems.grid_meter_holding = True

    assert ems.note_grid_reading(reading(10.5)) is False
    assert ems.grid_reading_in_dead_time is False
    assert ems.write_dead_time.held_in_a_row == 0


def test_a_repeated_reading_is_not_counted_as_held_in_the_dead_time(monkeypatch):
    ems = early_reading_controller(monkeypatch)
    ems.note_grid_reading(reading(10.5, sequence=4))

    assert ems.note_grid_reading(reading(10.5, sequence=4)) is True
    assert ems.grid_reading_repeated is True
    assert ems.grid_reading_in_dead_time is False
    assert ems.write_dead_time.held_in_a_row == 1


def test_a_reading_in_the_dead_time_is_logged_with_its_distance_to_the_write(monkeypatch, caplog):
    import logging

    ems = early_reading_controller(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        ems.note_grid_reading(reading(10.25))

    line = next(line for line in caplog.text.splitlines() if "event=grid_reading_in_dead_time" in line)
    assert "since_write_s=0.25" in line
    assert "dead_time_s=1.0" in line


def test_a_reading_in_the_dead_time_sizes_no_discharge_while_a_charge_is_left(monkeypatch):
    from tests.test_grid_reading_once import leaving_controller, total_states

    ems = leaving_controller(monkeypatch, repeated=False)
    ems.grid_reading_in_dead_time = True

    total = ems.discharge_total_leaving_a_charge(400, total_states(), -300, 1200)

    assert total == -300
    assert ems.commanded_total_w == -300
    assert list(ems.load_history) == [100, 120]
    assert ems.charges_being_left == {"WR1"}


class DatedMeter:
    """A meter that keeps its last reading, dated when it came in."""

    def __init__(self):
        self.health = CommHealth("dated", kind="read")
        self.value = 0.0
        self.sequence = 0
        self.received_at = None

    def publish(self, value):
        self.value = round(value, 1)
        self.sequence += 1

    def get_power(self):
        self.health.record_success(latency_ms=1.0)
        return self.value

    def get_reading(self):
        return GridReading(self.get_power(), self.sequence, received_at=self.received_at)


def put_next_reading_in_the_dead_time(controller, meter):
    controller.write_dead_time.moved_at = 100.0
    controller.write_dead_time.held_in_a_row = 0
    meter.received_at = 100.1


def test_a_reading_in_the_dead_time_records_no_entry_observation():
    from tests.test_grid_reading_once import ChargeLoop

    loop = ChargeLoop(1)
    loop.meter = DatedMeter()
    controller = loop.harness.controller
    controller.shelly = loop.meter
    output_control = {**cfg.OUTPUT_CONTROL_CONFIG, "write_dead_time_seconds": 1.0}
    with patch.object(cfg, "OUTPUT_CONTROL_CONFIG", output_control):
        loop.run(1)
        window = controller.charge_direction.entry_window

        put_next_reading_in_the_dead_time(controller, loop.meter)
        loop.run(1)

    assert controller.grid_reading_in_dead_time is True
    assert controller.charge_direction.entry_window == window


def test_the_dead_time_is_off_by_default_everywhere():
    from ems.config_catalog import build_default_template, get_config_catalog

    template = build_default_template()
    field = next(
        item
        for section in get_config_catalog()["sections"]
        for item in section["fields"]
        if item["path"] == "system.output_control.write_dead_time_seconds"
    )

    assert OUTPUT_CONTROL_DEFAULTS["write_dead_time_seconds"] == 0.0
    assert template["system"]["output_control"]["write_dead_time_seconds"] == 0.0
    assert field["default"] == 0.0
    assert field["min"] == 0
    assert field["max"] == MAX_DEAD_TIME_S


@pytest.mark.parametrize(
    "configured, effective",
    [(None, 0.0), (0, 0.0), (2.5, 2.5), (60, MAX_DEAD_TIME_S), (-3, 0.0), ("soon", 0.0)],
)
def test_the_configured_dead_time_is_kept_between_zero_and_its_maximum(configured, effective):
    ems = plain_controller()
    output_control = dict(OUTPUT_CONTROL_DEFAULTS)
    if configured is None:
        output_control.pop("write_dead_time_seconds")
    else:
        output_control["write_dead_time_seconds"] = configured

    with patch.object(cfg, "OUTPUT_CONTROL_CONFIG", output_control):
        assert ems.write_dead_time_s() == effective


class ChargeBehindDeadTime:
    """A device that follows its commands, behind a meter whose readings come inside the dead time.

    Each cycle writes one second after its read. ``frame_at`` dates the reading
    the next cycle gets: a pushed frame half a second after the read, before
    the write, shows the device as it was before that write; a read half a
    second after the previous write, as a fast loop or a slow cycle gives it,
    shows it as it is.
    """

    WRITE_AT_S = 1.0

    def __init__(self, house, pushed=True, dead_time=None, other_device_w=None, discharge=True):
        from tests.test_grid_reading_once import FollowingChargeLoop

        self.following = FollowingChargeLoop(1, house=house)
        self.following.dev.ac_discharge_enabled = discharge
        self.item = self.following.item
        self.controller = self.following.controller
        self.clock = self.following.harness.clock
        self.meter = DatedMeter()
        self.controller.shelly = self.meter
        self.house = house
        self.pushed = pushed
        self.output_control = dict(cfg.OUTPUT_CONTROL_CONFIG)
        if dead_time is not None:
            self.output_control["write_dead_time_seconds"] = dead_time
        hardware = self.following.hardware

        def write(dev, value, charge_exit=None):
            written = hardware(dev, value, charge_exit)
            if written:
                self.controller.write_dead_time.note_published(
                    dev.name, int(value), self.clock.now + self.WRITE_AT_S
                )
            return written

        self.controller.set_output_limit = write
        if other_device_w is not None:
            self.controller.write_dead_time.note_published("WR2", other_device_w, self.clock.now)
        self.charging = []

    def grid(self):
        return self.house + self.item.grid_input - self.item.output

    def run(self, cycles):
        with patch.object(cfg, "OUTPUT_CONTROL_CONFIG", self.output_control):
            for _ in range(cycles):
                start = self.clock.now
                if not self.pushed:
                    self.meter.publish(self.grid())
                    self.meter.received_at = start - self.following.harness.seconds_per_cycle + self.WRITE_AT_S + 0.5
                before_write = self.grid()
                self.following.harness.run(cycles=1, states=[self.item])
                if self.pushed:
                    self.meter.publish(before_write)
                    self.meter.received_at = start + 0.5
                self.charging.append(self.controller.charge_direction.charging)
        return self

    @property
    def switches(self):
        return sum(1 for before, after in zip(self.charging, self.charging[1:]) if before != after)


@pytest.mark.parametrize("dead_time", [None, 1.0], ids=["corrected", "dead_time"])
@pytest.mark.parametrize("pushed", [True, False], ids=["pushed_frame", "fast_read"])
@pytest.mark.parametrize("house", [-250, -300, -450, -500, -700, -900])
def test_a_steady_surplus_keeps_one_charge_through_readings_in_the_dead_time(house, pushed, dead_time):
    """A reading in the dead time judges a running charge on the deepest charge sent.

    Judged on the total from before the last used reading's correction, a
    charge ramp read as the surplus being gone: the device drew 400 W from a
    450 W surplus while the judged total climbed to the stop, and the charge
    was left and entered again until the hourly entry limit: twelve entries
    and twenty-four switches in ten minutes.
    """

    loop = ChargeBehindDeadTime(house, pushed=pushed, dead_time=dead_time).run(120)

    assert len(loop.controller.charge_direction.entries) == 1
    assert loop.switches == 1
    assert loop.charging[-1] is True


@pytest.mark.parametrize("dead_time", [None, 1.0], ids=["corrected", "dead_time"])
@pytest.mark.parametrize("pushed", [True, False], ids=["pushed_frame", "fast_read"])
@pytest.mark.parametrize("house", [-250, -300])
def test_a_device_the_regulator_no_longer_commands_leaves_the_judged_charge_alone(house, pushed, dead_time):
    """The last target of a device gone offline is no part of what a charge is judged on.

    Judged against the targets every device last got, a device that dropped out
    while feeding out 300 W made a steady small surplus look gone, and the charge
    of another device was left and entered again twelve times.
    """

    loop = ChargeBehindDeadTime(house, pushed=pushed, dead_time=dead_time, other_device_w=300).run(120)

    assert len(loop.controller.charge_direction.entries) == 1
    assert loop.switches == 1


@pytest.mark.parametrize("dead_time", [None, 1.0], ids=["corrected", "dead_time"])
@pytest.mark.parametrize("pushed", [True, False], ids=["pushed_frame", "fast_read"])
def test_a_device_that_dropped_out_while_charging_hides_no_deficit(pushed, dead_time):
    loop = ChargeBehindDeadTime(-450, pushed=pushed, dead_time=dead_time, other_device_w=-600).run(60)
    assert loop.charging[-1] is True
    cycles_before = len(loop.charging)

    loop.house = 800
    loop.run(3)

    assert loop.charging[cycles_before:].index(False) <= 1


@pytest.mark.parametrize("dead_time", [None, 1.0], ids=["corrected", "dead_time"])
@pytest.mark.parametrize("pushed", [True, False], ids=["pushed_frame", "fast_read"])
def test_a_deficit_in_the_dead_time_still_ends_a_charge_at_once(pushed, dead_time):
    loop = ChargeBehindDeadTime(-450, pushed=pushed, dead_time=dead_time).run(60)
    assert loop.charging[-1] is True
    cycles_before = len(loop.charging)

    loop.house = 800
    loop.run(3)

    assert loop.charging[cycles_before:][-1] is False
    assert loop.charging[cycles_before:].index(False) <= 1


class SlowDeviceCharge:
    """A device that applies each write two polls later, under a 1 s loop and a 3 s dead time.

    The meter is read at the start of each cycle and shows the device as it is;
    each cycle writes 0.2 s after its read.
    """

    def __init__(self, house, dead_time=3.0):
        from tests.test_ac_charge_regulator import (
            FollowingHardware,
            Harness,
            following_http_device,
            pv_less_state,
        )
        from tests.test_grid_reading_once import reporting_output

        self.item = pv_less_state(soc=50)
        dev, hardware = following_http_device(
            self.item, double=reporting_output(FollowingHardware), apply_after_polls=2
        )
        self.harness = Harness([dev], load=house, seconds_per_cycle=1)
        self.controller = self.harness.controller
        self.meter = DatedMeter()
        self.controller.shelly = self.meter
        self.house = house
        self.output_control = {**cfg.OUTPUT_CONTROL_CONFIG, "write_dead_time_seconds": dead_time}
        clock = self.harness.clock

        def write(device, value, charge_exit=None):
            written = hardware(device, value, charge_exit)
            if written:
                self.controller.write_dead_time.note_published(device.name, int(value), clock.now + 0.2)
            return written

        self.controller.set_output_limit = write
        self.charging = []

    def run(self, cycles):
        with patch.object(cfg, "OUTPUT_CONTROL_CONFIG", self.output_control):
            for _ in range(cycles):
                self.meter.publish(self.house + self.item.grid_input - self.item.output)
                self.meter.received_at = self.harness.clock.now
                self.harness.run(cycles=1, states=[self.item])
                self.charging.append(self.controller.charge_direction.charging)
        return self


@pytest.mark.parametrize("smaller", [-400, -250])
def test_a_shrinking_charge_behind_a_slow_device_is_kept(smaller):
    """A reading held by a dead time longer than the loop can show the device several cycles back.

    Judged against the totals commanded as the last two cycles started, a
    charge easing from a 900 W surplus to a smaller one read as the surplus
    being gone while the device still drew 680 W, and was left and entered
    again on every cloud.
    """

    loop = SlowDeviceCharge(-900).run(60)
    assert loop.charging[-1] is True

    loop.house = smaller
    loop.run(60)

    assert len(loop.controller.charge_direction.entries) == 1
    assert all(loop.charging[-60:])


@pytest.mark.parametrize("pushed", [True, False], ids=["pushed_frame", "fast_read"])
@pytest.mark.parametrize("after_entry", [0, 1, 2])
@pytest.mark.parametrize("deficit", [100, 200, 400])
def test_a_deficit_right_after_a_charge_entry_ends_it_within_two_cycles(pushed, after_entry, deficit):
    """Inside a dead time, a charge ramp still on its way hides no deficit past the next cycle.

    Judged against the commanded total, which runs ahead of the devices, a
    200 W deficit right after an entry was left only on the fourth reading,
    with the grid import climbing to 1.1 kW.
    """

    loop = ChargeBehindDeadTime(-900, pushed=pushed, dead_time=1.0)
    while not loop.controller.charge_direction.charging and len(loop.charging) < 40:
        loop.run(1)
    assert loop.controller.charge_direction.charging
    loop.run(after_entry)
    start = len(loop.charging)

    loop.house = deficit
    loop.run(4)

    assert False in loop.charging[start:start + 2]


@pytest.mark.parametrize("dead_time", [None, 1.0], ids=["corrected", "dead_time"])
@pytest.mark.parametrize("pushed", [True, False], ids=["pushed_frame", "fast_read"])
@pytest.mark.parametrize("house", [-300, -450, -700])
def test_a_device_that_may_only_charge_keeps_one_charge_through_the_dead_time(house, pushed, dead_time):
    """A device with ``ac_discharge_enabled`` false is commanded, though it may not supply.

    Left out of the judged total with the devices that may supply, its own
    charge ramp read as the surplus being gone: twelve entries in ten minutes.
    """

    loop = ChargeBehindDeadTime(house, pushed=pushed, dead_time=dead_time, discharge=False).run(120)

    assert len(loop.controller.charge_direction.entries) == 1
    assert loop.switches == 1


class ParkedFloorsCharge:
    """One device charging beside three that cannot, parked at the 35 W standby floor."""

    def __init__(self, house, pushed, dead_time=None):
        from tests.test_ac_charge_regulator import (
            FollowingHardware,
            Harness,
            following_http_device,
            pv_less_state,
        )
        from tests.test_grid_reading_once import reporting_output

        self.items = [pv_less_state(soc=50) for _ in range(4)]
        devices, hardware = [], {}
        for index, item in enumerate(self.items):
            dev, double = following_http_device(
                item, name=f"WR{index + 1}", double=reporting_output(FollowingHardware)
            )
            if index:
                dev.ac_charge_enabled = False
            devices.append(dev)
            hardware[dev.name] = double
        self.harness = Harness(devices, load=house, seconds_per_cycle=5, min_output_limit=35)
        self.controller = self.harness.controller
        self.meter = DatedMeter()
        self.controller.shelly = self.meter
        self.house = house
        self.pushed = pushed
        self.output_control = dict(cfg.OUTPUT_CONTROL_CONFIG)
        if dead_time is not None:
            self.output_control["write_dead_time_seconds"] = dead_time
        clock = self.harness.clock

        def write(dev, value, charge_exit=None):
            written = hardware[dev.name](dev, value, charge_exit)
            if written:
                self.controller.write_dead_time.note_published(dev.name, int(value), clock.now + 1.0)
            return written

        self.controller.set_output_limit = write
        self.charging = []

    def grid(self):
        return self.house + sum(item.grid_input - item.output for item in self.items)

    def run(self, cycles):
        with patch.object(cfg, "OUTPUT_CONTROL_CONFIG", self.output_control):
            for _ in range(cycles):
                start = self.harness.clock.now
                if not self.pushed:
                    self.meter.publish(self.grid())
                    self.meter.received_at = start - 5 + 1.5
                before_write = self.grid()
                self.harness.run(cycles=1, states=self.items)
                if self.pushed:
                    self.meter.publish(before_write)
                    self.meter.received_at = start + 0.5
                self.charging.append(self.controller.charge_direction.charging)
        return self


@pytest.mark.parametrize("dead_time", [None, 1.0], ids=["corrected", "dead_time"])
@pytest.mark.parametrize("pushed", [True, False], ids=["pushed_frame", "fast_read"])
def test_standby_floors_beside_a_charge_are_no_part_of_the_judged_total(pushed, dead_time):
    """The regulator counts what parked devices feed out as house load, and so does the judgement.

    Counted in, three floors of 35 W made a small surplus look gone a little
    early, and a charge on 90 W of surplus switched twenty-four times.
    """

    loop = ParkedFloorsCharge(-90, pushed, dead_time).run(240)

    entries = len(loop.controller.charge_direction.entries)
    switches = sum(1 for before, after in zip(loop.charging, loop.charging[1:]) if before != after)
    assert (entries, switches) == (1, 1)


@pytest.mark.parametrize("pushed, cycles", [(True, 4), (False, 2)], ids=["pushed_frame", "fast_read"])
@pytest.mark.parametrize("after_entry", [0, 1, 2])
@pytest.mark.parametrize("deficit", [100, 200, 400])
def test_a_corrected_reading_ends_a_charge_on_a_deficit_as_a_used_reading_does(
    pushed, cycles, after_entry, deficit
):
    """Without a dead time a reading is used, corrected, and the filter weighs the deficit.

    A pushed frame is a cycle older than a read at the cycle's start, so a
    deficit shows a cycle later, as it does for any meter that pushes its
    readings.
    """

    loop = ChargeBehindDeadTime(-900, pushed=pushed)
    while not loop.controller.charge_direction.charging and len(loop.charging) < 40:
        loop.run(1)
    assert loop.controller.charge_direction.charging
    loop.run(after_entry)
    start = len(loop.charging)

    loop.house = deficit
    loop.run(cycles + 1)

    assert False in loop.charging[start:start + cycles]


def test_a_forgotten_device_moves_nothing_and_its_next_target_is_not_corrected():
    """What a device does while the EMS does not command it is no target of the EMS."""

    dead_time = moved([], start=[("WR1", 300, 1.0), ("WR2", 200, 1.0)])

    dead_time.forget("WR1")
    dead_time.close_cycle(5)
    assert dead_time.moved_at == 1.0

    dead_time.note_published("WR1", 100, 9.0)
    assert dead_time.unseen_w(5.0, 10.0) == 0
    assert dead_time.deepest_charge_w(["WR1", "WR2"], 5.0, 1.0, 10.0) == 0


@pytest.mark.parametrize(
    "reason, commanded, forgotten",
    [
        ("runtime_role_skip_output_limit", False, True),
        ("device_disabled_skip_write", False, True),
        ("offline_skip_write", True, False),
        ("deadband_skip_write", True, False),
    ],
)
def test_a_device_the_ems_stops_commanding_is_forgotten(reason, commanded, forgotten):
    ems = plain_controller()
    ems.write_dead_time.note_published("WR1", 300, 1.0)

    ems.retire_pending_output_limit(ems.devices[0], reason, commanded=commanded)

    assert ("WR1" not in ems.write_dead_time._published) is forgotten


def test_a_corrected_reading_sizes_no_discharge_while_a_charge_is_left(monkeypatch):
    """A device leaving a charge is still behind the target the correction assumes it had."""

    from tests.test_grid_reading_once import leaving_controller, total_states

    ems = leaving_controller(monkeypatch, repeated=False)
    ems.grid_reading_correction_w = 200

    total = ems.discharge_total_leaving_a_charge(400, total_states(), -300, 1200)

    assert total == -300
    assert ems.commanded_total_w == -300
    assert list(ems.load_history) == [100, 120]
    assert ems.charges_being_left == set()
