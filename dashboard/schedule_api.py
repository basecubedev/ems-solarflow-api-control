# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dashboard's /api/schedule routes, apart from HTTP and authentication.

Every answer comes from the EMS's own scheduler modules: the save check, the
sentence a rule reads as, its next switch points and the clash with another
window. The browser renders them and decides none of them.
"""

from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, unquote

from ems.file_io import lock_timed_out
from ems.runtime_commands import RuntimeWriteError, effective_limits, emsctl_line, run_commands, validate_command
from ems.schedule_actions import ACTIONS, CHARGE, GROUP_LABELS, action_commands
from ems.schedule_model import ScheduleRule, ScheduleRuleError, rule_payload
from ems.schedule_recurrence import PRESETS, ScheduleTimeError, occurrences, parse_day, preset_repeat
from ems.schedule_store import ScheduleConflict, ScheduleNotFound, ScheduleStoreError
from ems.schedule_text import describe_action, describe_value, describe_when
from ems.scheduler import (
    FUTURE_TOLERANCE,
    ZONE_UNSUPPORTED,
    check_rule,
    clock_trusted,
    next_occurrence_day,
    next_switch_point,
    point_view,
    slot_of,
    switch_points,
    upcoming_switch_points,
    with_device_identity,
)

PREVIEW_DAYS = 7
PREVIEW_POINTS = 10
RULE_STATUSES = ("waiting", "paused", "orphaned", "refused", "finished", "broken")


class RuntimeStateUnwritable(Exception):
    """The runtime state could not be saved -- a full disk, a read-only file --
    as opposed to the schedules file."""


class ScheduleApi:
    def __init__(self, store, *, runtime_state, devices, validation_context, zone, clock, scheduler=None):
        self.store = store
        self.runtime_state = runtime_state
        self.devices = devices
        self.validation_context = validation_context
        self.zone = zone
        self.clock = clock
        self.scheduler = scheduler

    def handle(self, method, path, body=None, query=""):
        """``(status, payload)`` for one request under /api/schedule."""

        if self.zone is None and not _zoneless(method, path, body):
            return 503, {"error": "schedule_zone_unsupported", "message": ZONE_UNSUPPORTED}
        try:
            return self._route(method, path, body if isinstance(body, dict) else {}, parse_qs(query))
        except (ScheduleRuleError, ScheduleTimeError) as exc:
            return 400, {"error": "invalid_schedule_rule", "message": str(exc)}
        except ScheduleConflict as exc:
            return 409, {"error": "schedule_conflict", "message": str(exc)}
        except ScheduleNotFound as exc:
            return 404, {"error": "schedule_rule_not_found", "message": str(exc)}
        except (ScheduleStoreError, RuntimeWriteError) as exc:
            if lock_timed_out(exc):
                return 503, {"error": "schedule_busy", "message": f"{exc}; try again"}
            if isinstance(exc, ScheduleStoreError):
                return 503, {"error": "schedule_store_unreadable", "message": str(exc)}
            return 400, {"error": "invalid_runtime_update", "message": str(exc)}
        except RuntimeStateUnwritable as exc:
            return 503, {"error": "runtime_state_unwritable", "message": str(exc)}
        except OSError as exc:
            return 503, {"error": "schedule_store_unwritable", "message": str(exc)}

    def _route(self, method, path, body, query):
        parts = [unquote(part) for part in path.removeprefix("/api/schedule").split("/") if part]
        if not parts:
            if method == "GET":
                return 200, self.overview()
            if method == "PATCH":
                if set(body) != {"enabled"}:
                    raise ScheduleRuleError("send exactly {\"enabled\": true|false}")
                self.store.set_enabled(body["enabled"])
                return 200, self.overview()
        elif parts == ["preview"] and method == "POST":
            return 200, self.preview(body)
        elif parts[0] == "rules":
            if len(parts) == 1 and method == "POST":
                rule = self._save(body.get("rule", body), rule_id=None, revision=None)
                return 201, self._rule_view(rule, self.store.load(), self.devices())
            if len(parts) >= 2:
                return self._rule_route(method, parts[1], parts[2:], body, query)
        return 404, {"error": "not_found"}

    def _rule_route(self, method, rule_id, rest, body, query):
        if not rest and method == "PUT":
            rule = self._save(body.get("rule"), rule_id=rule_id, revision=_revision(body.get("revision")))
            return 200, self._rule_view(rule, self.store.load(), self.devices())
        if not rest and method == "DELETE":
            revision = (query.get("revision") or [None])[0]
            self.store.delete(rule_id, revision=None if revision in (None, "") else _revision(revision))
            return 200, {"deleted": rule_id}
        if rest == ["move"] and method == "POST":
            self.store.move(rule_id, body.get("direction"))
            return 200, self.overview()
        if rest == ["skip"] and method == "POST":
            rule = self._valid(rule_id)
            day = parse_day(body.get("date"), "date") if body.get("date") else next_occurrence_day(rule, self.zone, self.clock())
            if day is None:
                raise ScheduleRuleError(f"{rule.id} has no coming occurrence to skip")
            skipped = self.store.skip(rule_id, day, revision=_revision(body.get("revision")), by="dashboard")
            return 200, self._rule_view(skipped, self.store.load(), self.devices())
        if rest == ["apply-now"] and method == "POST":
            rule = self._valid(rule_id)
            if not rule.enabled or not self.store.load().enabled:
                raise ScheduleRuleError("switch the rule on, and the schedule, to apply it now")
            return 200, self.apply_now(rule)
        return 404, {"error": "not_found"}

    def overview(self):
        now = self.clock()
        book = self.store.load()
        status = self.scheduler.status(now) if self.scheduler is not None else {}
        devices = self.devices()
        context = self.validation_context()
        upcoming = [] if self.zone is None else upcoming_switch_points(
            book, self.zone, now, until=now + timedelta(days=PREVIEW_DAYS))[:PREVIEW_POINTS]
        return {
            "enabled": book.enabled,
            "zone": "unsupported" if self.zone is None else str(getattr(self.zone, "key", None) or self.zone),
            "zone_offset_minutes": None if self.zone is None else _offset_minutes(now, self.zone),
            "zone_message": ZONE_UNSUPPORTED if self.zone is None else None,
            "clock_trusted": clock_trusted(now),
            "rules": [self._rule_view(rule, book, devices, status.get("refused")) for rule in book.rules],
            "next": [point_view(point, self.zone) for point in upcoming],
            "applied": status.get("applied", {}),
            "groups": [{"id": group, "label": label} for group, label in GROUP_LABELS.items()],
            "actions": self.catalogue(devices, context),
            "presets": {name: preset_repeat(name) for name in PRESETS},
            "devices": sorted(devices),
            "limits": effective_limits(context),
        }

    def catalogue(self, devices, context):
        unsupported = set(effective_limits(context)["ac_role_unsupported"])
        devices = sorted(devices)
        return [
            {
                "id": action.id,
                "group": action.group,
                "label": action.label,
                "help": action.help,
                "value_type": action.value_type,
                "choices": list(action.choices),
                "per_device": action.per_device,
                "devices": [
                    name for name in devices
                    if action.value_type != CHARGE or name not in unsupported
                ] if action.per_device else [],
            }
            for action in ACTIONS
        ]

    def preview(self, body):
        """What a draft rule would do once the schedule runs; writes nothing."""

        now = self.clock()
        book = self.store.load()
        rule_id = body.get("id")
        payload = body.get("rule")
        try:
            rule = check_rule(payload, book, rule_id=rule_id, validation_context=self.validation_context(),
                              devices=self.devices(), zone=self.zone, now=now)
        except (ScheduleRuleError, ScheduleTimeError) as exc:
            return {"ok": False, "message": str(exc)}
        rules = list(book.rules)
        index = next((position for position, entry in enumerate(rules) if entry.id == rule_id), None)
        if index is None:
            rules.append(rule)
        else:
            rules[index] = rule
        draft_book = replace(book, rules=tuple(rules), enabled=True)
        until = now + timedelta(days=PREVIEW_DAYS)
        mine = [point for point in switch_points(rule, self.zone, after=now, until=until)][:PREVIEW_POINTS]
        timeline = [
            point_view(point, self.zone)
            for point in upcoming_switch_points(draft_book, self.zone, now, until=until)
            if point.slot == slot_of(rule)
        ]
        return {
            "ok": True,
            "summary": describe_when(rule.when),
            "action": describe_action(rule),
            "equals": [_emsctl_line(rule, value) for value in _values(rule)],
            "next": [point_view(point, self.zone) for point in mine],
            "timeline": timeline,
            "first_action": mine[0].at.astimezone(self.zone).isoformat() if mine else None,
        }

    def apply_now(self, rule):
        """Set what the rule says for this moment, once, as a person would.

        It is recorded as the scheduler records a switch point: inside a
        window as that window's start, so the window still ends -- at its
        time, or at once when the rule goes away -- and a charge started here
        does not run on without it. The value is checked and recorded before
        it is written, so a refusal or a busy store changes nothing; a runtime
        write that fails after that leaves the record, and the window, if one,
        ends with its end value. Under a clock stepped back it records no
        earlier than the setting's last record, so nothing fires twice.
        """

        now = self.clock()
        slot = slot_of(rule)
        recorded = self.store.load().applied.get(slot)
        if recorded is not None and now < recorded["at"] <= now + FUTURE_TOLERANCE:
            now = recorded["at"]
        value = rule.value
        window = None
        if rule.is_window:
            value = rule.end_value
            for occurrence in occurrences(rule.when, self.zone, since=now, until=now):
                if occurrence.start <= now < occurrence.end:
                    value = rule.value
                    window = {"action": rule.action.id, "device": rule.device,
                              "end_value": rule.end_value, "ends_at": occurrence.end}
        commands = action_commands(rule.action, value)
        context = self.validation_context()
        for command, argument in commands:
            validate_command(rule.action.scope, command, argument, device=rule.device, validation_context=context)
        self.store.record_applied(slot, rule_id=rule.id, at=now, value=value, window=window)
        try:
            run_commands(self.runtime_state, rule.action.scope, commands,
                         device=rule.device, validation_context=context, source="dashboard")
        except OSError as exc:
            raise RuntimeStateUnwritable(f"cannot save the runtime state: {exc}") from exc
        return {"applied": rule.id, "value": value, "text": describe_value(rule.action, value)}

    def _save(self, payload, *, rule_id, revision):
        now = self.clock()
        devices = self.devices()
        previous = None
        if rule_id is not None:
            previous = next((rule for rule in self.store.load().rules if rule.id == rule_id), None)
        payload = with_device_identity(payload, devices, previous)

        def check(book):
            check_rule(payload, book, rule_id=rule_id, validation_context=self.validation_context(),
                       devices=devices, zone=self.zone, now=now)

        if rule_id is None:
            return self.store.create(payload, by="dashboard", check=check)
        return self.store.replace(rule_id, payload, revision=revision, by="dashboard", check=check)

    def _valid(self, rule_id):
        rule = self.store.load().rule(rule_id)
        if not isinstance(rule, ScheduleRule):
            raise ScheduleRuleError(f"{rule_id} is broken ({rule.error}); delete it")
        return rule

    def _rule_view(self, rule, book, devices, refused=None):
        """A rule's card; ``refused`` is what the running scheduler could not
        write, by rule id, which the card names while the rule is unchanged."""

        if not isinstance(rule, ScheduleRule):
            return {"id": rule.id, "status": "broken", "message": rule.error}
        now = self.clock()
        status = "waiting"
        message = None
        if not rule.enabled or not book.enabled or self.zone is None:
            status = "paused"
        elif rule.device is not None and rule.device not in devices:
            status = "orphaned"
        refusal = (refused or {}).get(rule.id)
        if status == "waiting" and refusal and refusal.get("revision") == rule.revision:
            status, message = "refused", refusal.get("message")
        upcoming = next_switch_point(rule, self.zone, now) if self.zone is not None else None
        if upcoming is None and status == "waiting":
            status = "finished"
        view = {
            **rule_payload(rule),
            "status": status,
            "summary": describe_when(rule.when),
            "action_summary": describe_action(rule),
            "next": point_view(upcoming, self.zone) if upcoming else None,
        }
        if message:
            view["message"] = f"Not written: {message}"
        return view


def _offset_minutes(now, zone):
    """The zone's offset from UTC at ``now``, which the browser holds its own
    against to say when rule times are not the viewer's."""

    return int(now.astimezone(zone).utcoffset().total_seconds() // 60)


def _zoneless(method, path, body):
    """Whether a route reads no time, so it still works without a zone the
    scheduler can follow: list, pause or resume, move, delete, and save a
    rule switched off, which is checked for its shape only."""

    parts = [part for part in path.removeprefix("/api/schedule").split("/") if part]
    if not parts:
        return method in ("GET", "PATCH")
    if parts[0] != "rules" or len(parts) < 2:
        return False
    rule = body.get("rule") if isinstance(body, dict) else None
    switched_off = method == "PUT" and len(parts) == 2 and isinstance(rule, dict) and rule.get("enabled") is False
    return switched_off or (len(parts) == 2 and method == "DELETE") or (parts[2:] == ["move"] and method == "POST")


def _values(rule):
    return (rule.value, rule.end_value) if rule.is_window else (rule.value,)


def _emsctl_line(rule, value):
    return " && ".join(
        emsctl_line(rule.action.scope, command, argument, device=rule.device)
        for command, argument in action_commands(rule.action, value)
    )


def _revision(value):
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleRuleError("revision must be the rule's revision number") from exc
    if revision < 1:
        raise ScheduleRuleError("revision must be the rule's revision number")
    return revision
