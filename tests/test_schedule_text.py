# SPDX-License-Identifier: AGPL-3.0-or-later
"""A rule read back as the sentence the editor, emsctl and the list show."""

import pytest

from ems.schedule_model import parse_editable
from ems.schedule_recurrence import parse_when
from ems.schedule_store import ScheduleStore
from ems.schedule_text import describe_action, describe_when

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    "when,sentence",
    [
        ({"start": "2026-10-19T18:00"}, "Once, Mon 19 Oct 2026 at 18:00"),
        ({"start": "2026-10-19T18:00", "end": "2026-10-19T23:00"}, "Once, Mon 19 Oct 2026, 18:00–23:00"),
        (
            {"start": "2026-08-01T00:00", "end": "2026-08-15T00:00"},
            "Once, Sat 1 Aug 2026 00:00 – Sat 15 Aug 2026 00:00",
        ),
        (
            {"start": "2026-10-19T18:00", "end": "2026-10-19T23:00",
             "repeat": {"freq": "weekly", "by_weekday": ["mo", "tu", "we", "th", "fr"]}},
            "Every weekday, 18:00–23:00, from Mon 19 Oct 2026",
        ),
        (
            {"start": "2026-10-24T09:00", "repeat": {"freq": "weekly", "by_weekday": ["sa", "su"]}},
            "Every weekend, at 09:00, from Sat 24 Oct 2026",
        ),
        (
            {"start": "2026-10-25T09:00", "repeat": {"freq": "weekly", "by_weekday": ["su"]}},
            "Every Sun, at 09:00, from Sun 25 Oct 2026",
        ),
        (
            {"start": "2026-10-20T02:00", "end": "2026-10-20T05:00", "repeat": {"freq": "daily", "interval": 2}},
            "Every 2nd day, 02:00–05:00, from Tue 20 Oct 2026",
        ),
        (
            {"start": "2026-10-24T09:00", "repeat": {"freq": "daily", "interval": 2, "by_weekday": ["sa", "su"]}},
            "Every 2nd day, only on Sat and Sun, at 09:00, from Sat 24 Oct 2026",
        ),
        (
            {"start": "2026-10-24T09:00", "repeat": {"freq": "daily", "interval": 3, "by_weekday": ["mo"]}},
            "Every 3 days, only on Mon, at 09:00, from Sat 24 Oct 2026",
        ),
        (
            {"start": "2026-10-19T22:00", "end": "2026-10-20T06:00", "repeat": {"freq": "daily"}},
            "Every day, 22:00–06:00 (+1), from Mon 19 Oct 2026",
        ),
        (
            {"start": "2026-10-19T06:00", "repeat": {"freq": "weekly", "interval": 2, "by_weekday": ["mo", "fr"],
                                                       "until": "2027-03-31"}},
            "Every 2 weeks on Mon and Fri, at 06:00, from Mon 19 Oct 2026, until Wed 31 Mar 2027",
        ),
        (
            {"start": "2026-10-31T08:00", "repeat": {"freq": "monthly", "by_month_day": [-1]}},
            "Monthly on the last day, at 08:00, from Sat 31 Oct 2026",
        ),
        (
            {"start": "2026-11-08T08:00", "repeat": {"freq": "monthly", "by_weekday": ["su"], "by_set_pos": 2,
                                                       "count": 6}},
            "Monthly on the 2nd Sun, at 08:00, from Sun 8 Nov 2026, 6 times",
        ),
        (
            {"start": "2026-10-19T02:00", "end": "2026-10-19T05:00",
             "repeat": {"freq": "daily", "by_month": [10, 11, 12, 1, 2, 3]}},
            "Every day, 02:00–05:00, only in Oct–Mar, from Mon 19 Oct 2026",
        ),
        (
            {"start": "2026-10-25T09:00", "repeat": {"freq": "yearly", "by_month": [10], "by_weekday": ["su"],
                                                       "by_set_pos": -1}},
            "Yearly on the last Sun of Oct, at 09:00, only in Oct, from Sun 25 Oct 2026",
        ),
        (
            {"start": "2026-10-19T18:00", "repeat": {"freq": "daily"}, "exceptions": ["2026-12-24"]},
            "Every day, at 18:00, from Mon 19 Oct 2026, skipping 1 date",
        ),
        (
            {"start": "2026-10-23T18:00", "repeat": {"freq": "monthly"}},
            "Monthly on the 23rd, at 18:00, from Fri 23 Oct 2026",
        ),
    ],
)
def test_a_timing_reads_back_as_one_sentence(when, sentence):
    assert describe_when(parse_when(when)) == sentence


@pytest.mark.parametrize(
    "rule,sentence",
    [
        (
            {"target": {"scope": "device", "device": "WR1"},
             "action": {"id": "offgrid_socket", "value": "standard", "end_value": "off"},
             "when": {"start": "2026-10-19T18:00", "end": "2026-10-19T23:00"}},
            "WR1 · Off-grid socket standard, then off",
        ),
        (
            {"target": {"scope": "system"}, "action": {"id": "total_output_limit", "value": 1000},
             "when": {"start": "2026-10-19T06:00"}},
            "System · Total output limit 1000 W",
        ),
        (
            {"target": {"scope": "device", "device": "WR1"},
             "action": {"id": "charge_from_grid", "value": 600, "end_value": 0},
             "when": {"start": "2026-10-20T02:00", "end": "2026-10-20T05:00"}},
            "WR1 · Charge from the grid 600 W, then stop",
        ),
        (
            {"target": {"scope": "system"}, "action": {"id": "winter_mode", "value": False},
             "when": {"start": "2026-10-19T06:00"}},
            "System · Winter mode off",
        ),
    ],
)
def test_an_action_reads_back_with_its_target(tmp_path, rule, sentence):
    rule = {"name": "x", **rule}
    parse_editable(rule)
    stored = ScheduleStore(tmp_path / "schedules.json").create(rule, by="dashboard")

    assert describe_action(stored) == sentence
