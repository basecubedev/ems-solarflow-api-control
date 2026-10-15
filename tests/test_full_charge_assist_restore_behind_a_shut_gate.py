# SPDX-License-Identifier: AGPL-3.0-or-later
"""A full-charge assist restore owed behind a shut state gate holds no output control (K22).

With ``allow_state_reconciliation_writes`` off the restore cannot be written:
neither the configured ``socSet`` nor ``acMode=2``, nor, over a report without
``acMode``, the exit to idle the assist holds its charge record for. Its claim
still kept the device out of output control until the gate opened, whether its
mode was known or not. The restore now stays owed and raises no claim while the
gate is shut, as a restore of ``socSet`` over a report without one does, and
goes out once the gate opens: a device still in the assist's AC input then gets
its ``acMode=2``, or its exit to idle, as it would have before.

Meanwhile the device is in output control like any other. With AC charging on,
as shipped, a process that finds it in AC input after a start cannot tell that
charge from the one a stopped EMS left running, and sends the one exit to AC
input found after a start, on the power write gate the shut state gate does
not close: the device leaves the charge before the restore is written.
"""

import logging

import pytest

from ems.charge_record import EXIT_RESEND_SECONDS, FOUND_EXIT_ATTEMPTS
from test_ac_charge_regulator import IDLE_EXIT
from test_battery_full_charge_assist import state
from test_full_charge_assist_behind_a_shut_gate import _switch
from test_shutdown_ends_the_full_charge_assist import Assist, charging_item
from test_unknown_state_fields import AcModeUnreported, _assist_completed

pytestmark = [
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

STATE_FIELDS = {"minSoc", "socSet", "acMode", "inputLimit", "smartMode", "gridOffMode"}

RESTORES = {
    "socSet": dict(
        rig=Assist,
        ac_charge_mode=False,
        item=lambda: state(soc=88, max_soc=100, ac_mode=2, input_limit_w=0),
    ),
    "acMode known": dict(rig=Assist, ac_charge_mode=True, item=charging_item),
    "acMode unknown": dict(rig=AcModeUnreported, ac_charge_mode=True, item=charging_item),
}


def _owed(tmp_path, restore, *, ac_charge_control=False):
    spec = RESTORES[restore]
    rig = spec["rig"](
        tmp_path,
        item=spec["item"](),
        store=_assist_completed(tmp_path, ac_charge_mode=spec["ac_charge_mode"]),
    )
    rig.ac_charge_control = ac_charge_control
    rig.dev.max_soc = 90
    return rig


def _state_writes(writes):
    return [written for written in writes if STATE_FIELDS.intersection(written)]


def _record(rig):
    return rig.store.get_device_state("WR1")


@pytest.mark.parametrize("restore", RESTORES)
def test_a_restore_owed_behind_a_shut_state_gate_holds_no_output_control(tmp_path, restore):
    rig = _owed(tmp_path, restore)

    rig.run(cycles=FOUND_EXIT_ATTEMPTS * 8, state_writes=False)

    assert rig.controller.device_output_control_allowed(rig.dev) is True
    assert _state_writes(rig.hardware.writes) == []
    assert IDLE_EXIT not in rig.hardware.writes
    record = _record(rig)
    assert record["restore_pending"] is True
    assert record["ac_mode_restore_pending"] is (restore != "socSet")


@pytest.mark.parametrize("restore", RESTORES)
def test_with_ac_charging_on_a_device_in_ac_input_gets_the_one_exit_found_after_a_start(
    tmp_path, restore
):
    """Not the restore: the regulator's exit, once, and the device takes it."""

    rig = _owed(tmp_path, restore, ac_charge_control=True)
    in_ac_input = restore != "socSet"

    rig.run(cycles=FOUND_EXIT_ATTEMPTS * 8, state_writes=False)

    assert _state_writes(rig.hardware.writes) == ([IDLE_EXIT] if in_ac_input else [])
    assert rig.dev.found_exit_attempts == (1 if in_ac_input else 0)
    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0
    assert rig.controller.device_output_control_allowed(rig.dev) is True
    record = _record(rig)
    assert record["restore_pending"] is True
    assert record["ac_mode_restore_pending"] is in_ac_input


@pytest.mark.parametrize("restore", RESTORES)
def test_the_restore_goes_out_once_the_gate_opens(tmp_path, caplog, restore):
    """A device still in the assist's AC input gets its way back, not a dropped restore."""

    rig = _owed(tmp_path, restore)
    rig.run(cycles=6, state_writes=False)
    before = len(rig.hardware.writes)

    with caplog.at_level(logging.INFO):
        rig.run(cycles=3)

    written = rig.written_since(before)
    assert {"socSet": 900} in written
    if restore == "acMode known":
        assert {"acMode": 2} in written
    if restore == "acMode unknown":
        assert written.count(IDLE_EXIT) == 1
        assert "claim=battery_full_charge_assist_restore" in caplog.text
    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0
    record = _record(rig)
    assert record["restore_pending"] is False
    assert record["ac_mode_restore_pending"] is False
    assert rig.controller.device_output_control_allowed(rig.dev) is True


@pytest.mark.parametrize(
    "ac_charge_control", [False, True], ids=["AC charging off", "AC charging on"]
)
@pytest.mark.parametrize("switch", ["system", "device"])
@pytest.mark.parametrize("restore", RESTORES)
def test_with_control_switched_off_a_restore_holds_nothing_either(
    tmp_path, restore, switch, ac_charge_control
):
    """Off is off (R14-1): after a restart the device is any AC input found after a start.

    With AC charging on it gets the one exit found after a start, as a running
    assist's device does; otherwise the charge is left to it. The restore
    claimed the device all the same, wrote nothing, and kept that exit from
    it. Once control is back the restore goes out and settles.
    """

    rig = _owed(tmp_path, restore, ac_charge_control=ac_charge_control)
    in_ac_input = restore != "socSet"
    _switch(rig, switch, False)

    rig.run(cycles=FOUND_EXIT_ATTEMPTS * 8)

    exit_taken = ac_charge_control and in_ac_input
    assert _state_writes(rig.hardware.writes) == ([IDLE_EXIT] if exit_taken else [])
    assert rig.item.ac_mode == (1 if in_ac_input and not exit_taken else 2)
    record = _record(rig)
    assert record["restore_pending"] is True
    assert record["ac_mode_restore_pending"] is in_ac_input

    _switch(rig, switch, True)
    before = len(rig.hardware.writes)
    rig.run(cycles=FOUND_EXIT_ATTEMPTS * 8)

    assert {"socSet": 900} in rig.written_since(before)
    assert rig.item.ac_mode == 2
    assert rig.item.grid_input == 0
    record = _record(rig)
    assert record["restore_pending"] is False
    assert record["ac_mode_restore_pending"] is False
    assert rig.controller.device_output_control_allowed(rig.dev) is True


def _exit_times(rig, cycles):
    """When each exit to idle went out, by the rig's clock, over ``cycles`` cycles."""

    times = []
    for _ in range(cycles):
        before = len(rig.hardware.writes)
        now = rig.clock.now
        rig.run()
        times += [now] * rig.written_since(before).count(IDLE_EXIT)
    return times


@pytest.mark.parametrize("switch", ["system", "device"])
@pytest.mark.parametrize("restore", ["acMode known", "acMode unknown"])
def test_a_restore_on_record_switched_off_gets_the_assist_s_three_exits_and_no_other(
    tmp_path, restore, switch
):
    """One exit owner (R16-1): the assist's record, with its cap, not the exit found after a start too.

    After a restart with control on the restore put the charge on the
    assist's record; the device ignores ``acMode=2``. Switched off at
    runtime, the restore stands down (R14-1) and the device was any AC input
    found after a start as well: both exits went out, six instead of three.
    The record's three go out, one per resend window, and the exit found
    after a start never does.
    """

    rig = _owed(tmp_path, restore, ac_charge_control=True)
    rig.hardware.ignore(lambda written: written.get("acMode") == 2, times=10_000)
    exit_times = _exit_times(rig, cycles=2)
    assert "WR1" in rig.controller.full_charge_assist_charges
    _switch(rig, switch, False)

    exit_times += _exit_times(rig, cycles=FOUND_EXIT_ATTEMPTS * 8)

    assert len(exit_times) == FOUND_EXIT_ATTEMPTS
    assert all(
        later - earlier >= EXIT_RESEND_SECONDS
        for earlier, later in zip(exit_times, exit_times[1:])
    )
    assert rig.dev.found_exit_attempts == 0
    assert rig.item.ac_mode == 1


def test_a_restore_whose_write_fails_behind_an_open_gate_still_holds_the_device(tmp_path):
    """Only the shut gate releases the hold: a failed write over a known socSet keeps it (7c31ee13)."""

    rig = _owed(tmp_path, "socSet")
    rig.hardware.post = lambda url, json=None, **_kwargs: _Refused()

    rig.run(cycles=2)

    assert _record(rig)["restore_pending"] is True
    assert rig.controller.device_output_control_allowed(rig.dev) is False


class _Refused:
    status_code = 500
    text = "refused"

    def json(self):
        return {}
