# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a grid reading cannot show yet of the power the EMS wrote.

The control loop adds each grid reading to the total it already commands, on
the assumption that the reading shows that total. A reading that reached the
EMS before targets went out to the devices -- a pushed meter's frame between a
cycle's read and its write -- still shows the total from before them, and added
to the new one it counts their correction twice. The reading is corrected by
exactly those targets: what went out after it arrived, or waits in a queue to
go out, it cannot show. A target is dated when it went out -- an HTTP write
when the request returned -- and a device the EMS stops commanding is
forgotten, so nothing is taken off that the EMS did not send since.

A device that follows its targets more slowly than the next reading comes
needs more: within ``write_dead_time_seconds`` after the power moved a new
reading is held, and the cycle carries on towards the goal of the last reading
it used, the way a cycle that repeats a reading does (:mod:`ems.grid_reading`).
Times are on the :func:`time.monotonic` clock, the clock of
:attr:`ems.grid_reading.GridReading.received_at`. Import-side-effect free.
"""

import threading

MAX_DEAD_TIME_S = 10.0
MAX_HELD_IN_A_ROW = 3
PENDING_GIVE_UP_S = 60.0
HISTORY_S = 3 * MAX_DEAD_TIME_S


def _target_at(changes, moment, before_first):
    target = before_first
    for changed_at, changed_to in changes:
        if changed_at > moment:
            break
        target = changed_to
    return target


class WriteDeadTime:
    """The targets the devices got, when, and what a reading cannot show of them.

    The power moved when the total of the targets the devices last got changed
    by more than a threshold from one cycle to the next; the move is dated by
    the last target that changed what its device had. A target moved from one
    device to another, or one sent again unchanged, moves nothing. A target a
    transport queued behind the command before it is waited for, and after
    :data:`PENDING_GIVE_UP_S` without word of it no longer.

    At most :data:`MAX_HELD_IN_A_ROW` new readings in a row are held, so a total
    that keeps moving without a reading behind it cannot stop the regulation.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._published = {}
        self._pending = {}
        self._changes = {}
        self._changed_at = None
        self._total = 0
        self._threshold_w = 0
        self.moved_at = None
        self.held_in_a_row = 0

    def note_published(self, device_name, target_w, now):
        """A target went out to a device: an HTTP write returned, an MQTT target was published."""

        if not isinstance(target_w, (int, float)):
            return
        with self._lock:
            self._pending.pop(device_name, None)
            if self._published.get(device_name) != target_w:
                self._published[device_name] = target_w
                self._changed_at = now
                changes = self._changes.setdefault(device_name, [])
                changes.append((now, target_w))
                while len(changes) > 1 and changes[1][0] < now - HISTORY_S:
                    del changes[0]

    def note_queued(self, device_name, target_w, now):
        """A target waits behind the command before it; the device does not have it yet.

        The same target queued again keeps the time it first waited from.
        """

        if not isinstance(target_w, (int, float)):
            return
        with self._lock:
            pending = self._pending.get(device_name)
            if self._published.get(device_name) == target_w:
                self._pending.pop(device_name, None)
            elif pending is None or pending[0] != target_w:
                self._pending[device_name] = (target_w, now)

    def note_dropped(self, device_name, target_w):
        """A target will not go out: superseded, cancelled, refused or failed."""

        with self._lock:
            pending = self._pending.get(device_name)
            if pending is not None and pending[0] == target_w:
                del self._pending[device_name]

    def forget(self, device_name):
        """Drop what a device was sent; its next target counts as its first.

        The total the cycle is compared with drops it as well, so forgetting
        moves nothing.
        """

        with self._lock:
            target = self._published.pop(device_name, None)
            if target is not None:
                self._total -= target
            self._changes.pop(device_name, None)
            self._pending.pop(device_name, None)

    def close_cycle(self, threshold_w):
        """Compare what the devices got with the cycle before; call before the read."""

        with self._lock:
            self._threshold_w = threshold_w
            total = sum(self._published.values())
            if abs(total - self._total) > threshold_w:
                self.moved_at = self._changed_at
            self._total = total

    def unseen_w(self, received_at, now):
        """How far the targets that went out or wait after ``received_at`` move the total.

        A reading cannot show them. What a device had before its first known
        target is not known, so that change counts as none. ``0`` for a reading
        without an arrival time.
        """

        if received_at is None:
            return 0
        with self._lock:
            unseen = 0
            for name, changes in self._changes.items():
                pending = self._pending.get(name)
                current = (
                    pending[0]
                    if pending is not None and now - pending[1] < PENDING_GIVE_UP_S
                    else self._published[name]
                )
                unseen += current - _target_at(changes, received_at, changes[0][1])
            return unseen

    def deepest_charge_w(self, device_names, received_at, dead_time_s, now):
        """The deepest charge ``device_names`` were sent in the dead time before a reading arrived.

        A reading shows no target that went out after it arrived, and it may
        still show any the devices had in the dead time before: the one they
        had when it began, and each that went out until the reading arrived. A
        running charge judged on the reading against the deepest of them is
        never left because the charge ramp was still on its way, and a deficit
        beyond it still shows. Only charges count, as negative watts: the
        standby floor a device feeds out beside a charge is house load to the
        regulator. A device that had no target yet counts as zero. ``None`` when
        none of the devices was ever sent one.
        """

        end = now if received_at is None else received_at
        start = end - dead_time_s
        with self._lock:
            histories = [self._changes[name] for name in device_names if name in self._changes]
            if not histories:
                return None
            moments = [start] + sorted(
                {changed_at for changes in histories for changed_at, _ in changes if start < changed_at < end}
            )
            return min(
                sum(min(_target_at(changes, moment, 0), 0) for changes in histories)
                for moment in moments
            )

    def holds(self, reading, dead_time_s):
        """Whether a new ``reading`` came before the last move settled.

        A target that went out since the cycle began counts as a move already.
        Counts the readings held in a row; a reading it lets through starts the
        count again.
        """

        with self._lock:
            moved_at = self.moved_at
            if abs(sum(self._published.values()) - self._total) > self._threshold_w:
                moved_at = self._changed_at
        received_at = reading.received_at
        early = (
            dead_time_s > 0
            and moved_at is not None
            and received_at is not None
            and received_at < moved_at + dead_time_s
        )
        if early and self.held_in_a_row < MAX_HELD_IN_A_ROW:
            self.held_in_a_row += 1
            return True
        self.held_in_a_row = 0
        return False

    def since_move_s(self, reading):
        """How long after the last move ``reading`` arrived, if both are known."""

        with self._lock:
            moved_at = self.moved_at
        if moved_at is None or reading.received_at is None:
            return None
        return reading.received_at - moved_at
