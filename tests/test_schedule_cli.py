# SPDX-License-Identifier: AGPL-3.0-or-later
"""``emsctl schedule`` writes rules the way the dashboard does.

It builds the editor payload from options, runs the same save check, and
writes through the store; the running EMS applies the rules.
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ems import schedule_cli
from ems.runtime_commands import build_validation_context
from ems.schedule_cli import (
    ScheduleCommandError,
    add_schedule_parser,
    handle_schedule_command,
    parse_months,
    parse_repeat,
    parse_rrule,
)
from ems.schedule_model import RULE_ID
from ems.schedule_recurrence import localize
from ems.schedule_store import ScheduleStore

pytestmark = [pytest.mark.contract, pytest.mark.config]

BERLIN = ZoneInfo("Europe/Berlin")
NOW = localize(datetime(2026, 10, 18, 14, 0), BERLIN).astimezone(timezone.utc)
CONFIG = {
    "system": {"max_total_power": 900, "max_device_power": 800},
    "devices": [{"name": "WR1", "ip": "192.0.2.20", "sn": "SN-TEST-0001", "max_power": 800}],
}


@pytest.fixture
def cli(tmp_path):
    store = ScheduleStore(tmp_path / "schedules.json", clock=lambda: NOW)
    parser = argparse.ArgumentParser(prog="emsctl")
    add_schedule_parser(parser.add_subparsers(dest="command"))

    def run(*argv, control_status=None):
        lines = []
        args = parser.parse_args(["schedule", *argv])
        code = handle_schedule_command(
            args,
            store=store,
            devices={"WR1": "SN-TEST-0001"},
            validation_context=build_validation_context(CONFIG),
            zone=BERLIN,
            now=NOW,
            control_status=control_status,
            out=lines.append,
        )
        assert code == 0
        return lines

    run.store = store
    return run


def _only_rule(cli):
    rules = cli.store.load().rules
    assert len(rules) == 1
    return rules[0]


def test_a_window_on_weekdays_is_added_as_the_editor_would_add_it(cli):
    lines = cli("add", "--name", "Evening socket", "--device", "WR1", "--action", "offgrid_socket",
                "--value", "standard", "--from", "18:00", "--to", "23:00", "--end-value", "off",
                "--repeat", "weekdays", "--start", "2026-10-19")

    rule = _only_rule(cli)
    assert lines == [f"added {rule.id}: Every weekday, 18:00–23:00, from Mon 19 Oct 2026 · "
                     "WR1 · Off-grid socket standard, then off"]
    assert (rule.value, rule.end_value, rule.updated_by) == ("standard", "off", "emsctl")
    assert rule.when.repeat.by_weekday == (0, 1, 2, 3, 4)


def test_a_single_switch_point_starts_today_unless_told_otherwise(cli):
    cli("add", "--name", "Morning boost", "--system", "--action", "total_output_limit",
        "--value", "900", "--at", "18:30", "--repeat", "weekdays")

    rule = _only_rule(cli)
    assert rule.when.start == datetime(2026, 10, 18, 18, 30)
    assert rule.when.end is None and rule.end_value is None


def test_a_window_that_ends_before_it_starts_ends_the_next_day(cli):
    cli("add", "--name", "Night", "--device", "WR1", "--action", "device_output_limit",
        "--value", "200", "--from", "22:00", "--to", "06:00", "--end-value", "800", "--repeat", "daily")

    assert _only_rule(cli).when.end == datetime(2026, 10, 19, 6, 0)


def test_a_one_time_vacation_spans_days(cli):
    cli("add", "--name", "Vacation", "--system", "--action", "total_output_limit", "--value", "200",
        "--from", "2027-08-01T00:00", "--to", "2027-08-15T00:00", "--end-value", "900")

    rule = _only_rule(cli)
    assert rule.when.repeat is None
    assert (rule.when.end - rule.when.start).days == 14


def test_charging_every_second_night_in_the_winter_months(cli):
    cli("add", "--name", "Cheap night", "--device", "WR1", "--action", "charge_from_grid", "--value", "600",
        "--from", "02:00", "--to", "05:00", "--end-value", "stop", "--repeat", "every:2d", "--months", "10-3",
        "--start", "2026-10-20")

    rule = _only_rule(cli)
    assert (rule.value, rule.end_value) == (600, 0)
    assert (rule.when.repeat.interval, rule.when.repeat.by_month) == (2, (1, 2, 3, 10, 11, 12))


@pytest.mark.parametrize(
    "argv,message",
    [
        (["--device", "WR1", "--action", "offgrid_socket", "--value", "eco", "--at", "18:00"], "--name is required"),
        (["--name", "x", "--action", "offgrid_socket", "--value", "eco", "--at", "18:00"], "needs --device"),
        (["--name", "x", "--system", "--action", "winter_mode", "--value", "maybe", "--at", "18:00"],
         "takes on or off"),
        (["--name", "x", "--system", "--action", "winter_mode", "--value", "on", "--at", "18:00",
          "--from", "19:00"], "--at for a switch point or --from/--to"),
        (["--name", "x", "--system", "--action", "winter_mode", "--value", "on", "--from", "19:00"],
         "needs both --from and --to"),
        (["--name", "x", "--system", "--action", "winter_mode", "--value", "on", "--at", "18:00",
          "--repeat", "hourly"], "--repeat hourly is not one of"),
        (["--name", "x", "--system", "--action", "winter_mode", "--value", "on", "--at", "18:00",
          "--months", "10-3"], "--months needs a repeating rule"),
        (["--name", "x", "--system", "--action", "winter_mode", "--value", "on", "--at", "2026-10-17T18:00"],
         "lies in the past"),
        (["--name", "x", "--device", "WR9", "--action", "offgrid_socket", "--value", "eco", "--at", "18:00"],
         "unknown device WR9"),
        (["--name", "x", "--device", "WR1", "--action", "device_output_limit", "--value", "900", "--at", "18:00"],
         "max_power must be between 0 and 800"),
    ],
)
def test_an_add_the_installation_cannot_carry_out_is_refused_and_writes_nothing(cli, argv, message):
    with pytest.raises(ScheduleCommandError, match=message):
        cli("add", *argv)
    assert cli.store.load().rules == ()


def test_an_overlapping_window_names_the_rule_it_clashes_with(cli):
    cli("add", "--name", "Evening socket", "--device", "WR1", "--action", "offgrid_socket", "--value", "standard",
        "--from", "18:00", "--to", "23:00", "--end-value", "off", "--repeat", "daily", "--start", "2026-10-19")

    with pytest.raises(ScheduleCommandError, match="overlaps 'Evening socket'"):
        cli("add", "--name", "Late", "--device", "WR1", "--action", "offgrid_socket", "--value", "eco",
            "--from", "22:00", "--to", "23:30", "--end-value", "off", "--repeat", "daily", "--start", "2026-10-19")


def test_a_rule_is_shown_switched_moved_skipped_changed_and_removed(cli):
    cli("add", "--name", "A", "--device", "WR1", "--action", "offgrid_socket", "--value", "standard",
        "--from", "18:00", "--to", "23:00", "--end-value", "off", "--repeat", "daily", "--start", "2026-10-19")
    cli("add", "--name", "B", "--system", "--action", "winter_mode", "--value", "off", "--at", "2026-11-01T00:00")
    first, second = cli.store.load().rules

    shown = json.loads("\n".join(cli("show", first.id)))
    assert shown["summary"] == "Every day, 18:00–23:00, from Mon 19 Oct 2026 · WR1 · Off-grid socket standard, then off"
    cli("disable", first.id)
    assert cli.store.load().rule(first.id).enabled is False
    cli("move", second.id, "up")
    assert [rule.id for rule in cli.store.load().rules] == [second.id, first.id]
    assert cli("skip", first.id) == [f"{first.id} skips 2026-10-19"]
    cli("edit", first.id, "--to", "22:30")
    changed = cli.store.load().rule(first.id)
    assert (changed.when.start, changed.when.end) == (datetime(2026, 10, 19, 18, 0), datetime(2026, 10, 19, 22, 30))
    assert changed.enabled is False and changed.when.exceptions
    cli("remove", first.id)
    assert [rule.id for rule in cli.store.load().rules] == [second.id]


def test_list_preview_pause_and_status(cli):
    cli("add", "--name", "Evening socket", "--device", "WR1", "--action", "offgrid_socket", "--value", "standard",
        "--from", "18:00", "--to", "23:00", "--end-value", "off", "--repeat", "daily", "--start", "2026-10-19")
    rule = _only_rule(cli)

    listed = cli("list")
    assert listed[0] == "schedules on · Europe/Berlin"
    assert listed[-1] == "            next: Mon 19 Oct 18:00 → standard"
    preview = cli("preview", "--days", "2")
    assert preview[:2] == [
        "Mon 19 Oct 18:00  devices.WR1.offgrid_socket_mode → standard  (Evening socket)",
        "Mon 19 Oct 23:00  devices.WR1.offgrid_socket_mode → off  (Evening socket)",
    ]
    assert cli("pause") == ["schedules paused"]
    assert cli("preview", "--days", "2")[:2] == [
        "schedules are paused; nothing fires. After a resume:",
        "Mon 19 Oct 18:00  devices.WR1.offgrid_socket_mode → standard  (Evening socket)",
    ]
    assert cli("resume") == ["schedules resumed"]
    assert cli("status") == ["no running EMS has reported its schedule recently"]
    assert json.loads("\n".join(cli("status", control_status={"schedule": {"next": [], "rule": rule.id}}))) == {
        "next": [],
        "rule": rule.id,
    }


@pytest.mark.parametrize(
    "spec,repeat",
    [
        ("once", None),
        ("weekdays", {"freq": "weekly", "by_weekday": ["mo", "tu", "we", "th", "fr"]}),
        ("weekly:mo,we", {"freq": "weekly", "by_weekday": ["mo", "we"]}),
        ("every:2d", {"freq": "daily", "interval": 2}),
        ("every:2w:sa,su", {"freq": "weekly", "interval": 2, "by_weekday": ["sa", "su"]}),
        ("monthly:15", {"freq": "monthly", "by_month_day": [15]}),
        ("monthly:last", {"freq": "monthly", "by_month_day": [-1]}),
        ("monthly:2su", {"freq": "monthly", "by_weekday": ["su"], "by_set_pos": 2}),
        ("monthly:lastfr", {"freq": "monthly", "by_weekday": ["fr"], "by_set_pos": -1}),
    ],
)
def test_the_repeat_shorthand(spec, repeat):
    assert parse_repeat(spec) == repeat


@pytest.mark.parametrize("spec", ["every:2", "every:2d:sa", "monthly:5xx", "weekly:monday"])
def test_a_repeat_shorthand_that_says_nothing_known_is_refused(spec):
    with pytest.raises(ScheduleCommandError):
        parse_repeat(spec)


@pytest.mark.parametrize(
    "text,repeat",
    [
        ("FREQ=WEEKLY;BYDAY=MO,FR;INTERVAL=2", {"freq": "weekly", "by_weekday": ["mo", "fr"], "interval": 2}),
        ("RRULE:FREQ=MONTHLY;BYDAY=-1FR", {"freq": "monthly", "by_weekday": ["fr"], "by_set_pos": -1}),
        ("FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=6", {"freq": "monthly", "by_month_day": [-1], "count": 6}),
        ("FREQ=DAILY;BYMONTH=10,11;UNTIL=20270331T000000Z",
         {"freq": "daily", "by_month": [10, 11], "until": "2027-03-31"}),
    ],
)
def test_an_rrule_within_the_subset_is_read(text, repeat):
    assert parse_rrule(text) == repeat


@pytest.mark.parametrize(
    "text,message",
    [
        ("FREQ=DAILY;BYHOUR=6", "BYHOUR is not supported"),
        ("FREQ=WEEKLY;WKST=SU", "starting on Monday"),
        ("FREQ=MONTHLY;BYDAY=1MO,2TU", "with a position takes one weekday"),
        ("FREQ", "is not KEY=VALUE"),
    ],
)
def test_an_rrule_outside_the_subset_is_refused(text, message):
    with pytest.raises(ScheduleCommandError, match=message):
        parse_rrule(text)


@pytest.mark.parametrize(
    "text,months",
    [("10-3", [10, 11, 12, 1, 2, 3]), ("4-9", [4, 5, 6, 7, 8, 9]), ("11,12,1", [11, 12, 1])],
)
def test_the_month_filter_shorthand(text, months):
    assert parse_months(text) == months


@pytest.mark.parametrize("text", ["13-3", "winter", "0,1"])
def test_a_month_filter_outside_the_year_is_refused(text):
    with pytest.raises(ScheduleCommandError):
        parse_months(text)


def test_emsctl_stores_the_devices_serial_with_the_rule(cli):
    cli("add", "--name", "Evening socket", "--device", "WR1", "--action", "offgrid_socket", "--value", "standard",
        "--at", "23:00")

    assert _only_rule(cli).identity == "SN-TEST-0001"


def test_switching_a_rule_on_runs_the_save_check(cli):
    cli("add", "--name", "A", "--device", "WR1", "--action", "offgrid_socket", "--value", "standard",
        "--from", "18:00", "--to", "23:00", "--end-value", "off", "--repeat", "daily", "--start", "2026-10-19")
    cli("add", "--name", "B", "--device", "WR1", "--action", "offgrid_socket", "--value", "eco",
        "--from", "20:00", "--to", "22:00", "--end-value", "off", "--repeat", "daily", "--start", "2026-10-19",
        "--disabled")
    second = cli.store.load().rules[1]

    with pytest.raises(ScheduleCommandError, match="overlaps 'A'"):
        cli("enable", second.id)
    assert cli.store.load().rule(second.id).enabled is False


def test_editing_a_switch_point_into_half_a_window_is_refused_with_a_reason(cli):
    cli("add", "--name", "Morning", "--system", "--action", "total_output_limit", "--value", "800",
        "--at", "18:30", "--repeat", "daily")
    rule = _only_rule(cli)

    with pytest.raises(ScheduleCommandError, match="a window needs both --from and --to"):
        cli("edit", rule.id, "--from", "19:00")


def test_emsctl_says_why_it_cannot_schedule_under_a_posix_tz(tmp_path):
    parser = argparse.ArgumentParser(prog="emsctl")
    add_schedule_parser(parser.add_subparsers(dest="command"))

    with pytest.raises(ScheduleCommandError, match="POSIX rule string"):
        handle_schedule_command(parser.parse_args(["schedule", "list"]),
                                store=ScheduleStore(tmp_path / "schedules.json"), devices={},
                                validation_context={}, zone=None, now=NOW, out=print)


@pytest.mark.parametrize(
    "status,known,key",
    [
        ({"schedule": {"zone": "America/New_York"}}, True, "America/New_York"),
        ({"schedule": {"zone": "UTC"}}, True, None),
        ({"schedule": {"zone": "CET-1"}}, True, None),
        ({"schedule": {"zone": "/usr/share/zoneinfo/Europe/Berlin"}}, True, "Europe/Berlin"),
        ({"schedule": {"zone": "/nonexistent/zone"}}, False, None),
        ({"schedule": {"zone": "unsupported"}}, True, None),
        ({}, False, None),
        (None, False, None),
    ],
)
def test_emsctl_reads_times_in_the_zone_the_running_ems_reports(status, known, key):
    from ems.schedule_cli import zone_of_running_ems

    found, zone = zone_of_running_ems(status)

    assert found is known
    assert getattr(zone, "key", None) == key


def test_a_zone_the_running_ems_reports_as_a_fixed_offset_reads_back_as_that_offset():
    from ems.schedule_cli import zone_of_running_ems

    found, zone = zone_of_running_ems({"schedule": {"zone": "CET-1"}})

    assert found is True
    assert zone.utcoffset(datetime(2026, 7, 1)) == zone.utcoffset(datetime(2026, 1, 1))
    assert zone.utcoffset(datetime(2026, 7, 1)).total_seconds() == 3600


def test_moving_a_windows_start_past_its_end_is_refused_not_turned_into_a_day(cli):
    cli("add", "--name", "Early", "--system", "--action", "total_output_limit", "--value", "400",
        "--from", "05:00", "--to", "06:00", "--end-value", "800", "--repeat", "daily", "--start", "2026-10-19")
    rule = _only_rule(cli)

    with pytest.raises(ScheduleCommandError, match="end at 2026-10-19 06:00, not after its start at 2026-10-19 07:00"):
        cli("edit", rule.id, "--from", "07:00")
    cli("edit", rule.id, "--from", "05:30")

    changed = cli.store.load().rule(rule.id)
    assert (changed.when.start, changed.when.end) == (datetime(2026, 10, 19, 5, 30), datetime(2026, 10, 19, 6, 0))


def test_count_and_until_together_are_refused_not_one_dropped(cli):
    with pytest.raises(ScheduleCommandError, match="by --until or by --count, not both"):
        cli("add", "--name", "Morning", "--system", "--action", "total_output_limit", "--value", "800",
            "--at", "06:00", "--repeat", "daily", "--count", "5", "--until", "2027-03-31")


def test_disabling_a_rule_on_a_device_not_in_config_keeps_its_serial(cli):
    rule = cli.store.create({
        "name": "Garage socket",
        "target": {"scope": "device", "device": "Garage", "identity": "SN-GARAGE-0001"},
        "action": {"id": "offgrid_socket", "value": "standard"},
        "when": {"start": "2026-10-19T18:00"},
    }, by="emsctl")

    cli("disable", rule.id)

    assert cli.store.load().rule(rule.id).identity == "SN-GARAGE-0001"


@pytest.mark.parametrize("command", [["pause"], ["resume"], ["status"]])
def test_without_a_zone_the_schedule_can_still_be_paused_and_inspected(tmp_path, command):
    parser = argparse.ArgumentParser(prog="emsctl")
    add_schedule_parser(parser.add_subparsers(dest="command"))

    code = handle_schedule_command(parser.parse_args(["schedule", *command]),
                                   store=ScheduleStore(tmp_path / "schedules.json"), devices={},
                                   validation_context={}, zone=None, now=NOW, out=lambda line: None)

    assert code == 0


def test_editing_the_time_of_a_switch_point_keeps_its_date(cli):
    cli("add", "--name", "Christmas", "--system", "--action", "total_output_limit", "--value", "400",
        "--at", "06:00", "--start", "2026-12-24")
    rule = _only_rule(cli)

    cli("edit", rule.id, "--at", "07:00")

    assert cli.store.load().rule(rule.id).when.start == datetime(2026, 12, 24, 7, 0)


@pytest.mark.parametrize("spec", ["FREQ=MONTHLY;BYDAY=2MO,2WE", "FREQ=MONTHLY;BYDAY=1MO,FR"])
def test_an_rrule_with_a_position_on_several_weekdays_is_refused_not_misread(spec):
    with pytest.raises(ScheduleCommandError, match="with a position takes one weekday"):
        parse_rrule(spec)


def test_every_rule_id_the_help_and_the_docs_show_is_one_a_rule_can_have():
    root = Path(__file__).resolve().parent.parent
    texts = [Path(schedule_cli.__file__).read_text(), (root / "docs" / "scheduler.md").read_text(),
             (root / "docs" / "cli.md").read_text()]
    shown = {match for text in texts for match in re.findall(r"\br_[0-9a-z]+\b", text)}

    assert shown and all(RULE_ID.match(rule_id) for rule_id in shown), shown


@pytest.mark.parametrize(
    "edit,start,end",
    [
        (["--from", "01:00"], datetime(2026, 10, 20, 1, 0), datetime(2026, 10, 20, 2, 0)),
        (["--to", "03:00"], datetime(2026, 10, 19, 22, 0), datetime(2026, 10, 20, 3, 0)),
        (["--start", "2026-10-21", "--from", "21:00"], datetime(2026, 10, 21, 21, 0), datetime(2026, 10, 22, 2, 0)),
        (["--start", "2026-10-21", "--to", "01:00"], datetime(2026, 10, 21, 22, 0), datetime(2026, 10, 22, 1, 0)),
    ],
    ids=["from_alone", "to_alone", "start_and_from", "start_and_to"],
)
def test_one_edge_of_a_night_window_moves_only_that_edge(cli, edit, start, end):
    cli("add", "--name", "Night", "--system", "--action", "total_output_limit", "--value", "400",
        "--from", "22:00", "--to", "02:00", "--end-value", "800", "--start", "2026-10-19")
    rule = _only_rule(cli)

    cli("edit", rule.id, *edit)

    changed = cli.store.load().rule(rule.id).when
    assert (changed.start, changed.end) == (start, end)


def test_a_fortnights_window_keeps_its_length_when_its_end_time_moves(cli):
    cli("add", "--name", "Trip", "--system", "--action", "total_output_limit", "--value", "100",
        "--from", "2026-11-01T00:00", "--to", "2026-11-15T00:00", "--end-value", "800")
    rule = _only_rule(cli)

    cli("edit", rule.id, "--to", "12:00")

    assert cli.store.load().rule(rule.id).when.end == datetime(2026, 11, 15, 12, 0)
