# SPDX-License-Identifier: AGPL-3.0-or-later
"""Atomic worker-ownership and abandonment coordination.

The coordinator is the one primitive that keeps worker registration and
transition abandonment mutually exclusive per operation id, so it is impossible
to end with a cancelled transition and a still-active matching worker.
"""

import threading

import pytest

from admin.operation_coordinator import (
    OperationCoordinator,
    OperationWorkerActive,
    OperationWorkerStatusUnavailable,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
    pytest.mark.system_build,
]


def test_claim_makes_the_operation_active_and_release_clears_it():
    coordinator = OperationCoordinator()
    assert coordinator.is_active("op") is False
    token = coordinator.claim("op")
    assert token is not None
    assert coordinator.is_active("op") is True
    coordinator.release(token)
    assert coordinator.is_active("op") is False


def test_missing_operation_id_is_never_active_or_claimable():
    coordinator = OperationCoordinator()
    assert coordinator.claim("") is None
    assert coordinator.claim(None) is None
    assert coordinator.is_active("") is False
    assert coordinator.is_active(None) is False


def test_abandon_wins_when_no_worker_is_active_and_blocks_later_claims():
    coordinator = OperationCoordinator()
    calls = []

    def cancel():
        calls.append("cancelled")
        return "done"

    assert coordinator.abandon("op", cancel) == "done"
    assert calls == ["cancelled"]
    # Cancellation won the race: a worker that tries to start afterwards is refused.
    assert coordinator.claim("op") is None
    assert coordinator.is_active("op") is False


def test_abandon_is_rejected_while_a_worker_holds_a_claim():
    coordinator = OperationCoordinator()
    token = coordinator.claim("op")

    def cancel():  # pragma: no cover - must never run while a worker is active
        raise AssertionError("cancel must not run while a worker is active")

    with pytest.raises(OperationWorkerActive):
        coordinator.abandon("op", cancel)
    assert coordinator.is_active("op") is True
    # Once the worker stops, the same operation can be abandoned.
    coordinator.release(token)
    assert coordinator.abandon("op", lambda: "ok") == "ok"


def test_failed_cancel_rolls_back_the_abandonment_marker():
    coordinator = OperationCoordinator()

    def failing_cancel():
        raise RuntimeError("store write failed")

    with pytest.raises(RuntimeError):
        coordinator.abandon("op", failing_cancel)
    # The transition was not cancelled, so a worker may still legitimately claim it.
    assert coordinator.claim("op") is not None


def test_worker_claim_loses_when_abandon_holds_the_lock():
    # Deterministic race: the abandon callback blocks while holding the
    # coordinator lock; a concurrent claim must wait, then be rejected because
    # cancellation won.
    coordinator = OperationCoordinator()
    op = "op-race"
    in_cancel = threading.Event()
    finish_cancel = threading.Event()
    outcome = {}

    def cancel():
        in_cancel.set()
        assert finish_cancel.wait(2)
        return "cancelled"

    def abandon_worker():
        outcome["abandon"] = coordinator.abandon(op, cancel)

    def claim_worker():
        assert in_cancel.wait(2)
        # The abandon is mid-cancel and holds the lock; this blocks until it
        # commits, then is rejected.
        outcome["token"] = coordinator.claim(op)

    ta = threading.Thread(target=abandon_worker)
    tc = threading.Thread(target=claim_worker)
    ta.start()
    tc.start()
    assert in_cancel.wait(2)
    finish_cancel.set()
    ta.join(2)
    tc.join(2)

    assert outcome["abandon"] == "cancelled"
    assert outcome["token"] is None
    assert coordinator.is_active(op) is False


def test_abandon_loses_when_a_worker_claims_first_under_contention():
    # The mirror race: a worker claims and holds the operation; a concurrent
    # abandon must be rejected and the durable cancel must never run.
    coordinator = OperationCoordinator()
    op = "op-race2"
    token = coordinator.claim(op)
    result = {}

    def abandon_worker():
        try:
            coordinator.abandon(
                op,
                lambda: result.setdefault("cancelled", True),
            )
        except OperationWorkerActive:
            result["rejected"] = True

    thread = threading.Thread(target=abandon_worker)
    thread.start()
    thread.join(2)

    assert result.get("rejected") is True
    assert "cancelled" not in result
    coordinator.release(token)


def test_release_operation_clears_all_claims_idempotently():
    coordinator = OperationCoordinator()
    coordinator.claim("op")
    coordinator.claim("op")
    assert coordinator.is_active("op") is True
    coordinator.release_operation("op")
    assert coordinator.is_active("op") is False
    # Repeated release after the claims are gone is a no-op.
    coordinator.release_operation("op")
    assert coordinator.is_active("op") is False


def test_unavailable_liveness_is_a_distinct_typed_error():
    # The typed error the service maps to transition_worker_status_unavailable.
    exc = OperationWorkerStatusUnavailable("op")
    assert exc.operation_id == "op"
