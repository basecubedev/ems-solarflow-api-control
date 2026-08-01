# SPDX-License-Identifier: AGPL-3.0-or-later
"""One normalized reading of "which guided workflow owns the Admin right now".

Guided Setup, Guided Upgrade and the System Build transition each own a durable
record, and until now every caller that needed to know how they relate answered
it for itself: the start path, the upgrade conflict gate, the transition-write
gate and two browser helpers. Their answers diverged, so a user could reach a
state no single route could resolve.

``AdminWorkflowLifecycleService.inspect()`` is that one reading. It owns no
durable state of its own: it reads the existing authorities, normalizes them
into one owner/state verdict and binds the exact durable facts it used into a
fingerprint, so a later switch or recovery cannot act on a stale view.

See ``docs/technical/admin-workflow-state.md``.
"""

import json

import pytest

from admin.guided_setup_workflow import GuidedSetupWorkflowStore
from admin.guided_upgrade_context import GuidedUpgradeContextStore
from admin.operation_coordinator import OperationCoordinator
from admin.setup_lifecycle import SetupLifecycleCoordinator
from admin.workflow_lifecycle import (
    AdminWorkflowLifecycleService,
    ReplacementActivity,
    OWNER_ALIGN_EXISTING,
    OWNER_GUIDED_SETUP,
    OWNER_GUIDED_UPGRADE,
    OWNER_NONE,
    OWNER_UNKNOWN,
    STATE_ACTIVE,
    STATE_CLEANUP_PENDING,
    STATE_IDLE,
    STATE_MALFORMED,
    STATE_OPERATION_RUNNING,
    STATE_REVIEW_REQUIRED,
    WORKFLOW_OPERATION_IN_PROGRESS,
    WORKFLOW_OWNER_UNKNOWN,
    WORKFLOW_RECOVERY_REQUIRED,
    WORKFLOW_STATE_MALFORMED,
)

pytestmark = pytest.mark.simulation


class FakeAlignment:
    """Route-shaped transition status with an explicit durable stage."""

    def __init__(
        self,
        *,
        mode=None,
        stage=None,
        operation_id="op-1",
        ok=True,
        cancel_available=None,
        worker_active=False,
        system_tag="v0.9.0",
    ):
        self.mode = mode
        self.stage = stage
        self.operation_id = operation_id
        self.ok = ok
        self.cancel_available = cancel_available
        self.worker_active = worker_active
        self.system_tag = system_tag
        self.cancelled = []

    def status(self, *, operation_active=None):
        del operation_active
        if not self.ok:
            return {
                "ok": False,
                "active": True,
                "error": "state_malformed",
                "message": "the transition state file is corrupt",
                "transition": None,
                "known_good": None,
            }
        if self.mode is None:
            return {"ok": True, "active": False, "transition": None, "known_good": None}
        cancellable = (
            self.cancel_available
            if isinstance(self.cancel_available, bool)
            else self.stage
            in {
                "admin_update_pending",
                "admin_aligned",
                "resources_verified",
                "ems_operation_pending",
                "failed_recoverable",
            }
        )
        return {
            "ok": True,
            "active": self.stage not in {"completed", "cancelled"},
            "transition": {
                "operation_id": self.operation_id,
                "mode": self.mode,
                "stage": self.stage,
                "system_tag": self.system_tag,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:05:00Z",
                "expired": False,
                "worker_active": self.worker_active,
                "worker_status_available": True,
                "cancel_available": bool(cancellable and not self.worker_active),
                "resume_available": self.stage == "failed_recoverable",
            },
            "known_good": None,
        }

    def cancel(self, *, operation_id, coordinator=None):
        del coordinator
        if operation_id != self.operation_id:
            raise AssertionError(f"cancelled a foreign operation: {operation_id!r}")
        self.cancelled.append(operation_id)
        self.stage = "cancelled"
        return {"ok": True, "operation_id": operation_id, "stage": "cancelled"}


def build_service(tmp_path, alignment=None, **kwargs):
    """A service wired like production, with a Docker that proves inactivity.

    The productive runtime always injects a replacement probe; a test that is
    not about that probe would otherwise be refused by the fail-closed gate for
    a reason it never meant to exercise.
    """

    alignment = alignment if alignment is not None else FakeAlignment()
    kwargs.setdefault(
        "install_state_probe", lambda _operation_id: ReplacementActivity.INACTIVE
    )
    kwargs.setdefault("workflows", GuidedSetupWorkflowStore(tmp_path))
    kwargs.setdefault("lifecycle", SetupLifecycleCoordinator())
    kwargs.setdefault("coordinator", OperationCoordinator())
    kwargs.setdefault(
        "upgrade_contexts", GuidedUpgradeContextStore(tmp_path / "state")
    )
    return AdminWorkflowLifecycleService(
        alignment=alignment, admin_data_dir=tmp_path, **kwargs
    )


def write_upgrade_context(tmp_path, operation_id, *, target_system_tag="v0.9.0"):
    """A context record as the upgrade store writes it, without its own store.

    The store refuses to save a fingerprint its options cannot reproduce, and
    inspection must not depend on that reproduction: an orphaned or unreadable
    context is exactly the state the lifecycle view has to report.
    """

    path = tmp_path / "state" / "guided-upgrade-context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "format_version": 3,
                "operation_id": operation_id,
                "target_system_tag": target_system_tag,
                "options": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


# --- owner resolution --------------------------------------------------------


def test_no_durable_state_is_idle_and_switchable(tmp_path):
    view = build_service(tmp_path).inspect()

    assert view["ok"] is True
    assert view["owner"] == OWNER_NONE
    assert view["state"] == STATE_IDLE
    assert view["switchable"] is True
    assert view["blocking_reason"] is None
    assert view["setup"] is None
    assert view["transition"] is None
    assert view["upgrade_context"] is None
    assert view["fingerprint"].startswith("sha256:")


def test_active_setup_without_transition_is_owned_by_setup(tmp_path):
    service = build_service(tmp_path)
    record = service._workflows.ensure_active()

    view = service.inspect()

    assert view["owner"] == OWNER_GUIDED_SETUP
    assert view["state"] == STATE_ACTIVE
    assert view["switchable"] is True
    assert view["setup"]["workflow_id"] == record["workflow_id"]
    assert view["setup"]["status"] == "active"
    assert view["setup"]["cleanup"] == "not_required"
    assert view["setup"]["artifacts_claimed"] is False
    assert view["transition"] is None


def test_active_setup_with_its_own_transition_reports_both(tmp_path):
    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    service = build_service(tmp_path, alignment)
    record = service._workflows.ensure_active()
    service._workflows.record_transition(
        record["workflow_id"], operation_id="op-1", transition_mode="fresh_install"
    )

    view = service.inspect()

    assert view["owner"] == OWNER_GUIDED_SETUP
    assert view["state"] == STATE_ACTIVE
    assert view["transition"]["operation_id"] == "op-1"
    assert view["transition"]["mode"] == "fresh_install"
    assert view["transition"]["stage"] == "resources_verified"
    assert view["transition"]["owned_by_setup"] is True
    assert view["switchable"] is True


def test_setup_transition_operation_mismatch_fails_closed(tmp_path):
    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    service = build_service(tmp_path, alignment)
    record = service._workflows.ensure_active()
    service._workflows.record_transition(
        record["workflow_id"], operation_id="op-other", transition_mode="fresh_install"
    )

    view = service.inspect()

    assert view["owner"] == OWNER_GUIDED_SETUP
    assert view["transition"]["owned_by_setup"] is False
    assert view["switchable"] is False
    assert view["blocking_reason"] == "setup_transition_context_mismatch"


def test_active_guided_upgrade_is_owned_by_upgrade(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="resources_verified")
    service = build_service(tmp_path, alignment)
    write_upgrade_context(tmp_path, "op-1")

    view = service.inspect()

    assert view["owner"] == OWNER_GUIDED_UPGRADE
    assert view["state"] == STATE_ACTIVE
    assert view["switchable"] is True
    assert view["upgrade_context"]["operation_id"] == "op-1"
    assert view["upgrade_context"]["matches_transition"] is True


def test_terminal_transition_releases_ownership(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="completed")
    service = build_service(tmp_path, alignment)

    view = service.inspect()

    assert view["owner"] == OWNER_NONE
    assert view["state"] == STATE_IDLE
    assert view["switchable"] is True


def test_align_existing_transition_is_a_separate_unsupported_owner(tmp_path):
    alignment = FakeAlignment(mode="align_existing_install", stage="resources_verified")

    view = build_service(tmp_path, alignment).inspect()

    assert view["owner"] == OWNER_ALIGN_EXISTING
    assert view["switchable"] is False
    assert view["blocking_reason"] == WORKFLOW_OWNER_UNKNOWN


def test_unknown_transition_mode_fails_closed(tmp_path):
    alignment = FakeAlignment(mode="teleport_install", stage="resources_verified")

    view = build_service(tmp_path, alignment).inspect()

    assert view["owner"] == OWNER_UNKNOWN
    assert view["switchable"] is False
    assert view["blocking_reason"] == WORKFLOW_OWNER_UNKNOWN
    assert view["recoverable"] is True


def test_malformed_setup_record_fails_closed_and_offers_recovery(tmp_path):
    service = build_service(tmp_path)
    path = service._workflows.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    view = service.inspect()

    assert view["owner"] == OWNER_UNKNOWN
    assert view["state"] == STATE_MALFORMED
    assert view["switchable"] is False
    assert view["blocking_reason"] == WORKFLOW_STATE_MALFORMED
    assert view["recoverable"] is True
    assert view["setup"]["readable"] is False
    assert view["setup"]["workflow_id"] is None


def test_malformed_transition_fails_closed_and_offers_recovery(tmp_path):
    alignment = FakeAlignment(ok=False)

    view = build_service(tmp_path, alignment).inspect()

    assert view["owner"] == OWNER_UNKNOWN
    assert view["state"] == STATE_MALFORMED
    assert view["switchable"] is False
    assert view["blocking_reason"] == WORKFLOW_STATE_MALFORMED
    assert view["recoverable"] is True
    assert view["transition"]["readable"] is False


def test_orphaned_upgrade_context_is_reported_but_owns_nothing(tmp_path):
    service = build_service(tmp_path)
    write_upgrade_context(tmp_path, "op-gone")

    view = service.inspect()

    assert view["owner"] == OWNER_NONE
    assert view["upgrade_context"]["operation_id"] == "op-gone"
    assert view["upgrade_context"]["matches_transition"] is False
    assert view["recoverable"] is True


# --- cleanup and running-operation states ------------------------------------


def test_terminal_setup_with_pending_cleanup_still_owns_the_admin(tmp_path):
    service = build_service(tmp_path)
    record = service._workflows.ensure_active()
    service._workflows.finish(
        record["workflow_id"],
        status="abandoned",
        cleanup={
            "state": "pending",
            "attempted_at": "2026-01-01T00:00:00Z",
            "failed_count": 1,
            "review_count": 0,
            "artifacts": [{"kind": "generated_config", "status": "failed"}],
        },
    )

    view = service.inspect()

    assert view["owner"] == OWNER_GUIDED_SETUP
    assert view["state"] == STATE_CLEANUP_PENDING
    assert view["switchable"] is False
    assert view["recoverable"] is True
    assert view["blocking_reason"] == "setup_cleanup_required"


def test_genuine_review_required_blocks_and_asks_for_recovery(tmp_path):
    service = build_service(tmp_path)
    record = service._workflows.ensure_active()
    service._workflows.bind_generated_artifacts(
        record["workflow_id"], preview_id="pv-" + "0" * 16
    )
    service._workflows.finish(
        record["workflow_id"],
        status="abandoned",
        cleanup={
            "state": "review_required",
            "attempted_at": "2026-01-01T00:00:00Z",
            "failed_count": 0,
            "review_count": 1,
            "artifacts": [{"kind": "generated_config", "status": "review_required"}],
        },
    )

    view = service.inspect()

    assert view["owner"] == OWNER_GUIDED_SETUP
    assert view["state"] == STATE_REVIEW_REQUIRED
    assert view["switchable"] is False
    assert view["blocking_reason"] == WORKFLOW_RECOVERY_REQUIRED
    assert view["recoverable"] is True


def test_old_zero_claim_review_is_normalized_without_touching_files(tmp_path):
    service = build_service(tmp_path)
    record = service._workflows.ensure_active()
    marker = tmp_path / "state" / ".admin-deployment.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"release": "v0.8.0"}\n', encoding="utf-8")
    service._workflows.finish(
        record["workflow_id"],
        status="abandoned",
        cleanup={
            "state": "review_required",
            "attempted_at": "2026-01-01T00:00:00Z",
            "failed_count": 0,
            "review_count": 1,
            "artifacts": [{"kind": "deployment_marker", "status": "review_required"}],
        },
    )

    view = service.inspect()

    assert view["owner"] == OWNER_NONE
    assert view["state"] == STATE_IDLE
    assert view["switchable"] is True
    assert marker.read_text(encoding="utf-8") == '{"release": "v0.8.0"}\n'


def test_a_held_setup_mutation_claim_reports_a_running_operation(tmp_path):
    service = build_service(tmp_path)
    record = service._workflows.ensure_active()
    with service._lifecycle.claim_mutation(
        workflow_id=record["workflow_id"], operation="config_apply"
    ):
        view = service.inspect()

    assert view["owner"] == OWNER_GUIDED_SETUP
    assert view["state"] == STATE_OPERATION_RUNNING
    assert view["switchable"] is False
    assert view["blocking_reason"] == "setup_operation_in_progress"
    assert view["operation"] == "config_apply"


def test_a_non_cancellable_transition_stage_blocks_switching(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="admin_reconnect_pending")

    view = build_service(tmp_path, alignment).inspect()

    assert view["owner"] == OWNER_GUIDED_UPGRADE
    assert view["state"] == STATE_OPERATION_RUNNING
    assert view["switchable"] is False
    assert view["blocking_reason"] == WORKFLOW_OPERATION_IN_PROGRESS
    assert view["transition"]["cancellable"] is False


def test_a_live_worker_blocks_switching(tmp_path):
    alignment = FakeAlignment(
        mode="guided_upgrade", stage="ems_operation_pending", worker_active=True
    )

    view = build_service(tmp_path, alignment).inspect()

    assert view["state"] == STATE_OPERATION_RUNNING
    assert view["switchable"] is False
    assert view["blocking_reason"] == WORKFLOW_OPERATION_IN_PROGRESS


# --- fingerprint -------------------------------------------------------------


def test_fingerprint_is_stable_for_unchanged_durable_state(tmp_path):
    service = build_service(tmp_path)
    service._workflows.ensure_active()

    assert service.inspect()["fingerprint"] == service.inspect()["fingerprint"]


def test_fingerprint_changes_with_the_durable_setup_state(tmp_path):
    service = build_service(tmp_path)
    record = service._workflows.ensure_active()
    before = service.inspect()["fingerprint"]

    service._workflows.finish(record["workflow_id"], status="abandoned")

    assert service.inspect()["fingerprint"] != before


def test_fingerprint_changes_with_the_durable_transition_state(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="resources_verified")
    service = build_service(tmp_path, alignment)
    before = service.inspect()["fingerprint"]

    alignment.stage = "failed_recoverable"

    assert service.inspect()["fingerprint"] != before


def test_fingerprint_carries_no_secret_or_path(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="resources_verified")
    service = build_service(tmp_path, alignment)
    service._workflows.ensure_active()

    view = service.inspect()

    assert view["fingerprint"].startswith("sha256:")
    assert len(view["fingerprint"]) == len("sha256:") + 64
    assert str(tmp_path) not in json.dumps(view)
