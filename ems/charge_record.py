# SPDX-License-Identifier: AGPL-3.0-or-later
"""The EMS's own record of a charge it put on one device.

Each transport keeps one, because the transport knows what it put on the wire,
and that outlives the regulator's memory: the reset when control is switched
back on, and the zero an unreachable device is given. The controller reads it
as the EMS's own claim to a charge.

A charge command opens the record. Sending the way out does not close it: a
device can accept a write and not carry it out, and treating the accepted exit
as the end left such a device drawing from the grid with nobody watching. Only
the device closes the record, by reporting after an exit that it left the
charge -- out of the AC-input direction, its exit's ``inputLimit = 0``, or a
charge at a setpoint the EMS never wrote, which is someone else's charge. Until
then the exit is due again once per resend window, so a device that ignores it
is not sent the same exit every cycle. The exit counts as sent once the device
answered it, accepted or not; one the transport could not deliver is due again
at once.

The one exit to AC input found after a start (``CHARGE_EXIT_TO_OUTPUT``) is not
on record: the controller settles it, on the same kind of evidence. The device
took it when a report after it shows the device out of the charge, or charging
at a setpoint it did not show when the exit went out -- someone put it back
after the exit was carried out. Answered, accepted or refused, it is one
attempt and waits for the same resend window; one the transport could not
deliver is no attempt and is due again at once. After ``FOUND_EXIT_ATTEMPTS``
answered attempts it is due no more, whenever the caller reads the clock, and
once the last window has passed the controller stops asking, so a firmware
that puts its protection charge straight back is not fought for good.

``CHARGE_EXIT_FINAL`` is the shutdown release: the last command this process
writes, so neither the resend window nor a command still in flight holds it
back -- nothing is left to ask again.
"""

from ems.power_direction import in_ac_input_direction, out_of_charge

EXIT_RESEND_SECONDS = 30.0
FOUND_EXIT_ATTEMPTS = 3

CHARGE_EXIT_TO_OUTPUT = "to_output"
CHARGE_EXIT_IN_INPUT = "in_input"
CHARGE_EXIT_FINAL = "final"
CHARGE_EXIT_PENDING = "charge_exit_pending"


class ChargeRecord:
    """Whether the EMS's own charge is on one device, by what the device reports."""

    def __init__(self, resend_after_s=EXIT_RESEND_SECONDS):
        self.resend_after_s = resend_after_s
        self.open = False
        self.exit_sent_at = None
        self.found_exit_sent_at = None
        self.found_exit_attempts = 0
        self.found_charge_left = False
        self._found_setpoint = None
        self._last_setpoint = None
        self._commanded_w = None
        self._reported_w = None

    def charge_sent(self, setpoint_w):
        """A charge command went out; the charge is the EMS's from here."""

        self.open = True
        self.exit_sent_at = None
        self._commanded_w = setpoint_w

    def exit_sent(self, now):
        if self.open:
            self.exit_sent_at = now

    def found_exit_sent(self, now):
        """The device answered the one exit to AC input found after a start.

        Accepted or refused, that is one attempt, and it opens a resend window.
        An exit that goes out inside the window -- a changed MQTT target
        replacing one still in flight -- is the same attempt, and none counts
        past ``FOUND_EXIT_ATTEMPTS``.
        """

        if not self.found_exit_due(now):
            return
        self.found_exit_attempts += 1
        self.found_exit_sent_at = now
        self._found_setpoint = self._last_setpoint

    def found_exit_due(self, now):
        """Whether that exit may go out now: never answered, or answered a window ago.

        Answered ``FOUND_EXIT_ATTEMPTS`` times, never again. The bound is the
        record's own: the controller reads the clock earlier in a cycle than
        the transport does, and a window that ended between the two readings
        sent a fourth exit, then one every window.
        """

        return (
            self.found_exit_attempts < FOUND_EXIT_ATTEMPTS
            and self._found_exit_window_over(now)
        )

    def found_exit_exhausted(self, now):
        """Whether that exit is spent: answered ``FOUND_EXIT_ATTEMPTS`` times, a window ago."""

        return (
            self.found_exit_attempts >= FOUND_EXIT_ATTEMPTS
            and self._found_exit_window_over(now)
        )

    def _found_exit_window_over(self, now):
        sent_at = self.found_exit_sent_at
        return sent_at is None or now - sent_at >= self.resend_after_s

    def exit_due(self, now):
        """Whether an exit should go out now: none yet, or the last one is a window old."""

        if not self.open:
            return False
        return self.exit_sent_at is None or now - self.exit_sent_at >= self.resend_after_s

    def observe(self, state, *, setpoint_reported):
        """Read one report of the device.

        While the charge runs, the setpoint the device shows counts as the
        EMS's own beside the one it last wrote, so a device that clamps the
        written value, or has yet to take the latest one, is not taken for
        someone else's charge. After the one exit to AC input found after a
        start, the report also says whether the device took it.
        """

        setpoint = state.input_limit_w if setpoint_reported else None
        self._observe_found_exit(state, setpoint)
        self._last_setpoint = setpoint
        if not self.open:
            return
        if self.exit_sent_at is None:
            if setpoint and in_ac_input_direction(state):
                self._reported_w = setpoint
            return
        if out_of_charge(state) or (
            setpoint is not None
            and setpoint not in (self._commanded_w, self._reported_w)
        ):
            self.release()

    def _observe_found_exit(self, state, setpoint):
        if not self.found_exit_attempts or self.found_charge_left:
            return
        moved = (
            setpoint is not None
            and self._found_setpoint is not None
            and setpoint != self._found_setpoint
        )
        if out_of_charge(state) or moved:
            self.found_charge_left = True

    def release(self):
        self.open = False
        self.exit_sent_at = None
        self._commanded_w = None
        self._reported_w = None


__all__ = [
    "CHARGE_EXIT_FINAL",
    "CHARGE_EXIT_IN_INPUT",
    "CHARGE_EXIT_PENDING",
    "CHARGE_EXIT_TO_OUTPUT",
    "EXIT_RESEND_SECONDS",
    "FOUND_EXIT_ATTEMPTS",
    "ChargeRecord",
]
