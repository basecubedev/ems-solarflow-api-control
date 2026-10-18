# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one writer of ``schedules.json``: the rules, their order, the pause
switch, and which switch point was last applied to each runtime key.

The dashboard writes it in the EMS process, emsctl from a process of its own,
and the scheduler records what it applied. Every change re-reads the file
first and replaces it whole through a temp file of its own. An edit names the
revision it was made against, so a stale one is refused rather than saved over
a newer rule.
"""

import contextlib
import hashlib
import json
import math
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ems.schedule_model import (
    EDITABLE_FIELDS,
    EDITORS,
    ScheduleRuleError,
    format_utc,
    parse_editable,
    parse_rule,
    parse_utc,
    rule_payload,
)
from ems.file_io import FileLockTimeout, atomic_write_bytes, exclusive_file_lock
from ems.schedule_actions import ScheduleActionError, action_commands, action_slot, schedule_action
from ems.schedule_recurrence import when_payload

SCHEMA_VERSION = 1
MAX_RULES = 100
DIRECTIONS = ("up", "down")


class ScheduleStoreError(ValueError):
    """The file cannot be read as schedules; nothing in it is applied."""


class ScheduleNotFound(ScheduleStoreError):
    pass


class ScheduleConflict(ScheduleStoreError):
    """The rule changed since the editor read it."""


@dataclass(frozen=True)
class BrokenRule:
    """A stored entry that is not a valid rule: shown, never applied."""

    id: str | None
    payload: object
    error: str


@dataclass(frozen=True)
class ScheduleBook:
    enabled: bool
    rules: tuple
    applied: dict
    resumed_at: datetime | None = None

    def rule(self, rule_id):
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise ScheduleNotFound(f"no schedule rule {rule_id}")


def empty_book():
    return ScheduleBook(enabled=True, rules=(), applied={})


class ScheduleStore:
    def __init__(self, path, *, clock=None):
        self.path = str(path)
        self.lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @contextlib.contextmanager
    def _exclusive(self):
        """One change at a time, across processes: emsctl, the EMS and its
        dashboard each read, change and replace the whole file.

        The flock, which every thread opens for itself, is waited for first
        and for a bounded time: the control loop records what it applied
        through here and must not queue behind a dashboard save that waits
        for emsctl.
        """

        with contextlib.ExitStack() as stack:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            try:
                stack.enter_context(exclusive_file_lock(f"{self.path}.lock"))
            except FileLockTimeout as exc:
                raise ScheduleStoreError(
                    f"schedules {self.path} are busy: another process holds their lock"
                ) from exc
            stack.enter_context(self.lock)
            yield

    def load(self):
        """The book on disk; an absent file is an empty book."""

        return self.snapshot()[0]

    def snapshot(self):
        """The book and the bytes it was read from, in one read."""

        raw = self._raw()
        return self.parse(raw), raw

    def read_bytes(self):
        return self._raw()

    def parse(self, raw):
        """The book ``raw`` holds, as :meth:`load` would read it."""

        return _book(self._decode(raw))

    def create(self, payload, *, by, check=None):
        """Add a rule; ``check(book)`` runs on the book read under the lock, so two
        editors saving at once cannot both pass a check the other would fail."""

        fields = _editable(payload, by)
        with self._exclusive():
            data = self._read()
            if check is not None:
                check(_book(data))
            if len(data["rules"]) >= MAX_RULES:
                raise ScheduleRuleError(f"at most {MAX_RULES} schedule rules")
            taken = {entry.get("id") for entry in data["rules"] if isinstance(entry, dict)}
            rule_id = _new_id(taken)
            stored = self._stamp(fields, rule_id=rule_id, revision=1, by=by)
            data["rules"].append(stored)
            self._write(data)
            return parse_rule(stored)

    def replace(self, rule_id, payload, *, revision, by, check=None):
        fields = _editable(payload, by)
        with self._exclusive():
            data = self._read()
            index = _index(data, rule_id, revision)
            if check is not None:
                check(_book(data))
            stored = self._stamp(fields, rule_id=rule_id, revision=revision + 1, by=by)
            data["rules"][index] = stored
            self._write(data)
            return parse_rule(stored)

    def delete(self, rule_id, *, revision):
        with self._exclusive():
            data = self._read()
            index = _index(data, rule_id, revision)
            del data["rules"][index]
            self._write(data)

    def move(self, rule_id, direction):
        """One place up or down in the list, the order ties are decided by."""

        if direction not in DIRECTIONS:
            raise ScheduleRuleError("direction must be up or down")
        with self._exclusive():
            data = self._read()
            index = _index(data, rule_id, None)
            other = index - 1 if direction == "up" else index + 1
            if 0 <= other < len(data["rules"]):
                rules = data["rules"]
                rules[index], rules[other] = rules[other], rules[index]
                self._write(data)

    def skip(self, rule_id, day, *, revision, by):
        """Add ``day`` to the rule's skipped dates, dropping those that have
        passed in every time zone, so the limit counts only coming ones."""

        with self._exclusive():
            data = self._read()
            index = _index(data, rule_id, revision)
            stored = rule_payload(parse_rule(data["rules"][index]))
            when = stored["when"]
            passed = (self._clock().astimezone(timezone.utc).date() - timedelta(days=1)).isoformat()
            coming = {skipped for skipped in when.get("exceptions", ()) if skipped >= passed}
            when["exceptions"] = sorted({*coming, day.isoformat()})
            fields = {key: stored[key] for key in EDITABLE_FIELDS if key in stored}
            stamped = self._stamp(_editable(fields, by), rule_id=rule_id, revision=revision + 1, by=by)
            data["rules"][index] = stamped
            self._write(data)
            return parse_rule(stamped)

    def set_enabled(self, enabled):
        """Pause or resume every rule; a resume fires nothing that fell in the pause."""

        if not isinstance(enabled, bool):
            raise ScheduleRuleError("enabled must be true or false")
        with self._exclusive():
            data = self._read()
            if enabled and not data["enabled"]:
                data["resumed_at"] = format_utc(self._clock())
            data["enabled"] = enabled
            self._write(data)

    def record_applied(self, path, *, rule_id, at, value, window=None):
        """What the scheduler applied to ``path``, so it fires each switch point once.

        A window's start also records ``window`` -- its action, device, end
        and end value -- so the window can be closed if its rule goes away
        before then. Returns the bytes written.
        """

        record = {"rule": rule_id, "at": format_utc(at), "value": value}
        if window is not None:
            record["window"] = {**window, "ends_at": format_utc(window["ends_at"])}
        with self._exclusive():
            data = self._read()
            data["applied"][path] = record
            return self._write(data)

    def restore(self, raw, *, write=atomic_write_bytes, keep_applied=True):
        """Put ``raw``, a schedules file from a backup, in place.

        A backup brings back the rules, not the past. Each rule counts as
        saved now, so none of its switch points from before the restore fires,
        as for a rule saved by hand. What the scheduler applied describes the
        runtime state it wrote to: with ``keep_applied`` the current file's,
        for the current runtime state, so a window running now still ends;
        without, the backup's, for a runtime state restored with it, so a
        window that ran when the backup was taken ends at once. A restore
        that resumes a paused schedule fires nothing from the pause, as a
        resume does. A file this build cannot read is restored as it is,
        and shown broken. ``write(path, bytes)`` puts it in place; a backup
        restore passes the one that keeps the replaced file's mode and owner.
        """

        with self._exclusive():
            try:
                data = self._decode(raw)
            except ScheduleStoreError:
                write(self.path, raw)
                return raw
            stamp = format_utc(self._clock())
            for entry in data["rules"]:
                if isinstance(entry, dict) and "updated_at" in entry:
                    entry["updated_at"] = stamp
            try:
                current = self._read()
            except ScheduleStoreError:
                current = None
            if current is None:
                if keep_applied:
                    data["applied"] = {}
                write(self.path, _encode(data))
                return _encode(data)
            if keep_applied:
                data["applied"] = current["applied"]
            resumes = [stamp for stamp in (data.get("resumed_at"), current.get("resumed_at")) if isinstance(stamp, str)]
            if data["enabled"] and not current["enabled"]:
                resumes.append(format_utc(self._clock()))
            if resumes:
                data["resumed_at"] = max(resumes)
            write(self.path, _encode(data))
            return _encode(data)

    def rename_devices(self, names_by_identity):
        """Follow a device renamed in config, matched by its identity.

        What was applied moves with the device; a record left on the new name
        by a device removed from config before gives way to it.
        """

        with self._exclusive():
            data = self._read()
            changed = False
            moved = {}
            renamed = {}
            for entry in data["rules"]:
                target = entry.get("target") if isinstance(entry, dict) else None
                if not isinstance(target, dict):
                    continue
                identity = target.get("identity")
                name = names_by_identity.get(identity) if isinstance(identity, str) else None
                old = target.get("device")
                if name and isinstance(old, str) and old != name:
                    action = entry.get("action")
                    action_id = action.get("id") if isinstance(action, dict) else None
                    with contextlib.suppress(ScheduleActionError):
                        action = schedule_action(action_id)
                        moved[action_slot(action, old)] = action_slot(action, name)
                    renamed[old] = name
                    target["device"] = name
                    changed = True
            if changed:
                applied = {}
                for slot, record in data["applied"].items():
                    window = record.get("window") if isinstance(record, dict) else None
                    if isinstance(window, dict) and isinstance(window.get("device"), str) \
                            and window["device"] in renamed:
                        window["device"] = renamed[window["device"]]
                    key = moved.get(slot, slot)
                    if key not in applied or slot in moved:
                        applied[key] = record
                data["applied"] = applied
                self._write(data)
            return changed

    def _stamp(self, fields, *, rule_id, revision, by):
        return {
            "id": rule_id,
            "revision": revision,
            **fields,
            "updated_at": format_utc(self._clock()),
            "updated_by": by,
        }

    def _read(self):
        return self._decode(self._raw())

    def _raw(self):
        try:
            with open(self.path, "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ScheduleStoreError(f"cannot read schedules {self.path}: {exc}") from exc

    def _decode(self, raw):
        if raw is None:
            return {"schema_version": SCHEMA_VERSION, "enabled": True, "rules": [], "applied": {}}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ScheduleStoreError(f"cannot read schedules {self.path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise ScheduleStoreError(
                f"schedules {self.path} is not a schema_version {SCHEMA_VERSION} file"
            )
        if not isinstance(data.get("enabled", True), bool):
            raise ScheduleStoreError("schedules enabled must be true or false")
        if not isinstance(data.get("rules", []), list) or not isinstance(data.get("applied", {}), dict):
            raise ScheduleStoreError("schedules rules must be a list and applied an object")
        data.setdefault("enabled", True)
        data.setdefault("rules", [])
        data.setdefault("applied", {})
        return data

    def _write(self, data):
        raw = _encode(data)
        atomic_write_bytes(self.path, raw)
        return raw


def _encode(data):
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _book(data):
    rules = []
    for entry in data["rules"]:
        try:
            rules.append(parse_rule(entry))
        except ScheduleRuleError as exc:
            rules.append(BrokenRule(id=_entry_id(entry), payload=entry, error=str(exc)))
    applied = {}
    for path, record in data["applied"].items():
        if isinstance(record, dict) and isinstance(record.get("rule"), str):
            with contextlib.suppress(ScheduleRuleError):
                value = record.get("value")
                if isinstance(value, float) and not math.isfinite(value):
                    raise ScheduleRuleError("applied value must be a number")
                entry = {
                    "rule": record["rule"],
                    "at": parse_utc(record.get("at"), "applied at"),
                    "value": value,
                }
                window = record.get("window")
                if _closable(window):
                    entry["window"] = {
                        "action": window["action"],
                        "device": window.get("device"),
                        "end_value": window.get("end_value"),
                        "ends_at": parse_utc(window.get("ends_at"), "applied window end"),
                    }
                applied[path] = entry
    resumed_at = None
    with contextlib.suppress(ScheduleRuleError):
        resumed_at = parse_utc(data.get("resumed_at"), "resumed_at") if data.get("resumed_at") else None
    return ScheduleBook(
        enabled=data["enabled"], rules=tuple(rules), applied=applied, resumed_at=resumed_at
    )


def _editable(payload, by):
    if by not in EDITORS:
        raise ScheduleRuleError(f"an edit comes from one of: {', '.join(EDITORS)}")
    fields = parse_editable(payload)
    rule = {
        "name": fields["name"],
        "enabled": fields["enabled"],
        "target": {"scope": "device" if fields["action"].per_device else "system"},
        "action": {"id": fields["action"].id, "value": fields["value"]},
        "when": None,
    }
    if fields["device"] is not None:
        rule["target"]["device"] = fields["device"]
    if fields["identity"] is not None:
        rule["target"]["identity"] = fields["identity"]
    if fields["end_value"] is not None:
        rule["action"]["end_value"] = fields["end_value"]
    rule["when"] = when_payload(fields["when"])
    return rule


def _closable(window):
    """A recorded window this build can close: a known action and an end value it takes."""

    if not isinstance(window, dict):
        return False
    try:
        action_commands(schedule_action(window.get("action")), window.get("end_value"))
    except ScheduleActionError:
        return False
    return True


def _entry_id(entry):
    """An entry's id, or for one without a usable id a name taken from its content."""

    rule_id = entry.get("id") if isinstance(entry, dict) else None
    if isinstance(rule_id, str) and rule_id:
        return rule_id
    content = json.dumps(entry, sort_keys=True, default=str).encode("utf-8")
    return "x_" + hashlib.sha256(content).hexdigest()[:8]


def _index(data, rule_id, revision):
    for index, entry in enumerate(data["rules"]):
        if _entry_id(entry) == rule_id:
            stored = entry.get("revision") if isinstance(entry, dict) else None
            if revision is not None and stored != revision:
                raise ScheduleConflict(
                    f"schedule rule {rule_id} changed since it was read "
                    f"(revision {stored}, not {revision})"
                )
            return index
    raise ScheduleNotFound(f"no schedule rule {rule_id}")


def _new_id(taken):
    while True:
        rule_id = f"r_{secrets.token_hex(4)}"
        if rule_id not in taken:
            return rule_id
