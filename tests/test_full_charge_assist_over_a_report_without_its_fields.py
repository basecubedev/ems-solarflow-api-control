# SPDX-License-Identifier: AGPL-3.0-or-later
"""A full-charge assist over a report without a field it writes holds no output control.

Nothing is written over a field the report does not carry: ``socSet`` not
(K20), the assist's ``acMode=1`` not over an unknown mode (K10), and that
``acMode=1`` goes out with its ``inputLimit`` or not at all. Every gate open, a
due assist started over such a report all the same and claimed the device, out
of output control: every ``outputLimit`` write was skipped and the device kept
its last limit into an export, the K25 shape behind a shut gate
(``test_full_charge_assist_behind_a_shut_gate.py``). A due assist now waits
for a report that carries what it writes, and a running one claims nothing
while the report lacks a field it still has to write. A charge it already
entered stays its own while it runs and the report shows it: it is on
record, and a switch-off ends it. Once the device left it, the assist needs
its fields again (R14-3). With control on, a field the report does not carry
causes no write: over a report without ``acMode`` and without a charging
``acStatus`` the assist's claim holds only the direction while its charge
is on record, output control as the claim below it has it and no regulator
charge into it (R19), and the record stays as it is (R17-1).
A charge shown by grid draw alone after the exits of a switch-off is the
device's after three, as on a transport's record, and the closed record
stays in the process (R16-2).
"""

import logging
from dataclasses import replace

import pytest

from ems.charge_record import EXIT_RESEND_SECONDS, FOUND_EXIT_ATTEMPTS
from ems.controller import EMSController
from test_ac_charge_regulator import IDLE_EXIT, following_http_device
from test_battery_full_charge_assist import state
from test_failed_cycles_return_the_charge import RELEASE_AFTER, assist_cycles
from test_full_charge_assist_behind_a_shut_gate import (
    _dashboard_assist,
    _feeding_the_house,
    _output_limits,
    _record,
    _state_writes,
)
from test_shutdown_ends_the_full_charge_assist import DUE, POWER, Assist
from test_write_gates import ShellyStub

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

HOUSE = 400
UNREPORTED = {
    "socSet": {"max_soc": 0, "max_soc_reported": False},
    "acMode": {"ac_mode": 0, "ac_mode_reported": False},
    "inputLimit": {"input_limit_w": 0, "input_limit_reported": False},
}
CASES = {
    "AC mode off, no socSet": (False, ("socSet",)),
    "AC mode on, no socSet": (True, ("socSet",)),
    "AC mode on, neither socSet nor acMode": (True, ("socSet", "acMode")),
    "AC mode on, no acMode": (True, ("acMode",)),
    "AC mode on, no inputLimit": (True, ("inputLimit",)),
}


class Unreported(Assist):
    """The loop over a device whose report leaves the fields in ``hidden`` out."""

    hidden = ()

    def fetch(self, _devices):
        self.dev.fetch()
        changes = {}
        for field in self.hidden:
            changes.update(UNREPORTED[field])
        return [replace(self.item, **changes)]


def _feeding(tmp_path, hidden):
    rig = _feeding_the_house(tmp_path, Unreported, state_writes=True)
    rig.hidden = hidden
    return rig


@pytest.mark.parametrize("ac_mode,hidden", CASES.values(), ids=CASES.keys())
def test_a_due_assist_waits_for_a_report_that_carries_what_it_writes(
    tmp_path, ac_mode, hidden
):
    rig = _feeding(tmp_path, hidden)
    fed = rig.item.output_limit
    rig.store.update_device_state("WR1", DUE, next_due_at=DUE.isoformat())
    before = len(rig.hardware.writes)

    rig.run(cycles=60, ac_mode=ac_mode)

    written = rig.written_since(before)
    assert _state_writes(written) == []
    assert _output_limits(written), "the device kept its limit into the export"
    assert rig.item.output_limit < fed
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    record = _record(rig)
    assert record["full_charge_assist_active"] is False
    assert record["next_due_at"] == DUE.isoformat()

    rig.hidden = ()
    before = len(rig.hardware.writes)
    rig.run(cycles=2, ac_mode=ac_mode)

    written = rig.written_since(before)
    assert _record(rig)["full_charge_assist_active"] is True
    assert {"socSet": 1000} in written
    assert ({"acMode": 1} in written) is ac_mode


@pytest.mark.parametrize("ac_mode,hidden", CASES.values(), ids=CASES.keys())
def test_a_running_assist_claims_nothing_while_the_report_lacks_what_it_writes(
    tmp_path, ac_mode, hidden
):
    """Its requests stay open, the card says it waits, and it goes on at a full report."""

    rig = _feeding(tmp_path, hidden)
    fed = rig.item.output_limit
    rig.store.mark_assist_started("WR1", DUE, ac_mode)
    before = len(rig.hardware.writes)

    rig.run(cycles=60, ac_mode=ac_mode)

    written = rig.written_since(before)
    soc_set_unreported = "socSet" in hidden
    state_written = _state_writes(written)
    if soc_set_unreported:
        assert state_written == []
    else:
        assert state_written
        assert all(properties == {"socSet": 1000} for properties in state_written)
    assert rig.item.output_limit < fed, "the device kept its limit into the export"
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    record = _record(rig)
    assert record["full_charge_assist_active"] is True
    assert record["max_soc_request_pending"] is soc_set_unreported
    assert record["ac_input_request_pending"] is ac_mode
    with rig.loop(ac_mode=ac_mode):
        assert rig.controller.full_charge_assist_held_by(rig.dev) == "fields_not_reported"
    assert _dashboard_assist(rig, ac_mode=ac_mode)["status"] == "waiting"

    rig.hidden = ()
    before = len(rig.hardware.writes)
    rig.run(cycles=2, ac_mode=ac_mode)

    written = rig.written_since(before)
    assert {"socSet": 1000} in written
    assert ({"acMode": 1} in written) is ac_mode
    assert rig.controller.device_output_control_allowed(rig.dev) is False
    assert _dashboard_assist(rig, ac_mode=ac_mode)["status"] == "active"


@pytest.mark.parametrize("hidden", [("acMode",), ("socSet", "acMode", "inputLimit")])
def test_a_charge_the_assist_entered_stays_its_own_over_a_report_without_its_fields(
    tmp_path, hidden
):
    """The device sits in the assist's AC input: claimed, on record, ended by a switch-off.

    Its ``acMode=1`` and ``inputLimit`` went out; claiming nothing here would
    drop the charge from the record a switch-off ends it by (OFF-1).
    """

    rig = Unreported(tmp_path)
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.hidden = hidden

    rig.run(cycles=6)

    assert rig.controller.device_output_control_allowed(rig.dev) is False
    with rig.loop():
        assert rig.controller.full_charge_assist_held_by(rig.dev) is None
        assert rig.controller.charges_held_by_ems() == {"WR1": POWER}
    rig.runtime.system["enabled"] = False
    before = len(rig.hardware.writes)

    rig.run()

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0


def _entered(tmp_path, **options):
    """The assist's charge running: its ``acMode=1`` and ``inputLimit`` went out."""

    rig = Unreported(tmp_path, **options)
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    return rig


def _left_by_a_switch_off(tmp_path):
    rig = _entered(tmp_path)
    rig.runtime.system["enabled"] = False
    rig.run()
    rig.runtime.system["enabled"] = True
    return rig


def _left_by_failed_cycles(tmp_path):
    rig = _entered(tmp_path)
    assist_cycles(rig, RELEASE_AFTER, failing=True)
    return rig


def _left_by_a_stop(tmp_path):
    rig = _entered(tmp_path)
    rig.release()
    return Unreported(tmp_path, item=rig.item, store=rig.store)


LEFT = {
    "switch-off exit": _left_by_a_switch_off,
    "release after failed cycles": _left_by_failed_cycles,
    "stop and restart": _left_by_a_stop,
}
NO_AC_MODE = {"no acMode": ("acMode",), "neither acMode nor inputLimit": ("acMode", "inputLimit")}


@pytest.mark.parametrize("hidden", NO_AC_MODE.values(), ids=NO_AC_MODE.keys())
@pytest.mark.parametrize("left", LEFT.values(), ids=LEFT.keys())
def test_a_charge_the_device_left_holds_it_no_more_over_a_report_without_its_fields(
    tmp_path, left, hidden
):
    """Once the device left the assist's charge, the assist needs its fields again (R14-3).

    The exemption for a charge it entered read the owed restore of ``acMode``,
    which only a claim of ``acMode`` 2 settles, so it outlived the charge: the
    assist claimed the idle device into AC input, nothing could write that
    over the unknown mode, every ``outputLimit`` was skipped while the house
    imported, and the charge stood on record at the assist's setpoint.
    """

    rig = left(tmp_path)
    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0
    rig.hidden = hidden
    rig.controller.shelly = ShellyStub(HOUSE)
    before = len(rig.hardware.writes)

    assist_cycles(rig, 30)

    written = rig.written_since(before)
    assert not [w for w in written if {"acMode", "inputLimit"} & set(w)]
    assert _output_limits(written), "the idle device was held out of output control"
    assert rig.item.output_limit > 0
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    with rig.loop():
        assert rig.controller.full_charge_assist_held_by(rig.dev) == "fields_not_reported"
        assert rig.controller.charges_held_by_ems() == {}
    assert _record(rig)["full_charge_assist_active"] is True
    assert _dashboard_assist(rig)["status"] == "waiting"

    rig.hidden = ()
    assist_cycles(rig, 2)

    assert rig.item.ac_mode == 1
    assert rig.item.grid_input == POWER
    assert rig.controller.device_output_control_allowed(rig.dev) is False


@pytest.mark.parametrize("hidden", NO_AC_MODE.values(), ids=NO_AC_MODE.keys())
def test_after_a_restart_a_charge_the_device_still_shows_stays_the_assist_s(
    tmp_path, hidden
):
    """No record outlives the process: the report that shows the charge keeps it claimed.

    A stop by signal leaves the assist's charge running; the next process
    claims it over a report without ``acMode``, puts it on record, and a
    switch-off ends it.
    """

    first = _entered(tmp_path)
    rig = Unreported(tmp_path, item=first.item, store=first.store)
    rig.hidden = hidden

    rig.run(cycles=6)

    assert rig.controller.device_output_control_allowed(rig.dev) is False
    with rig.loop():
        assert rig.controller.charges_held_by_ems() == {"WR1": POWER}
    rig.runtime.system["enabled"] = False
    before = len(rig.hardware.writes)

    rig.run()

    assert rig.written_since(before) == [IDLE_EXIT]
    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0


IDLE_DRAW = 8
WINDOW_CYCLES = int(EXIT_RESEND_SECONDS // 5)


class IdleDraw(Unreported):
    """A device that draws a few watts idle once out of its charge (see :mod:`ems.charge_record`)."""

    idle_draw = 0
    offline = False

    def fetch(self, devices):
        if self.offline:
            return [None]
        report = super().fetch(devices)[0]
        if self.idle_draw and self.item.ac_mode == 2:
            report = replace(report, grid_input=self.idle_draw)
        return [report]


def _left_lines(caplog):
    return [
        line for line in caplog.text.splitlines()
        if "event=ac_charge_left_to_device" in line
    ]


def _ac_mode_writes(written):
    return [w for w in written if "acMode" in w]


def _assert_left_to_output_control(rig):
    with rig.loop():
        assert rig.controller.device_output_control_allowed(rig.dev) is True
        assert rig.controller.full_charge_assist_held_by(rig.dev) == "fields_not_reported"
        assert rig.controller.charges_held_by_ems() == {}
    assert _record(rig)["full_charge_assist_active"] is True
    assert _dashboard_assist(rig)["status"] == "waiting"


def _takes_its_ac_input_back_at_a_full_report(rig):
    rig.hidden = ()
    assist_cycles(rig, 2)

    assert rig.item.ac_mode == 1
    assert rig.item.grid_input == POWER
    assert rig.controller.device_output_control_allowed(rig.dev) is False


THREE_WINDOWS = FOUND_EXIT_ATTEMPTS * WINDOW_CYCLES + 2


def _idle_after_one_exit(tmp_path, hidden):
    """The assist's charge, one switch-off cycle: the device took its exit and draws a few watts idle."""

    rig = IdleDraw(tmp_path)
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.hidden = hidden
    rig.idle_draw = IDLE_DRAW
    rig.runtime.system["enabled"] = False
    rig.controller.shelly = ShellyStub(HOUSE)
    return rig


@pytest.mark.parametrize("hidden", NO_AC_MODE.values(), ids=NO_AC_MODE.keys())
def test_a_charge_shown_by_idle_draw_alone_after_its_exits_is_left_to_the_device(
    tmp_path, caplog, hidden
):
    """Bounded as a transport's record bounds it (R16-2): three exits, then the device's.

    The device took the exit of a switch-off and draws a few watts idle;
    its report carries no ``acMode`` and no charging ``acStatus``, so it never
    shows that it left. The assist's record had no bound for that, and once
    control was back its claim put the charge back on record: the idle device
    was claimed into AC input for good, nothing written over the unknown mode,
    every ``outputLimit`` skipped while the house imported.
    """

    rig = _idle_after_one_exit(tmp_path, hidden)

    with caplog.at_level(logging.INFO):
        rig.run(cycles=THREE_WINDOWS)
        assert rig.item.ac_mode == 2
        rig.runtime.system["enabled"] = True
        before = len(rig.hardware.writes)
        assist_cycles(rig, 30)

    assert rig.hardware.writes.count(IDLE_EXIT) == FOUND_EXIT_ATTEMPTS
    left = _left_lines(caplog)
    assert len(left) == 1, caplog.text
    assert "charge=battery_full_charge_assist" in left[0]
    written = rig.written_since(before)
    assert _output_limits(written), "the idle device was held out of output control"
    assert rig.item.output_limit > 0
    _assert_left_to_output_control(rig)
    _takes_its_ac_input_back_at_a_full_report(rig)


def _assert_one_exit_stands_in_output_control(rig, written):
    assert _ac_mode_writes(written) == []
    assert _output_limits(written), "the idle device was held out of output control"
    assert rig.item.output_limit > 0
    with rig.loop():
        assert rig.controller.device_output_control_allowed(rig.dev) is True
        assert rig.controller.full_charge_assist_held_by(rig.dev) == "fields_not_reported"
        assert rig.controller.charges_held_by_ems() == {"WR1": IDLE_DRAW}


@pytest.mark.parametrize("hidden", NO_AC_MODE.values(), ids=NO_AC_MODE.keys())
def test_an_exit_shown_by_idle_draw_alone_stands_with_control_on_until_the_next_switch_off(
    tmp_path, caplog, hidden
):
    """With control on, a report without ``acMode`` causes no write (R17-1).

    One cycle off sent one exit. Control back on, the claim stands down
    without writing, so the idle device is in output control, and the record
    is left as it is: its exit stands, nothing put the charge back on it.
    The next switch-off sends the two the bound has left, and the charge is
    the device's.
    """

    rig = _idle_after_one_exit(tmp_path, hidden)

    with caplog.at_level(logging.INFO):
        rig.run()
        assert rig.item.ac_mode == 2
        rig.runtime.system["enabled"] = True
        before = len(rig.hardware.writes)
        assist_cycles(rig, 30)
        _assert_one_exit_stands_in_output_control(rig, rig.written_since(before))
        assert _left_lines(caplog) == []
        rig.runtime.system["enabled"] = False
        rig.run(cycles=THREE_WINDOWS)
        rig.runtime.system["enabled"] = True
        assist_cycles(rig, 2)

    assert rig.hardware.writes.count(IDLE_EXIT) == FOUND_EXIT_ATTEMPTS
    assert len(_left_lines(caplog)) == 1, caplog.text
    _assert_left_to_output_control(rig)
    _takes_its_ac_input_back_at_a_full_report(rig)


def test_a_cycle_without_a_report_does_not_take_back_an_exit_that_stands(tmp_path, caplog):
    """Only a report puts the charge back on record: the claim alone wrote nothing.

    Taken back in a cycle the device did not answer, the exit and its count
    of exits answered by grid draw alone started afresh, the assist's cap of
    three ran out first, and the idle device was held for good. The exit
    stands across the cycle without a report, and the next switch-off sends
    the two the bound has left.
    """

    rig = _idle_after_one_exit(tmp_path, ("acMode",))

    with caplog.at_level(logging.INFO):
        rig.run()
        rig.runtime.system["enabled"] = True
        before = len(rig.hardware.writes)
        assist_cycles(rig, 2)
        rig.offline = True
        assist_cycles(rig, 1)
        rig.offline = False
        assist_cycles(rig, 30)
        _assert_one_exit_stands_in_output_control(rig, rig.written_since(before))
        rig.runtime.system["enabled"] = False
        rig.run(cycles=THREE_WINDOWS)
        rig.runtime.system["enabled"] = True
        assist_cycles(rig, 2)

    assert rig.hardware.writes.count(IDLE_EXIT) == FOUND_EXIT_ATTEMPTS
    assert len(_left_lines(caplog)) == 1, caplog.text
    _assert_left_to_output_control(rig)


@pytest.mark.parametrize("hidden", NO_AC_MODE.values(), ids=NO_AC_MODE.keys())
def test_a_charge_the_device_left_is_not_claimed_again_by_its_idle_draw(tmp_path, hidden):
    """A closed record stays in the process: the after-a-restart rule is not for it (R16-2).

    The record was dropped once nothing held the device, and the next report
    that showed a few watts of draw over no ``acMode`` was read by the rule
    for a process that never had a record: the charge was the assist's again,
    the idle device claimed into AC input for good.
    """

    rig = _left_by_a_switch_off(tmp_path)
    rig.__class__ = IdleDraw
    rig.hidden = hidden
    rig.controller.shelly = ShellyStub(HOUSE)
    assist_cycles(rig, 10)
    rig.idle_draw = IDLE_DRAW
    before = len(rig.hardware.writes)

    assist_cycles(rig, 30)

    written = rig.written_since(before)
    assert not [w for w in written if {"acMode", "inputLimit"} & set(w)]
    assert rig.item.output_limit > 0
    _assert_left_to_output_control(rig)
    _takes_its_ac_input_back_at_a_full_report(rig)


@pytest.mark.parametrize("hidden", NO_AC_MODE.values(), ids=NO_AC_MODE.keys())
def test_after_a_restart_a_charge_shown_by_idle_draw_alone_is_not_claimed(
    tmp_path, caplog, hidden
):
    """No record from before, and a report that does not show the charge: nothing is claimed.

    A stop released the charge; the device draws a few watts idle and its
    report shows no direction. The next process read that as the charge
    running, claimed it and sent it exits with control on, over a report
    without ``acMode`` (R17-1). It claims nothing and writes no ``acMode``.
    """

    first = _entered(tmp_path)
    first.release()
    rig = IdleDraw(tmp_path, item=first.item, store=first.store)
    rig.hidden = hidden
    rig.idle_draw = IDLE_DRAW
    rig.controller.shelly = ShellyStub(HOUSE)
    before = len(rig.hardware.writes)

    with caplog.at_level(logging.INFO):
        assist_cycles(rig, 30)

    written = rig.written_since(before)
    assert _ac_mode_writes(written) == []
    assert _left_lines(caplog) == []
    assert _output_limits(written)
    assert "WR1" not in rig.controller.full_charge_assist_charges
    _assert_left_to_output_control(rig)
    _takes_its_ac_input_back_at_a_full_report(rig)


LONG_RUN = 120


class Intermittent(Unreported):
    """A model without ``acStatus`` whose report lacks ``acMode`` once in ``every`` polls."""

    every = 0
    polls = 0

    def fetch(self, devices):
        report = super().fetch(devices)[0]
        if not self.every:
            return [report]
        self.polls += 1
        report = replace(report, ac_status=0)
        if self.polls % self.every == 0:
            report = replace(report, **UNREPORTED["acMode"])
        return [report]


class Flapping(Unreported):
    """A report without the fields in ``hidden`` whose ``acStatus`` alternates 2 and 1."""

    polls = 0

    def fetch(self, devices):
        report = super().fetch(devices)[0]
        if not self.hidden:
            return [report]
        self.polls += 1
        return [replace(report, ac_status=2 if self.polls % 2 else 1)]


@pytest.mark.parametrize("every", [4, 12])
def test_a_report_that_loses_ac_mode_now_and_then_moves_no_relay_with_control_on(
    tmp_path, every
):
    """A field the report does not carry causes no write with control on (R17-1).

    The assist's charge runs on a model that reports no ``acStatus``, and one
    report in ``every`` lacks ``acMode``. Each such report was owed the exit
    and the next full one put the device back into AC input: over 600 s, 30
    exits and 29 re-entries with one report in four. The claim stands down
    over such a report without writing, and the charge stays on record.
    """

    rig = Intermittent(tmp_path)
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.every = every
    before = len(rig.hardware.writes)

    rig.run(cycles=LONG_RUN)

    assert _ac_mode_writes(rig.written_since(before)) == []
    assert rig.item.ac_mode == 1
    assert rig.item.grid_input == POWER
    with rig.loop():
        assert rig.controller.charges_held_by_ems() == {"WR1": POWER}
    rig.runtime.system["enabled"] = False
    before = len(rig.hardware.writes)

    rig.run()

    assert rig.written_since(before) == [IDLE_EXIT]


def test_a_charge_its_ac_status_shows_every_other_report_is_sent_no_exit_with_control_on(
    tmp_path, caplog
):
    """No exit over a report without ``acMode`` with control on (R17-1); a switch-off sends three.

    The report lacks ``acMode`` and its ``acStatus`` alternates 2 and 1 while
    the device charges on and ignores ``acMode=2``. Each report with
    ``acStatus`` 1 was owed the exit, each with 2 put the charge back on
    record and its count of exits afresh: 60 exits in 600 s, one every 10 s.
    """

    rig = Flapping(tmp_path)
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.hardware.ignore(lambda written: written.get("acMode") == 2, times=10_000)
    rig.hidden = ("acMode",)
    before = len(rig.hardware.writes)

    rig.run(cycles=LONG_RUN)

    assert _ac_mode_writes(rig.written_since(before)) == []
    rig.runtime.system["enabled"] = False
    before = len(rig.hardware.writes)
    with caplog.at_level(logging.INFO):
        rig.run(cycles=THREE_WINDOWS)

    assert _ac_mode_writes(rig.written_since(before)) == [IDLE_EXIT] * FOUND_EXIT_ATTEMPTS
    assert len(_left_lines(caplog)) == 1, caplog.text
    assert rig.item.ac_mode == 1


def test_a_report_that_shows_the_charge_by_ac_status_alone_takes_back_no_exit(tmp_path):
    """Only a known ``acMode`` puts the charge back on record once an exit is out (R17-1).

    A switch-off sent one exit; the device ignored it and charges on, its
    report without ``acMode`` showing ``acStatus`` 2. Control back on, each
    such report put the charge back on record and its count of exits
    afresh, so the next switch-off sent three more. The exit stands: the
    two switch-offs send three between them.
    """

    rig = Unreported(tmp_path)
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.hardware.ignore(lambda written: written.get("acMode") == 2, times=10_000)
    rig.hidden = ("acMode",)
    before = len(rig.hardware.writes)
    rig.runtime.system["enabled"] = False
    rig.run()
    rig.runtime.system["enabled"] = True
    rig.run(cycles=2 * WINDOW_CYCLES)
    assert rig.controller.device_output_control_allowed(rig.dev) is False
    rig.runtime.system["enabled"] = False

    rig.run(cycles=THREE_WINDOWS)

    assert _ac_mode_writes(rig.written_since(before)) == [IDLE_EXIT] * FOUND_EXIT_ATTEMPTS
    assert rig.item.ac_mode == 1
    assert rig.item.grid_input == POWER


OPERATOR_ROLES = {
    "operator ac_output": ("ac_output", True),
    "operator park": ("ac_input", False),
}


@pytest.mark.parametrize("role,output_control", OPERATOR_ROLES.values(), ids=OPERATOR_ROLES.keys())
def test_while_its_charge_is_on_record_no_operator_role_writes_ac_mode_over_a_report_without_it(
    tmp_path, role, output_control
):
    """The assist holds the direction while its charge is on record (R17-1).

    Standing down over a report without ``acMode``, the assist's claim left
    the device to the next claim in line, and a role the operator set is
    written over an unknown mode (K10): one report in four without
    ``acMode`` gave 30 bare ``acMode=2`` writes and 29 re-entries in 600 s
    with an ``ac_output`` role. The claim now holds the direction -- no
    ``acMode`` of its own, the power command's -- at the assist's priority,
    and leaves output control as the claim below it has it: the regulator's
    under ``ac_output``, nobody's under a park (R19-2).
    """

    rig = Intermittent(tmp_path)
    rig.runtime.devices["WR1"] = {"runtime_role": role}
    rig.controller.shelly = ShellyStub(HOUSE)
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.every = 4
    before = len(rig.hardware.writes)

    rig.run(cycles=LONG_RUN)

    written = rig.written_since(before)
    assert _ac_mode_writes(written) == []
    assert bool(_output_limits(written)) is output_control
    assert rig.item.ac_mode == 1
    assert rig.item.grid_input == POWER
    claim = rig.controller.runtime_intents["WR1"]
    assert (claim.reason, claim.desired_ac_mode, claim.output_control_allowed) == (
        "battery_full_charge_assist",
        None,
        output_control,
    )
    with rig.loop():
        assert rig.controller.full_charge_assist_held_by(rig.dev) == "fields_not_reported"
        assert rig.controller.charges_held_by_ems() == {"WR1": POWER}
    assert _dashboard_assist(rig)["status"] == "waiting"


def test_once_the_device_left_the_charge_the_assist_holds_no_direction(tmp_path):
    """With its record closed the assist claims nothing, and a role the operator set acts (K10).

    The direction is held only while the assist's charge is on record: the
    device that took a switch-off's exit is any device over a report
    without ``acMode`` again.
    """

    rig = _left_by_a_switch_off(tmp_path)
    rig.runtime.devices["WR1"] = {"runtime_role": "ac_output"}
    rig.hidden = ("acMode",)
    before = len(rig.hardware.writes)

    assist_cycles(rig, 3)

    assert rig.controller.runtime_intents["WR1"].reason == "ac_output"
    assert {"acMode": 2} in rig.written_since(before)


PARKS = {"park": "ac_input", "legacy reserved": "reserved"}


@pytest.mark.parametrize("hidden", NO_AC_MODE.values(), ids=NO_AC_MODE.keys())
@pytest.mark.parametrize("park", PARKS.values(), ids=PARKS.keys())
def test_a_park_keeps_the_device_out_of_output_control_under_the_direction_hold(
    tmp_path, park, hidden
):
    """The hold leaves output control as the claim below it has it (R19-2).

    One switch-off cycle sent its exit; the parked device took it, sits in
    ``acMode`` 2 and draws a few watts idle, its report without ``acMode``.
    The hold outranks the park and allowed output control, so the regulator
    wrote 400 W and then 800 W into the parked device, which discharged it.
    It stays parked, and nothing writes ``acMode`` over the unknown mode.
    """

    rig = _idle_after_one_exit(tmp_path, hidden)
    rig.runtime.devices["WR1"] = {"runtime_role": park}
    rig.run()
    assert rig.item.ac_mode == 2
    rig.runtime.system["enabled"] = True
    before = len(rig.hardware.writes)

    assist_cycles(rig, 30)

    written = rig.written_since(before)
    assert _output_limits(written) == []
    assert _ac_mode_writes(written) == []
    claim = rig.controller.runtime_intents["WR1"]
    assert (claim.reason, claim.desired_ac_mode, claim.output_control_allowed) == (
        "battery_full_charge_assist",
        None,
        False,
    )


def test_under_the_direction_hold_the_regulator_charges_nothing(tmp_path, caplog):
    """The assist's charge on record is not the regulator's to charge into (R19-1).

    The report lacks ``acMode`` and shows no ``acStatus``; with AC charging
    on and an export the regulator entered a charge of its own over the
    unknown mode, ``acMode=1`` and ``inputLimit``, into the assist's charge,
    and its transport record and the assist's stood open together: at a
    switch-off the device that ignored the exit was sent 40 exits in 1200 s.
    The device is not eligible for a regulator charge while the hold stands,
    and the switch-off sends the assist's record its three.
    """

    rig = Intermittent(tmp_path)
    rig.ac_charge_control = True
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.every = 1
    rig.controller.shelly = ShellyStub(-600)
    before = len(rig.hardware.writes)

    rig.run(cycles=40)

    written = rig.written_since(before)
    assert [w for w in written if {"acMode", "inputLimit"} & set(w)] == []
    assert rig.dev.charge_commanded is False
    assert rig.item.ac_mode == 1
    assert rig.item.grid_input == POWER
    rig.hardware.ignore(lambda written: written.get("acMode") == 2, times=10_000)
    rig.runtime.system["enabled"] = False
    before = len(rig.hardware.writes)
    with caplog.at_level(logging.INFO):
        rig.run(cycles=240)

    assert _ac_mode_writes(rig.written_since(before)) == [IDLE_EXIT] * FOUND_EXIT_ATTEMPTS
    assert rig.dev.charge_commanded is False


class TwoDevices(Assist):
    """WR1 in the assist's charge, its report without ``acMode`` or ``acStatus`` once ``hide``; WR2 free to charge."""

    hide = False

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.item2 = state(soc=40, max_soc=100, ac_mode=2, input_limit_w=0)
        self.dev2, self.hardware2 = following_http_device(
            self.item2, name="WR2", hardware_profile="solarflow_800_pro_2"
        )
        self.controller = EMSController(
            devices=[self.dev, self.dev2],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=self.runtime,
            battery_full_charge_store=self.store,
        )

    def fetch(self, _devices):
        self.dev.fetch()
        self.dev2.fetch()
        report = self.item
        if self.hide:
            report = replace(report, ac_status=0, **UNREPORTED["acMode"])
        return [report, self.item2]


def test_while_another_device_charges_the_held_device_takes_no_charge(tmp_path):
    """The guard holds through a fleet charge, not only before one starts (R21-1).

    WR2 charges from the export; WR1's direction the assist holds. Read only
    while no charge ran, the guard let the regulator give WR1 a share once
    WR2 had started one: ``acMode=1`` and an ``inputLimit`` over the unknown
    mode, a transport record beside the assist's, and 40 exits in 1200 s at
    a switch-off the device ignored.
    """

    rig = TwoDevices(tmp_path)
    rig.ac_charge_control = True
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.hide = True
    rig.controller.shelly = ShellyStub(-1200)
    before = len(rig.hardware.writes)

    rig.run(cycles=60)

    assert rig.controller.charge_direction.charging is True
    assert rig.item2.grid_input > 0, "WR2 never charged"
    written = rig.written_since(before)
    assert [w for w in written if {"acMode", "inputLimit"} & set(w)] == []
    assert rig.dev.charge_commanded is False
    rig.hardware.ignore(lambda written: written.get("acMode") == 2, times=10_000)
    rig.runtime.system["enabled"] = False
    before = len(rig.hardware.writes)

    rig.run(cycles=240)

    assert _ac_mode_writes(rig.written_since(before)) == [IDLE_EXIT] * FOUND_EXIT_ATTEMPTS
    assert rig.dev.charge_commanded is False


def _after_a_restart(tmp_path):
    first = _entered(tmp_path)
    rig = Intermittent(tmp_path, item=first.item, store=first.store)
    rig.every = 1
    return rig


def _after_the_three_exits_of_a_switch_off(tmp_path):
    rig = _idle_after_one_exit(tmp_path, ("acMode",))
    rig.run(cycles=THREE_WINDOWS)
    rig.runtime.system["enabled"] = True
    return rig


def _after_the_device_left(tmp_path):
    rig = _left_by_a_switch_off(tmp_path)
    rig.hidden = ("acMode",)
    return rig


NO_CHARGE_ON_RECORD = {
    "the device left the charge": _after_the_device_left,
    "after a restart": _after_a_restart,
    "after the three exits of a switch-off": _after_the_three_exits_of_a_switch_off,
}


@pytest.mark.parametrize("make", NO_CHARGE_ON_RECORD.values(), ids=NO_CHARGE_ON_RECORD.keys())
def test_without_its_charge_on_record_the_assist_keeps_no_device_from_the_regulator(
    tmp_path, make
):
    """The guard refuses only while the hold stands, the assist's charge open on record (R21-2).

    The running assist stands down over a report without ``acMode`` and
    holds nothing: the device left its charge, a restart left no record, or
    three exits left the charge to the device. With AC charging on and an
    export the regulator enters its own charge there, as on any device.
    """

    rig = make(tmp_path)
    rig.ac_charge_control = True
    rig.controller.shelly = ShellyStub(-600)
    before = len(rig.hardware.writes)

    assist_cycles(rig, 30)

    with rig.loop():
        assert rig.controller.full_charge_assist_held_by(rig.dev) == "fields_not_reported"
        assert rig.controller.full_charge_assist_holds_direction(rig.dev) is False
    assert _record(rig)["full_charge_assist_active"] is True
    entries = [
        w for w in rig.written_since(before)
        if w.get("acMode") == 1 and {"outputLimit", "inputLimit"} <= set(w)
    ]
    assert entries, "the regulator never entered its charge"
    assert rig.dev.charge_commanded is True


@pytest.mark.parametrize("hide", [False, True], ids=["its claim", "its direction hold"])
def test_the_control_view_names_the_assist_for_a_device_it_holds_out_of_a_fleet_charge(
    tmp_path, hide
):
    """WR2 charges from surplus while the assist holds WR1: WR1's reason names the assist.

    It read ``ac_charge_not_permitted``, which the Control view tells as a
    model without an AC charge path, charging switched off or a battery
    full, drained or unavailable, while WR1 drew 200 W in the assist's charge.
    """

    rig = TwoDevices(tmp_path)
    rig.ac_charge_control = True
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.hide = hide
    rig.controller.shelly = ShellyStub(-1200)

    rig.run(cycles=40)

    explanation = rig.controller.last_control_explanation.to_dict()
    assert explanation["mode"] == "ac_charge"
    devices = explanation["devices"]
    assert devices["WR2"]["decision_reason"] == "ac_charge_allocation"
    assert devices["WR1"]["decision_reason"] == "ac_charge_held_by_full_charge_assist"
    assert rig.item.grid_input == POWER


def _wr1_reason_in_a_fleet_charge(rig):
    explanation = rig.controller.last_control_explanation.to_dict()
    assert explanation["mode"] == "ac_charge"
    assert explanation["devices"]["WR2"]["decision_reason"] == "ac_charge_allocation"
    return explanation["devices"]["WR1"]["decision_reason"]


def test_the_control_view_names_the_assist_for_a_device_its_restore_holds(tmp_path):
    """The assist completed while WR2 charges; WR1 ignores ``acMode=2`` and stays under the restore."""

    rig = TwoDevices(tmp_path)
    rig.ac_charge_control = True
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.controller.shelly = ShellyStub(-1200)
    rig.run(cycles=40)
    rig.hardware.ignore(lambda written: written.get("acMode") == 2, times=10_000)
    rig.item.soc_limit = 1

    rig.run(cycles=5)

    assert _record(rig)["full_charge_assist_active"] is False
    assert rig.controller.runtime_intents["WR1"].reason == "battery_full_charge_assist_restore"
    assert _wr1_reason_in_a_fleet_charge(rig) == "ac_charge_held_by_full_charge_assist"


def _never_entered(rig):
    rig.store.mark_assist_started("WR1", DUE, True)
    rig.hide = True


def _left_the_charge(rig):
    rig.run()
    assert rig.item.ac_mode == 1, "the assist never put the device into AC input"
    rig.run()
    rig.runtime.system["enabled"] = False
    rig.run()
    assert rig.item.ac_mode == 2
    rig.runtime.system["enabled"] = True
    rig.hide = True


ASSIST_STANDING_DOWN = {"never entered": _never_entered, "left its charge": _left_the_charge}


@pytest.mark.parametrize("setup", ASSIST_STANDING_DOWN.values(), ids=ASSIST_STANDING_DOWN.keys())
def test_an_assist_that_claims_nothing_is_not_named_for_a_device_switched_off_for_charging(
    tmp_path, setup
):
    """WR1's own switch keeps it from the charge, not the assist, which stands down and holds nothing."""

    rig = TwoDevices(tmp_path)
    rig.ac_charge_control = True
    setup(rig)
    rig.runtime.devices["WR1"] = {"ac_charge_enabled": False}
    rig.controller.shelly = ShellyStub(-1200)

    rig.run(cycles=40)

    with rig.loop():
        assert rig.controller.full_charge_assist_held_by(rig.dev) == "fields_not_reported"
        assert rig.controller.full_charge_assist_holds_direction(rig.dev) is False
    assert _record(rig)["full_charge_assist_active"] is True
    assert _wr1_reason_in_a_fleet_charge(rig) == "ac_charge_not_permitted"


@pytest.mark.parametrize("role", PARKS.values(), ids=PARKS.keys())
def test_a_park_without_the_assist_is_not_named_as_the_assist(tmp_path, role):
    """An operator's AC-input role keeps WR1 from the charge; no assist runs on it."""

    rig = TwoDevices(tmp_path)
    rig.ac_charge_control = True
    rig.store.update_device_state("WR1", DUE, next_due_at="2099-01-01T00:00:00+00:00")
    rig.runtime.devices["WR1"] = {"runtime_role": role}
    rig.controller.shelly = ShellyStub(-1200)

    rig.run(cycles=40)

    assert _record(rig)["full_charge_assist_active"] is False
    assert rig.controller.device_output_control_allowed(rig.dev) is False
    assert _wr1_reason_in_a_fleet_charge(rig) == "ac_charge_not_permitted"
