# SPDX-License-Identifier: AGPL-3.0-or-later
"""``emsctl schedule``: list, add, change and preview schedule rules.

The rules are written through the store and checked by the same save check as
the dashboard's; what a rule then does is applied by the running EMS, not by
this command. Import-side-effect-free.
"""

import argparse
import json
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ems.schedule_actions import ACTIONS, CHARGE, CHOICE, FACTOR, SWITCH, WATTS, schedule_action
from ems.schedule_model import EDITABLE_FIELDS, ScheduleRule, ScheduleRuleError, rule_payload
from ems.schedule_recurrence import WEEKDAYS, ScheduleTimeError, parse_day, parse_local, preset_repeat
from ems.schedule_store import ScheduleStoreError
from ems.schedule_text import DAY_NAMES, MONTH_NAMES, describe_action, describe_value, describe_when
from ems.scheduler import (
    ZONE_UNSUPPORTED,
    check_rule,
    fixed_offset_zone,
    local_zone,
    next_occurrence_day,
    next_switch_point,
    slot_of,
    upcoming_switch_points,
    with_device_identity,
)

REPEAT_HELP = (
    "once, daily, weekdays, weekends, sundays, weekly[:mo,we], every:2d, "
    "every:2w[:sa,su], monthly[:15|:last|:2su|:lastfr], yearly"
)
EXAMPLES = """\
Examples:
  python3 emsctl.py schedule list
  python3 emsctl.py schedule add --name "Evening socket" --device WR1 --action offgrid_socket \\
      --value standard --from 18:00 --to 23:00 --end-value off --repeat weekdays
  python3 emsctl.py schedule add --name "Morning boost" --system --action total_output_limit \\
      --value 1000 --at 06:00 --repeat weekdays
  python3 emsctl.py schedule add --name Vacation --system --action total_output_limit \\
      --value 200 --from 2026-08-01T00:00 --to 2026-08-15T00:00 --end-value 800
  python3 emsctl.py schedule add --name "Cheap night" --device WR1 --action charge_from_grid \\
      --value 600 --from 02:00 --to 05:00 --end-value stop --repeat every:2d --months 10-3
  python3 emsctl.py schedule skip r_4f9c2a1b --date 2026-12-24
  python3 emsctl.py schedule preview --days 7
  python3 emsctl.py schedule pause
"""


class ScheduleCommandError(ValueError):
    pass


def zone_of_running_ems(control_status):
    """``(known, zone)``: the zone the running EMS reports it fires rules in.

    emsctl may run on the host while the EMS runs in a container with its own
    TZ; times are read and checked in the EMS's zone whenever it reported one.
    """

    schedule = (control_status or {}).get("schedule") if isinstance(control_status, dict) else None
    name = schedule.get("zone") if isinstance(schedule, dict) else None
    if not isinstance(name, str) or not name:
        return False, None
    if name == "unsupported":
        return True, None
    if name == "UTC":
        return True, timezone.utc
    if name.startswith("/"):
        return (True, local_zone({"TZ": name})) if os.path.isfile(name) else (False, None)
    try:
        return True, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        fixed = fixed_offset_zone(name)
        return (True, fixed) if fixed is not None else (False, None)


def add_schedule_parser(subparsers, formatter_class=argparse.RawDescriptionHelpFormatter):
    schedule = subparsers.add_parser(
        "schedule",
        help="List, add, change and preview schedule rules.",
        description=(
            "Schedule rules set runtime values at given times, exactly as the matching "
            "emsctl command would. The running EMS applies them."
        ),
        epilog=EXAMPLES + "\nActions: " + ", ".join(action.id for action in ACTIONS) + "\n",
        formatter_class=formatter_class,
    )
    commands = schedule.add_subparsers(dest="schedule_command", required=True)
    commands.add_parser("list", help="List the rules in their order.")
    commands.add_parser("status", help="What the running EMS last applied and does next.")
    commands.add_parser("pause", help="Pause every rule.")
    commands.add_parser("resume", help="Resume the rules; nothing from the pause fires late.")
    preview = commands.add_parser("preview", help="The switch points of the coming days.")
    preview.add_argument("rule", nargs="?", help="Only this rule.")
    preview.add_argument("--days", type=int, default=7)
    for name in ("add", "edit"):
        parser = commands.add_parser(name, help=f"{name.capitalize()} a rule.")
        if name == "edit":
            parser.add_argument("rule", help="The rule id, for example r_4f9c2a1b.")
        _rule_options(parser)
    for name in ("show", "remove", "enable", "disable"):
        commands.add_parser(name, help=f"{name.capitalize()} a rule.").add_argument("rule")
    move = commands.add_parser("move", help="Move a rule up or down; the order decides ties.")
    move.add_argument("rule")
    move.add_argument("direction", choices=("up", "down"))
    skip = commands.add_parser("skip", help="Skip one date of a rule.")
    skip.add_argument("rule")
    skip.add_argument("--date", help="The date to skip (default: the next occurrence).")
    return schedule


def _rule_options(parser):
    parser.add_argument("--name")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--device", help="The device the rule sets.")
    target.add_argument("--system", action="store_true", help="The rule sets a system value.")
    parser.add_argument("--action", choices=[action.id for action in ACTIONS])
    parser.add_argument("--value", help="on/off, watts, a factor, a mode, or stop.")
    parser.add_argument("--end-value", help="The value a window sets when it ends.")
    parser.add_argument("--at", help="A single switch point: HH:MM or YYYY-MM-DDTHH:MM.")
    parser.add_argument("--from", dest="window_from", help="A window's start: HH:MM or YYYY-MM-DDTHH:MM.")
    parser.add_argument("--to", dest="window_to", help="A window's end; earlier than --from means the next day.")
    parser.add_argument("--start", help="The first date, YYYY-MM-DD (default: today).")
    parser.add_argument("--repeat", help=REPEAT_HELP)
    parser.add_argument("--rrule", help="An RFC 5545 RRULE within the supported subset.")
    parser.add_argument("--months", help="Only in these months: 10-3 or 11,12,1.")
    parser.add_argument("--count", type=int, help="End after this many occurrences.")
    parser.add_argument("--until", help="End on this date, YYYY-MM-DD.")
    parser.add_argument("--disabled", action="store_true", help="Save the rule switched off.")


ZONELESS_COMMANDS = ("status", "pause", "resume", "show", "remove", "disable", "move")


def handle_schedule_command(args, *, store, devices, validation_context, zone, now, control_status=None, out=print):
    """Run one ``emsctl schedule`` command. Without a zone the scheduler can
    follow only the commands that read no time run, so a schedule that fires
    nothing can still be paused, inspected and cleaned up."""

    command = args.schedule_command
    if zone is None and command not in ZONELESS_COMMANDS:
        raise ScheduleCommandError(ZONE_UNSUPPORTED)
    try:
        if command == "list":
            return _list(store, zone, now, out)
        if command == "status":
            return _status(control_status, out)
        if command == "pause":
            store.set_enabled(False)
            out("schedules paused")
            return 0
        if command == "resume":
            store.set_enabled(True)
            out("schedules resumed")
            return 0
        if command == "preview":
            return _preview(store, zone, now, args.days, args.rule, out)
        book = store.load()
        def checked(payload, rule_id=None):
            return lambda current: check_rule(payload, current, rule_id=rule_id, validation_context=validation_context,
                                              devices=devices, zone=zone, now=now)

        if command == "add":
            payload = with_device_identity(rule_from_args(args, zone=zone, now=now), devices)
            rule = store.create(payload, by="emsctl", check=checked(payload))
            out(f"added {rule.id}: {describe_when(rule.when)} · {describe_action(rule)}")
            return 0
        rule = book.rule(args.rule)
        if not isinstance(rule, ScheduleRule):
            if command != "remove":
                raise ScheduleCommandError(f"{args.rule} is broken ({rule.error}); remove it")
            store.delete(args.rule, revision=None)
            out(f"removed {args.rule}")
            return 0
        if command == "show":
            out(json.dumps({**rule_payload(rule), "summary": _summary(rule)}, indent=2, ensure_ascii=False))
            return 0
        if command == "remove":
            store.delete(rule.id, revision=rule.revision)
            out(f"removed {rule.id}")
            return 0
        if command in ("enable", "disable"):
            payload = with_device_identity({**_editable(rule), "enabled": command == "enable"}, devices, rule)
            changed = store.replace(rule.id, payload, revision=rule.revision, by="emsctl",
                                    check=checked(payload, rule.id))
            out(f"{command}d {changed.id}")
            return 0
        if command == "move":
            store.move(rule.id, args.direction)
            out(f"moved {rule.id} {args.direction}")
            return 0
        if command == "skip":
            day = parse_day(args.date, "--date") if args.date else next_occurrence_day(rule, zone, now)
            if day is None:
                raise ScheduleCommandError(f"{rule.id} has no coming occurrence to skip")
            store.skip(rule.id, day, revision=rule.revision, by="emsctl")
            out(f"{rule.id} skips {day.isoformat()}")
            return 0
        if command == "edit":
            payload = with_device_identity(rule_from_args(args, zone=zone, now=now, base=_editable(rule)), devices,
                                           rule)
            changed = store.replace(rule.id, payload, revision=rule.revision, by="emsctl",
                                    check=checked(payload, rule.id))
            out(f"changed {changed.id}: {describe_when(changed.when)} · {describe_action(changed)}")
            return 0
    except (ScheduleRuleError, ScheduleTimeError, ScheduleStoreError) as exc:
        raise ScheduleCommandError(str(exc)) from exc
    raise ScheduleCommandError(f"unknown schedule command {command}")


def rule_from_args(args, *, zone, now, base=None):
    """The editor payload ``emsctl schedule add/edit`` describes."""

    payload = json.loads(json.dumps(base)) if base else {}
    if args.name is not None:
        payload["name"] = args.name
    if base is None and not args.name:
        raise ScheduleCommandError("--name is required")
    if args.disabled:
        payload["enabled"] = False
    action_id = args.action or (payload.get("action") or {}).get("id")
    if not action_id:
        raise ScheduleCommandError("--action is required")
    action = schedule_action(action_id)
    if args.device is not None:
        payload["target"] = {"scope": "device", "device": args.device}
    elif args.system:
        payload["target"] = {"scope": "system"}
    elif base is None:
        payload["target"] = {"scope": "device" if action.per_device else "system"}
        if action.per_device:
            raise ScheduleCommandError(f"{action.id} needs --device")
    action_payload = dict(payload.get("action") or {})
    action_payload["id"] = action.id
    if args.value is not None:
        action_payload["value"] = parse_value(action, args.value)
    if args.end_value is not None:
        action_payload["end_value"] = parse_value(action, args.end_value)
    if "value" not in action_payload:
        raise ScheduleCommandError("--value is required")
    when = _when(args, base, zone, now)
    if when.get("end") is None:
        action_payload.pop("end_value", None)
    payload["action"] = action_payload
    payload["when"] = when
    return payload


def parse_value(action, text):
    word = str(text).strip()
    if action.value_type == SWITCH:
        if word.lower() not in ("on", "off"):
            raise ScheduleCommandError(f"{action.id} takes on or off")
        return word.lower() == "on"
    if action.value_type == CHARGE and word.lower() == "stop":
        return 0
    if action.value_type in (WATTS, CHARGE):
        if not re.fullmatch(r"\d+", word):
            raise ScheduleCommandError(f"{action.id} takes whole watts")
        return int(word)
    if action.value_type == FACTOR:
        try:
            return float(word)
        except ValueError as exc:
            raise ScheduleCommandError(f"{action.id} takes a number") from exc
    if action.value_type == CHOICE:
        return word.lower()
    return word


def parse_repeat(spec):
    """An editor repeat from ``--repeat``; None for ``once``."""

    spec = (spec or "once").strip().lower()
    head, _, rest = spec.partition(":")
    if head in ("once", "daily", "weekdays", "weekends", "sundays", "yearly") and not rest:
        return preset_repeat(head)
    if head == "weekly":
        return {"freq": "weekly", **({"by_weekday": _days(rest)} if rest else {})}
    if head == "every":
        match = re.fullmatch(r"(\d+)([dw])(?::([a-z,]+))?", rest)
        if not match:
            raise ScheduleCommandError(f"--repeat every:{rest} is not every:2d or every:2w[:sa,su]")
        repeat = {"freq": "daily" if match.group(2) == "d" else "weekly", "interval": int(match.group(1))}
        if match.group(3):
            if match.group(2) == "d":
                raise ScheduleCommandError("weekdays go with every:Nw, not every:Nd")
            repeat["by_weekday"] = _days(match.group(3))
        return repeat
    if head == "monthly":
        if not rest:
            return {"freq": "monthly"}
        if rest == "last":
            return {"freq": "monthly", "by_month_day": [-1]}
        if re.fullmatch(r"\d{1,2}", rest):
            return {"freq": "monthly", "by_month_day": [int(rest)]}
        match = re.fullmatch(r"(1|2|3|4|last|-1)(mo|tu|we|th|fr|sa|su)", rest)
        if match:
            position = -1 if match.group(1) in ("last", "-1") else int(match.group(1))
            return {"freq": "monthly", "by_weekday": [match.group(2)], "by_set_pos": position}
    raise ScheduleCommandError(f"--repeat {spec} is not one of: {REPEAT_HELP}")


def parse_rrule(text):
    """An editor repeat from an RRULE, refused outside the supported subset."""

    parts = {}
    for item in str(text).strip().removeprefix("RRULE:").split(";"):
        key, _, value = item.partition("=")
        if not key or not value:
            raise ScheduleCommandError(f"--rrule part {item!r} is not KEY=VALUE")
        parts[key.upper()] = value.upper()
    unsupported = sorted(set(parts) - {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "UNTIL", "COUNT", "WKST"})
    if unsupported:
        raise ScheduleCommandError(f"--rrule {unsupported[0]} is not supported")
    if parts.get("WKST", "MO") != "MO":
        raise ScheduleCommandError("--rrule supports weeks starting on Monday only")
    repeat = {"freq": parts.get("FREQ", "").lower()}
    if "INTERVAL" in parts:
        repeat["interval"] = _whole(parts["INTERVAL"], "INTERVAL")
    if "BYDAY" in parts:
        days, positions = [], set()
        for item in parts["BYDAY"].split(","):
            match = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", item)
            if not match:
                raise ScheduleCommandError(f"--rrule BYDAY {item} is not supported")
            days.append(match.group(2).lower())
            if match.group(1):
                positions.add(int(match.group(1)))
        repeat["by_weekday"] = days
        if positions and len(days) > 1:
            raise ScheduleCommandError(
                "--rrule BYDAY with a position takes one weekday, as 2SU or -1FR"
            )
        if positions:
            repeat["by_set_pos"] = positions.pop()
    if "BYMONTHDAY" in parts:
        repeat["by_month_day"] = [_whole(item, "BYMONTHDAY", allow_last=True) for item in parts["BYMONTHDAY"].split(",")]
    if "BYMONTH" in parts:
        repeat["by_month"] = [_whole(item, "BYMONTH") for item in parts["BYMONTH"].split(",")]
    if "UNTIL" in parts:
        try:
            repeat["until"] = datetime.strptime(parts["UNTIL"][:8], "%Y%m%d").date().isoformat()
        except ValueError as exc:
            raise ScheduleCommandError("--rrule UNTIL must start with YYYYMMDD") from exc
    if "COUNT" in parts:
        repeat["count"] = _whole(parts["COUNT"], "COUNT")
    return repeat


def parse_months(text):
    text = str(text).strip()
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", text)
    if match:
        first, last = int(match.group(1)), int(match.group(2))
        if not (1 <= first <= 12 and 1 <= last <= 12):
            raise ScheduleCommandError("--months takes months from 1 to 12")
        return [(first - 1 + offset) % 12 + 1 for offset in range((last - first) % 12 + 1)]
    try:
        months = [int(item) for item in text.split(",")]
    except ValueError as exc:
        raise ScheduleCommandError("--months takes 10-3 or 11,12,1") from exc
    if any(not 1 <= month <= 12 for month in months):
        raise ScheduleCommandError("--months takes months from 1 to 12")
    return months


def _when(args, base, zone, now):
    when = dict((base or {}).get("when") or {})
    times = [args.at, args.window_from, args.window_to]
    if args.at and (args.window_from or args.window_to):
        raise ScheduleCommandError("use --at for a switch point or --from/--to for a window, not both")
    if bool(args.window_from) != bool(args.window_to) and (base is None or not when.get("end")):
        raise ScheduleCommandError("a window needs both --from and --to")
    today = now.astimezone(zone).date()
    start_day = parse_day(args.start, "--start") if args.start else None
    if args.at:
        day = start_day or (_local(when["start"]).date() if when.get("start") else today)
        when = {**{k: v for k, v in when.items() if k not in ("start", "end")}, "start": _moment(args.at, day)}
    elif args.window_from and args.window_to:
        old_start = _local(when["start"]) if when.get("start") else None
        start = _local(_moment(args.window_from, start_day or (old_start.date() if old_start else today)))
        end = _local(_moment(args.window_to, start.date()))
        if end <= start:
            end += timedelta(days=1)
        when["start"], when["end"] = _text(start), _text(end)
    elif args.window_from or args.window_to:
        old_start, old_end = _local(when["start"]), _local(when["end"])
        shift = datetime.combine(start_day, old_start.time()) - old_start if start_day else timedelta(0)
        start, end = old_start + shift, old_end + shift
        if args.window_from:
            start = _nearest(args.window_from, start, fixed_day=start_day)
        else:
            end = _nearest(args.window_to, end)
        if end <= start:
            raise ScheduleCommandError(
                f"the window would end at {end:%Y-%m-%d %H:%M}, not after its start at {start:%Y-%m-%d %H:%M}; "
                "give --from and --to"
            )
        when["start"], when["end"] = _text(start), _text(end)
    elif start_day is not None and when.get("start"):
        moved = datetime.combine(start_day, _local(when["start"]).time())
        shift = moved - _local(when["start"])
        when["start"] = moved.strftime("%Y-%m-%dT%H:%M")
        if when.get("end"):
            when["end"] = (_local(when["end"]) + shift).strftime("%Y-%m-%dT%H:%M")
    if not when.get("start") and not any(times):
        raise ScheduleCommandError("give --at or --from and --to")
    if args.repeat is not None or args.rrule is not None:
        if args.repeat is not None and args.rrule is not None:
            raise ScheduleCommandError("use --repeat or --rrule, not both")
        repeat = parse_rrule(args.rrule) if args.rrule else parse_repeat(args.repeat)
        when.pop("repeat", None)
        if repeat is not None:
            when["repeat"] = repeat
    repeat = when.get("repeat")
    if args.count is not None and args.until is not None:
        raise ScheduleCommandError("a repeat ends by --until or by --count, not both")
    for option, key, value in (("--months", "by_month", args.months), ("--count", "count", args.count),
                               ("--until", "until", args.until)):
        if value is None:
            continue
        if repeat is None:
            raise ScheduleCommandError(f"{option} needs a repeating rule")
        if key == "by_month":
            repeat[key] = parse_months(value)
        elif key == "until":
            repeat[key] = parse_day(value, "--until").isoformat()
            repeat.pop("count", None)
        else:
            repeat[key] = value
            repeat.pop("until", None)
    return when


def _moment(text, day):
    text = str(text).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hour, minute = (int(part) for part in text.split(":"))
        if hour > 23 or minute > 59:
            raise ScheduleCommandError(f"{text} is not a time of day")
        return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute).strftime("%Y-%m-%dT%H:%M")
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M").strftime("%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ScheduleCommandError(f"{text} is not HH:MM or YYYY-MM-DDTHH:MM") from exc


def _local(text):
    return parse_local(text, "start")


def _text(moment):
    return moment.strftime("%Y-%m-%dT%H:%M")


def _nearest(text, anchor, *, fixed_day=None):
    """The moment ``text`` names, for one window edge given alone.

    A time of day lands on ``fixed_day`` when --start names one, else on the
    day before, of or after ``anchor`` nearest to it: moving 22:00-02:00's
    start to 01:00 gives 01:00-02:00, moving its end to 03:00 gives
    22:00-03:00, and a fortnight's window keeps its length.
    """

    if fixed_day is not None or not re.fullmatch(r"\s*\d{1,2}:\d{2}\s*", str(text)):
        return _local(_moment(text, fixed_day or anchor.date()))
    candidates = [_local(_moment(text, anchor.date() + timedelta(days=offset))) for offset in (1, 0, -1)]
    return min(candidates, key=lambda moment: abs(moment - anchor))


def _days(text):
    days = [day for day in text.split(",") if day]
    if not days or any(day not in WEEKDAYS for day in days):
        raise ScheduleCommandError(f"weekdays are {','.join(WEEKDAYS)}")
    return days


def _whole(text, name, allow_last=False):
    if not re.fullmatch(r"-?\d+", text) or (text.startswith("-") and not (allow_last and text == "-1")):
        raise ScheduleCommandError(f"--rrule {name} {text} is not supported")
    return int(text)


def _editable(rule):
    payload = rule_payload(rule)
    return {key: payload[key] for key in EDITABLE_FIELDS if key in payload}


def _summary(rule):
    return f"{describe_when(rule.when)} · {describe_action(rule)}"


def _list(store, zone, now, out):
    book = store.load()
    out(f"schedules {'on' if book.enabled else 'paused'} · {getattr(zone, 'key', zone)}")
    if not book.rules:
        out("no rules")
        return 0
    for rule in book.rules:
        if not isinstance(rule, ScheduleRule):
            out(f"{rule.id or '(no id)'}  broken  {rule.error}")
            continue
        out(f"{rule.id}  {'on ' if rule.enabled else 'off'}  {rule.name}")
        out(f"            {describe_when(rule.when)}")
        out(f"            {describe_action(rule)}")
        mine = next_switch_point(rule, zone, now) if rule.enabled else None
        if mine is not None:
            out(f"            next: {_point_time(mine.at, zone)} → {describe_value(rule.action, mine.value)}")
    return 0


def _preview(store, zone, now, days, only, out):
    book = store.load()
    if not book.enabled:
        out("schedules are paused; nothing fires. After a resume:")
        book = replace(book, enabled=True)
    for point in upcoming_switch_points(book, zone, now, until=now + timedelta(days=days)):
        if only and point.rule.id != only:
            continue
        out(f"{_point_time(point.at, zone)}  {slot_of(point.rule)} → "
            f"{describe_value(point.rule.action, point.value)}  ({point.rule.name})")
    return 0


def _status(control_status, out):
    if not control_status or "schedule" not in control_status:
        out("no running EMS has reported its schedule recently")
        return 0
    out(json.dumps(control_status["schedule"], indent=2, ensure_ascii=False))
    return 0


def _point_time(moment, zone):
    local = moment.astimezone(zone)
    return f"{DAY_NAMES[local.weekday()]} {local.day} {MONTH_NAMES[local.month - 1]} {local:%H:%M}"
