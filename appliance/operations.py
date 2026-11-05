# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable operation model for every mutating appliance action.

The privileged agent is the only writer of this store, so in-process locking
plus atomic file replacement is enough to serialise mutations. Durability is
what makes a browser reload or an agent restart harmless: the operation record,
not a request or a thread, is the authority for what is currently running.
"""

import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from appliance.operation_schema import OPERATION_SCHEMA_VERSION
from appliance.paths import AGENT_FILE_MODE, atomic_write, ensure_within
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
STATE_MANUAL_ACTION_REQUIRED = "manual_action_required"

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
    STATE_MANUAL_ACTION_REQUIRED,
)

TERMINAL_STATES = frozenset(
    {
        STATE_SUCCEEDED,
        STATE_ROLLED_BACK,
        STATE_FAILED_TERMINAL,
        STATE_CANCELLED,
        STATE_MANUAL_ACTION_REQUIRED,
    }
)

# A recoverable failure has stopped acting on the host. It stays retryable
# in place, so it is not terminal -- but holding the single mutation slot
# while it waits for the operator makes the appliance refuse the very
# operation that would fix it, and refuse to be acknowledged because it is
# not finished. A first Admin install failed exactly this way.
SETTLED_STATES = TERMINAL_STATES | {STATE_FAILED_RECOVERABLE}

TRANSITIONS = {
    STATE_PLANNED: frozenset({STATE_AWAITING_CONFIRMATION, STATE_CANCELLED}),
    # A plan refused at its own confirmation never runs, and it is not a plan
    # the operator withdrew: it is terminally failed with nothing touched.
    STATE_AWAITING_CONFIRMATION: frozenset(
        {STATE_RUNNING, STATE_CANCELLED, STATE_FAILED_TERMINAL}
    ),
    STATE_RUNNING: frozenset(
        {
            STATE_VERIFYING,
            STATE_SUCCEEDED,
            STATE_ROLLING_BACK,
            STATE_FAILED_RECOVERABLE,
            STATE_FAILED_TERMINAL,
            STATE_MANUAL_ACTION_REQUIRED,
        }
    ),
    STATE_VERIFYING: frozenset(
        {
            STATE_SUCCEEDED,
            STATE_ROLLING_BACK,
            STATE_FAILED_RECOVERABLE,
            STATE_FAILED_TERMINAL,
            STATE_MANUAL_ACTION_REQUIRED,
        }
    ),
    STATE_ROLLING_BACK: frozenset({STATE_ROLLED_BACK, STATE_FAILED_TERMINAL}),
    STATE_FAILED_RECOVERABLE: frozenset({STATE_RUNNING, STATE_CANCELLED, STATE_FAILED_TERMINAL}),
    STATE_SUCCEEDED: frozenset(),
    STATE_ROLLED_BACK: frozenset(),
    STATE_FAILED_TERMINAL: frozenset(),
    STATE_CANCELLED: frozenset(),
    STATE_MANUAL_ACTION_REQUIRED: frozenset(),
}

MAX_PROGRESS_ENTRIES = 200
MAX_RETAINED_OPERATIONS = 50

# The stages an executor is in before it has changed anything on the host, and
# a power request, which is safe to ask again. An operation the agent's restart
# interrupted anywhere else may have left a change half made: a retry ran it
# from the top again, failed its own precondition, and reported the Admin it
# had stopped as untouched.
UNTOUCHED_STAGES = frozenset(
    {
        "running",
        "preflight",
        "pulling_image",
        "inspecting_image",
        "verifying_target_image",
        "refreshing_index",
        "reboot_requested",
        "shutdown_requested",
    }
)

BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")


def read_boot_id():
    try:
        return BOOT_ID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class OperationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# What a caller that is not an operator -- a timer, a boot unit -- reads as
# "someone else holds the appliance; try again next time".
BUSY_CODES = ("operation_in_progress", "operation_conflict")


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
    # Written by this version; a record without it predates the required
    # authority and must be replanned rather than executed.
    schema_version: int = 0
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
    plan_request_digest: str = ""
    # The boot an operation asked the host to go down in. A later boot is the
    # host having done it, whether or not the record said so before it went.
    host_down_boot: str = ""

    @property
    def terminal(self):
        return self.state in TERMINAL_STATES

    @property
    def settled(self):
        """Finished acting on the host, whether or not it can still be retried."""

        return self.state in SETTLED_STATES

    @property
    def blocking(self):
        return self.state not in SETTLED_STATES

    def to_dict(self, *, include_token=False):
        data = asdict(self)
        data.pop("plan_request_digest", None)
        if not include_token:
            data.pop("confirmation_token", None)
        data["terminal"] = self.terminal
        data["settled"] = self.settled
        return redact_mapping(data)

    def summary(self):
        """The record as a list shows it: without the tool output a result carries.

        Lists are polled every two seconds; apt's output, clamped at 256 KiB a
        record, took a few unacknowledged updates past the client's 8 MiB limit.
        """

        data = self.to_dict()
        if isinstance(data.get("result"), dict) and "output" in data["result"]:
            data["result"] = {key: value for key, value in data["result"].items() if key != "output"}
        return data

    @classmethod
    def from_dict(cls, data):
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


def _request_digest(plan_request):
    return hashlib.sha256(str(plan_request).encode("utf-8")).hexdigest()


class OperationStore:
    """Durable, single-writer operation store with one active mutation."""

    def __init__(self, directory, *, time_fn=None, id_factory=None, token_factory=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._time = time_fn or time.time
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)
        self._new_token = token_factory or (lambda: secrets.token_urlsafe(24))
        # Reentrant: a plan closes what a full card left open and checks for an
        # active operation under one hold, and the close is the recovery.
        self._lock = threading.RLock()
        # Operations an agent restart interrupted whose recovery could not be
        # written -- a full card is the ordinary way. They still hold the lock.
        # By id, with the record's updated_at as that recovery read it.
        self.unrecorded = {}
        # Ends that could not be written, by operation: the first one asked
        # for, since a failure path's own finish fails on the same card.
        self.unfinished = {}
        # Records the agent's start could not read and moved aside, by id.
        self.unreadable = []
        # Plans whose planner is still running. One cancelled meanwhile is
        # cancelled, but its planner -- apt-get update, an index fetch, a
        # support archive -- runs on, so it keeps the slot until it returns.
        self._planning = set()

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
        # The record carries the confirmation token, so it is root-only.
        atomic_write(
            self._path(operation.operation_id),
            json.dumps(asdict(operation), indent=2, sort_keys=True) + "\n",
            mode=AGENT_FILE_MODE,
            owner_root=True,
        )
        self.unrecorded.pop(operation.operation_id, None)
        self.unfinished.pop(operation.operation_id, None)
        return operation

    def _all(self):
        return self._read_all()[0]

    def _read_all(self):
        records = []
        unreadable = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(Operation.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except OSError:
                continue
            except (AttributeError, TypeError, ValueError):
                unreadable.append(path)
        return records, unreadable

    # --- lifecycle -------------------------------------------------------

    def active(self):
        for operation in self._all():
            if operation.blocking or operation.operation_id in self._planning:
                return operation
        return None

    def planning_ended(self, operation_id):
        with self._lock:
            self._planning.discard(operation_id)

    def create(self, operation_type, requested_target=None, *, actor="", plan_request="", planning=False):
        """Take the lock with a new plan; ``plan_request`` is kept only as its digest.

        ``planning`` holds the slot, cancelled or not, until ``planning_ended``.
        """

        with self._lock:
            self._close_unrecorded()
            active = self.active()
            if active is not None:
                raise OperationConflictError(active.operation_id, active.type)
            now = self._time()
            operation = Operation(
                operation_id=self._new_id(),
                type=operation_type,
                schema_version=OPERATION_SCHEMA_VERSION,
                requested_target=dict(requested_target or {}),
                state=STATE_PLANNED,
                stage="planned",
                started_at=now,
                updated_at=now,
                confirmation_token=self._new_token(),
                actor=str(actor or ""),
                plan_request_digest=_request_digest(plan_request) if plan_request else "",
            )
            self._prune()
            self._save(operation)
            if planning:
                self._planning.add(operation.operation_id)
            return operation

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

    def resume(self, operation_id, plan_request):
        """The plan and its token, for the caller that sent ``plan_request`` with it.

        Only a plan waiting for confirmation is handed out, and only against the
        digest kept when it was created: a plan made without a request id is
        handed to nobody.
        """

        operation = self._load(operation_id)
        stored = str(operation.plan_request_digest or "")
        presented = _request_digest(plan_request) if plan_request else ""
        if not stored or not secrets.compare_digest(stored, presented):
            raise OperationError(
                "plan_request_mismatch", "this plan was not started by this request"
            )
        if operation.state != STATE_AWAITING_CONFIRMATION:
            raise OperationError(
                "plan_not_awaiting_confirmation", f"the plan is {operation.state}"
            )
        return operation

    @staticmethod
    def _require_token(operation, token):
        if not secrets.compare_digest(str(operation.confirmation_token), str(token or "")):
            raise OperationError("confirmation_token_mismatch", "confirmation token is invalid")

    def require_token(self, operation_id, token):
        """Refuse a caller who never saw this plan, before anything else is judged."""

        with self._lock:
            self._require_token(self._load(operation_id), token)

    def confirm(self, operation_id, token):
        """Start execution. The token proves the caller saw *this* plan."""

        with self._lock:
            operation = self._load(operation_id)
            self._require_token(operation, token)
            self._transition(operation, STATE_RUNNING)
            operation.stage = "running"
            operation.host_down_boot = ""
            return self._save(operation)

    def retry(self, operation_id, token):
        with self._lock:
            operation = self._load(operation_id)
            self._require_token(operation, token)
            # No longer holding the slot is not the same permission as taking
            # it back while somebody else has it.
            active = self.active()
            if active is not None and active.operation_id != operation_id:
                raise OperationConflictError(active.operation_id, active.type)
            self._transition(operation, STATE_RUNNING)
            operation.stage = "running"
            operation.error = None
            operation.host_down_boot = ""
            return self._save(operation)

    def update_target(self, operation_id, values):
        with self._lock:
            operation = self._load(operation_id)
            operation.requested_target.update(dict(values))
            return self._save(operation)

    def advance(self, operation_id, stage, *, state=None, detail=None, host_down=False):
        with self._lock:
            operation = self._load(operation_id)
            if state is not None and state != operation.state:
                self._transition(operation, state)
            if host_down:
                operation.host_down_boot = read_boot_id()
            operation.stage = str(stage)
            entry = {"stage": operation.stage, "at": self._time()}
            if detail:
                entry["detail"] = str(detail)
            operation.progress.append(entry)
            del operation.progress[:-MAX_PROGRESS_ENTRIES]
            return self._save(operation)

    def finish(self, operation_id, state, *, result=None, error=None, stage=None):
        with self._lock:
            # The end the card had no room for is the one that happened; a
            # failure path's later finish is only about the write.
            kept = self.unfinished.get(operation_id)
            if kept is not None:
                state, result, error, stage = kept["state"], kept["result"], kept["error"], kept["stage"]
            try:
                return self._finish(
                    self._load(operation_id), state, result=result, error=error, stage=stage
                )
            except OSError:
                self.unfinished.setdefault(
                    operation_id, {"state": state, "result": result, "error": error, "stage": stage}
                )
                raise

    def _close_unrecorded(self):
        """Write the ends and recoveries a full card refused, now that there may be room."""

        with self._lock:
            for operation_id, end in list(self.unfinished.items()):
                try:
                    operation = self._load(operation_id)
                    if not operation.terminal:
                        self._finish(operation, **end)
                except OSError:
                    continue
                except OperationError:
                    pass
                self.unfinished.pop(operation_id, None)
            if self.unrecorded:
                self.recover_interrupted(only=dict(self.unrecorded))

    def _finish(self, operation, state, *, result=None, error=None, stage=None):
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

    def discard(self, operation_id):
        """Forget a plan that found nothing to do; no operation took place.

        Only a plan still being made is forgotten -- one an operator cancelled
        meanwhile is kept as they left it.
        """

        with self._lock:
            operation = self._load(operation_id)
            if operation.state != STATE_PLANNED:
                return False
            self._path(operation_id).unlink()
            return True

    def abandon(self, operation_id, *, error=None, acknowledge=False):
        """Cancel a plan that could not be made, and keep why.

        An operator may have cancelled it while it was being made; that record
        stays as it is, so the planner giving up is no second transition.
        ``acknowledge`` is for a refusal already answered to whoever asked.
        """

        with self._lock:
            operation = self._load(operation_id)
            if operation.terminal:
                return operation
            operation.acknowledged = bool(acknowledge)
            return self._finish(operation, STATE_CANCELLED, error=error, stage="cancelled")

    def note(self, operation_id, detail):
        """Replace the detail of the stage under way; False once the operation has ended."""

        with self._lock:
            operation = self._load(operation_id)
            if operation.terminal:
                return False
            if operation.progress:
                operation.progress[-1] = {**operation.progress[-1], "detail": str(detail)}
            self._save(operation)
            return True

    def acknowledge(self, operation_id):
        with self._lock:
            operation = self._load(operation_id)
            if not operation.settled:
                raise OperationError(
                    "operation_not_terminal", "only a finished operation can be acknowledged"
                )
            operation.acknowledged = True
            return self._save(operation)

    def unacknowledged(self):
        return [item for item in self.list() if item.settled and not item.acknowledged]

    def recover_interrupted(self, *, only=None):
        """Turn operations interrupted by a service restart into a visible failure.

        Without this, a crash during ``running`` would hold the mutation lock
        forever and the UI would show progress that nothing is driving.

        ``only`` maps ids to the ``updated_at`` this recovery once read: the
        records a full card kept it from writing. Any other record, or one
        written since -- a plan confirmed once there was room -- is live, and
        recovering it as interrupted let a second operation start beside it.
        """

        recovered = []
        unrecorded = {}
        booted = read_boot_id()
        with self._lock:
            records, unreadable = self._read_all()
            # Skipped silently, a record the card corrupted opened the lock and
            # its operation vanished; moved aside, it is at least named.
            for path in unreadable if only is None else ():
                try:
                    path.rename(path.with_name(path.name + ".unreadable"))
                except OSError:
                    continue
                self.unreadable.append(path.stem)
            for operation in records:
                if operation.operation_id in self.unfinished:
                    continue
                seen = operation.updated_at
                if only is not None and only.get(operation.operation_id) != seen:
                    continue
                if (
                    operation.state == STATE_RUNNING
                    and operation.host_down_boot
                    and booted
                    and operation.host_down_boot != booted
                ):
                    # systemd stops the agent on the way down, often before
                    # finish() reached the card; a retry would go down again.
                    operation.state = STATE_SUCCEEDED
                    operation.stage = STATE_SUCCEEDED
                    operation.result = {"accepted": True, "host_went_down": True}
                    operation.error = None
                    operation.finished_at = self._time()
                    operation.progress.append({"stage": operation.stage, "at": operation.finished_at})
                elif operation.state == STATE_RUNNING and operation.stage in UNTOUCHED_STAGES:
                    operation.state = STATE_FAILED_RECOVERABLE
                    operation.stage = "interrupted"
                    operation.error = {
                        "code": "operation_interrupted",
                        "message": "the appliance agent restarted while this operation was running",
                    }
                elif operation.state in (STATE_RUNNING, STATE_VERIFYING, STATE_ROLLING_BACK):
                    operation.error = {
                        "code": "operation_interrupted_after_change",
                        "message": "the appliance agent restarted while this operation was at "
                        f"{operation.stage}, after it had begun to change the appliance; it is not "
                        "run again over a change that may be half made. Check the appliance, "
                        "then plan the operation anew.",
                    }
                    operation.state = STATE_MANUAL_ACTION_REQUIRED
                    operation.stage = "interrupted"
                    operation.finished_at = self._time()
                elif operation.state in (STATE_PLANNED, STATE_AWAITING_CONFIRMATION):
                    operation.state = STATE_CANCELLED
                    operation.stage = "cancelled"
                    operation.finished_at = self._time()
                    operation.error = {
                        "code": "operation_interrupted",
                        "message": "the plan expired when the appliance agent restarted",
                    }
                elif operation.state not in ALL_STATES:
                    # Another Manager version's state, after a step back: no
                    # transition leads out of it, so it held the lock for good.
                    operation.error = {
                        "code": "operation_state_unknown",
                        "message": f"another Appliance Manager version left this operation in "
                        f"a state this one does not know ({operation.state})",
                    }
                    operation.state = STATE_FAILED_TERMINAL
                    operation.stage = "failed"
                    operation.finished_at = self._time()
                else:
                    continue
                # The agent runs this before it serves. A record it cannot
                # write -- a full card -- used to end the start, and systemd
                # restarted it for ever: no sign-in, no logs, no support
                # archive. It is left for the next plan to close instead.
                try:
                    recovered.append(self._save(operation))
                except OSError:
                    unrecorded[operation.operation_id] = seen
            self.unrecorded = unrecorded
        return recovered

    # --- helpers ---------------------------------------------------------

    def _transition(self, operation, target):
        if target not in ALL_STATES:
            raise InvalidTransitionError(operation.state, target)
        if target not in TRANSITIONS.get(operation.state, ()):
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
