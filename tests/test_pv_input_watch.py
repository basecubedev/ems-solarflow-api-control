# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The EMS notices an input that stays dark and an empty-marked one that delivers.

It keeps each input's highest power of the day. A day on which an input stays
dark while another input of the same device delivers counts towards the hint
that it carries no module, or one that failed; three such days in a row make
the hint, which the Admin console offers and the operator decides. A dull day
on which nothing delivers says nothing either way. An input marked empty that
delivers is the opposite hint. The watch only reads and never changes the
config.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ems.clients import parse_device
from ems.control_status import ControlStatusWriter
from ems.pv_input_watch import DARK_DAYS, PvInputWatch

pytestmark = [
    pytest.mark.unit,
]

DAY = 86400.0
NOON = datetime(2026, 11, 22, 12, tzinfo=timezone.utc).timestamp()


class _Watch(PvInputWatch):
    """Asks for the hints at the time of its last reading, unless told another."""

    read_at = None

    def observe(self, name, powers, now=None):
        self.read_at = now
        super().observe(name, powers, now)

    def hints(self, now=None):
        return super().hints(self.read_at if now is None else now)


def _watch(tmp_path, unused=frozenset(), **kwargs):
    return _Watch(tmp_path / "pv-input-watch.json", unused_for=lambda name: unused, zone=timezone.utc, **kwargs)


def _days(watch, powers, count, start=NOON, name="WR1"):
    for day in range(count):
        watch.observe(name, powers, start + day * DAY)
    return start + count * DAY


def test_an_input_dark_for_three_days_while_another_delivered_is_a_hint(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [0.0, 23.0, 59.0, 66.0], DARK_DAYS)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)

    assert watch.hints() == [{"device": "WR1", "input": 1, "kind": "dark", "days": DARK_DAYS}]


def test_two_days_are_no_hint_yet(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [0.0, 23.0, 59.0, 66.0], DARK_DAYS - 1)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)

    assert watch.hints() == []


def test_the_day_still_running_does_not_count(tmp_path):
    watch = _watch(tmp_path)
    _days(watch, [0.0, 23.0, 59.0, 66.0], DARK_DAYS)

    assert watch.hints() == []


def test_a_dull_day_says_nothing_and_a_day_the_input_delivers_starts_again(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [0.0, 60.0, 60.0, 60.0], 2)
    after = _days(watch, [0.0, 20.0, 20.0, 20.0], 1, start=after)
    after = _days(watch, [0.0, 60.0, 60.0, 60.0], 1, start=after)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)
    assert watch.hints() == [{"device": "WR1", "input": 1, "kind": "dark", "days": 3}]

    after = _days(watch, [5.0, 60.0, 60.0, 60.0], 1, start=after + DAY)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)
    assert watch.hints() == []


def test_a_module_that_gives_a_little_light_is_never_dark(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [3.0, 400.0, 400.0, 400.0], 5)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)

    assert watch.hints() == []


def test_an_input_the_device_does_not_report_is_no_hint(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [None, 300.0, None, None], 5)
    watch.observe("WR1", [None, 0.0, None, None], after)

    assert watch.hints() == []


def test_an_input_marked_empty_is_no_dark_hint_and_is_one_once_it_delivers(tmp_path):
    watch = _watch(tmp_path, unused=frozenset({1}))
    after = _days(watch, [0.0, 23.0, 59.0, 66.0], 5)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)
    assert watch.hints() == []

    watch.observe("WR1", [180.0, 20.0, 20.0, 20.0], after + 60)
    assert watch.hints() == [{"device": "WR1", "input": 1, "kind": "delivers", "peak_w": 180}]


def test_the_delivering_hint_stays_through_the_next_day(tmp_path):
    watch = _watch(tmp_path, unused=frozenset({1}))
    watch.observe("WR1", [180.0, 20.0, 20.0, 20.0], NOON)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], NOON + DAY)

    assert watch.hints() == [{"device": "WR1", "input": 1, "kind": "delivers", "peak_w": 180}]


def test_a_clock_that_goes_back_a_day_is_no_new_day(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [0.0, 60.0, 60.0, 60.0], DARK_DAYS)
    watch.observe("WR1", [0.0, 60.0, 60.0, 60.0], after - 2 * DAY)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)

    assert watch.hints()[0]["days"] == DARK_DAYS


def test_the_count_survives_a_restart(tmp_path):
    first = _watch(tmp_path)
    after = _days(first, [0.0, 23.0, 59.0, 66.0], DARK_DAYS)
    first.save()

    second = _watch(tmp_path)
    second.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)
    assert second.hints() == [{"device": "WR1", "input": 1, "kind": "dark", "days": DARK_DAYS}]


def test_a_day_that_ends_is_saved_at_once_and_readings_at_most_every_ten_minutes(tmp_path):
    path = tmp_path / "pv-input-watch.json"
    watch = _watch(tmp_path)
    watch.observe("WR1", [0.0, 60.0, 60.0, 60.0], NOON)
    watch.save_if_due(NOON)
    assert path.exists()
    saved = path.read_text(encoding="utf-8")

    watch.observe("WR1", [0.0, 90.0, 60.0, 60.0], NOON + 60)
    watch.save_if_due(NOON + 60)
    assert path.read_text(encoding="utf-8") == saved

    watch.observe("WR1", [0.0, 90.0, 60.0, 60.0], NOON + DAY)
    watch.save_if_due(NOON + DAY)
    assert json.loads(path.read_text(encoding="utf-8"))["devices"]["WR1"]["dark_days"] == [1, 0, 0, 0]


_DARK_ENTRY = {"day": "2026-11-21", "last_day": "2026-11-20", "peaks": [0.0, 60.0, None, None], "last_peaks": [None] * 4, "dark_days": [3, 0, 0, 0]}


def test_a_readable_file_with_a_dark_run_gives_its_hint(tmp_path):
    (tmp_path / "pv-input-watch.json").write_text(json.dumps({"version": 1, "devices": {"WR1": _DARK_ENTRY}}), encoding="utf-8")

    watch = _watch(tmp_path)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], NOON)

    assert watch.hints() == [{"device": "WR1", "input": 1, "kind": "dark", "days": 4}]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"version": 99, "devices": {"WR1": _DARK_ENTRY}}),
        json.dumps({"version": 1, "devices": [_DARK_ENTRY]}),
        json.dumps({"version": 1, "devices": {"WR1": {**_DARK_ENTRY, "dark_days": [3, 0, 0]}}}),
        json.dumps({"version": 1, "devices": {"WR1": {**_DARK_ENTRY, "day": "20261121"}}}),
        json.dumps({"version": 1, "devices": {"WR1": _DARK_ENTRY}}).replace("60.0", "1" + "0" * 400),
    ],
)
def test_a_file_or_entry_this_cannot_read_starts_afresh(tmp_path, content):
    (tmp_path / "pv-input-watch.json").write_text(content, encoding="utf-8")

    watch = _watch(tmp_path)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], NOON)

    assert watch.hints() == []


def test_a_file_it_cannot_write_never_stops_the_loop(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory", encoding="utf-8")
    watch = PvInputWatch(blocked / "pv-input-watch.json", unused_for=lambda name: frozenset(), zone=timezone.utc)

    watch.observe("WR1", [0.0, 60.0, 60.0, 60.0], NOON)
    watch.save_if_due(NOON)
    watch.save_if_due(NOON + DAY)


def test_only_a_device_seen_in_this_run_is_hinted(tmp_path):
    first = _watch(tmp_path)
    after = _days(first, [0.0, 60.0, 60.0, 60.0], DARK_DAYS, name="OLD")
    first.observe("OLD", [0.0, 0.0, 0.0, 0.0], after)
    first.save()
    assert first.hints(after)

    assert _watch(tmp_path).hints(after) == []


def test_the_controller_s_online_devices_and_read_only_tiles_are_watched(tmp_path, monkeypatch):
    state = parse_device({"properties": {"solarPower1": 0, "solarPower2": 80}})
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        last_states={"WR1": state, "WR2": state},
        device_online={"WR1": True, "WR2": False},
    )
    tiles = [
        {"name": "DTU", "online": True, "state": state},
        {"name": "DARK", "online": False, "state": state},
        {"name": "NEVER", "online": True, "has_reading": False, "state": state},
    ]
    monkeypatch.setattr("dashboard.telemetry.telemetry_only_device_tiles", lambda controller: tiles)
    watch = _watch(tmp_path)

    for day in range(DARK_DAYS + 1):
        watch.observe_controller(controller, NOON + day * DAY)

    assert [(hint["device"], hint["input"]) for hint in watch.hints()] == [("DTU", 1), ("WR1", 1)]


def test_a_failure_while_watching_never_stops_the_loop(tmp_path, monkeypatch):
    def broken(controller):
        raise RuntimeError("tiles unavailable")

    monkeypatch.setattr("dashboard.telemetry.telemetry_only_device_tiles", broken)
    controller = SimpleNamespace(devices=[], last_states={}, device_online={})

    _watch(tmp_path).observe_controller(controller, NOON)


def test_the_control_snapshot_carries_the_hints(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [0.0, 60.0, 60.0, 60.0], DARK_DAYS)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)
    writer = ControlStatusWriter(tmp_path / "control-status.json", pv_input_watch=watch)
    controller = SimpleNamespace(
        last_control_explanation=None,
        runtime_system_int=lambda key, default, minimum=None: 5,
        last_fetch_at=None,
        cycle_failures=0,
        devices=[],
        last_grid_reading=None,
    )

    assert writer.write(controller, now=after, now_monotonic=1.0)

    status = json.loads((tmp_path / "control-status.json").read_text(encoding="utf-8"))
    assert status["pv_input_hints"] == [{"device": "WR1", "input": 1, "kind": "dark", "days": DARK_DAYS}]


def test_a_dark_input_that_gives_light_today_is_no_hint_any_more(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [0.0, 60.0, 60.0, 60.0], DARK_DAYS)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)
    assert watch.hints()

    watch.observe("WR1", [250.0, 60.0, 60.0, 60.0], after + 3600)
    assert watch.hints() == []


def test_the_day_before_is_the_calendar_day_not_the_last_day_the_ems_ran(tmp_path):
    watch = _watch(tmp_path, unused=frozenset({1}))
    watch.observe("WR1", [200.0, 20.0, 20.0, 20.0], NOON)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], NOON + 6 * DAY)

    assert watch.hints() == []


def test_days_the_ems_did_not_see_break_a_run(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [0.0, 60.0, 60.0, 60.0], 2)
    after = _days(watch, [0.0, 60.0, 60.0, 60.0], 1, start=after + 5 * DAY)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)

    assert watch.hints() == []


def test_a_clock_that_ran_a_year_ahead_and_came_back_keeps_the_watch_going(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [0.0, 60.0, 60.0, 60.0], 1)
    watch.observe("WR1", [0.0, 60.0, 60.0, 60.0], after + 365 * DAY)
    after = _days(watch, [0.0, 60.0, 60.0, 60.0], DARK_DAYS, start=after)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)

    assert watch.hints() == [{"device": "WR1", "input": 1, "kind": "dark", "days": DARK_DAYS + 1}]


def test_a_dark_hint_ends_when_the_ems_no_longer_sees_the_device(tmp_path):
    watch = _watch(tmp_path)
    after = _days(watch, [0.0, 60.0, 60.0, 60.0], DARK_DAYS)
    watch.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)
    assert watch.hints(after + DAY)

    assert watch.hints(after + 2 * DAY) == []


def test_a_delivering_hint_ends_two_days_after_the_last_reading(tmp_path):
    watch = _watch(tmp_path, unused=frozenset({1}))
    watch.observe("WR1", [180.0, 20.0, 20.0, 20.0], NOON)

    assert watch.hints(NOON + DAY) == [{"device": "WR1", "input": 1, "kind": "delivers", "peak_w": 180}]
    assert watch.hints(NOON + 2 * DAY) == []


def test_a_run_the_ems_stopped_in_shows_nothing_until_it_is_counted_again(tmp_path):
    first = _watch(tmp_path)
    after = _days(first, [0.0, 60.0, 60.0, 60.0], DARK_DAYS)
    first.observe("WR1", [0.0, 0.0, 0.0, 0.0], after)
    first.save()

    second = _watch(tmp_path)
    second.observe("WR1", [0.0, 0.0, 0.0, 0.0], after + 4 * DAY)
    assert second.hints() == []
