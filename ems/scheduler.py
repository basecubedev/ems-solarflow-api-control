# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scheduler: each due switch point, once, as emsctl would apply it.

Once per EMS cycle ``Scheduler.tick`` works out, per runtime key, the latest
switch point that has come and was not applied yet, and runs it through the
one command path with ``source="schedule"``. It writes no hardware and holds
no authority of its own: a value the validators refuse today is not written,
and what reaches an inverter is decided downstream exactly as for a person's
command.

The latest switch point wins, scheduled or manual: a person's change holds
until the key's next switch point. After the EMS was down only the latest
missed switch point per key fires. A switch point earlier than its rule's last
save, or than the last resume, never fires.
"""

import logging
import math
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ems.logging_utils import log_event
from ems.runtime_commands import RuntimeWriteError, run_commands, validate_command
from ems.file_io import file_signature, lock_timed_out
from ems.schedule_actions import ScheduleActionError, action_commands, action_slot, schedule_action
from ems.schedule_model import ScheduleRule, ScheduleRuleError, parse_editable
from ems.schedule_recurrence import localize, occurrences, wall_windows
from ems.schedule_store import ScheduleStoreError
from ems.schedule_text import describe_value

CLOCK_FLOOR = datetime(2026, 10, 1, tzinfo=timezone.utc)
CATCH_UP_AFTER = timedelta(minutes=2)
LOOKAHEAD = timedelta(days=400)
FAR_AHEAD = timedelta(days=366 * 500)
CLASH_HORIZON = timedelta(days=366 * 28)
STATUS_AHEAD = timedelta(days=7)
STATUS_POINTS = 10
RELOAD_EVERY = timedelta(seconds=60)
FUTURE_TOLERANCE = timedelta(days=1)
NEVER = datetime(1970, 1, 1, tzinfo=timezone.utc)
START, END = "start", "end"
BUSY = "busy"
FAILED = "failed"
UNREAD = object()
ZONE_UNSUPPORTED = (
    "TZ is a POSIX rule string the scheduler cannot follow; set it to a zone name such as Europe/Berlin"
)


@dataclass(frozen=True)
class SwitchPoint:
    at: datetime
    slot: str
    rule: ScheduleRule
    value: object
    kind: str
    rank: int
    ends_at: datetime | None = None

    def order(self):
        """Later wins; at one instant a start beats an end, then the higher rule."""

        return (self.at, self.kind == START, -self.rank)


def slot_of(rule):
    """The runtime keys a rule sets, as one name: what "latest wins" is per."""

    return action_slot(rule.action, rule.device)


def valid_rules(book):
    return [
        (rank, rule)
        for rank, rule in enumerate(book.rules)
        if isinstance(rule, ScheduleRule) and rule.enabled
    ]


def switch_points(rule, zone, *, after, until, rank=0):
    """A rule's switch points with ``after < at <= until``, in order."""

    slot = slot_of(rule)
    for occurrence in occurrences(rule.when, zone, since=after, until=until):
        if after < occurrence.start <= until:
            yield SwitchPoint(occurrence.start, slot, rule, rule.value, START, rank, occurrence.end)
        if occurrence.end is not None and after < occurrence.end <= until:
            yield SwitchPoint(occurrence.end, slot, rule, rule.end_value, END, rank)


def due_switch_points(book, zone, now):
    """Per slot, the latest switch point that is due and was not applied.

    A save, resume or applied time up to a day later than ``now`` means the
    clock stepped back: it still bounds, so nothing fires twice and the
    setting waits until the clock has caught up. One further ahead was
    stamped by a clock that ran far ahead; it bounds nothing, or the setting
    would wait months for it.
    """

    if not book.enabled:
        return {}
    due = {}
    for rank, rule in valid_rules(book):
        slot = slot_of(rule)
        after = _bound(rule, book.applied.get(slot, {}).get("at", NEVER), book, now)
        for point in switch_points(rule, zone, after=after, until=now, rank=rank):
            if slot not in due or point.order() > due[slot].order():
                due[slot] = point
    return due


def _bound(rule, applied_at, book, now):
    """The moment before which none of ``rule``'s switch points fire: its last
    save, the setting's last applied switch point or the last resume, of those
    a clock can have stamped; one more than a day ahead of ``now`` cannot."""

    bounds = [bound for bound in (rule.updated_at, applied_at, book.resumed_at or NEVER)
              if bound <= now + FUTURE_TOLERANCE]
    return max(bounds, default=NEVER)


def upcoming_switch_points(book, zone, now, *, until=None):
    """Every enabled rule's switch points after ``now``, in the order they
    apply: by time, and at one instant the point that wins last. None while
    the schedule is paused."""

    if not book.enabled:
        return []
    until = until or now + LOOKAHEAD
    points = []
    for rank, rule in valid_rules(book):
        points.extend(switch_points(rule, zone, after=now, until=until, rank=rank))
    return sorted(points, key=SwitchPoint.order)


def window_still_open(record, book, zone, now):
    """Whether the window whose start ``record`` applied still ends by itself.

    It does while an enabled rule with that id still sets the same thing on
    the same device and has an occurrence that covers ``now`` and ends later
    than the rule's last save, an end the scheduler will fire: an edit that
    keeps now inside the window -- another start, a later end -- keeps it
    running. Deleted, switched off, paused, skipped, moved to another device
    or away from now, or cut to an end that has already passed, it does not.
    A clock stepped back before the window's start is judged at that start.
    """

    rule = _same_window_rule(record, book) if book.enabled else None
    if rule is None:
        return False
    bound = _bound(rule, record["at"], book, now)
    moment = record["at"] if now < record["at"] <= now + FUTURE_TOLERANCE else now
    return any(
        occurrence.start <= moment < occurrence.end and occurrence.end > bound
        for occurrence in occurrences(rule.when, zone, since=moment, until=moment)
    )


def _same_window_rule(record, book):
    """The enabled rule that still sets what the recorded window set, or None."""

    window = record["window"]
    for _, rule in valid_rules(book):
        if (rule.id == record["rule"] and rule.is_window and rule.action.id == window["action"]
                and rule.device == window.get("device")):
            return rule
    return None


def next_switch_point(rule, zone, now):
    """The rule's first switch point after ``now``, or None when it has none.

    Searched lazily as far as a repeat can reach -- every 366th year, or a
    29 February only every 400 years -- not just a window's clash horizon.
    """

    return next(iter(switch_points(rule, zone, after=now, until=now + FAR_AHEAD)), None)


def point_view(point, zone=None):
    """A switch point as the control snapshot, emsctl and the dashboard show it."""

    at = point.at.astimezone(zone) if zone is not None else point.at
    return {
        "at": at.isoformat(),
        "slot": point.slot,
        "rule": point.rule.id,
        "name": point.rule.name,
        "kind": point.kind,
        "value": point.value,
        "text": describe_value(point.rule.action, point.value),
    }


def next_switch_at(book, zone, now):
    """When the scheduler next has something to fire, or None."""

    if not book.enabled:
        return None
    points = (next_switch_point(rule, zone, now) for _, rule in valid_rules(book))
    return min((point.at for point in points if point is not None), default=None)


def window_clash(candidate, book, zone, now):
    """The first window of another rule on the same keys that overlaps ``candidate``.

    Returns ``(rule, start)`` of the clash, or None. Windows on one key may not
    overlap: the first one's end would cut into the second. They are compared
    in wall-clock time. Two daily or weekly patterns repeat together after
    the least common multiple of their periods, so that span after both have
    begun and their last skipped date has passed is enough;
    any other pair is compared over CLASH_HORIZON, 28 years, the cycle in
    which every date falls on every weekday -- a yearly window on 1 February
    meets a Sunday one in 2032 -- walking the sparser rule and looking up
    the other around each of its windows.
    """

    if not candidate.is_window:
        return None
    slot = slot_of(candidate)
    since = now.astimezone(zone).replace(tzinfo=None)
    for _, other in valid_rules(book):
        if other.id == candidate.id or not other.is_window or slot_of(other) != slot:
            continue
        start = _first_overlap(candidate.when, other.when, since)
        if start is not None:
            return other, localize(start, zone)
    return None


def _period_days(when):
    """The days after which a window's wall-clock pattern repeats, or None
    when it follows the calendar's months or years."""

    repeat = when.repeat
    if repeat is None or repeat.by_month or repeat.by_month_day or repeat.by_set_pos is not None:
        return None
    if repeat.freq == "daily":
        return math.lcm(repeat.interval, 7) if repeat.by_weekday else repeat.interval
    if repeat.freq == "weekly":
        return 7 * repeat.interval
    return None


def _first_overlap(first, second, since):
    """The wall-clock start of the first overlap of two rules' windows from
    ``since``, or None."""

    periods = [_period_days(when) for when in (first, second)]
    if None not in periods:
        skipped = [datetime.combine(day + timedelta(days=1), datetime.min.time())
                   for when in (first, second) for day in when.exceptions]
        begins = max([since, first.start, second.start, *skipped])
        until = begins + timedelta(days=math.lcm(*periods)) + max(first.length, second.length)
        return _overlap_in_order(wall_windows(first, since), wall_windows(second, since), until)
    until = since + CLASH_HORIZON
    sparse, dense = sorted((first, second), key=lambda when: _windows_in_a_year(when, since))
    for start, end in wall_windows(sparse, since):
        if start > until:
            return None
        for other_start, other_end in wall_windows(dense, start):
            if other_end <= start:
                continue
            if other_start < end:
                return max(start, other_start)
            break
    return None


def _overlap_in_order(mine, theirs, until):
    a, b = next(mine, None), next(theirs, None)
    while a is not None and b is not None and min(a[0], b[0]) <= until:
        if a[1] <= b[0]:
            a = next(mine, None)
        elif b[1] <= a[0]:
            b = next(theirs, None)
        else:
            start = max(a[0], b[0])
            return start if start <= until else None
    return None


def _windows_in_a_year(when, since):
    count = 0
    for start, _ in wall_windows(when, since):
        if start > since + timedelta(days=366):
            break
        count += 1
    return count


def with_device_identity(payload, devices, previous=None):
    """``payload`` with its device's serial, so a rename in config is followed.

    The serial comes from the installation, never from the editor. For a
    device the installation does not name -- renamed while the EMS was
    stopped, its rename not followed yet -- an edit keeps the serial the
    ``previous`` version of the rule had on that device, so the rename can
    still be followed.
    """

    target = payload.get("target") if isinstance(payload, dict) else None
    if not isinstance(target, dict) or target.get("scope") != "device" or not isinstance(target.get("device"), str):
        return payload
    identity = devices.get(target.get("device"))
    if identity is None and isinstance(previous, ScheduleRule) and previous.device == target.get("device"):
        identity = previous.identity
    target = {key: value for key, value in target.items() if key != "identity"}
    if identity:
        target["identity"] = identity
    return {**payload, "target": target}


def check_rule(payload, book, *, rule_id=None, validation_context, devices, zone, now):
    """Refuse a rule this installation cannot carry out, before it is saved.

    The shape is the model's; this asks the installation: the device exists,
    both values pass the validators the command path applies, no window on
    the same keys overlaps it, and it still has a switch point to come. A
    rule saved switched off fires nothing and is only checked for its shape,
    so a rule whose device is gone can still be switched off. Raises
    ScheduleRuleError with the reason.
    """

    fields = parse_editable(payload)
    rule = ScheduleRule(
        id=rule_id or "r_00000000",
        revision=1,
        updated_at=now,
        updated_by="dashboard",
        **fields,
    )
    if not rule.enabled:
        return rule
    if rule.device is not None and rule.device not in devices:
        known = ", ".join(sorted(devices)) or "(none)"
        raise ScheduleRuleError(f"unknown device {rule.device}; known devices: {known}")
    for value in (rule.value, rule.end_value) if rule.is_window else (rule.value,):
        for command, argument in action_commands(rule.action, value):
            try:
                validate_command(rule.action.scope, command, argument, device=rule.device,
                                 validation_context=validation_context)
            except RuntimeWriteError as exc:
                raise ScheduleRuleError(str(exc)) from exc
    if rule.when.repeat is None:
        last = localize(rule.when.end or rule.when.start, zone)
        if last <= now:
            raise ScheduleRuleError("this one-time rule lies in the past and would never fire")
    elif next_switch_point(rule, zone, now) is None:
        raise ScheduleRuleError("this repeat has ended, by its date or count, and would never fire")
    clash = window_clash(rule, book, zone, now)
    if clash is not None:
        other, start = clash
        local = start.astimezone(zone).strftime("%a %d %b %Y %H:%M")
        raise ScheduleRuleError(f"overlaps {other.name!r} on {local}; windows on the same setting must not overlap")
    return rule


def next_occurrence_day(rule, zone, now):
    """The local date of the occurrence *Skip next* leaves out, switched on or
    not: the window running now, else the next one to start."""

    for occurrence in occurrences(rule.when, zone, since=now, until=now):
        if occurrence.end is not None and occurrence.start <= now < occurrence.end:
            return occurrence.day
    for point in switch_points(rule, zone, after=now, until=now + FAR_AHEAD):
        if point.kind == START:
            return point.at.astimezone(zone).date()
    return None


def clock_trusted(now):
    """Whether ``now`` can be this release's time at all.

    A Raspberry Pi without a clock of its own boots far in the past until NTP
    answers; switch points must not fire on that time. A clock that is merely
    behind fires what is due late, never twice: the applied record bounds it.
    """

    return now >= CLOCK_FLOOR


_POSIX_FIXED = re.compile(r"(?:<[A-Za-z0-9+-]{3,}>|[A-Za-z]{3,})([+-]?)(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?")


def fixed_offset_zone(name):
    """The zone a POSIX ``TZ`` without a daylight-saving part names, or None.

    ``UTC0`` and ``CET-1`` are fixed offsets, read as the C library reads
    them: west of Greenwich is positive. The zone keeps ``name`` as its name,
    so emsctl, given the name the EMS reports, reads the same offset.
    """

    match = _POSIX_FIXED.fullmatch(str(name or ""))
    if match is None:
        return None
    sign, hours, minutes, seconds = match.groups()
    west = timedelta(hours=int(hours), minutes=int(minutes or 0), seconds=int(seconds or 0))
    if west >= timedelta(hours=24) or int(minutes or 0) > 59 or int(seconds or 0) > 59:
        return None
    return timezone(west if sign == "-" else -west, name)


def local_zone(environ=None, localtime="/etc/localtime"):
    """The zone the EMS's hour-based windows open in: ``TZ``, else ``/etc/localtime``.

    A ``TZ`` naming no zone file is read as UTC, as the C library reads it
    for winter ``adjust_hour`` and full-charge ``force_time``; one such as
    ``UTC0`` or ``CET-1`` as the fixed offset it gives. A POSIX rule string
    with a daylight-saving part, such as ``CET-1CEST,M3.5.0,M10.5.0/3``, is
    one the C library follows and zoneinfo cannot: None, and the scheduler
    fires nothing rather than an hour off after the next clock change.
    """

    environ = os.environ if environ is None else environ
    name = str(environ.get("TZ") or "").strip()
    name = name[1:] if name.startswith(":") else name
    try:
        if name.startswith("/"):
            with open(name, "rb") as handle:
                return ZoneInfo.from_file(handle, key=_zone_key(os.path.realpath(name), name))
        if name:
            return ZoneInfo(name)
        if os.path.exists(localtime):
            with open(localtime, "rb") as handle:
                return ZoneInfo.from_file(handle, key=_zone_key(os.path.realpath(localtime), "localtime"))
        return timezone.utc
    except (ZoneInfoNotFoundError, ValueError, OSError):
        fixed = fixed_offset_zone(name)
        if fixed is not None:
            return fixed
        if any(character.isdigit() for character in name):
            return None
        return timezone.utc


def _zone_key(path, fallback):
    """A zone file's name in the zone database, which emsctl, reading the
    zone the EMS reports, can load on another host too."""

    return path.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in path else fallback


class Scheduler:
    """Fires due switch points; one instance per EMS process."""

    def __init__(self, store, runtime_state, *, zone, validation_context, devices=None):
        self._store = store
        self._devices = devices
        self._runtime_state = runtime_state
        self._zone = zone
        self._validation_context = validation_context
        self._book = None
        self._signature = None
        self._raw = UNREAD
        self._loaded_at = None
        self._next_at = None
        self._last_now = None
        self._stale = True
        self._upcoming = []
        self._upcoming_at = None
        self._reported = set()
        self._unrecorded = {}
        self._open_windows = {}
        self._store_busy = False
        self._unreadable = False
        self._refused = {}
        self._lock = threading.Lock()

    def tick(self, now, *, devices=None):
        """Apply what is due at ``now``. ``devices`` maps config names to the
        serials rules follow them by; the EMS gives them at construction. A
        rule on a device not among them is orphaned; with no map at all,
        nothing is."""

        with self._lock:
            self._tick(now, devices)

    def _tick(self, now, devices):
        if self._zone is None:
            self._report_once("zone", logging.WARNING, "schedule_zone_unsupported", tz=os.environ.get("TZ", ""))
            return
        devices = self._devices if devices is None else devices
        self._store_busy = False
        changed = self._refresh(now, devices)
        book = self._book
        trusted = clock_trusted(now)
        if book is None:
            if trusted:
                self._end_open_windows(now)
            return
        if not trusted:
            self._report_once("clock", logging.WARNING, "schedule_clock_untrusted", now=now.isoformat())
            self._stale = True
            return
        self._reported.discard("clock")
        went_back = self._last_now is not None and now < self._last_now
        self._last_now = now
        reached = self._next_at is not None and now >= self._next_at
        if not (changed or went_back or reached or self._stale):
            return
        self._stale = False
        self._record_unrecorded()
        busy = False
        for point in sorted(due_switch_points(self._book, self._zone, now).values(), key=SwitchPoint.order):
            if point.rule.device is not None and devices is not None and point.rule.device not in devices:
                self._report_once(
                    ("orphaned", point.rule.id), logging.WARNING, "schedule_rule_orphaned",
                    rule=point.rule.id, device=point.rule.device,
                )
                continue
            if self._apply(point, now) == BUSY:
                busy = True
                break
        if not busy:
            self._close_lost_windows(now)
        self._next_at = next_switch_at(self._book, self._zone, now)
        self._refresh_upcoming(now)

    def status(self, now):
        """What the scheduler holds, for the control snapshot and the dashboard.

        The dashboard asks from its own threads while the loop ticks.
        """

        with self._lock:
            return self._status(now)

    def _status(self, now):
        if self._zone is None:
            return {"loaded": False, "zone": "unsupported"}
        book = self._book
        if book is None:
            return {"loaded": False}
        if self._upcoming_at is None or not timedelta(0) <= now - self._upcoming_at < RELOAD_EVERY:
            self._refresh_upcoming(now)
        return {
            "loaded": True,
            "enabled": book.enabled,
            "clock_trusted": clock_trusted(now),
            "zone": str(getattr(self._zone, "key", None) or self._zone),
            "next": list(self._upcoming),
            "applied": {slot: _record_view(record) for slot, record in book.applied.items()},
            "refused": {rule_id: dict(refusal) for rule_id, refusal in self._refused.items()},
        }

    def _refresh_upcoming(self, now):
        upcoming = upcoming_switch_points(self._book, self._zone, now, until=now + STATUS_AHEAD)
        self._upcoming = [point_view(point, self._zone) for point in upcoming[:STATUS_POINTS]]
        self._upcoming_at = now

    def _refresh(self, now, devices):
        """Re-read the store when it changed, and once a minute regardless:
        the file's mtime comes from a coarse clock and can hide an edit. The
        rules are parsed again only when the bytes differ, from the same read."""

        signature = _file_signature(self._store.path)
        due_again = self._loaded_at is None or now - self._loaded_at >= RELOAD_EVERY or now < self._loaded_at
        known = self._book is not None or self._unreadable
        if signature == self._signature and known and not due_again:
            return False
        self._loaded_at = now
        self._signature = signature
        try:
            raw = self._store.read_bytes()
            if self._book is not None and raw == self._raw:
                return False
            book = self._store.parse(raw)
        except (ScheduleStoreError, OSError) as exc:
            self._report_once("store", logging.WARNING, "schedule_store_load_error", error=exc)
            self._unreadable = True
            if self._book is not None:
                self._open_windows = {
                    slot: record for slot, record in self._book.applied.items() if "window" in record
                }
            self._book = None
            self._raw = UNREAD
            return True
        self._reported.discard("store")
        self._unreadable = False
        book, raw = self._follow_renames(book, raw, devices or {})
        self._carry_open_windows(book)
        self._open_windows = {}
        book = self._with_unrecorded(book)
        changed = book != self._book
        self._book = book
        self._raw = raw
        return changed

    def _follow_renames(self, book, raw, devices):
        """Move rules whose device was renamed in config to its new name.

        With the store's lock busy the rules are taken as they are, and the
        rename is tried again at the next reload: the rules on that device
        wait for it rather than every rule waiting for the lock. A serial two
        configured devices share names neither, so no rule moves between them.
        """

        names = {}
        for name, identity in devices.items():
            if identity:
                names.setdefault(identity, []).append(name)
        renamed = {
            rule.identity: names[rule.identity][0]
            for rule in book.rules
            if isinstance(rule, ScheduleRule) and len(names.get(rule.identity, ())) == 1
            and rule.device != names[rule.identity][0]
        }
        if not renamed:
            return book, raw
        try:
            if self._store.rename_devices(renamed):
                self._signature = _file_signature(self._store.path)
                book, raw = self._store.snapshot()
        except (ScheduleStoreError, OSError) as exc:
            self._report_once("rename", logging.WARNING, "schedule_rename_deferred", error=exc)
            if lock_timed_out(exc):
                self._store_busy = True
            return book, UNREAD
        self._reported.discard("rename")
        return book, raw

    def _carry_open_windows(self, book):
        """A window this scheduler started stays its own to end when the file
        is replaced under it -- a backup restored, the file removed or mended
        by hand -- and the new file has no record of it. Kept as applied but
        unrecorded, it ends at its time, or at once when its rule went with
        the old file."""

        previous = self._book.applied if self._book is not None else self._open_windows
        for slot, record in previous.items():
            if "window" not in record or slot in self._unrecorded:
                continue
            on_disk = book.applied.get(slot)
            if on_disk is not None and on_disk["at"] >= record["at"]:
                continue
            if any(other["rule"] == record["rule"] and other["at"] == record["at"]
                   for other in book.applied.values()):
                continue
            self._unrecorded[slot] = record

    def _with_unrecorded(self, book):
        """Keep what was applied but could not be recorded, until the file
        holds it or something later, so it does not fire again every cycle."""

        applied = dict(book.applied)
        for slot, record in list(self._unrecorded.items()):
            on_disk = applied.get(slot)
            if on_disk is not None and on_disk["at"] >= record["at"]:
                del self._unrecorded[slot]
                continue
            applied[slot] = record
        return replace(book, applied=applied) if applied != book.applied else book

    def _record_unrecorded(self):
        for slot, record in list(self._unrecorded.items()):
            try:
                recorded = self._record(slot, record)
            except (ScheduleStoreError, OSError):
                recorded = False
            if not recorded:
                self._stale = True
                return
            del self._unrecorded[slot]
            self._signature = None
            self._raw = UNREAD

    def _record(self, slot, record):
        """Store what was applied to ``slot``; False when the store refused.

        After the store's lock was busy once, this cycle stops asking: each
        attempt waits for it, and the control loop is the caller.
        """

        if self._store_busy:
            return False
        try:
            self._store.record_applied(slot, rule_id=record["rule"], at=record["at"],
                                       value=record["value"], window=record.get("window"))
        except (ScheduleStoreError, OSError) as exc:
            if lock_timed_out(exc):
                self._store_busy = True
            raise
        return True

    def _end_open_windows(self, now):
        """While ``schedules.json`` cannot be read nothing starts, but a window
        that was running still ends at its time, with the end value its start
        recorded: a charge from the grid must not run on until the file is
        mended. An end that is refused or fails is tried again each cycle."""

        for slot, record in sorted(self._open_windows.items(), key=lambda item: item[1]["window"]["ends_at"]):
            window = record["window"]
            if window["ends_at"] > now:
                continue
            done = self._write(
                schedule_action(window["action"]), window.get("device"), window.get("end_value"), now,
                rule_id=record["rule"], slot=slot, at=window["ends_at"], kind=END,
            )
            if done in (BUSY, FAILED):
                return
            if done is not True:
                continue
            del self._open_windows[slot]
            log_event(logging.INFO, "schedule_window_closed", rule=record["rule"], slot=slot,
                      value=window.get("end_value"))

    def _close_lost_windows(self, now):
        """End a window whose rule went away -- deleted, switched off, paused,
        moved or skipped -- at once, with the end value its start recorded:
        its own end will not come, and a start value such as a charge from the
        grid must not stay for good. It runs after the cycle's due switch
        points, so a later switch point on the same setting replaces the
        window instead."""

        for slot, record in list(self._book.applied.items()):
            window = record.get("window")
            if window is None or window_still_open(record, self._book, self._zone, now):
                continue
            action = schedule_action(window["action"])
            end_value = window.get("end_value")
            current = _same_window_rule(record, self._book)
            if current is not None:
                end_value = current.end_value
            done = self._write(
                action, window.get("device"), end_value, now,
                rule_id=record["rule"], slot=slot, at=now, kind=END,
                report_as=(record["rule"], record["at"], "close"),
            )
            if done is True:
                log_event(logging.INFO, "schedule_window_closed", rule=record["rule"], slot=slot,
                          value=end_value)
            if done == BUSY:
                return

    def _apply(self, point, now):
        rule = point.rule
        window = None
        if point.kind == START and point.ends_at is not None:
            window = {"action": rule.action.id, "device": rule.device,
                      "end_value": rule.end_value, "ends_at": point.ends_at}
        done = self._write(rule.action, rule.device, point.value, now, rule_id=rule.id,
                           slot=point.slot, at=point.at, kind=point.kind, window=window,
                           revision=rule.revision)
        if done is True:
            log_event(
                logging.INFO,
                "schedule_catch_up" if now - point.at > CATCH_UP_AFTER else "schedule_switch_applied",
                rule=rule.id,
                slot=point.slot,
                value=point.value,
                at=point.at.isoformat(),
            )
        return done

    def _write(self, action, device, value, now, *, rule_id, slot, at, kind, window=None, report_as=None,
               revision=None):
        """Apply one action's value in one runtime write and record it.

        True when written; BUSY when another process held the runtime state,
        so the rest of this cycle's points wait for the next one; FAILED when
        the write failed and is tried again; False when it was refused.
        A failure is reported once per ``report_as``, by default the switch
        point: a lost window's close is retried at each evaluation. A refusal
        of a rule at ``revision`` is kept for the dashboard to show, until the
        rule's next write lands.
        """

        report_as = report_as or (rule_id, at, kind)
        context = self._validation_context() if callable(self._validation_context) else self._validation_context
        try:
            commands = action_commands(action, value)
        except ScheduleActionError as exc:
            self._report_once(
                ("refused", *report_as), logging.WARNING, "schedule_switch_refused",
                rule=rule_id, slot=slot, value=value, error=exc,
            )
            self._remember_refusal(rule_id, revision, exc)
            return False
        try:
            run_commands(
                self._runtime_state,
                action.scope,
                commands,
                device=device,
                validation_context=context,
                source="schedule",
            )
        except RuntimeWriteError as exc:
            if lock_timed_out(exc):
                self._report_once(
                    ("busy", *report_as), logging.WARNING, "schedule_switch_failed",
                    rule=rule_id, slot=slot, value=value, error=exc,
                )
                self._stale = True
                return BUSY
            self._report_once(
                ("refused", *report_as), logging.WARNING, "schedule_switch_refused",
                rule=rule_id, slot=slot, value=value, error=exc,
            )
            self._remember_refusal(rule_id, revision, exc)
            return False
        except OSError as exc:
            self._report_once(
                ("failed", *report_as), logging.WARNING, "schedule_switch_failed",
                rule=rule_id, slot=slot, value=value, error=exc,
            )
            self._stale = True
            return FAILED
        self._refused.pop(rule_id, None)
        record = {"rule": rule_id, "at": at, "value": value}
        if window is not None:
            record["window"] = window
        try:
            recorded = self._record(slot, record)
        except (ScheduleStoreError, OSError) as exc:
            self._report_once(
                ("unrecorded", *report_as), logging.WARNING, "schedule_switch_failed",
                rule=rule_id, slot=slot, value=value, error=exc,
            )
            recorded = False
        if recorded:
            self._unrecorded.pop(slot, None)
        else:
            self._unrecorded[slot] = record
            self._stale = True
        if self._book is not None:
            self._book = replace(self._book, applied={**self._book.applied, slot: record})
        self._signature = None
        self._raw = UNREAD
        return True

    def _remember_refusal(self, rule_id, revision, exc):
        if revision is not None:
            self._refused[rule_id] = {"revision": revision, "message": str(exc)}

    def _report_once(self, key, level, event, **fields):
        if key in self._reported:
            return
        self._reported.add(key)
        log_event(level, event, **fields)


def _record_view(record):
    view = {**record, "at": record["at"].isoformat()}
    if "window" in record:
        view["window"] = {**record["window"], "ends_at": record["window"]["ends_at"].isoformat()}
    return view


def _file_signature(path):
    try:
        return file_signature(os.stat(path))
    except FileNotFoundError:
        return None
