# SPDX-License-Identifier: AGPL-3.0-or-later
"""One arbiter for "which guided workflow owns the Admin, and may it change?".

Guided Setup, Guided Upgrade and the System Build transition keep their own
durable authorities. This service owns none of them: it reads them, normalizes
them into a single owner/state verdict, and is the only place that decides
whether a workflow may resume, switch, cancel or be recovered.

Authorities it reads, and never duplicates:

* :class:`admin.guided_setup_workflow.GuidedSetupWorkflowStore` — Guided Setup
  identity, lifecycle, artifact claims and cleanup state;
* :class:`admin.admin_update.PendingTransitionStore` through
  :class:`admin.system_alignment.SystemAlignmentService` — System Build mode,
  operation id and stage;
* :class:`admin.guided_upgrade_context.GuidedUpgradeContextStore` — the
  operation-bound Guided Upgrade execution context;
* :class:`admin.setup_lifecycle.SetupLifecycleCoordinator` and
  :class:`admin.operation_coordinator.OperationCoordinator` — whether a mutation
  or a worker is running right now.

Every mutating operation binds the exact durable facts it decided on into a
fingerprint, so a stale browser view can never switch or recover on state it
never saw. Termination, cancellation and cleanup are delegated to the owning
service; nothing here removes an artifact by itself.

See ``docs/technical/admin-workflow-state.md``.
"""

import contextlib
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from admin.admin_update import (
    CANCELLABLE_TRANSITION_STAGES,
    SETUP_TRANSITION_MODES,
    SUPPORTED_TRANSITION_MODES,
    TERMINAL_TRANSITION_STAGES,
    TRANSITION_MODE_ALIGN_EXISTING,
    TRANSITION_MODE_GUIDED_UPGRADE,
    ADMIN_UPDATER_CONTAINER_PREFIX,
    admin_update_sidecar_container_name,
)
from admin.guided_setup_workflow import (
    CLEANUP_PENDING,
    CLEANUP_REVIEW_REQUIRED,
    SETUP_CLEANUP_REQUIRED,
    STATUS_ACTIVE,
    GuidedSetupWorkflowError,
    cleanup_state,
)
from admin.setup_lifecycle import SETUP_OPERATION_IN_PROGRESS
from admin.setup_workflow import (
    CLEANUP_INCOMPLETE,
    OWNERSHIP_OWNED,
    SETUP_TRANSITION_CONTEXT_MISMATCH,
    SETUP_TRANSITION_OWNER_UNPROVEN,
    SetupWorkflowAbandonError,
    abandon_setup_workflow,
    reconcile_unclaimed_review,
    setup_artifact_claims,
    transition_ownership,
)
from admin.system_alignment import SystemAlignmentError

OWNER_GUIDED_SETUP = "guided_setup"
OWNER_GUIDED_UPGRADE = "guided_upgrade"
OWNER_ALIGN_EXISTING = TRANSITION_MODE_ALIGN_EXISTING
OWNER_NONE = "none"
OWNER_UNKNOWN = "unknown"
# Two durable records claim the console at once. Naming it is the whole point:
# picking one of them would silently discard the other one's state.
OWNER_CONFLICT = "conflict"

TARGET_GUIDED_SETUP = OWNER_GUIDED_SETUP
TARGET_GUIDED_UPGRADE = OWNER_GUIDED_UPGRADE
TARGET_NONE = OWNER_NONE
SWITCH_TARGETS = frozenset({TARGET_GUIDED_SETUP, TARGET_GUIDED_UPGRADE, TARGET_NONE})

STATE_IDLE = "idle"
STATE_ACTIVE = "active"
STATE_OPERATION_RUNNING = "operation_running"
STATE_CLEANUP_PENDING = "cleanup_pending"
STATE_REVIEW_REQUIRED = "review_required"
STATE_CONFLICT = "conflict"
STATE_MALFORMED = "malformed"

ACTION_NONE = "none"
ACTION_DISCARD_SETUP = "discard_guided_setup"
ACTION_CANCEL_UPGRADE = "cancel_guided_upgrade"
ACTION_START_SETUP = "start_guided_setup"
ACTION_RESUME_SETUP = "resume_guided_setup"

# Who owns the active transition, as far as the narrow cancel primitive cares.
CANCEL_VERDICT_ABSENT = "absent"
CANCEL_VERDICT_SETUP_OWNED = "setup_owned"
CANCEL_VERDICT_GUIDED_UPGRADE = "guided_upgrade"
CANCEL_VERDICT_UNSUPPORTED = "unsupported"

RECOVERY_MODE_SAFE = "safe"
RECOVERY_MODE_RELEASE_STALE_STATE = "release_stale_state"
RECOVERY_MODES = frozenset({RECOVERY_MODE_SAFE, RECOVERY_MODE_RELEASE_STALE_STATE})

WORKFLOW_LIFECYCLE_CHANGED = "workflow_lifecycle_changed"
WORKFLOW_SWITCH_BLOCKED = "workflow_switch_blocked"
WORKFLOW_OPERATION_IN_PROGRESS = "workflow_operation_in_progress"
WORKFLOW_RECOVERY_REQUIRED = "workflow_recovery_required"
WORKFLOW_RECOVERY_UNSAFE = "workflow_recovery_unsafe"
WORKFLOW_STATE_MALFORMED = "workflow_state_malformed"
WORKFLOW_OWNER_UNKNOWN = "workflow_owner_unknown"
WORKFLOW_OWNER_CONFLICT = "workflow_owner_conflict"
WORKFLOW_RECOVERY_FAILED = "workflow_recovery_failed"
WORKFLOW_SWITCH_REQUIRED = "workflow_switch_required"
CONFIRMATION_REQUIRED = "confirmation_required"
RECOVERY_REASON_REQUIRED = "recovery_reason_required"

REPLACEMENT_ACTIVE = "replacement_active"
INSTALL_STATE_UNAVAILABLE = "install_state_unavailable"

# Why a durable Admin workflow file cannot be used and cannot be repaired by the
# normal domain operations. Reported per file so a release says what it releases.
STALE_UNREADABLE_STATE = "unreadable_state"
STALE_UNSUPPORTED_TRANSITION_MODE = "unsupported_transition_mode"
STALE_UNUSABLE_UPGRADE_CONTEXT = "unusable_upgrade_context"

# What a guided workflow switch or recovery never touches. Listed for the
# preview so the operator reads the same promise the implementation keeps.
PRESERVED_BY_SWITCH = (
    "live EMS configuration",
    "runtime data",
    "deployment marker",
    "containers",
    "volumes",
    "backups",
)
PRESERVED_BY_RECOVERY = (
    "config/config.json",
    "data/runtime-state.json",
    "docker-compose.yml",
    "state/.admin-deployment.json",
    "state/known-good-system-build.json",
    "backups",
    "containers and volumes",
)

RECOVERY_BACKUP_DIRECTORY = "workflow-recovery"
RECOVERY_MANIFEST_FILE = "recovery-manifest.json"
RECOVERY_MANIFEST_VERSION = 1

MAX_RECOVERY_REASON_CHARS = 200

# A blocking reason that already is its own public contract keeps it; anything
# else is reported as a switch refusal carrying the exact reason as detail.
_BLOCKING_ERROR_CODES = {
    SETUP_OPERATION_IN_PROGRESS: SETUP_OPERATION_IN_PROGRESS,
    WORKFLOW_OPERATION_IN_PROGRESS: WORKFLOW_OPERATION_IN_PROGRESS,
    WORKFLOW_OWNER_UNKNOWN: WORKFLOW_OWNER_UNKNOWN,
    WORKFLOW_OWNER_CONFLICT: WORKFLOW_OWNER_CONFLICT,
    WORKFLOW_STATE_MALFORMED: WORKFLOW_STATE_MALFORMED,
    WORKFLOW_RECOVERY_REQUIRED: WORKFLOW_RECOVERY_REQUIRED,
    SETUP_CLEANUP_REQUIRED: WORKFLOW_RECOVERY_REQUIRED,
}

# Reasons that block even *entering* the workflow that already owns the console:
# an unreadable or contradictory state, an unconverged cleanup that is the only
# ownership record for the files it left behind, and a transition this workflow
# cannot prove it owns. What is left out is the one thing entry may walk
# through: an operation the same workflow is still finishing, whose reason
# clears itself once it does.
_ENTRY_BLOCKING_REASONS = frozenset(
    {
        WORKFLOW_STATE_MALFORMED,
        WORKFLOW_OWNER_CONFLICT,
        WORKFLOW_OWNER_UNKNOWN,
        WORKFLOW_RECOVERY_REQUIRED,
        SETUP_CLEANUP_REQUIRED,
        SETUP_TRANSITION_CONTEXT_MISMATCH,
        SETUP_TRANSITION_OWNER_UNPROVEN,
    }
)

_BLOCKING_MESSAGES = {
    SETUP_OPERATION_IN_PROGRESS: (
        "Guided Setup is still finishing another operation. Wait for it to "
        "finish, then switch."
    ),
    WORKFLOW_OPERATION_IN_PROGRESS: (
        "A System Build operation is still running. Resume or wait for it "
        "instead of switching."
    ),
    WORKFLOW_OWNER_UNKNOWN: (
        "The current System Build operation is not owned by a guided workflow "
        "this Admin can switch away from."
    ),
    WORKFLOW_OWNER_CONFLICT: (
        "Two Admin workflow records claim the console at once. Resolve it in "
        "Maintenance → Workflow recovery; nothing was changed."
    ),
    WORKFLOW_STATE_MALFORMED: (
        "The stored Admin workflow state could not be read. Use Maintenance → "
        "Workflow recovery."
    ),
    WORKFLOW_RECOVERY_REQUIRED: (
        "The previous guided workflow left state that has to be resolved "
        "first. Use Maintenance → Workflow recovery."
    ),
}


def _content_digest(path):
    """Digest an unreadable durable file so a stale view cannot act on it.

    Identity fields cannot distinguish two different corrupt records, and a
    recovery preview has to bind the exact bytes it was shown for.
    """

    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


class AdminWorkflowLifecycleError(Exception):
    """A switch or recovery was refused; nothing durable was changed."""

    def __init__(self, code, message, *, status=409, detail=None, lifecycle=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail
        self.lifecycle = lifecycle

    def as_payload(self):
        payload = {"ok": False, "error": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.lifecycle is not None:
            payload["lifecycle"] = self.lifecycle
        return payload


class ReplacementActivity(str, Enum):
    """Whether an Admin replacement sidecar is running, gone, or unprovable."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


_LIVE_CONTAINER_STATES = frozenset({"created", "restarting", "running"})
# Docker's terminal container states. Naming them explicitly is what makes an
# unrecognised state answerable: a row this build cannot classify must not be
# read as "not live", because that reads on as "gone".
_DEAD_CONTAINER_STATES = frozenset({"dead", "exited", "paused", "removing"})


def _container_activity(container):
    """``ACTIVE``/``INACTIVE`` for a recognised state, ``UNKNOWN`` otherwise."""

    if not isinstance(container, dict):
        return ReplacementActivity.UNKNOWN
    status = container.get("status")
    if status in _LIVE_CONTAINER_STATES:
        return ReplacementActivity.ACTIVE
    if status in _DEAD_CONTAINER_STATES:
        return ReplacementActivity.INACTIVE
    return ReplacementActivity.UNKNOWN


def admin_replacement_activity(docker, operation_id):
    """Prove whether an Admin replacement is running for ``operation_id``.

    Exactly the states an advanced recovery has to tell apart: a daemon that
    cannot be reached is ``UNKNOWN``, never ``INACTIVE``. The durable transition
    stage cannot stand in for this — the states that need a release (unreadable
    transition, unknown mode, missing operation identity) are precisely the ones
    where no stage can be trusted. Without an operation id the canonical updater
    prefix is scanned, and an abstraction that cannot list stays ``UNKNOWN``.

    ``INACTIVE`` is a proof and never a default: it takes either Docker
    answering that no such container exists, or every container it did report
    naming a state this build recognises as terminal.
    """

    if docker is None:
        return ReplacementActivity.UNKNOWN
    if not operation_id:
        listing = getattr(docker, "list_containers", None)
        if not callable(listing):
            return ReplacementActivity.UNKNOWN
        try:
            containers = listing(ADMIN_UPDATER_CONTAINER_PREFIX)
        except Exception:
            return ReplacementActivity.UNKNOWN
        if not isinstance(containers, list):
            return ReplacementActivity.UNKNOWN
        activities = [_container_activity(entry) for entry in containers]
        if ReplacementActivity.ACTIVE in activities:
            return ReplacementActivity.ACTIVE
        if ReplacementActivity.UNKNOWN in activities:
            return ReplacementActivity.UNKNOWN
        return ReplacementActivity.INACTIVE
    try:
        container = docker.inspect_container(
            admin_update_sidecar_container_name(operation_id)
        )
    except Exception:
        return ReplacementActivity.UNKNOWN
    if container is None:
        return ReplacementActivity.INACTIVE
    return _container_activity(container)


def worker_aware_alignment_status(alignment, operation_active=None):
    """The one worker-aware transition status read, safe to call from anywhere.

    A service that raises or answers a non-mapping is reported as an unreadable
    transition rather than propagating: every caller has to be able to decide
    fail-closed on the result.
    """

    try:
        payload = alignment.status(operation_active=operation_active)
    except Exception as exc:
        return {
            "ok": False,
            "active": True,
            "error": "system_alignment_status_failed",
            "message": str(exc),
            "transition": None,
            "known_good": None,
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "active": True,
            "error": "system_alignment_status_failed",
            "message": "the System Build status could not be read",
            "transition": None,
            "known_good": None,
        }
    return payload


class AdminWorkflowLifecycleService:
    """The single cross-workflow decision layer for the Admin console."""

    def __init__(
        self,
        *,
        workflows,
        alignment,
        lifecycle=None,
        coordinator=None,
        upgrade_contexts=None,
        setup_intents=None,
        admin_data_dir=None,
        transition_store=None,
        install_state_probe=None,
        clock=None,
        revision=None,
    ):
        self._workflows = workflows
        self._alignment = alignment
        self._lifecycle = lifecycle
        self._coordinator = coordinator
        self._upgrade_contexts = upgrade_contexts
        self._setup_intents = setup_intents
        self._install_state_probe = install_state_probe
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._revision = revision
        base = admin_data_dir or getattr(workflows, "admin_data_dir", None)
        self._admin_data_dir = Path(base) if base is not None else None
        self._transition_path = (
            Path(transition_store.path)
            if transition_store is not None
            else self._state_dir() / "pending-transition.json"
        )
        # Serializes the decide-then-act sequence of switch and recovery. Durable
        # state stays authoritative; this only stops two callers from acting on
        # one reading, and the fingerprint refuses the loser afterwards.
        self._lock = threading.RLock()

    def bind_alignment(self, alignment):
        """Point the arbiter at the runtime's current System Alignment service.

        The composition root owns this wiring: a runtime that replaces the
        alignment service after construction has to rebind it, so the arbiter
        can never read a detached one.
        """

        self._alignment = alignment

    def bind_install_state_probe(self, probe):
        """Point the arbiter at the runtime's replacement-activity probe."""

        self._install_state_probe = probe

    # --- inspection ---------------------------------------------------------

    def inspect(self):
        """One normalized reading of the durable guided-workflow state.

        Normalizes stale bookkeeping on the way (a review state that only ever
        named installed-system files), which changes the workflow record and no
        file. Everything else is read-only.
        """

        reconcile_unclaimed_review(self._workflows)
        setup = self._setup_facts()
        transition = self._transition_facts()
        context = self._context_facts(transition)
        operation = self._active_setup_operation(setup)
        owner = self._resolve_owner(setup, transition)
        state = self._resolve_state(owner, setup, transition, operation)
        blocking = self._resolve_blocking_reason(owner, setup, transition, state)
        recoverable = self._resolve_recoverable(state, blocking, context)
        return {
            "ok": True,
            "owner": owner,
            "state": state,
            "switchable": blocking is None,
            "recoverable": recoverable,
            "blocking_reason": blocking,
            "operation": operation,
            "setup": setup,
            "transition": transition,
            "upgrade_context": context,
            "fingerprint": self._fingerprint(setup, transition, context),
        }

    def _setup_facts(self):
        path = getattr(self._workflows, "path", None)
        try:
            present = bool(path is not None and path.exists())
        except OSError:
            present = True
        if not present:
            return None
        record = self._workflows.load()
        if record is None:
            return {
                "present": True,
                "readable": False,
                "digest": _content_digest(path),
                "workflow_id": None,
                "status": None,
                "cleanup": None,
                "artifacts_claimed": False,
                "operation_id": None,
                "updated_at": None,
            }
        claims = setup_artifact_claims(record)
        return {
            "present": True,
            "readable": True,
            "digest": None,
            "workflow_id": record["workflow_id"],
            "status": record["status"],
            "cleanup": cleanup_state(record),
            "artifacts_claimed": bool(claims is not None and claims.claims_anything()),
            "operation_id": record.get("operation_id"),
            "updated_at": record.get("updated_at"),
        }

    def _alignment_status(self):
        probe = None
        if self._coordinator is not None:
            probe = self._coordinator.is_active
        return worker_aware_alignment_status(self._alignment, probe)

    def _transition_facts(self):
        status = self._alignment_status()
        if status.get("ok") is False:
            return {
                "present": True,
                "readable": False,
                "digest": _content_digest(self._transition_path),
                "operation_id": None,
                "mode": None,
                "stage": None,
                "terminal": False,
                "cancellable": False,
                "resume_available": False,
                "worker_active": None,
                "expired": None,
                "owned_by_setup": False,
                "updated_at": None,
            }
        transition = status.get("transition")
        if not isinstance(transition, dict) or not transition:
            return None
        stage = transition.get("stage")
        terminal = stage in TERMINAL_TRANSITION_STAGES
        record = self._workflows.load()
        return {
            "present": True,
            "readable": True,
            "digest": None,
            "operation_id": transition.get("operation_id"),
            "mode": transition.get("mode"),
            "stage": stage,
            "system_tag": transition.get("system_tag"),
            "terminal": terminal,
            "cancellable": (not terminal) and self._cancellable(transition, stage),
            "resume_available": bool(transition.get("resume_available")),
            "worker_active": transition.get("worker_active"),
            "expired": transition.get("expired"),
            "owned_by_setup": transition_ownership(record, transition)
            == OWNERSHIP_OWNED,
            "updated_at": transition.get("updated_at"),
        }

    @staticmethod
    def _cancellable(transition, stage):
        """Whether the transition owner says a cancel may run right now.

        ``cancel_available`` is System Alignment's own verdict and is always
        present on a productive status; the derived fallback keeps the same rule
        for a status double that reports the durable stage only.
        """

        available = transition.get("cancel_available")
        if isinstance(available, bool):
            return available
        return (
            stage in CANCELLABLE_TRANSITION_STAGES
            and transition.get("worker_active") is not True
        )

    def _context_facts(self, transition):
        store = self._upgrade_contexts
        describe = getattr(store, "describe", None) if store is not None else None
        described = describe() if callable(describe) else None
        if not described or not described.get("present"):
            return None
        operation_id = described.get("operation_id")
        transition_operation = (transition or {}).get("operation_id")
        identity_readable = bool(described.get("identity_readable"))
        domain_valid = bool(described.get("domain_valid"))
        return {
            "present": True,
            "identity_readable": identity_readable,
            "domain_valid": domain_valid,
            "reason": described.get("reason"),
            "digest": None if domain_valid else _content_digest(store.path),
            "operation_id": operation_id,
            "target_system_tag": described.get("target_system_tag"),
            "matches_transition": bool(
                operation_id and operation_id == transition_operation
            ),
        }

    def _active_setup_operation(self, setup):
        if self._lifecycle is None or not setup or not setup.get("workflow_id"):
            return None
        return self._lifecycle.active_operation(setup["workflow_id"])

    @staticmethod
    def _setup_record_owns(setup):
        """Whether the durable Setup record still claims the console."""

        return bool(
            setup
            and setup["readable"]
            and (
                setup["status"] == STATUS_ACTIVE
                or setup["cleanup"] in {CLEANUP_PENDING, CLEANUP_REVIEW_REQUIRED}
            )
        )

    @classmethod
    def _owner_conflict(cls, setup, transition):
        """Two readable records claiming the console for different owners.

        A Setup-mode transition is the Setup record's own operation, so it is
        never a conflict — an operation *mismatch* there is a different, already
        fail-closed contract. Any other live transition beside an owning Setup
        record is a contradiction that must not be resolved by preferring one.
        """

        if not (transition and transition["readable"] and not transition["terminal"]):
            return False
        if transition["mode"] in SETUP_TRANSITION_MODES:
            return False
        return cls._setup_record_owns(setup)

    @classmethod
    def _resolve_owner(cls, setup, transition):
        if (setup and not setup["readable"]) or (
            transition and not transition["readable"]
        ):
            return OWNER_UNKNOWN
        if cls._owner_conflict(setup, transition):
            return OWNER_CONFLICT
        if transition and not transition["terminal"]:
            mode = transition["mode"]
            if mode in SETUP_TRANSITION_MODES:
                return OWNER_GUIDED_SETUP
            if mode == TRANSITION_MODE_GUIDED_UPGRADE:
                return OWNER_GUIDED_UPGRADE
            if mode == TRANSITION_MODE_ALIGN_EXISTING:
                return OWNER_ALIGN_EXISTING
            return OWNER_UNKNOWN
        if cls._setup_record_owns(setup):
            return OWNER_GUIDED_SETUP
        return OWNER_NONE

    @staticmethod
    def _resolve_state(owner, setup, transition, operation):
        # A running mutation outranks an unreadable record: a recovery must stay
        # blocked while something can still write, whatever else is corrupt.
        if operation is not None:
            return STATE_OPERATION_RUNNING
        if (
            transition
            and transition["readable"]
            and not transition["terminal"]
            and not transition["cancellable"]
        ):
            return STATE_OPERATION_RUNNING
        if owner == OWNER_UNKNOWN and (
            (setup and not setup["readable"])
            or (transition and not transition["readable"])
        ):
            return STATE_MALFORMED
        if owner == OWNER_CONFLICT:
            return STATE_CONFLICT
        if setup and setup["cleanup"] == CLEANUP_PENDING:
            return STATE_CLEANUP_PENDING
        if setup and setup["cleanup"] == CLEANUP_REVIEW_REQUIRED:
            return STATE_REVIEW_REQUIRED
        if owner == OWNER_NONE:
            return STATE_IDLE
        return STATE_ACTIVE

    @staticmethod
    def _resolve_blocking_reason(owner, setup, transition, state):
        if state == STATE_MALFORMED:
            return WORKFLOW_STATE_MALFORMED
        if owner == OWNER_CONFLICT:
            return WORKFLOW_OWNER_CONFLICT
        if owner in {OWNER_UNKNOWN, OWNER_ALIGN_EXISTING}:
            return WORKFLOW_OWNER_UNKNOWN
        if state == STATE_OPERATION_RUNNING:
            if setup and setup.get("workflow_id") and transition is None:
                return SETUP_OPERATION_IN_PROGRESS
            if transition and not transition["cancellable"]:
                return WORKFLOW_OPERATION_IN_PROGRESS
            return SETUP_OPERATION_IN_PROGRESS
        if state == STATE_REVIEW_REQUIRED:
            return WORKFLOW_RECOVERY_REQUIRED
        if state == STATE_CLEANUP_PENDING:
            return SETUP_CLEANUP_REQUIRED
        if (
            owner == OWNER_GUIDED_SETUP
            and transition is not None
            and not transition["terminal"]
            and not transition["owned_by_setup"]
        ):
            return (
                SETUP_TRANSITION_CONTEXT_MISMATCH
                if (setup or {}).get("operation_id")
                else SETUP_TRANSITION_OWNER_UNPROVEN
            )
        return None

    @staticmethod
    def _resolve_recoverable(state, blocking, context):
        if state == STATE_OPERATION_RUNNING:
            return False
        orphaned_context = bool(
            context
            and (not context["matches_transition"] or not context["domain_valid"])
        )
        return bool(blocking is not None or orphaned_context)

    def _fingerprint(self, setup, transition, context):
        """Bind exactly the durable facts the verdict was derived from.

        In-process liveness is deliberately out: a claim that comes and goes
        must not invalidate a preview the operator is still reading, and the
        running-operation gate is re-evaluated at execution time anyway.
        """

        body = {
            "fingerprint_version": 1,
            "setup": None
            if setup is None
            else {
                key: setup[key]
                for key in (
                    "present",
                    "readable",
                    "digest",
                    "workflow_id",
                    "status",
                    "cleanup",
                    "artifacts_claimed",
                    "operation_id",
                )
            },
            "transition": None
            if transition is None
            else {
                key: transition[key]
                for key in (
                    "present",
                    "readable",
                    "digest",
                    "operation_id",
                    "mode",
                    "stage",
                )
            },
            "upgrade_context": None
            if context is None
            else {
                key: context[key]
                for key in (
                    "present",
                    "identity_readable",
                    "domain_valid",
                    "digest",
                    "operation_id",
                    "target_system_tag",
                )
            },
        }
        encoded = json.dumps(
            body, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    # --- switching ----------------------------------------------------------

    def transition_cancel_verdict(self):
        """Who owns the active transition, for the narrow cancel primitive.

        The primitive still performs its own cancellation; only the ownership
        question is answered here, so ``server.py`` keeps no second classifier.
        """

        view = self.inspect()
        transition = view["transition"] or {}
        if not transition.get("present"):
            return CANCEL_VERDICT_ABSENT
        if not transition.get("readable"):
            return CANCEL_VERDICT_UNSUPPORTED
        if transition.get("mode") in SETUP_TRANSITION_MODES:
            return CANCEL_VERDICT_SETUP_OWNED
        if view["owner"] == OWNER_CONFLICT:
            return CANCEL_VERDICT_UNSUPPORTED
        if transition.get("mode") == TRANSITION_MODE_GUIDED_UPGRADE:
            return CANCEL_VERDICT_GUIDED_UPGRADE
        return CANCEL_VERDICT_UNSUPPORTED

    def plan_switch(self, target):
        """Preview one switch without changing anything."""

        target = self._require_target(target)
        view = self.inspect()
        return self._switch_plan(view, target)

    def switch(
        self, target, *, expected_fingerprint=None, confirm=False, session_id=None
    ):
        """Terminate the previous guided workflow exactly, then enter ``target``."""

        target = self._require_target(target)
        with self._lock:
            view = self.inspect()
            self._require_fingerprint(view, expected_fingerprint)
            plan = self._switch_plan(view, target)
            if plan["blocked"]:
                raise self._blocked_error(view, plan["blocking_reason"])
            if plan["confirmation_required"] and confirm is not True:
                raise AdminWorkflowLifecycleError(
                    CONFIRMATION_REQUIRED,
                    "Confirm the switch before the previous workflow is stopped.",
                    status=400,
                    lifecycle=view,
                )
            result = self._execute_switch(view, plan, session_id=session_id)
        result["lifecycle"] = self.inspect()
        return result

    @staticmethod
    def _require_target(target):
        if target not in SWITCH_TARGETS:
            raise ValueError(f"unsupported workflow target: {target!r}")
        return target

    def _switch_plan(self, view, target):
        owner = view["owner"]
        action = self._switch_action(view, target)
        blocked = self._switch_is_blocked(view, target, action)
        # A blocked switch resets nothing, so it promises nothing: the scope is
        # what this request will do, never what some other state would allow.
        will_reset = [] if blocked else self._switch_reset_scope(view, action)
        return {
            "ok": True,
            "target": target,
            "current_owner": owner,
            "target_owner": target,
            "action": action,
            "blocked": blocked,
            "blocking_reason": view["blocking_reason"] if blocked else None,
            "confirmation_required": bool(will_reset) and not blocked,
            "will_reset": will_reset,
            "will_preserve": list(PRESERVED_BY_SWITCH),
            "resume_available": bool(
                (view["transition"] or {}).get("resume_available")
            ),
            "recoverable": view["recoverable"],
            "fingerprint": view["fingerprint"],
            "lifecycle": view,
        }

    @classmethod
    def _switch_is_blocked(cls, view, target, action):
        """Whether this request may not run against the current state.

        A blocking reason blocks by default. The two exceptions are narrow and
        named: a no-op whose target is already safely in charge, and *entering*
        the owner that already owns the console — resuming Guided Setup is not a
        switch away from it, so an operation it is still finishing must not lock
        the operator out of their own workflow.

        Entry never walks through a reason that resuming cannot clear. An
        unprovable transition owner is structural: it is exactly as blocking
        after the resume as before, so answering ``ok`` there would contradict
        the verdict this same arbiter keeps reporting.
        """

        reason = view["blocking_reason"]
        if reason is None:
            return False
        if cls._noop_satisfies_target(view, target, action):
            return False
        entering_owner = target == view["owner"] and action in {
            ACTION_RESUME_SETUP,
            ACTION_START_SETUP,
            ACTION_NONE,
        }
        return not (entering_owner and reason not in _ENTRY_BLOCKING_REASONS)

    @staticmethod
    def _noop_satisfies_target(view, target, action):
        """Whether doing nothing genuinely leaves ``target`` in charge.

        Only then may a no-op answer through a blocking reason. An unknown,
        conflicting or unreadable owner never qualifies: reporting success there
        is what turned a block into a silent bypass.
        """

        if action != ACTION_NONE:
            return False
        owner = view["owner"]
        if owner in {OWNER_UNKNOWN, OWNER_CONFLICT, OWNER_ALIGN_EXISTING}:
            return False
        if view["state"] == STATE_MALFORMED:
            return False
        if target == TARGET_GUIDED_UPGRADE:
            return owner in {OWNER_NONE, OWNER_GUIDED_UPGRADE}
        if target == TARGET_NONE:
            return owner == OWNER_NONE
        return False

    @staticmethod
    def _switch_action(view, target):
        owner = view["owner"]
        setup = view["setup"]
        if target == TARGET_GUIDED_UPGRADE:
            return ACTION_DISCARD_SETUP if owner == OWNER_GUIDED_SETUP else ACTION_NONE
        if target == TARGET_NONE:
            if owner == OWNER_GUIDED_SETUP:
                return ACTION_DISCARD_SETUP
            if owner == OWNER_GUIDED_UPGRADE:
                return ACTION_CANCEL_UPGRADE
            return ACTION_NONE
        if owner == OWNER_GUIDED_UPGRADE:
            return ACTION_CANCEL_UPGRADE
        if owner == OWNER_GUIDED_SETUP and setup and setup["status"] == STATUS_ACTIVE:
            return ACTION_RESUME_SETUP
        if owner in {OWNER_NONE, OWNER_GUIDED_SETUP}:
            return ACTION_START_SETUP
        return ACTION_NONE

    @staticmethod
    def _switch_reset_scope(view, action):
        if action == ACTION_DISCARD_SETUP:
            scope = ["Guided Setup workflow"]
            transition = view["transition"] or {}
            if transition.get("owned_by_setup") and not transition.get("terminal"):
                scope.append("Setup System Build transition")
            if (view["setup"] or {}).get("artifacts_claimed"):
                scope.append("workflow-owned preview files")
            return scope
        if action == ACTION_CANCEL_UPGRADE:
            scope = ["Guided Upgrade System Build transition"]
            if (view["upgrade_context"] or {}).get("matches_transition"):
                scope.append("Guided Upgrade execution context")
            return scope
        return []

    def _execute_switch(self, view, plan, *, session_id):
        action = plan["action"]
        result = {"ok": True, "action": action, "target": plan["target"]}
        if action == ACTION_DISCARD_SETUP:
            result.update(self._terminate_setup(view))
        elif action == ACTION_CANCEL_UPGRADE:
            result.update(self._cancel_upgrade(view))
        if plan["target"] == TARGET_GUIDED_SETUP and action != ACTION_NONE:
            result.update(self._start_or_resume_setup(session_id=session_id))
        return result

    def _terminate_setup(self, view):
        setup = view["setup"] or {}
        workflow_id = setup.get("workflow_id")
        operation = (
            "cleanup_retry" if setup.get("status") != STATUS_ACTIVE else "abandon"
        )
        try:
            claim = (
                self._lifecycle.claim_termination(
                    workflow_id=workflow_id, operation=operation
                )
                if workflow_id and self._lifecycle is not None
                else contextlib.nullcontext()
            )
        except GuidedSetupWorkflowError as exc:
            raise AdminWorkflowLifecycleError(
                exc.code, exc.message, status=exc.status, lifecycle=view
            ) from exc
        try:
            with claim:
                outcome = abandon_setup_workflow(
                    alignment=self._alignment,
                    coordinator=self._coordinator,
                    status=self._alignment_status(),
                    workflows=self._workflows,
                    workflow_id=workflow_id,
                )
                if self._setup_intents is not None and workflow_id:
                    self._setup_intents.invalidate_workflow(workflow_id)
        except SetupWorkflowAbandonError as exc:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_SWITCH_BLOCKED, exc.message, detail=exc.code, lifecycle=view
            ) from exc
        except SystemAlignmentError as exc:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_OPERATION_IN_PROGRESS,
                exc.message,
                detail=exc.code,
                lifecycle=view,
            ) from exc
        except OSError as exc:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_FAILED,
                f"The Guided Setup state could not be cleared: {exc}",
                status=500,
                lifecycle=view,
            ) from exc
        if outcome.get("ok") is not True:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_REQUIRED,
                outcome.get("message")
                or "The previous Guided Setup could not be cleared completely.",
                detail=outcome.get("error") or CLEANUP_INCOMPLETE,
                lifecycle=self.inspect(),
            )
        return {
            "cancelled_operation_id": (outcome.get("transition") or {}).get(
                "operation_id"
            )
            if setup.get("operation_id")
            else None,
            "cleanup_state": outcome.get("cleanup_state"),
            "setup_workflow_id": None,
        }

    def _cancel_upgrade(self, view):
        transition = view["transition"] or {}
        operation_id = transition.get("operation_id")
        try:
            cancelled = self._alignment.cancel(
                operation_id=operation_id, coordinator=self._coordinator
            )
        except SystemAlignmentError as exc:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_OPERATION_IN_PROGRESS,
                exc.message,
                detail=exc.code,
                lifecycle=view,
            ) from exc
        cleared = False
        if (cancelled or {}).get("stage") == "cancelled":
            cleared = self._clear_upgrade_context(operation_id)
        return {
            "cancelled_operation_id": operation_id,
            "cleared_upgrade_context": cleared,
        }

    def _clear_upgrade_context(self, operation_id):
        store = self._upgrade_contexts
        if store is None or not operation_id:
            return False
        try:
            return bool(store.clear_for_operation(operation_id))
        except OSError:
            # A leftover context is refused by its own fail-closed loader; it
            # must never fail the lifecycle event that triggered the cleanup.
            return False

    def _start_or_resume_setup(self, *, session_id):
        try:
            record = self._workflows.ensure_active()
        except GuidedSetupWorkflowError as exc:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_REQUIRED,
                exc.message,
                detail=exc.code,
                lifecycle=self.inspect(),
            ) from exc
        result = {"setup_workflow_id": record["workflow_id"]}
        if self._setup_intents is not None and session_id:
            intent = self._setup_intents.issue(
                session_id=session_id, workflow_id=record["workflow_id"]
            )
            result["setup_intent_id"] = intent.intent_id
        return result

    # --- recovery -----------------------------------------------------------

    def plan_recovery(self):
        """Preview both recoveries against the current lifecycle state."""

        view = self.inspect()
        safe_actions = self._safe_recovery_scope(view)
        stale = [
            {"name": name, "reason": reason}
            for name, _path, reason in self._stale_state_targets(view)
        ]
        running = view["state"] == STATE_OPERATION_RUNNING
        return {
            "ok": True,
            "blocking": not view["switchable"],
            "operation_running": running,
            "safe": {
                "available": bool(safe_actions) and not running and self._readable(view),
                "actions": safe_actions,
                "confirmation_required": True,
            },
            "advanced": {
                "available": bool(stale) and not running,
                "files": stale,
                "confirmation_required": True,
                "reason_required": True,
            },
            "will_preserve": list(PRESERVED_BY_RECOVERY),
            "fingerprint": view["fingerprint"],
            "lifecycle": view,
        }

    def recover(self, *, mode, expected_fingerprint=None, confirm=False, reason=None):
        """Run one recovery mode; a refusal changes nothing."""

        if mode not in RECOVERY_MODES:
            raise ValueError(f"unsupported recovery mode: {mode!r}")
        with self._lock:
            view = self.inspect()
            self._require_fingerprint(view, expected_fingerprint)
            if confirm is not True:
                raise AdminWorkflowLifecycleError(
                    CONFIRMATION_REQUIRED,
                    "Confirm the recovery before Admin workflow state is changed.",
                    status=400,
                    lifecycle=view,
                )
            reason = (reason or "").strip()
            if mode == RECOVERY_MODE_RELEASE_STALE_STATE and not reason:
                raise AdminWorkflowLifecycleError(
                    RECOVERY_REASON_REQUIRED,
                    "Give a reason before releasing stale Admin workflow state.",
                    status=400,
                    lifecycle=view,
                )
            self._require_recovery_safe(view, mode)
            if mode == RECOVERY_MODE_SAFE:
                result = self._recover_safe(view)
            else:
                result = self._release_stale_state(view, reason=reason)
        result["lifecycle"] = self.inspect()
        return result

    @staticmethod
    def _readable(view):
        setup = view["setup"]
        transition = view["transition"]
        return not (
            (setup and not setup["readable"]) or (transition and not transition["readable"])
        )

    def _require_recovery_safe(self, view, mode):
        if view["state"] == STATE_OPERATION_RUNNING:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_UNSAFE,
                "An Admin workflow operation is still running. Recovery stays "
                "blocked until it finishes.",
                detail=view["blocking_reason"] or WORKFLOW_OPERATION_IN_PROGRESS,
                lifecycle=view,
            )
        if mode == RECOVERY_MODE_SAFE and not self._readable(view):
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_UNSAFE,
                "The stored Admin workflow state could not be read, so the "
                "normal recovery cannot resolve it.",
                detail=WORKFLOW_STATE_MALFORMED,
                lifecycle=view,
            )
        if mode == RECOVERY_MODE_RELEASE_STALE_STATE:
            self._require_no_external_mutation(view)

    def _require_no_external_mutation(self, view):
        """Require positive proof that no Admin replacement is running.

        The durable stage cannot stand in for this: the states that need a
        release — unreadable transition, unsupported mode, no operation identity
        — are exactly the ones whose stage proves nothing. So only a probe that
        answers ``INACTIVE`` may continue; unreachable, malformed and missing
        probes are all refusals.
        """

        probe = self._install_state_probe
        if not callable(probe):
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_UNSAFE,
                "This Admin cannot prove that no replacement is running, so "
                "releasing Admin workflow state is refused.",
                detail=INSTALL_STATE_UNAVAILABLE,
                lifecycle=view,
            )
        try:
            activity = probe((view["transition"] or {}).get("operation_id"))
        except Exception as exc:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_UNSAFE,
                "The installed system state could not be inspected, so "
                "releasing Admin workflow state is refused.",
                detail=INSTALL_STATE_UNAVAILABLE,
                lifecycle=view,
            ) from exc
        if activity is ReplacementActivity.ACTIVE:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_UNSAFE,
                "An Admin replacement is still running. Releasing Admin "
                "workflow state is refused.",
                detail=REPLACEMENT_ACTIVE,
                lifecycle=view,
            )
        if activity is not ReplacementActivity.INACTIVE:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_UNSAFE,
                "Whether an Admin replacement is running could not be proven, "
                "so releasing Admin workflow state is refused.",
                detail=INSTALL_STATE_UNAVAILABLE,
                lifecycle=view,
            )

    def _safe_recovery_scope(self, view):
        """What the normal domain operations can still resolve by themselves."""

        actions = []
        transition = view["transition"] or {}
        if (
            transition.get("readable")
            and not transition.get("terminal")
            and transition.get("cancellable")
            and transition.get("mode") not in SETUP_TRANSITION_MODES
            and transition.get("mode") in SUPPORTED_TRANSITION_MODES
        ):
            actions.append("transition_cancel")
        setup = view["setup"] or {}
        # An abandon that cannot name the transition refuses on both halves, so
        # offering it as a recovery would promise a convergence it never reaches.
        unprovable_owner = view["blocking_reason"] in {
            SETUP_TRANSITION_CONTEXT_MISMATCH,
            SETUP_TRANSITION_OWNER_UNPROVEN,
        }
        if (
            setup.get("readable")
            and not unprovable_owner
            and (
                setup.get("status") == STATUS_ACTIVE
                or setup.get("cleanup") in {CLEANUP_PENDING, CLEANUP_REVIEW_REQUIRED}
            )
        ):
            actions.append("setup_cleanup")
        context = view["upgrade_context"] or {}
        # Only a context the loader still accepts may be cleared as an ordinary
        # orphan; an unusable one is evidence an operator has to see backed up.
        if (
            context.get("present")
            and context.get("domain_valid")
            and not context.get("matches_transition")
        ):
            actions.append("upgrade_context_clear")
        return actions

    def _recover_safe(self, view):
        performed = []
        transition = view["transition"] or {}
        scope = self._safe_recovery_scope(view)
        if "transition_cancel" in scope:
            operation_id = transition.get("operation_id")
            try:
                cancelled = self._alignment.cancel(
                    operation_id=operation_id, coordinator=self._coordinator
                )
            except SystemAlignmentError as exc:
                raise AdminWorkflowLifecycleError(
                    WORKFLOW_RECOVERY_UNSAFE,
                    exc.message,
                    detail=exc.code,
                    lifecycle=view,
                ) from exc
            performed.append("transition_cancel")
            if (cancelled or {}).get(
                "stage"
            ) == "cancelled" and self._clear_upgrade_context(operation_id):
                performed.append("upgrade_context_clear")
        if "setup_cleanup" in scope:
            self._terminate_setup(view)
            performed.append("setup_cleanup")
        if "upgrade_context_clear" in scope and "upgrade_context_clear" not in performed:
            context = view["upgrade_context"] or {}
            if self._clear_upgrade_context(context.get("operation_id")):
                performed.append("upgrade_context_clear")
        return {"ok": True, "mode": RECOVERY_MODE_SAFE, "actions": performed}

    # --- stale-state release -------------------------------------------------

    def _state_dir(self):
        base = self._admin_data_dir
        return (base / "state") if base is not None else Path("state")

    def _recoverable_state_files(self):
        """The server-derived allowlist; a browser never names a recovery path."""

        return (
            ("state/guided-setup-workflow.json", Path(self._workflows.path)),
            ("state/pending-transition.json", Path(self._transition_path)),
            (
                "state/guided-upgrade-context.json",
                Path(self._upgrade_contexts.path)
                if self._upgrade_contexts is not None
                else self._state_dir() / "guided-upgrade-context.json",
            ),
        )

    def _stale_state_reasons(self, view):
        """Why each allowlisted file is beyond the normal domain operations.

        Unreadable state is the obvious case, but a *readable* record can be
        just as unusable: a transition mode this Admin does not support, a
        contradiction between two records, a Setup that cannot name the
        transition it would have to cancel, or a context that no longer
        reproduces. Every one of those is a deadlock without a release.
        """

        reasons = {}
        setup = view["setup"]
        transition = view["transition"]
        context = view["upgrade_context"]
        if setup and not setup["readable"]:
            reasons["state/guided-setup-workflow.json"] = STALE_UNREADABLE_STATE
        if transition and not transition["readable"]:
            reasons["state/pending-transition.json"] = STALE_UNREADABLE_STATE
        elif (
            transition
            and not transition["terminal"]
            and transition["mode"] not in SUPPORTED_TRANSITION_MODES
        ):
            reasons["state/pending-transition.json"] = (
                STALE_UNSUPPORTED_TRANSITION_MODE
            )
        blocking = view["blocking_reason"]
        if blocking == WORKFLOW_OWNER_CONFLICT:
            reasons.setdefault(
                "state/guided-setup-workflow.json", WORKFLOW_OWNER_CONFLICT
            )
            reasons.setdefault(
                "state/pending-transition.json", WORKFLOW_OWNER_CONFLICT
            )
        elif blocking in {
            SETUP_TRANSITION_CONTEXT_MISMATCH,
            SETUP_TRANSITION_OWNER_UNPROVEN,
        }:
            reasons.setdefault("state/guided-setup-workflow.json", blocking)
            reasons.setdefault("state/pending-transition.json", blocking)
        if context and context["present"] and not context["domain_valid"]:
            reasons["state/guided-upgrade-context.json"] = (
                STALE_UNUSABLE_UPGRADE_CONTEXT
            )
        return reasons

    def _stale_state_targets(self, view):
        """Allowlisted files that exist and cannot be repaired normally."""

        reasons = self._stale_state_reasons(view)
        targets = []
        for name, path in self._recoverable_state_files():
            reason = reasons.get(name)
            if reason is None:
                continue
            try:
                exists = path.is_file()
            except OSError:
                exists = False
            if exists:
                targets.append((name, path, reason))
        return targets

    def _verify_state_directory(self, view):
        state_dir = self._state_dir()
        try:
            if state_dir.is_symlink():
                raise AdminWorkflowLifecycleError(
                    WORKFLOW_RECOVERY_UNSAFE,
                    "The Admin state directory is not a plain directory, so "
                    "releasing workflow state is refused.",
                    detail="state_directory_unsafe",
                    lifecycle=view,
                )
            resolved = os.path.realpath(state_dir)
        except OSError as exc:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_UNSAFE,
                "The Admin state directory could not be inspected.",
                detail="state_directory_unsafe",
                lifecycle=view,
            ) from exc
        return Path(resolved)

    def _verified_target(self, path, resolved_state_dir, view):
        if path.is_symlink() or os.path.dirname(os.path.realpath(path)) != str(
            resolved_state_dir
        ):
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_UNSAFE,
                "An Admin workflow state file is not where the Admin stores it, "
                "so releasing it is refused.",
                detail="state_file_unsafe",
                lifecycle=view,
            )
        return path

    def _backup_directory(self, resolved_state_dir):
        stamp = self._clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root = resolved_state_dir / RECOVERY_BACKUP_DIRECTORY
        candidate = root / stamp
        suffix = 1
        while candidate.exists():
            suffix += 1
            candidate = root / f"{stamp}-{suffix}"
        candidate.mkdir(parents=True)
        return candidate

    def _release_stale_state(self, view, *, reason):
        targets = self._stale_state_targets(view)
        if not targets:
            return {
                "ok": True,
                "mode": RECOVERY_MODE_RELEASE_STALE_STATE,
                "released": [],
                "backup": None,
            }
        resolved_state_dir = self._verify_state_directory(view)
        verified = [
            (name, self._verified_target(path, resolved_state_dir, view), reason)
            for name, path, reason in targets
        ]
        try:
            directory = self._backup_directory(resolved_state_dir)
            entries = []
            for name, path, target_reason in verified:
                raw = path.read_bytes()
                (directory / path.name).write_bytes(raw)
                entries.append(
                    {
                        "name": name,
                        "reason": target_reason,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw),
                    }
                )
            manifest = {
                "manifest_version": RECOVERY_MANIFEST_VERSION,
                "created_at": self._clock()
                .astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "mode": RECOVERY_MODE_RELEASE_STALE_STATE,
                "reason": reason[:MAX_RECOVERY_REASON_CHARS],
                "admin_revision": self._revision,
                "lifecycle_fingerprint": view["fingerprint"],
                "files": entries,
            }
            (directory / RECOVERY_MANIFEST_FILE).write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            for _name, path, _reason in verified:
                path.unlink()
        except OSError as exc:
            raise AdminWorkflowLifecycleError(
                WORKFLOW_RECOVERY_FAILED,
                f"The Admin workflow state could not be released: {exc}",
                status=500,
                lifecycle=view,
            ) from exc
        return {
            "ok": True,
            "mode": RECOVERY_MODE_RELEASE_STALE_STATE,
            "released": [name for name, _path, _reason in verified],
            "backup": {
                "directory": f"state/{RECOVERY_BACKUP_DIRECTORY}/{directory.name}",
                "files": [entry["name"] for entry in entries],
            },
        }

    # --- shared refusals ------------------------------------------------------

    def _require_fingerprint(self, view, expected_fingerprint):
        if expected_fingerprint == view["fingerprint"]:
            return
        raise AdminWorkflowLifecycleError(
            WORKFLOW_LIFECYCLE_CHANGED,
            "The Admin workflow state changed since this view was loaded. "
            "Review the current state and try again.",
            lifecycle=view,
        )

    @staticmethod
    def _blocked_error(view, blocking_reason):
        code = _BLOCKING_ERROR_CODES.get(blocking_reason, WORKFLOW_SWITCH_BLOCKED)
        message = _BLOCKING_MESSAGES.get(
            code,
            "The current guided workflow cannot be switched away from right now.",
        )
        return AdminWorkflowLifecycleError(
            code, message, detail=blocking_reason, lifecycle=view
        )


__all__ = [
    "ACTION_CANCEL_UPGRADE",
    "ACTION_DISCARD_SETUP",
    "ACTION_NONE",
    "ACTION_RESUME_SETUP",
    "ACTION_START_SETUP",
    "AdminWorkflowLifecycleError",
    "AdminWorkflowLifecycleService",
    "CANCEL_VERDICT_ABSENT",
    "CANCEL_VERDICT_GUIDED_UPGRADE",
    "CANCEL_VERDICT_SETUP_OWNED",
    "CANCEL_VERDICT_UNSUPPORTED",
    "CONFIRMATION_REQUIRED",
    "INSTALL_STATE_UNAVAILABLE",
    "OWNER_ALIGN_EXISTING",
    "OWNER_CONFLICT",
    "OWNER_GUIDED_SETUP",
    "OWNER_GUIDED_UPGRADE",
    "OWNER_NONE",
    "OWNER_UNKNOWN",
    "PRESERVED_BY_RECOVERY",
    "PRESERVED_BY_SWITCH",
    "RECOVERY_MODES",
    "RECOVERY_MODE_RELEASE_STALE_STATE",
    "RECOVERY_MODE_SAFE",
    "RECOVERY_REASON_REQUIRED",
    "REPLACEMENT_ACTIVE",
    "ReplacementActivity",
    "STALE_UNREADABLE_STATE",
    "STALE_UNSUPPORTED_TRANSITION_MODE",
    "STALE_UNUSABLE_UPGRADE_CONTEXT",
    "STATE_ACTIVE",
    "STATE_CLEANUP_PENDING",
    "STATE_CONFLICT",
    "STATE_IDLE",
    "STATE_MALFORMED",
    "STATE_OPERATION_RUNNING",
    "STATE_REVIEW_REQUIRED",
    "SWITCH_TARGETS",
    "TARGET_GUIDED_SETUP",
    "TARGET_GUIDED_UPGRADE",
    "TARGET_NONE",
    "WORKFLOW_LIFECYCLE_CHANGED",
    "WORKFLOW_OPERATION_IN_PROGRESS",
    "WORKFLOW_OWNER_CONFLICT",
    "WORKFLOW_OWNER_UNKNOWN",
    "WORKFLOW_RECOVERY_FAILED",
    "WORKFLOW_RECOVERY_REQUIRED",
    "WORKFLOW_RECOVERY_UNSAFE",
    "WORKFLOW_STATE_MALFORMED",
    "WORKFLOW_SWITCH_BLOCKED",
    "WORKFLOW_SWITCH_REQUIRED",
    "admin_replacement_activity",
    "worker_aware_alignment_status",
]
