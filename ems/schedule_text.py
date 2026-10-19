# SPDX-License-Identifier: AGPL-3.0-or-later
"""A schedule rule read back as one English sentence.

The editor's check step, ``emsctl schedule list`` and the dashboard's rule
list all show the same sentence, so it is written once, here.
Import-side-effect-free.
"""

from ems.schedule_actions import CHARGE, CHOICE, SWITCH, WATTS

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", -1: "last"}
WEEKDAYS_ONLY = (0, 1, 2, 3, 4)
WEEKEND = (5, 6)


def describe_when(when):
    """``Every weekday, 18:00–23:00, from Mon 19 Oct 2026`` and the like."""

    if when.repeat is None:
        if when.end is None:
            return f"Once, {_date(when.start)} at {_clock(when.start)}"
        if when.end.date() == when.start.date():
            return f"Once, {_date(when.start)}, {_clock(when.start)}–{_clock(when.end)}"
        return f"Once, {_date(when.start)} {_clock(when.start)} – {_date(when.end)} {_clock(when.end)}"
    parts = [_how_often(when)]
    parts.append(_times(when))
    if when.repeat.by_month:
        parts.append("only in " + _month_list(when.repeat.by_month))
    parts.append(f"from {_date(when.start)}")
    if when.repeat.until is not None:
        parts.append(f"until {_day(when.repeat.until)}")
    if when.repeat.count is not None:
        parts.append(f"{when.repeat.count} times")
    if when.exceptions:
        parts.append(f"skipping {len(when.exceptions)} date{'s' if len(when.exceptions) != 1 else ''}")
    return ", ".join(parts)


def describe_value(action, value):
    if action.value_type == SWITCH:
        return "on" if value else "off"
    if action.value_type == CHARGE:
        return "stop" if value == 0 else f"{value} W"
    if action.value_type == WATTS:
        return f"{value} W"
    if action.value_type == CHOICE:
        return str(action.choice_label(value)).lower()
    return str(value)


def describe_action(rule):
    """``WR1 · Off-grid socket standard, then off``."""

    where = rule.device if rule.device is not None else "System"
    what = f"{rule.action.label} {describe_value(rule.action, rule.value)}"
    if rule.is_window:
        what += f", then {describe_value(rule.action, rule.end_value)}"
    return f"{where} · {what}"


def _how_often(when):
    repeat = when.repeat
    if repeat.freq == "daily":
        if repeat.interval == 1:
            return "Every " + _weekday_list(repeat.by_weekday) if repeat.by_weekday else "Every day"
        every = "Every 2nd day" if repeat.interval == 2 else f"Every {repeat.interval} days"
        if repeat.by_weekday:
            return f"{every}, only on {_weekday_list(repeat.by_weekday)}"
        return every
    if repeat.freq == "weekly":
        days = repeat.by_weekday or (when.start.weekday(),)
        if repeat.interval == 1:
            if days == WEEKDAYS_ONLY:
                return "Every weekday"
            if days == WEEKEND:
                return "Every weekend"
            return "Every " + _weekday_list(days)
        return f"Every {repeat.interval} weeks on " + _weekday_list(days)
    if repeat.freq == "monthly":
        prefix = "Monthly" if repeat.interval == 1 else f"Every {repeat.interval} months"
        return f"{prefix} on {_month_position(when)}"
    prefix = "Yearly" if repeat.interval == 1 else f"Every {repeat.interval} years"
    months = repeat.by_month or (when.start.month,)
    return f"{prefix} on {_month_position(when)} of {_month_list(months)}"


def _month_position(when):
    repeat = when.repeat
    if repeat.by_month_day:
        return " and ".join("the last day" if day == -1 else f"the {_nth(day)}" for day in repeat.by_month_day)
    if repeat.by_weekday:
        days = _weekday_list(repeat.by_weekday)
        if repeat.by_set_pos is None:
            return f"every {days}"
        return f"the {ORDINALS[repeat.by_set_pos]} {days}"
    return f"the {_nth(when.start.day)}"


def _times(when):
    if when.end is None:
        return f"at {_clock(when.start)}"
    text = f"{_clock(when.start)}–{_clock(when.end)}"
    days = (when.end.date() - when.start.date()).days
    return text + (f" (+{days})" if days else "")


def _weekday_list(days):
    names = [DAY_NAMES[day] for day in days]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _month_list(months):
    ordered = list(months)
    if len(ordered) > 2 and _consecutive_around_the_year(ordered):
        start = _wrap_start(ordered)
        end = (start + len(ordered) - 2) % 12 + 1
        return f"{MONTH_NAMES[start - 1]}–{MONTH_NAMES[end - 1]}"
    return ", ".join(MONTH_NAMES[month - 1] for month in ordered)


def _consecutive_around_the_year(months):
    start = _wrap_start(months)
    expected = {(start - 1 + offset) % 12 + 1 for offset in range(len(months))}
    return expected == set(months)


def _wrap_start(months):
    present = set(months)
    for month in sorted(present):
        if (month - 2) % 12 + 1 not in present:
            return month
    return min(present)


def _nth(number):
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _date(moment):
    return _day(moment.date())


def _day(day):
    return f"{DAY_NAMES[day.weekday()]} {day.day} {MONTH_NAMES[day.month - 1]} {day.year}"


def _clock(moment):
    return moment.strftime("%H:%M")
