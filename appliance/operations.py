# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable operation model for every mutating appliance action.

The privileged agent is the only writer of this store, so in-process locking
plus atomic file replacement is enough to serialise mutations. Durability is
what makes a browser reload or an agent restart harmless: the operation record,
not a request or a thread, is the authority for what is currently running.
"""

import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from appliance.paths import atomic_write, ensure_within
from appliance.redaction import redact_mapping

STATE_PLANNED = "planned"
STATE_AWAITING_CONFIRMATION = "awaiting_confirmation"
STATE_RUNNING = "running"
STATE_VERIFYING = "verifying"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED_RECOVERABLE = "failed_recoverable"
STATE_ROLLING_BACK = "rolling_back"
STATE_ROLLED_BACK = "rolled_back"
STATE_FAILED_TERMINAL = "failed_terminal"
STATE_CANCELLED = "cancelled"

ALL_STATES = (
    STATE_PLANNED,
    STATE_AWAITING_CONFIRMATION,
    STATE_RUNNING,
    STATE_VERIFYING,
    STATE_SUCCEEDED,
    STATE_FAILED_RECOVERABLE,
    STATE_ROLLING_BACK,
    STATE_ROLLED_BACK,
    STATE_FAILED_TERMINAL,
    STATE_CANCELLED,
)

TERMINAL_STATES = frozenset(
    {STATE_SUCCEEDED, STATE_ROLLED_BACK, STATE_FAILED_TERMINAL, STATE_CANCELLED}
)

TRANSITIONS = {
    STATE_PLANNED: frozenset({STATE_AWAITING_CONFIRMATION, STATE_CANCELLED}),
    STATE_AWAITING_CONFIRMATION: frozenset({STATE_RUNNING, STATE_CANCELLED}),
    STATE_RUNNING: frozenset(
        {
            STATE_VERIFYING,
            STATE_SUCCEEDED,
            STATE_ROLLING_BACK,
            STATE_FAILED_RECOVERABLE,
            STATE_FAILED_TERMINAL,
        }
    ),
    STATE_VERIFYING: frozenset(
        {
            STATE_SUCCEEDED,
            STATE_ROLLING_BACK,
            STATE_FAILED_RECOVERABLE,
            STATE_FAILED_TERMINAL,
        }
    ),
    STATE_ROLLING_BACK: frozenset({STATE_ROLLED_BACK, STATE_FAILED_TERMINAL}),
    STATE_FAILED_RECOVERABLE: frozenset({STATE_RUNNING, STATE_CANCELLED, STATE_FAILED_TERMINAL}),
    STATE_SUCCEEDED: frozenset(),
    STATE_ROLLED_BACK: frozenset(),
    STATE_FAILED_TERMINAL: frozenset(),
    STATE_CANCELLED: frozenset(),
}

MAX_PROGRESS_ENTRIES = 200
MAX_RETAINED_OPERATIONS = 50


class OperationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class OperationConflictError(OperationError):
    def __init__(self, active_id, active_type):
        super().__init__(
            "operation_conflict",
            f"operation {active_type} ({active_id}) is still active",
        )
        self.active_id = active_id
        self.active_type = active_type


class UnknownOperationError(OperationError):
    def __init__(self, operation_id):
        super().__init__("unknown_operation_id", f"no operation {operation_id}")


class InvalidTransitionError(OperationError):
    def __init__(self, current, target):
        super().__init__(
            "invalid_operation_transition", f"cannot move an operation from {current} to {target}"
        )


@dataclass
class Operation:
    operation_id: str
    type: str
    requested_target: dict = field(default_factory=dict)
    state: str = STATE_PLANNED
    stage: str = "planned"
    started_at: float = 0.0
    updated_at: float = 0.0
    finished_at: float = None
    confirmation_token: str = ""
    result: dict = None
    error: dict = None
    progress: list = field(default_factory=list)
    acknowledged: bool = False
    actor: str = ""

    @property
    def terminal(self):
        return self.state in TERMINAL_STATES

    @property
    def blocking(self):
        return self.state not in TERMINAL_STATES

    def to_dict(self, *, include_token=False):
        data = asdict(self)
        if not include_token:
            data.pop("confirmation_token", None)
        data["terminal"] = self.terminal
        return redact_mapping(data)

    @classmethod
    def from_dict(cls, data):
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


class OperationStore:
    """Durable, single-writer operation store with one active mutation."""

    def __init__(self, directory, *, time_fn=None, id_factory=None, token_factory=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._time = time_fn or time.time
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)
        self._new_token = token_factory or (lambda: secrets.token_urlsafe(24))
        self._lock = threading.Lock()

    # --- persistence -----------------------------------------------------

    def _path(self, operation_id):
        return ensure_within(self.directory, self.directory / f"{operation_id}.json")

    def _load(self, operation_id):
        try:
            raw = self._path(operation_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise UnknownOperationError(operation_id)
        try:
            return Operation.from_dict(json.loads(raw))
        except (TypeError, ValueError):
            raise OperationError("operation_record_invalid", f"operation {operation_id} is corrupt")

    def _save(self, operation):
        operation.updated_at = self._time()
        atomic_write(
            self._path(operation.operation_id),
            json.dumps(asdict(operation), indent=2, sort_keys=True) + "\n",
            mode=0o640,
        )
        return operation

    def _all(self):
        records = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(Operation.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, TypeError, ValueError):
                continue
        return records

    # --- lifecycle -------------------------------------------------------

    def active(self):
        for operation in self._all():
            if operation.blocking:
                return operation
        return None

    def create(self, operation_type, requested_target=None, *, actor=""):
        with self._lock:
            active = self.active()
            if active is not None:
                raise OperationConflictError(active.operation_id, active.type)
            now = self._time()
            operation = Operation(
                operation_id=self._new_id(),
                type=operation_type,
                requested_target=dict(requested_target or {}),
                state=STATE_PLANNED,
                stage="planned",
                started_at=now,
                updated_at=now,
                confirmation_token=self._new_token(),
                actor=str(actor or ""),
            )
            self._prune()
            return self._save(operation)

    def get(self, operation_id, *, include_token=False):
        operation = self._load(operation_id)
        if not include_token:
            operation.confirmation_token = ""
        return operation

    def list(self, limit=MAX_RETAINED_OPERATIONS):
        return sorted(self._all(), key=lambda item: item.started_at, reverse=True)[:limit]

    def await_confirmation(self, operation_id, plan):
        with self._lock:
            operation = self._load(operation_id)
            self._transition(operation, STATE_AWAITING_CONFIRMATION)
            operation.stage = "awaiting_confirmation"
            operation.result = {"plan": plan}
            return self._save(operation)

    def confirm(self, operation_id, token):
        """Start execution. The token proves the caller saw *this* plan."""

        with self._lock:
            operation = self._load(operation_id)
            if not secrets.compare_digest(str(operation.confirmation_token), str(token or "")):
                raise OperationError("confirmation_token_mismatch", "confirmation token is invalid")
            self._transition(operation, STATE_RUNNING)
            operation.stage = "running"
            return self._save(operation)

    def retry(self, operation_id, token):
        with self._lock:
            operation = self._load(operation_id)
            if not secrets.compare_digest(str(operation.confirmation_token), str(token or "")):
                raise OperationError("confirmation_token_mismatch", "confirmation token is invalid")
            self._transition(operation, STATE_RUNNING)
            operation.stage = "running"
            operation.error = None
            return self._save(operation)

    def update_target(self, operation_id, values):
        with self._lock:
            operation = self._load(operation_id)
            operation.requested_target.update(dict(values))
            return self._save(operation)

    def advance(self, operation_id, stage, *, state=None, detail=None):
        with self._lock:
            operation = self._load(operation_id)
            if state is not None and state != operation.state:
                self._transition(operation, state)
            operation.stage = str(stage)
            entry = {"stage": operation.stage, "at": self._time()}
            if detail:
                entry["detail"] = str(detail)
            operation.progress.append(entry)
            del operation.progress[:-MAX_PROGRESS_ENTRIES]
            return self._save(operation)

    def finish(self, operation_id, state, *, result=None, error=None, stage=None):
        with self._lock:
            operation = self._load(operation_id)
            self._transition(operation, state)
            operation.stage = str(stage or state)
            operation.result = result if result is None else dict(result)
            operation.error = error if error is None else dict(error)
            operation.finished_at = self._time()
            operation.progress.append({"stage": operation.stage, "at": operation.finished_at})
            del operation.progress[:-MAX_PROGRESS_ENTRIES]
            return self._save(operation)

    def cancel(self, operation_id):
        return self.finish(operation_id, STATE_CANCELLED, stage="cancelled")

    def acknowledge(self, operation_id):
        with self._lock:
            operation = self._load(operation_id)
            if not operation.terminal:
                raise OperationError(
                    "operation_not_terminal", "only a finished operation can be acknowledged"
                )
            operation.acknowledged = True
            return self._save(operation)

    def unacknowledged(self):
        return [item for item in self.list() if item.terminal and not item.acknowledged]

    def recover_interrupted(self):
        """Turn operations interrupted by a service restart into a visible failure.

        Without this, a crash during ``running`` would hold the mutation lock
        forever and the UI would show progress that nothing is driving.
        """

        recovered = []
        with self._lock:
            for operation in self._all():
                if operation.state in (STATE_RUNNING, STATE_VERIFYING, STATE_ROLLING_BACK):
                    operation.state = STATE_FAILED_RECOVERABLE
                    operation.stage = "interrupted"
                    operation.error = {
                        "code": "operation_interrupted",
                        "message": "the appliance agent restarted while this operation was running",
                    }
                    recovered.append(self._save(operation))
                elif operation.state in (STATE_PLANNED, STATE_AWAITING_CONFIRMATION):
                    operation.state = STATE_CANCELLED
                    operation.stage = "cancelled"
                    operation.finished_at = self._time()
                    operation.error = {
                        "code": "operation_interrupted",
                        "message": "the plan expired when the appliance agent restarted",
                    }
                    recovered.append(self._save(operation))
        return recovered

    # --- helpers ---------------------------------------------------------

    def _transition(self, operation, target):
        if target not in ALL_STATES:
            raise InvalidTransitionError(operation.state, target)
        if target not in TRANSITIONS[operation.state]:
            raise InvalidTransitionError(operation.state, target)
        operation.state = target

    def _prune(self):
        records = sorted(self._all(), key=lambda item: item.started_at, reverse=True)
        for operation in records[MAX_RETAINED_OPERATIONS:]:
            if not operation.terminal:
                continue
            try:
                os.unlink(self._path(operation.operation_id))
            except OSError:
                pass
