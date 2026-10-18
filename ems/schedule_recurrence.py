# SPDX-License-Identifier: AGPL-3.0-or-later
"""When a schedule rule's switch points fall, in local wall-clock time.

The repeat model is a subset of RFC 5545 RRULE, kept structured: frequency,
interval, weekdays, month days, the n-th weekday, a month filter, an end by date
or by count, and skipped dates. Pure and import-side-effect-free: the zone and
the clock are passed in.

One departure from RRULE: a yearly repeat stays in the start's month unless
``by_month`` names months, where RRULE would spread month days or weekdays over
the whole year.
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

WEEKDAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
SET_POSITIONS = (1, 2, 3, 4, -1)
MAX_INTERVAL = 366
MAX_COUNT = 1000
MAX_EXCEPTIONS = 50
FIRST_OCCURRENCE_HORIZON_YEARS = 8
SELF_OVERLAP_HORIZON_DAYS = 800
MIN_WINDOW = timedelta(minutes=1)
LOCAL_FORMAT = "%Y-%m-%dT%H:%M"

PRESETS = {
    "once": None,
    "daily": {"freq": "daily"},
    "every_2nd_day": {"freq": "daily", "interval": 2},
    "weekdays": {"freq": "weekly", "by_weekday": ["mo", "tu", "we", "th", "fr"]},
    "weekends": {"freq": "weekly", "by_weekday": ["sa", "su"]},
    "sundays": {"freq": "weekly", "by_weekday": ["su"]},
    "weekly": {"freq": "weekly"},
    "every_2nd_week": {"freq": "weekly", "interval": 2},
    "monthly": {"freq": "monthly"},
    "monthly_last_day": {"freq": "monthly", "by_month_day": [-1]},
    "yearly": {"freq": "yearly"},
}


class ScheduleTimeError(ValueError):
    """A rule's timing that cannot be scheduled; the message says why."""


@dataclass(frozen=True)
class Repeat:
    freq: str
    interval: int = 1
    by_weekday: tuple = ()
    by_month_day: tuple = ()
    by_set_pos: int | None = None
    by_month: tuple = ()
    until: date | None = None
    count: int | None = None


@dataclass(frozen=True)
class When:
    """A rule's first occurrence in local time, how it repeats, and what it skips.

    ``end`` is None for a single switch point. A window's later occurrences
    last as long on the clock as the first one does.
    """

    start: datetime
    end: datetime | None = None
    repeat: Repeat | None = None
    exceptions: frozenset = frozenset()

    @property
    def length(self):
        return None if self.end is None else self.end - self.start


@dataclass(frozen=True)
class Occurrence:
    """One occurrence: its local date and its switch points in UTC.

    UTC because Python compares and subtracts two times of one zone by their
    wall clocks, which is wrong across a clock change.
    """

    day: date
    start: datetime
    end: datetime | None


def parse_when(payload):
    """A validated :class:`When` from its JSON form, or ScheduleTimeError."""

    if not isinstance(payload, dict):
        raise ScheduleTimeError("when must be an object")
    unknown = sorted(set(payload) - {"start", "end", "repeat", "exceptions"})
    if unknown:
        raise ScheduleTimeError(f"unknown when field: {unknown[0]}")
    start = parse_local(payload.get("start"), "start")
    end = None if payload.get("end") is None else parse_local(payload["end"], "end")
    if end is not None and end - start < MIN_WINDOW:
        raise ScheduleTimeError("end must be at least one minute after start")
    repeat = _repeat(payload.get("repeat"), start)
    exceptions = _exceptions(payload.get("exceptions"))
    when = When(start=start, end=end, repeat=repeat, exceptions=exceptions)
    _require_an_occurrence(when)
    _refuse_self_overlap(when)
    return when


def when_payload(when):
    """The JSON form of a :class:`When`, the inverse of :func:`parse_when`."""

    payload = {"start": when.start.strftime(LOCAL_FORMAT)}
    if when.end is not None:
        payload["end"] = when.end.strftime(LOCAL_FORMAT)
    if when.repeat is not None:
        repeat = when.repeat
        payload["repeat"] = {
            "freq": repeat.freq,
            "interval": repeat.interval,
            "by_weekday": [WEEKDAYS[day] for day in repeat.by_weekday],
            "by_month_day": list(repeat.by_month_day),
            "by_set_pos": repeat.by_set_pos,
            "by_month": list(repeat.by_month),
            "until": repeat.until.isoformat() if repeat.until else None,
            "count": repeat.count,
        }
    if when.exceptions:
        payload["exceptions"] = sorted(day.isoformat() for day in when.exceptions)
    return payload


def preset_repeat(name):
    """The repeat a named preset stands for (None for a one-time rule)."""

    if name not in PRESETS:
        raise ScheduleTimeError(f"unknown repeat preset {name}")
    preset = PRESETS[name]
    return None if preset is None else dict(preset)


def localize(naive, zone):
    """An aware time for a local wall-clock time, as the clock on the wall reads it.

    A time in the spring-forward gap lands that much later (02:30 becomes
    03:30); one in the repeated fall-back hour is its first occurrence.
    """

    return naive.replace(tzinfo=zone, fold=0).astimezone(timezone.utc).astimezone(zone)


def occurrences(when, zone, *, since=None, until=None):
    """Occurrences in order, from the first one that ends at or after ``since``.

    ``since`` and ``until`` are aware times; ``until`` bounds the start of the
    last occurrence yielded. Skipped dates are left out but still count
    towards a repeat's ``count``. A window whose start the spring-forward gap
    moves past its end keeps its length.
    """

    since = None if since is None else since.astimezone(timezone.utc)
    until = None if until is None else until.astimezone(timezone.utc)
    for day in _days(when, since):
        local_start = datetime.combine(day, when.start.time())
        start = localize(local_start, zone).astimezone(timezone.utc)
        if until is not None and start > until:
            return
        end = None
        if when.end is not None:
            end = localize(local_start + when.length, zone).astimezone(timezone.utc)
            if end <= start:
                end = start + when.length
        if since is not None and (end or start) < since:
            continue
        if day in when.exceptions:
            continue
        yield Occurrence(day=day, start=start, end=end)


def wall_windows(when, since=None):
    """A window rule's occurrences as wall-clock ``(start, end)`` pairs, in
    order, from the first that ends at or after the naive local ``since``.

    For comparing two rules' windows with each other: both read the same
    clock, so no zone is needed, and a long span costs only date arithmetic.
    """

    for day in _days(when, since):
        start = datetime.combine(day, when.start.time())
        end = start + when.length
        if since is not None and end < since:
            continue
        if day in when.exceptions:
            continue
        yield start, end


def _days(when, since):
    if when.repeat is None:
        yield when.start.date()
        return
    first = when.start.date()
    repeat = when.repeat
    skip_to = None
    if since is not None and repeat.count is None:
        span = when.length or timedelta(0)
        skip_to = (since - span - timedelta(days=2)).date()
    produced = 0
    empty_periods = 0
    for period in _periods(repeat, first, skip_to):
        candidates = [day for day in _period_days(repeat, first, period) if day >= first]
        empty_periods = 0 if candidates else empty_periods + 1
        if empty_periods > _max_empty_periods(repeat):
            return
        for day in candidates:
            if repeat.until is not None and day > repeat.until:
                return
            produced += 1
            if repeat.count is not None and produced > repeat.count:
                return
            yield day


def _max_empty_periods(repeat):
    return {"daily": 400, "weekly": 60, "monthly": 100, "yearly": 10}[repeat.freq]


def _periods(repeat, first, skip_to):
    """The start date of every ``interval``-th period from the one ``first`` is in."""

    if repeat.freq == "daily":
        index = 0 if skip_to is None else max(0, (skip_to - first).days // repeat.interval)
        while True:
            yield first + timedelta(days=index * repeat.interval)
            index += 1
    elif repeat.freq == "weekly":
        week = first - timedelta(days=first.weekday())
        index = 0 if skip_to is None else max(0, (skip_to - week).days // 7 // repeat.interval)
        while True:
            yield week + timedelta(weeks=index * repeat.interval)
            index += 1
    elif repeat.freq == "monthly":
        months = 0
        if skip_to is not None:
            elapsed = (skip_to.year - first.year) * 12 + skip_to.month - first.month
            months = max(0, elapsed // repeat.interval)
        while True:
            total = first.month - 1 + months * repeat.interval
            yield date(first.year + total // 12, total % 12 + 1, 1)
            months += 1
    else:
        years = 0 if skip_to is None else max(0, (skip_to.year - first.year) // repeat.interval)
        while True:
            yield date(first.year + years * repeat.interval, 1, 1)
            years += 1


def _period_days(repeat, first, period):
    if repeat.freq == "daily":
        days = [period]
    elif repeat.freq == "weekly":
        weekdays = repeat.by_weekday or (first.weekday(),)
        days = [period + timedelta(days=weekday) for weekday in sorted(weekdays)]
    elif repeat.freq == "monthly":
        days = _month_days(repeat, first, period.year, period.month)
    else:
        months = repeat.by_month or (first.month,)
        days = [
            day
            for month in sorted(months)
            for day in _month_days(repeat, first, period.year, month)
        ]
    return [
        day
        for day in days
        if (not repeat.by_month or day.month in repeat.by_month)
        and (repeat.freq != "daily" or not repeat.by_weekday or day.weekday() in repeat.by_weekday)
    ]


def _month_days(repeat, first, year, month):
    length = calendar.monthrange(year, month)[1]
    if repeat.by_month_day:
        days = set()
        for value in repeat.by_month_day:
            number = length if value == -1 else value
            if number <= length:
                days.add(date(year, month, number))
        return sorted(days)
    if repeat.by_weekday:
        matching = [
            date(year, month, number)
            for number in range(1, length + 1)
            if date(year, month, number).weekday() in repeat.by_weekday
        ]
        if repeat.by_set_pos is None:
            return matching
        index = repeat.by_set_pos - 1 if repeat.by_set_pos > 0 else repeat.by_set_pos
        return [matching[index]] if -len(matching) <= index < len(matching) else []
    return [date(year, month, first.day)] if first.day <= length else []


def parse_local(value, field):
    """A local wall-clock time as rules write it, ``2026-10-19T18:00``."""

    if not isinstance(value, str):
        raise ScheduleTimeError(f"{field} must be a local time like 2026-10-19T18:00")
    try:
        return datetime.strptime(value.strip(), LOCAL_FORMAT)
    except ValueError as exc:
        raise ScheduleTimeError(f"{field} must be a local time like 2026-10-19T18:00") from exc


def _repeat(payload, start):
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ScheduleTimeError("repeat must be an object")
    known = {"freq", "interval", "by_weekday", "by_month_day", "by_set_pos", "by_month", "until", "count"}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ScheduleTimeError(f"unknown repeat field: {unknown[0]}")
    freq = payload.get("freq")
    if freq not in FREQUENCIES:
        raise ScheduleTimeError(f"repeat freq must be one of: {', '.join(FREQUENCIES)}")
    interval = _whole(payload.get("interval", 1), "interval", 1, MAX_INTERVAL)
    by_weekday = _weekdays(payload.get("by_weekday"))
    by_month_day = _month_days_field(payload.get("by_month_day"))
    by_month = _months(payload.get("by_month"))
    by_set_pos = payload.get("by_set_pos")
    if by_set_pos is not None:
        if isinstance(by_set_pos, bool) or by_set_pos not in SET_POSITIONS:
            raise ScheduleTimeError("by_set_pos must be 1, 2, 3, 4 or -1")
        if freq not in ("monthly", "yearly") or not by_weekday:
            raise ScheduleTimeError("by_set_pos needs a monthly or yearly repeat with weekdays")
        if by_month_day:
            raise ScheduleTimeError("by_set_pos and by_month_day cannot be combined")
    if by_month_day and freq not in ("monthly", "yearly"):
        raise ScheduleTimeError("by_month_day needs a monthly or yearly repeat")
    if by_month_day and by_weekday:
        raise ScheduleTimeError("by_month_day and by_weekday cannot be combined")
    until = payload.get("until")
    count = payload.get("count")
    if until is not None and count is not None:
        raise ScheduleTimeError("a repeat ends by until or by count, not both")
    if until is not None:
        until = parse_day(until, "until")
        if until < start.date():
            raise ScheduleTimeError("until must not be before the start date")
    if count is not None:
        count = _whole(count, "count", 1, MAX_COUNT)
    return Repeat(
        freq=freq,
        interval=interval,
        by_weekday=by_weekday,
        by_month_day=by_month_day,
        by_set_pos=by_set_pos,
        by_month=by_month,
        until=until,
        count=count,
    )


def _whole(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ScheduleTimeError(f"{field} must be a whole number from {minimum} to {maximum}")
    return value


def _weekdays(value):
    if value is None:
        return ()
    if not isinstance(value, list) or any(day not in WEEKDAYS for day in value):
        raise ScheduleTimeError(f"by_weekday must list days from: {', '.join(WEEKDAYS)}")
    return tuple(sorted({WEEKDAYS.index(day) for day in value}))


def _month_days_field(value):
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        isinstance(day, bool) or not isinstance(day, int) or not (day == -1 or 1 <= day <= 31)
        for day in value
    ):
        raise ScheduleTimeError("by_month_day must list days from 1 to 31, or -1 for the last")
    return tuple(sorted(set(value)))


def _months(value):
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12
        for month in value
    ):
        raise ScheduleTimeError("by_month must list months from 1 to 12")
    return tuple(sorted(set(value)))


def parse_day(value, field):
    """A date as rules write it, ``2027-03-31``."""

    if not isinstance(value, str):
        raise ScheduleTimeError(f"{field} must be a date like 2027-03-31")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ScheduleTimeError(f"{field} must be a date like 2027-03-31") from exc


def _exceptions(value):
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ScheduleTimeError("exceptions must be a list of dates")
    if len(value) > MAX_EXCEPTIONS:
        raise ScheduleTimeError(f"at most {MAX_EXCEPTIONS} exceptions")
    return frozenset(parse_day(day, "exception") for day in value)


def _require_an_occurrence(when):
    horizon = when.start.date().replace(day=1) + timedelta(days=366 * FIRST_OCCURRENCE_HORIZON_YEARS)
    for day in _days(when, None):
        if day > horizon:
            break
        return
    raise ScheduleTimeError("this repeat never occurs")


def _refuse_self_overlap(when):
    if when.end is None or when.repeat is None:
        return
    horizon = when.start.date() + timedelta(days=SELF_OVERLAP_HORIZON_DAYS)
    previous = None
    for day in _days(when, None):
        if day > horizon:
            return
        start = datetime.combine(day, when.start.time())
        if previous is not None and start < previous + when.length:
            raise ScheduleTimeError(
                f"the window ends after its next start ({start.strftime(LOCAL_FORMAT)})"
            )
        previous = start
