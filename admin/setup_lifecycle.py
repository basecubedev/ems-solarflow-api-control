# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mutually exclusive ownership of one Guided Setup workflow's lifecycle.

Verifying workflow and preview authority once proves a mutation *may* start; it
does not keep the workflow alive while the mutation runs. Every Setup operation
that changes durable state therefore holds a named claim on its workflow for as
long as its irreversible work can still commit, and every terminal operation
holds a mutually exclusive one:

* a mutation claim is refused while any claim is held (``setup_operation_in_progress``)
  and once terminalization has begun (``setup_workflow_not_active``);
* a terminal claim is refused while a mutation claim is held, so abandon,
  supersede and completion can never terminalize underneath a running commit;
* a refused claim changes nothing, and a claim is released on success and on
  exception alike.

Transient by design: claims live in this process only, shared by the HTTP and
HTTPS listeners through one ``AdminRuntime``. Durable workflow and cleanup state
belongs to :mod:`admin.guided_setup_workflow`, so an Admin restart starts with no
claims and every commit is still gated by the durable record — never by a
remembered claim that cannot have survived.

Deliberately NOT here: System Alignment stage state (:mod:`admin.system_alignment`
owns it), worker liveness per transition (:class:`admin.operation_coordinator.OperationCoordinator`
owns that) and any knowledge of the filesystem, Docker or HTTP.

See ``docs/technical/admin-workflow-state.md``.
"""

import threading

from admin.guided_setup_workflow import (
    SETUP_WORKFLOW_NOT_ACTIVE,
    SETUP_WORKFLOW_REQUIRED,
    WORKFLOW_NOT_ACTIVE_MESSAGE,
    WORKFLOW_REQUIRED_MESSAGE,
    GuidedSetupWorkflowError,
)

SETUP_OPERATION_IN_PROGRESS = "setup_operation_in_progress"

MUTATION_OPERATIONS = frozenset(
    {
        "config_write",
        "config_apply",
        "deployment_prepare",
        "deployment_start",
        "permission_repair",
        "container_conflict_resolution",
    }
)
TERMINAL_OPERATIONS = frozenset(
    {"abandon", "supersede", "cleanup_retry", "complete"}
)

MUTATION = "mutation"
TERMINATION = "termination"

# Terminalization is a permanent barrier for the workflow it names, but the
# barrier only ever *adds* to the durable record's own refusal, so forgetting the
# oldest entries of a long-lived Admin process cannot authorize anything.
MAX_TERMINALIZED = 64


class SetupOperationInProgress(GuidedSetupWorkflowError):
    """Another Setup operation owns this workflow right now."""

    def __init__(self, operation):
        super().__init__(
            SETUP_OPERATION_IN_PROGRESS,
            "Guided Setup is still finishing another operation. Wait for it to "
            "finish, then try again.",
            status=409,
        )
        self.operation = operation


class SetupLifecycleClaim:
    """Exclusive ownership of one workflow for one named operation."""

    def __init__(self, coordinator, *, workflow_id, operation, kind, token):
        self.workflow_id = workflow_id
        self.operation = operation
        self.kind = kind
        self._coordinator = coordinator
        self._token = token
        self._released = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release(failed=exc_type is not None)
        return False

    def release(self, *, failed=False):
        """Release the claim; idempotent.

        ``failed`` rolls a terminal claim's barrier back: a termination that
        raised changed no durable state, so the workflow must stay mutable.
        """

        if self._released:
            return
        self._released = True
        self._coordinator._release(self._token, failed=failed)


class SetupLifecycleCoordinator:
    """Process-shared claim arbiter for the Guided Setup workflow lifecycle."""

    def __init__(self):
        self._lock = threading.Lock()
        self._claims = {}
        self._terminalized = {}
        self._sequence = 0

    # --- claims -----------------------------------------------------------

    def claim_mutation(self, *, workflow_id, operation):
        return self._claim(workflow_id, operation, MUTATION)

    def claim_termination(self, *, workflow_id, operation):
        return self._claim(workflow_id, operation, TERMINATION)

    def active_operation(self, workflow_id):
        """The operation currently owning ``workflow_id``, else ``None``."""

        if not workflow_id:
            return None
        with self._lock:
            claim = self._claims.get(workflow_id)
        return claim[1] if claim else None

    def is_terminalized(self, workflow_id):
        if not workflow_id:
            return False
        with self._lock:
            return workflow_id in self._terminalized

    # --- internals --------------------------------------------------------

    def _claim(self, workflow_id, operation, kind):
        allowed = MUTATION_OPERATIONS if kind == MUTATION else TERMINAL_OPERATIONS
        if operation not in allowed:
            raise ValueError(f"unknown Guided Setup {kind} operation: {operation!r}")
        if not isinstance(workflow_id, str) or not workflow_id:
            raise GuidedSetupWorkflowError(
                SETUP_WORKFLOW_REQUIRED, WORKFLOW_REQUIRED_MESSAGE
            )
        with self._lock:
            # Terminalization is checked first on purpose: once it has begun the
            # answer is "this workflow is gone", not "retry in a moment".
            if kind == MUTATION and workflow_id in self._terminalized:
                raise GuidedSetupWorkflowError(
                    SETUP_WORKFLOW_NOT_ACTIVE, WORKFLOW_NOT_ACTIVE_MESSAGE
                )
            held = self._claims.get(workflow_id)
            if held is not None:
                raise SetupOperationInProgress(held[1])
            self._sequence += 1
            token = (workflow_id, self._sequence)
            self._claims[workflow_id] = (kind, operation, token)
            if kind == TERMINATION:
                self._mark_terminalized_locked(workflow_id)
        return SetupLifecycleClaim(
            self, workflow_id=workflow_id, operation=operation, kind=kind, token=token
        )

    def _mark_terminalized_locked(self, workflow_id):
        self._terminalized[workflow_id] = True
        while len(self._terminalized) > MAX_TERMINALIZED:
            del self._terminalized[next(iter(self._terminalized))]

    def _release(self, token, *, failed=False):
        workflow_id = token[0]
        with self._lock:
            held = self._claims.get(workflow_id)
            if held is None or held[2] != token:
                return
            del self._claims[workflow_id]
            if failed and held[0] == TERMINATION:
                self._terminalized.pop(workflow_id, None)


__all__ = [
    "MUTATION_OPERATIONS",
    "SETUP_OPERATION_IN_PROGRESS",
    "TERMINAL_OPERATIONS",
    "SetupLifecycleClaim",
    "SetupLifecycleCoordinator",
    "SetupOperationInProgress",
]
