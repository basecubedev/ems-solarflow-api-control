# SPDX-License-Identifier: AGPL-3.0-or-later
"""A schedule rule as the store keeps it, validated by its shape.

A rule targets the system or one device, names an action from the catalogue,
the value it sets and, for a window, the value it sets when the window ends,
and when it fires. Whether a value fits this installation's limits is the
command path's question, asked when the rule is saved and again when it fires.
Import-side-effect-free.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ems.schedule_actions import ScheduleActionError, action_commands, schedule_action
from ems.schedule_recurrence import ScheduleTimeError, parse_when, when_payload

NAME_MAX_LENGTH = 80
DEVICE_NAME_MAX_LENGTH = 64
EDITORS = ("dashboard", "emsctl")
RULE_ID = re.compile(r"^r_[0-9a-f]{8}$")
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
EDITABLE_FIELDS = ("name", "enabled", "target", "action", "when")
STORED_FIELDS = EDITABLE_FIELDS + ("id", "revision", "updated_at", "updated_by")


class ScheduleRuleError(ValueError):
    """A rule the store cannot keep; the message says why."""


@dataclass(frozen=True)
class ScheduleRule:
    id: str
    revision: int
    name: str
    enabled: bool
    action: object
    device: str | None
    identity: str | None
    value: object
    end_value: object
    when: object
    updated_at: datetime
    updated_by: str

    @property
    def is_window(self):
        return self.when.end is not None


def parse_editable(payload):
    """The fields an editor sends, validated, as a dict of parsed values."""

    if not isinstance(payload, dict):
        raise ScheduleRuleError("a rule must be an object")
    unknown = sorted(set(payload) - set(EDITABLE_FIELDS))
    if unknown:
        raise ScheduleRuleError(f"unknown rule field: {unknown[0]}")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_MAX_LENGTH:
        raise ScheduleRuleError(f"name must be 1 to {NAME_MAX_LENGTH} characters")
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ScheduleRuleError("enabled must be true or false")
    try:
        when = parse_when(payload.get("when"))
    except ScheduleTimeError as exc:
        raise ScheduleRuleError(str(exc)) from exc
    action, value, end_value = _action(payload.get("action"), when)
    device, identity = _target(payload.get("target"), action)
    return {
        "name": name.strip(),
        "enabled": enabled,
        "action": action,
        "device": device,
        "identity": identity,
        "value": value,
        "end_value": end_value,
        "when": when,
    }


def parse_rule(payload):
    """A stored rule, validated, as a :class:`ScheduleRule`."""

    if not isinstance(payload, dict):
        raise ScheduleRuleError("a rule must be an object")
    unknown = sorted(set(payload) - set(STORED_FIELDS))
    if unknown:
        raise ScheduleRuleError(f"unknown rule field: {unknown[0]}")
    rule_id = payload.get("id")
    if not isinstance(rule_id, str) or not RULE_ID.match(rule_id):
        raise ScheduleRuleError("id must look like r_0123abcd")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ScheduleRuleError("revision must be a whole number from 1")
    updated_by = payload.get("updated_by")
    if updated_by not in EDITORS:
        raise ScheduleRuleError(f"updated_by must be one of: {', '.join(EDITORS)}")
    updated_at = parse_utc(payload.get("updated_at"), "updated_at")
    fields = parse_editable({key: payload[key] for key in EDITABLE_FIELDS if key in payload})
    return ScheduleRule(
        id=rule_id,
        revision=revision,
        updated_at=updated_at,
        updated_by=updated_by,
        **fields,
    )


def rule_payload(rule):
    """The JSON form of a rule, the inverse of :func:`parse_rule`."""

    target = {"scope": "device" if rule.action.per_device else "system"}
    if rule.device is not None:
        target["device"] = rule.device
    if rule.identity is not None:
        target["identity"] = rule.identity
    action = {"id": rule.action.id, "value": rule.value}
    if rule.end_value is not None:
        action["end_value"] = rule.end_value
    return {
        "id": rule.id,
        "revision": rule.revision,
        "name": rule.name,
        "enabled": rule.enabled,
        "target": target,
        "action": action,
        "when": when_payload(rule.when),
        "updated_at": format_utc(rule.updated_at),
        "updated_by": rule.updated_by,
    }


def parse_utc(value, field):
    if not isinstance(value, str):
        raise ScheduleRuleError(f"{field} must be a UTC time like 2026-10-18T14:02:11Z")
    try:
        return datetime.strptime(value, UTC_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ScheduleRuleError(f"{field} must be a UTC time like 2026-10-18T14:02:11Z") from exc


def format_utc(moment):
    return moment.astimezone(timezone.utc).strftime(UTC_FORMAT)


def _action(payload, when):
    if not isinstance(payload, dict):
        raise ScheduleRuleError("action must be an object")
    unknown = sorted(set(payload) - {"id", "value", "end_value"})
    if unknown:
        raise ScheduleRuleError(f"unknown action field: {unknown[0]}")
    try:
        action = schedule_action(payload.get("id"))
        value = payload.get("value")
        action_commands(action, value)
        end_value = payload.get("end_value")
        if when.end is None:
            if end_value is not None:
                raise ScheduleRuleError("a single switch point has no end_value")
        else:
            if end_value is None:
                raise ScheduleRuleError("a window needs the end_value it sets when it ends")
            action_commands(action, end_value)
    except ScheduleActionError as exc:
        raise ScheduleRuleError(str(exc)) from exc
    return action, value, end_value


def _target(payload, action):
    if not isinstance(payload, dict):
        raise ScheduleRuleError("target must be an object")
    unknown = sorted(set(payload) - {"scope", "device", "identity"})
    if unknown:
        raise ScheduleRuleError(f"unknown target field: {unknown[0]}")
    scope = payload.get("scope")
    expected = "device" if action.per_device else "system"
    if scope != expected:
        raise ScheduleRuleError(f"{action.id} applies to the {expected}, not to {scope!r}")
    if scope == "system":
        if payload.get("device") is not None or payload.get("identity") is not None:
            raise ScheduleRuleError(f"{action.id} applies to the system and names no device")
        return None, None
    device = payload.get("device")
    if not isinstance(device, str) or not device.strip() or len(device) > DEVICE_NAME_MAX_LENGTH:
        raise ScheduleRuleError(f"device must be 1 to {DEVICE_NAME_MAX_LENGTH} characters")
    identity = payload.get("identity")
    if identity is not None and (not isinstance(identity, str) or not identity.strip()):
        raise ScheduleRuleError("identity must be a non-empty string when given")
    return device.strip(), identity.strip() if identity else None
