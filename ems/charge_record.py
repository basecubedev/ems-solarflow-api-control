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
charge -- out of the AC-input direction (without a known ``acMode``, only
with no grid draw), its exit's ``inputLimit = 0``, or a charge at a setpoint
the EMS never wrote, which is someone else's charge. Until
then the exit is due again once per resend window, so a device that ignores it
is not sent the same exit every cycle. The exit counts as sent once the device
answered it, accepted or not; one the transport could not deliver is due again
at once.

Grid draw alone is weaker evidence: a device that reports neither a known
``acMode`` nor a charging ``acStatus`` may draw a few watts idle and never
show 0. An exit after which a report shows the charge only that way is
bounded as the exit found after a start is: after ``FOUND_EXIT_ATTEMPTS`` of
them no exit is due, whenever the caller reads the clock, and once the last
one's window has passed with the latest report still showing grid draw alone,
the charge is the device's (:meth:`ChargeRecord.exit_exhausted`) -- the
caller releases the record and says so. Within that last window the charge is
still on record, so the shutdown release, which no window holds back, still
goes to it.

Between exits a transport holds every other output target, except a safety
reduction below the target the last exit carried (``safety_reduction`` in
:mod:`ems.power_direction`): a stop to 0 W or a large cut goes out at once.
It is no exit of its own (:meth:`ChargeRecord.reduction_sent`): the window and
the count stay the last exit's, so the bound on a charge only grid draw shows
ends when it would have.

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

from ems.power_direction import in_ac_input_direction, out_of_charge, safety_reduction

EXIT_RESEND_SECONDS = 30.0
FOUND_EXIT_ATTEMPTS = 3

CHARGE_EXIT_TO_OUTPUT = "to_output"
CHARGE_EXIT_IN_INPUT = "in_input"
CHARGE_EXIT_FINAL = "final"
CHARGE_EXIT_PENDING = "charge_exit_pending"


class ChargeRecord:
    """Whether the EMS's own charge is on one device, by what the device reports.

    ``bounded_by_draw`` counts the exits after which only grid draw showed the
    charge. The full-charge assist's record goes without: the controller caps
    its exits itself, three per switch-off (OFF-1b).
    """

    def __init__(self, resend_after_s=EXIT_RESEND_SECONDS, *, bounded_by_draw=True):
        self.resend_after_s = resend_after_s
        self.bounded_by_draw = bounded_by_draw
        self.open = False
        self.exit_sent_at = None
        self.exit_target_w = None
        self.drawn_exits = 0
        self._exit_drawn = False
        self._drawn = False
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
        self.exit_target_w = None
        self._commanded_w = setpoint_w
        self._forget_drawn_exits()

    def exit_sent(self, now, target_w=None):
        """An exit went out with ``target_w`` as its output target; its window starts at ``now``."""

        if self.open:
            self.exit_sent_at = now
            self.exit_target_w = target_w
            self._exit_drawn = False

    def reduction_sent(self, target_w):
        """A safety reduction went out while the exit was held: the device's target, not an exit.

        It keeps the last exit's resend window and its count, so a stream of
        reductions cannot keep the charge on record.
        """

        if self.open:
            self.exit_target_w = target_w

    def reduces_exit(self, target_w, *, margin_w, tolerance_w=0):
        """Whether ``target_w`` is a safety reduction below the target the last exit left on the device."""

        return (
            self.open
            and self.exit_target_w is not None
            and safety_reduction(
                self.exit_target_w, target_w, margin_w=margin_w, tolerance_w=tolerance_w
            )
        )

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
        """Whether an exit should go out now: none yet, or the last one is a window old.

        Never once the exits are spent on a charge only grid draw shows.
        """

        if not self.open or self.drawn_exits_spent():
            return False
        return self.exit_sent_at is None or now - self.exit_sent_at >= self.resend_after_s

    def exit_exhausted(self, now):
        """Whether the charge is the device's: shown by grid draw alone after the last exit, a window ago.

        ``FOUND_EXIT_ATTEMPTS`` exits went out, a report after each showed the
        charge only by its grid draw over an unknown direction, and so does
        the latest one.
        """

        return (
            self.open
            and self.drawn_exits_spent()
            and now - self.exit_sent_at >= self.resend_after_s
        )

    def drawn_exits_spent(self):
        """Whether ``FOUND_EXIT_ATTEMPTS`` exits went out to a charge only grid draw shows."""

        return self._drawn and self.drawn_exits >= FOUND_EXIT_ATTEMPTS

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
            return
        self._observe_drawn_exit(state)

    def _observe_drawn_exit(self, state):
        """Count the exit after which a report shows the charge by its grid draw alone.

        ``out_of_charge`` already ruled the report not out, so out of the
        AC-input direction that is grid draw over an unknown direction.
        """

        self._drawn = self.bounded_by_draw and not in_ac_input_direction(state)
        if self._drawn and not self._exit_drawn:
            self._exit_drawn = True
            self.drawn_exits += 1

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
        self.exit_target_w = None
        self._commanded_w = None
        self._reported_w = None
        self._forget_drawn_exits()

    def _forget_drawn_exits(self):
        self.drawn_exits = 0
        self._exit_drawn = False
        self._drawn = False


__all__ = [
    "CHARGE_EXIT_FINAL",
    "CHARGE_EXIT_IN_INPUT",
    "CHARGE_EXIT_PENDING",
    "CHARGE_EXIT_TO_OUTPUT",
    "EXIT_RESEND_SECONDS",
    "FOUND_EXIT_ATTEMPTS",
    "ChargeRecord",
]
