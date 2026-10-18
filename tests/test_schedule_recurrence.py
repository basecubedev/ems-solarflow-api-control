# SPDX-License-Identifier: AGPL-3.0-or-later
"""When a schedule rule's switch points fall.

The scheduler fires a switch point once its time has come, so every date and
every clock time here is one an operator will see happen at home: a rule for
weekdays that also fired on a Saturday, or a 02:30 rule that vanished on the
night the clocks change, would be a visible fault, not a rounding detail.
"""

from datetime import date, datetime, timedelta, timezone
from itertools import islice
from zoneinfo import ZoneInfo

import pytest

from ems.schedule_recurrence import (
    PRESETS,
    ScheduleTimeError,
    localize,
    occurrences,
    parse_when,
    preset_repeat,
    when_payload,
)

pytestmark = [pytest.mark.unit]

BERLIN = ZoneInfo("Europe/Berlin")


def _days(payload, n=8, since=None):
    when = parse_when(payload)
    return [item.day for item in islice(occurrences(when, BERLIN, since=since), n)]


def _clock(moment):
    local = moment.astimezone(BERLIN)
    return (local.hour, local.minute)


def _rule(start="2026-10-19T18:00", end="2026-10-19T23:00", **repeat):
    payload = {"start": start}
    if end is not None:
        payload["end"] = end
    if repeat:
        payload["repeat"] = repeat
    return payload


def test_a_one_time_rule_occurs_once():
    assert _days(_rule()) == [date(2026, 10, 19)]


@pytest.mark.parametrize(
    "preset,expected",
    [
        ("daily", [date(2026, 10, 19) + timedelta(days=n) for n in range(8)]),
        ("every_2nd_day", [date(2026, 10, 19) + timedelta(days=2 * n) for n in range(8)]),
        (
            "weekdays",
            [date(2026, 10, d) for d in (19, 20, 21, 22, 23, 26, 27, 28)],
        ),
        ("weekends", [date(2026, 10, d) for d in (24, 25)] + [date(2026, 10, 31), date(2026, 11, 1)]
         + [date(2026, 11, d) for d in (7, 8, 14, 15)]),
        ("sundays", [date(2026, 10, 25) + timedelta(weeks=n) for n in range(8)]),
        ("weekly", [date(2026, 10, 19) + timedelta(weeks=n) for n in range(8)]),
        ("every_2nd_week", [date(2026, 10, 19) + timedelta(weeks=2 * n) for n in range(8)]),
        (
            "monthly",
            [date(2026, 10, 19), date(2026, 11, 19), date(2026, 12, 19)]
            + [date(2027, m, 19) for m in range(1, 6)],
        ),
        (
            "monthly_last_day",
            [date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31),
             date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30), date(2027, 5, 31)],
        ),
        ("yearly", [date(2026 + n, 10, 19) for n in range(8)]),
    ],
)
def test_every_preset_falls_on_the_days_its_name_says(preset, expected):
    assert _days(_rule(**preset_repeat(preset))) == expected


def test_every_preset_name_is_tested():
    tested = {
        "once", "daily", "every_2nd_day", "weekdays", "weekends", "sundays", "weekly",
        "every_2nd_week", "monthly", "monthly_last_day", "yearly",
    }
    assert set(PRESETS) == tested
    assert preset_repeat("once") is None


def test_every_second_day_counts_from_the_start_date_not_from_today():
    days = _days(
        _rule(start="2026-01-01T18:00", end="2026-01-01T19:00", freq="daily", interval=2),
        n=2,
        since=localize(datetime(2026, 10, 18, 12, 0), BERLIN),
    )

    assert days == [date(2026, 10, 18), date(2026, 10, 20)]
    assert (days[0] - date(2026, 1, 1)).days % 2 == 0


def test_a_31st_skips_the_months_that_have_none():
    days = _days(_rule(start="2027-01-31T08:00", end=None, freq="monthly"), n=4)

    assert days == [date(2027, 1, 31), date(2027, 3, 31), date(2027, 5, 31), date(2027, 7, 31)]


@pytest.mark.parametrize(
    "repeat,expected",
    [
        (
            {"freq": "monthly", "by_weekday": ["su"], "by_set_pos": 2},
            [date(2026, 11, 8), date(2026, 12, 13), date(2027, 1, 10)],
        ),
        (
            {"freq": "monthly", "by_weekday": ["fr"], "by_set_pos": -1},
            [date(2026, 10, 30), date(2026, 11, 27), date(2026, 12, 25)],
        ),
        (
            {"freq": "monthly", "by_weekday": ["mo"], "by_set_pos": 1},
            [date(2026, 11, 2), date(2026, 12, 7), date(2027, 1, 4)],
        ),
    ],
)
def test_the_nth_and_the_last_weekday_of_a_month(repeat, expected):
    assert _days(_rule(start="2026-10-19T06:00", end=None, **repeat), n=3) == expected


def test_a_yearly_rule_on_the_last_sunday_of_october_stays_in_october():
    days = _days(
        _rule(start="2026-10-25T09:00", end=None, freq="yearly", by_month=[10], by_weekday=["su"], by_set_pos=-1),
        n=3,
    )

    assert days == [date(2026, 10, 25), date(2027, 10, 31), date(2028, 10, 29)]


def test_a_month_filter_keeps_a_daily_rule_to_the_winter():
    days = _days(
        _rule(start="2027-03-30T02:00", end="2027-03-30T05:00", freq="daily", by_month=[10, 11, 12, 1, 2, 3]),
        n=4,
    )

    assert days == [date(2027, 3, 30), date(2027, 3, 31), date(2027, 10, 1), date(2027, 10, 2)]


def test_until_is_the_last_day_it_may_fall_on():
    days = _days(_rule(freq="daily", until="2026-10-21"))

    assert days == [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)]


def test_a_skipped_date_still_counts_towards_count():
    payload = _rule(freq="daily", count=3)
    payload["exceptions"] = ["2026-10-20"]

    assert _days(payload) == [date(2026, 10, 19), date(2026, 10, 21)]


def test_a_window_past_midnight_ends_on_the_next_day():
    when = parse_when(_rule(start="2026-11-02T22:00", end="2026-11-03T06:00", freq="daily"))
    first = next(occurrences(when, BERLIN))

    assert first.start == datetime(2026, 11, 2, 22, 0, tzinfo=BERLIN)
    assert first.end == datetime(2026, 11, 3, 6, 0, tzinfo=BERLIN)


def test_a_switch_point_in_the_spring_forward_gap_comes_that_much_later():
    when = parse_when(_rule(start="2027-03-27T02:30", end=None, freq="daily"))
    days = list(islice(occurrences(when, BERLIN), 3))

    gap_night = days[1]
    assert gap_night.day == date(2027, 3, 28)
    assert gap_night.start == datetime(2027, 3, 28, 1, 30, tzinfo=timezone.utc)
    assert _clock(gap_night.start) == (3, 30)
    assert _clock(days[2].start) == (2, 30)


def test_a_switch_point_in_the_repeated_hour_falls_once_on_its_first_pass():
    when = parse_when(_rule(start="2026-10-25T02:30", end=None))
    only = next(occurrences(when, BERLIN))

    assert only.start == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    assert only.start.astimezone(BERLIN).utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize(
    "night,real_hours",
    [("2027-03-27", 7), ("2026-10-24", 9), ("2026-11-07", 8)],
)
def test_a_night_window_keeps_its_clock_times_across_a_clock_change(night, real_hours):
    start = f"{night}T22:00"
    end = (datetime.fromisoformat(night) + timedelta(days=1)).strftime("%Y-%m-%dT06:00")
    only = next(occurrences(parse_when(_rule(start=start, end=end)), BERLIN))

    assert (only.end - only.start) == timedelta(hours=real_hours)
    assert (_clock(only.start), _clock(only.end)) == ((22, 0), (6, 0))


def test_since_starts_with_the_window_still_running():
    when = parse_when(_rule(start="2026-01-01T22:00", end="2026-01-02T06:00", freq="daily"))
    since = localize(datetime(2026, 10, 19, 3, 0), BERLIN)

    first = next(occurrences(when, BERLIN, since=since))

    assert first.day == date(2026, 10, 18)
    assert first.start <= since < first.end


def test_until_bounds_the_last_start_yielded():
    when = parse_when(_rule(freq="daily"))
    until = localize(datetime(2026, 10, 21, 18, 0), BERLIN)

    assert [item.day for item in occurrences(when, BERLIN, until=until)] == [
        date(2026, 10, 19),
        date(2026, 10, 20),
        date(2026, 10, 21),
    ]


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"start": "2026-10-19 18:00"}, "start must be a local time"),
        ({"start": "2026-10-19T18:00", "end": "2026-10-19T18:00"}, "at least one minute"),
        ({"start": "2026-10-19T18:00", "at": "x"}, "unknown when field: at"),
        (_rule(freq="hourly"), "repeat freq must be one of"),
        (_rule(freq="weekly", interval=0), "interval must be a whole number"),
        (_rule(freq="weekly", by_weekday=["monday"]), "by_weekday must list days"),
        (_rule(freq="monthly", by_set_pos=2), "by_set_pos needs a monthly or yearly repeat with weekdays"),
        (_rule(freq="daily", by_month_day=[1]), "by_month_day needs a monthly or yearly repeat"),
        (_rule(freq="monthly", by_month_day=[32]), "by_month_day must list days"),
        (_rule(freq="daily", by_month=[13]), "by_month must list months"),
        (_rule(freq="daily", until="2026-10-30", count=3), "until or by count"),
        (_rule(freq="daily", until="2026-10-18"), "until must not be before the start date"),
        (_rule(freq="daily", count=0), "count must be a whole number"),
        (_rule(freq="daily", extra=1), "unknown repeat field: extra"),
        (_rule(freq="monthly", by_month_day=[31], by_month=[2]), "never occurs"),
        (_rule(start="2026-10-19T20:00", end="2026-10-20T21:00", freq="daily"), "ends after its next start"),
        (_rule(start="2026-10-19T08:00", end="2026-10-27T08:00", freq="weekly"), "ends after its next start"),
    ],
)
def test_a_timing_that_cannot_be_scheduled_is_refused_with_its_reason(payload, message):
    with pytest.raises(ScheduleTimeError, match=message):
        parse_when(payload)


def test_more_than_fifty_skipped_dates_are_refused():
    payload = _rule(freq="daily")
    payload["exceptions"] = [(date(2026, 11, 1) + timedelta(days=n)).isoformat() for n in range(51)]

    with pytest.raises(ScheduleTimeError, match="at most 50 exceptions"):
        parse_when(payload)


def test_a_series_whose_window_just_fits_its_gap_is_accepted():
    parse_when(_rule(start="2026-10-19T06:00", end="2026-10-20T06:00", freq="daily"))


@pytest.mark.parametrize(
    "payload",
    [
        _rule(),
        _rule(end=None),
        _rule(freq="weekly", by_weekday=["fr", "mo"], until="2027-03-31"),
        _rule(freq="monthly", by_weekday=["su"], by_set_pos=-1, count=12),
        {**_rule(freq="daily", by_month=[11, 1]), "exceptions": ["2026-12-24", "2026-11-01"]},
    ],
)
def test_the_json_form_reads_back_as_the_same_timing(payload):
    when = parse_when(payload)

    assert parse_when(when_payload(when)) == when


def test_a_long_running_series_is_answered_from_near_since_not_from_its_start():
    when = parse_when(_rule(start="2000-01-03T06:00", end=None, freq="daily"))
    since = localize(datetime(2026, 10, 18, 7, 0), BERLIN)

    first = next(occurrences(when, BERLIN, since=since))

    assert first.day == date(2026, 10, 19)


def test_a_window_the_gap_would_turn_around_keeps_its_length():
    when = parse_when(_rule(start="2027-03-27T02:30", end="2027-03-27T03:15", freq="daily"))
    gap_night = list(islice(occurrences(when, BERLIN), 2))[1]

    assert gap_night.day == date(2027, 3, 28)
    assert (_clock(gap_night.start), _clock(gap_night.end)) == ((3, 30), (4, 15))
    assert gap_night.end - gap_night.start == timedelta(minutes=45)
