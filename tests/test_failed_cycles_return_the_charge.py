# SPDX-License-Identifier: AGPL-3.0-or-later
"""Three failed control cycles in a row return a charging device.

R1-1 in docs/developer/review-coverage.md. Since K8 a failing cycle no longer
ends the process, so the shutdown release that returned a charging device on
the way out never ran: a charge drew from the grid for as long as cycles
failed, up to the device's maximum SoC. Owner decision 2026-10-12: return a
charging device best effort after three failed cycles in a row, with the
release a stop uses, the full-charge assist's charge included.

The process goes on, so that release is not the last command: it keeps the
records' resend window, as the exit of a switch-off does, and goes out again
while a record says the charge is held. A release that fails is logged and the
loop goes on. A cycle that runs through ends the streak, and normal control may
charge again.
"""

import logging
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.charge_record import EXIT_RESEND_SECONDS, FOUND_EXIT_ATTEMPTS
from ems.config import AC_CHARGE_CONTROL_DEFAULTS
from ems.controller import CYCLE_FAILURES_BEFORE_CHARGE_RELEASE, EMSController
from ems.diagnostics import diagnose_live_snapshot_root_causes
from tests.test_ac_charge_regulator import (
    IDLE_EXIT,
    SteppedClock,
    following_device,
    idle_output_state,
)
from tests.test_shutdown_ends_the_full_charge_assist import (
    POWER,
    assist_charging,
    gates,
)
from tests.test_write_gates import ShellyStub

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]

CYCLE_SECONDS = 5
RELEASE_AFTER = 3


class BrokenMeter:
    def get_power(self):
        raise RuntimeError("meter client broke")


FAILURES = {
    "before the devices are read": "meter",
    "after the devices are read": "targets",
}


class Loop:
    """The live loop's guarded cycle over local-API clients on hardware doubles.

    Every write goes through the controller's own gated write and the
    device's client, as in the live loop; ``failing`` breaks the cycle at the
    meter read or in the target math.
    """

    def __init__(self, names=("WR1",), load=-900):
        self.items = {name: idle_output_state() for name in names}
        self.devices = []
        self.hardware = {}
        for name in names:
            dev, hardware = following_device(self.items[name], name=name)
            self.devices.append(dev)
            self.hardware[name] = hardware
        self.load = load
        self.controller = EMSController(
            devices=self.devices,
            shelly=ShellyStub(load),
            sleep_enabled=False,
        )
        self.clock = SteppedClock()
        self.results = []
        self.unreachable = set()

    def fetch(self, _devices):
        states = []
        for dev in self.devices:
            if dev.name in self.unreachable:
                states.append(None)
                continue
            dev.fetch()
            states.append(self.items[dev.name])
        return states

    @contextmanager
    def failing(self, where):
        if where == "meter":
            self.controller.shelly = BrokenMeter()
            try:
                yield
            finally:
                self.controller.shelly = ShellyStub(self.load)
            return
        with patch(
            "ems.controller.calculate_targets",
            side_effect=RuntimeError("target math broke"),
        ):
            yield

    def run(self, cycles=1, failing=None, **gate_options):
        with (
            gates(**gate_options),
            patch("ems.controller.fetch_all_devices", side_effect=self.fetch),
            patch.object(cfg, "SYSTEM_ENABLED", True),
            patch.object(cfg, "MAX_TOTAL_POWER", 800),
            patch.object(cfg, "MAX_DEVICE_POWER", 800),
            patch.object(cfg, "MIN_OUTPUT_LIMIT", 0),
            patch.object(cfg, "DEADBAND", 10),
            patch.object(cfg, "SOC_RECONCILE_INTERVAL", 0),
            patch.object(
                cfg,
                "AC_CHARGE_CONTROL_CONFIG",
                {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": True},
            ),
            patch("time.monotonic", self.clock),
            self.failing(failing) if failing else _nothing(),
        ):
            for _ in range(cycles):
                self.results.append(self.controller.run_guarded())
                self.clock.now += CYCLE_SECONDS

    def writes(self, name="WR1"):
        return self.hardware[name].writes


@contextmanager
def _nothing():
    yield


def charging_loop(names=("WR1",), **gate_options):
    loop = Loop(names)
    loop.run(cycles=20, **gate_options)
    for name in names:
        if not gate_options:
            assert loop.items[name].ac_mode == 1, "the regulator never charged"
    return loop


def released(caplog):
    return [
        line
        for line in caplog.text.splitlines()
        if "event=ac_charge_released_after_failed_cycles" in line
    ]


@pytest.mark.parametrize("where", FAILURES.values(), ids=FAILURES.keys())
def test_a_charge_is_returned_after_three_failed_cycles_in_a_row(where, caplog):
    loop = charging_loop()
    before = len(loop.writes())

    loop.run(cycles=RELEASE_AFTER - 1, failing=where)
    assert loop.writes()[before:] == []
    assert loop.items["WR1"].ac_mode == 1

    with caplog.at_level(logging.INFO):
        loop.run(failing=where)

    assert loop.results[-3:] == [False, False, False]
    assert loop.writes()[before:] == [IDLE_EXIT]
    assert loop.items["WR1"].ac_mode == 2
    assert loop.items["WR1"].grid_input == 0
    lines = released(caplog)
    assert len(lines) == 1, caplog.text
    assert "failed_cycles=3" in lines[0]
    assert "charge=ems_charge_on_record" in lines[0]
    assert "event=ac_charge_released_on_shutdown" not in caplog.text


@pytest.mark.parametrize("where", FAILURES.values(), ids=FAILURES.keys())
def test_a_device_that_charges_on_gets_the_release_again_once_per_window(where):
    loop = charging_loop()
    loop.hardware["WR1"].ignore(lambda properties: properties == IDLE_EXIT, times=99)
    before = len(loop.writes())

    loop.run(cycles=RELEASE_AFTER, failing=where)
    assert loop.writes()[before:] == [IDLE_EXIT]

    within = int(EXIT_RESEND_SECONDS // CYCLE_SECONDS) - 1
    loop.run(cycles=within, failing=where)
    assert loop.writes()[before:] == [IDLE_EXIT]

    loop.run(failing=where)
    assert loop.writes()[before:] == [IDLE_EXIT, IDLE_EXIT]
    assert loop.items["WR1"].ac_mode == 1


def test_a_device_that_reported_it_left_is_released_nothing_more():
    loop = charging_loop()
    before = len(loop.writes())

    loop.run(cycles=RELEASE_AFTER + 20, failing="targets")

    assert loop.writes()[before:] == [IDLE_EXIT]
    assert loop.devices[0].charge_commanded is False


def test_without_a_report_the_release_goes_out_once_per_window():
    """A cycle that fails before the read gets no word from the device that it left."""

    loop = charging_loop()
    before = len(loop.writes())
    windows = 3
    cycles = int(windows * EXIT_RESEND_SECONDS // CYCLE_SECONDS)

    loop.run(cycles=RELEASE_AFTER - 1 + cycles, failing="meter")

    assert loop.writes()[before:] == [IDLE_EXIT] * windows


def test_a_cycle_that_runs_through_ends_the_streak():
    loop = charging_loop()
    before = len(loop.writes())

    for _ in range(3):
        loop.run(cycles=RELEASE_AFTER - 1, failing="targets")
        loop.run()

    assert IDLE_EXIT not in loop.writes()[before:]
    assert loop.items["WR1"].ac_mode == 1
    assert loop.controller.cycle_failures == 0


def test_an_unreachable_device_gets_the_release_when_it_answers_again():
    loop = charging_loop()
    before = len(loop.writes())
    loop.unreachable.add("WR1")

    loop.run(cycles=RELEASE_AFTER + 10, failing="targets")
    assert loop.writes()[before:] == []

    loop.unreachable.clear()
    loop.run(failing="targets")

    assert loop.writes()[before:] == [IDLE_EXIT]
    assert loop.items["WR1"].ac_mode == 2


def test_normal_control_may_charge_again_once_a_cycle_runs_through():
    loop = charging_loop()
    entries = loop.controller.charge_direction.entries
    assert entries
    loop.run(cycles=RELEASE_AFTER, failing="targets")
    assert loop.items["WR1"].ac_mode == 2
    assert loop.controller.charge_direction.charging is False
    assert loop.controller.charge_direction.entries == entries

    loop.run(cycles=20)

    assert loop.items["WR1"].ac_mode == 1
    assert loop.items["WR1"].grid_input > 0


def test_a_device_the_ems_never_charged_is_released_nothing():
    loop = Loop(load=600)
    loop.run(cycles=5)
    before = len(loop.writes())

    loop.run(cycles=RELEASE_AFTER + 2, failing="targets")

    assert loop.writes()[before:] == []


def test_a_dry_run_release_is_withheld_once(caplog):
    """The regulator's target behind a shut gate reached no device; nothing is written."""

    loop = charging_loop(dry_run=True)
    assert loop.controller.charge_commanded_by_ems(loop.devices[0]) is True

    with caplog.at_level(logging.INFO):
        loop.run(
            cycles=RELEASE_AFTER + 10,
            failing="targets",
            dry_run=True,
        )

    assert loop.writes() == []
    withheld = [
        line
        for line in caplog.text.splitlines()
        if "event=ac_charge_release_withheld" in line
    ]
    assert len(withheld) == 1, caplog.text
    assert "blocked_by=dry_run" in withheld[0]
    assert "failed_cycles=3" in withheld[0]


def test_a_release_that_raises_does_not_take_down_the_loop(caplog):
    loop = charging_loop(names=("WR1", "WR2"))
    real_write = loop.controller.write_target

    def write_target(dev, value, charge_exit=None):
        if dev.name == "WR1":
            raise RuntimeError("release broke on WR1")
        return real_write(dev, value, charge_exit)

    loop.controller.write_target = write_target
    with caplog.at_level(logging.INFO):
        loop.run(cycles=RELEASE_AFTER, failing="targets")

    assert loop.results[-1] is False
    assert loop.items["WR1"].ac_mode == 1
    assert loop.items["WR2"].ac_mode == 2
    failed = [
        line
        for line in caplog.text.splitlines()
        if "event=ac_charge_release_failed" in line
    ]
    assert len(failed) == 1, caplog.text
    assert "device=WR1" in failed[0]
    assert "release broke on WR1" in failed[0]

    loop.controller.write_target = real_write
    loop.run(failing="targets")
    assert loop.items["WR1"].ac_mode == 2


def test_a_release_that_raises_before_its_write_does_not_take_down_the_loop(caplog):
    loop = charging_loop(names=("WR1", "WR2"))
    real_check = loop.controller.charge_commanded_by_ems

    def charge_commanded_by_ems(dev):
        if dev.name == "WR1" and loop.controller.cycle_failures:
            raise RuntimeError("record read broke on WR1")
        return real_check(dev)

    loop.controller.charge_commanded_by_ems = charge_commanded_by_ems
    with caplog.at_level(logging.INFO):
        loop.run(cycles=RELEASE_AFTER, failing="meter")

    assert loop.results[-1] is False
    assert loop.items["WR2"].ac_mode == 2
    assert "record read broke on WR1" in caplog.text


def assist_charging_at_full_window(tmp_path):
    """The assist's charge running, the device showing the assist's ``socSet``."""

    rig = assist_charging(tmp_path)
    rig.item.max_soc = 100
    return rig


@contextmanager
def broken_meter(rig):
    shelly = rig.controller.shelly
    rig.controller.shelly = BrokenMeter()
    try:
        yield
    finally:
        rig.controller.shelly = shelly


def assist_cycles(rig, cycles, failing=False):
    if failing == "meter":
        broken = broken_meter(rig)
    elif failing:
        broken = patch(
            "ems.controller.calculate_targets",
            side_effect=RuntimeError("target math broke"),
        )
    else:
        broken = _nothing()
    with rig.loop(), broken:
        for _ in range(cycles):
            rig.controller.run_guarded()
            rig.clock.now += CYCLE_SECONDS


def test_the_assist_s_charge_is_returned_and_not_put_back_while_cycles_fail(
    tmp_path, caplog
):
    rig = assist_charging_at_full_window(tmp_path)
    before = len(rig.hardware.writes)

    with caplog.at_level(logging.INFO):
        assist_cycles(rig, RELEASE_AFTER, failing=True)
    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.ac_mode == 2
    lines = released(caplog)
    assert len(lines) == 1, caplog.text
    assert "charge=battery_full_charge_assist" in lines[0]

    assist_cycles(rig, 20, failing=True)

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.ac_mode == 2
    assert rig.store.get_device_state("WR1")["full_charge_assist_active"] is True


def test_the_assist_goes_on_once_three_cycles_in_a_row_run_through(tmp_path):
    rig = assist_charging_at_full_window(tmp_path)
    assist_cycles(rig, RELEASE_AFTER + 2, failing=True)
    assert rig.item.ac_mode == 2

    assist_cycles(rig, CYCLE_FAILURES_BEFORE_CHARGE_RELEASE)
    assert rig.item.ac_mode == 2

    assist_cycles(rig, 1)

    assert rig.item.ac_mode == 1
    assert rig.item.grid_input == POWER
    assert rig.store.get_device_state("WR1")["ac_mode_restore_pending"] is True


def test_the_assist_s_ac_input_is_not_put_back_between_failure_streaks(tmp_path):
    """Three failed cycles, one through, over and over: the AC input stays returned.

    The assist took its AC input back in the first failing cycle after one that
    ran through, and the third failure released it again: acMode 1 and 2 in
    turn every fourth cycle. The regulator's entry confirmation keeps it from
    doing the same.
    """

    rig = assist_charging_at_full_window(tmp_path)
    before = len(rig.hardware.writes)

    for _ in range(6):
        assist_cycles(rig, RELEASE_AFTER, failing=True)
        assist_cycles(rig, 1)

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.ac_mode == 2


def _assist_entries(writes):
    return sum(1 for written in writes if written.get("acMode") == 1)


def test_the_assist_takes_its_ac_input_back_within_the_hourly_entry_limit(tmp_path, caplog):
    """Three failed cycles, three through, over and over (K24).

    The assist took its AC input back after every third pass and the next
    failure streak released it: the relay moved in and out every six cycles,
    where the regulator enters a charge no more often than
    ``max_charge_entries_per_hour``. The assist's re-entries after such a
    release now share that limit.
    """

    limit = AC_CHARGE_CONTROL_DEFAULTS["max_charge_entries_per_hour"]
    rig = assist_charging_at_full_window(tmp_path)
    before = len(rig.hardware.writes)

    with caplog.at_level(logging.INFO):
        for _ in range(limit + 8):
            assist_cycles(rig, RELEASE_AFTER, failing=True)
            assist_cycles(rig, CYCLE_FAILURES_BEFORE_CHARGE_RELEASE)

    assert _assist_entries(rig.written_since(before)) == limit
    assert rig.controller.assist_ac_input_withheld() is True
    limited = [
        line for line in caplog.text.splitlines()
        if "event=full_charge_assist_ac_input_rate_limited" in line
    ]
    assert len(limited) == 1, caplog.text
    assert limited[0].startswith("WARNING")

    rig.clock.now += 3600
    entries = _assist_entries(rig.hardware.writes)
    assist_cycles(rig, 2)

    assert _assist_entries(rig.hardware.writes) == entries + 1
    assert rig.item.ac_mode == 1


def test_a_failure_between_passes_restarts_the_three_in_a_row(tmp_path):
    """Passes that are not three in a row keep the assist's AC input withheld (R2a).

    A single failure short of a streak sets the count of passes back; without
    that, two passes, a failure and two more passes took the AC input back.
    """

    rig = assist_charging_at_full_window(tmp_path)
    assist_cycles(rig, RELEASE_AFTER, failing=True)
    assert rig.item.ac_mode == 2

    assist_cycles(rig, CYCLE_FAILURES_BEFORE_CHARGE_RELEASE - 1)
    assist_cycles(rig, 1, failing=True)
    assist_cycles(rig, CYCLE_FAILURES_BEFORE_CHARGE_RELEASE - 1)

    assert rig.item.ac_mode == 2
    assert rig.controller.assist_ac_input_withheld() is True

    assist_cycles(rig, 2)

    assert rig.item.ac_mode == 1


def test_the_assist_s_charge_left_in_input_gets_the_release_again_a_window_later(
    tmp_path,
):
    """A device that ignored the release is sent it once more, on the record's window."""

    rig = assist_charging_at_full_window(tmp_path)
    rig.hardware.ignore(lambda properties: properties == IDLE_EXIT)
    before = len(rig.hardware.writes)
    within = int(EXIT_RESEND_SECONDS // CYCLE_SECONDS) - 1

    assist_cycles(rig, RELEASE_AFTER + within, failing=True)
    assert rig.written_since(before) == [IDLE_EXIT]

    assist_cycles(rig, 10, failing=True)

    assert rig.written_since(before) == [IDLE_EXIT, IDLE_EXIT]
    assert rig.item.ac_mode == 2


def test_a_device_that_keeps_the_assist_s_ac_input_gets_three_exits_while_cycles_fail(
    tmp_path,
):
    """The release's cap, not a reconciler write every cycle (R1b).

    The assist's claim asked for acMode 2 in every failing cycle that reached
    the reconciler, which keeps no window, and each such cycle set the
    release's count back: a device that stayed in AC input was written every
    five seconds for as long as cycles failed.
    """

    rig = assist_charging_at_full_window(tmp_path)
    rig.hardware.ignore(lambda properties: properties.get("acMode") == 2, times=10_000)
    before = len(rig.hardware.writes)

    assist_cycles(rig, RELEASE_AFTER + 60, failing=True)

    assert rig.written_since(before) == [IDLE_EXIT] * FOUND_EXIT_ATTEMPTS
    assert rig.item.ac_mode == 1


def test_without_a_report_the_assist_s_charge_gets_its_three_exits_at_most(tmp_path):
    """Once per resend window, and no more than the cap the owner kept (OFF-1b)."""

    rig = assist_charging_at_full_window(tmp_path)
    rig.hardware.ignore(lambda properties: properties == IDLE_EXIT, times=99)
    before = len(rig.hardware.writes)
    within = int(EXIT_RESEND_SECONDS // CYCLE_SECONDS) - 1

    assist_cycles(rig, RELEASE_AFTER, failing="meter")
    assert rig.written_since(before) == [IDLE_EXIT]
    assist_cycles(rig, within, failing="meter")
    assert rig.written_since(before) == [IDLE_EXIT]
    assist_cycles(rig, 1, failing="meter")
    assert rig.written_since(before) == [IDLE_EXIT] * 2

    assist_cycles(rig, 10 * (within + 1), failing="meter")

    assert rig.written_since(before) == [IDLE_EXIT] * FOUND_EXIT_ATTEMPTS
    assert rig.item.ac_mode == 1


def test_diagnose_says_a_charge_is_returned_while_cycles_fail():
    causes = diagnose_live_snapshot_root_causes(
        {"status": "fresh", "failed_cycles": 3}
    )

    assert [cause["code"] for cause in causes] == ["control_cycles_failing"]
    message = causes[0]["message"]
    assert "a charging one keeps charging" not in message
    assert "third failed cycle in a row" in message


def test_the_release_follows_the_owner_s_three_failed_cycles():
    """The count the docs and the diagnose message name as the third."""

    assert CYCLE_FAILURES_BEFORE_CHARGE_RELEASE == RELEASE_AFTER == 3
