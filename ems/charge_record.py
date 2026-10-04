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
is asked again without being written to every cycle.
"""

from ems.power_direction import in_ac_input_direction, out_of_charge

EXIT_RESEND_SECONDS = 30.0

CHARGE_EXIT_TO_OUTPUT = "to_output"
CHARGE_EXIT_IN_INPUT = "in_input"
CHARGE_EXIT_PENDING = "charge_exit_pending"


class ChargeRecord:
    """Whether the EMS's own charge is on one device, by what the device reports."""

    def __init__(self, resend_after_s=EXIT_RESEND_SECONDS):
        self.resend_after_s = resend_after_s
        self.open = False
        self.exit_sent_at = None
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
        someone else's charge.
        """

        if not self.open:
            return
        setpoint = state.input_limit_w if setpoint_reported else None
        if self.exit_sent_at is None:
            if setpoint and in_ac_input_direction(state):
                self._reported_w = setpoint
            return
        if out_of_charge(state) or (
            setpoint is not None
            and setpoint not in (self._commanded_w, self._reported_w)
        ):
            self.release()

    def release(self):
        self.open = False
        self.exit_sent_at = None
        self._commanded_w = None
        self._reported_w = None


__all__ = [
    "CHARGE_EXIT_IN_INPUT",
    "CHARGE_EXIT_PENDING",
    "CHARGE_EXIT_TO_OUTPUT",
    "EXIT_RESEND_SECONDS",
    "ChargeRecord",
]
