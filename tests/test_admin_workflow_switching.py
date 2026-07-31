# SPDX-License-Identifier: AGPL-3.0-or-later
"""Switching between Guided Setup and Guided Upgrade is one backend operation.

Before this, a user who had started one guided workflow and then opened the
other met a refusal that named a manual action — Discard setup, Cancel upgrade —
and the two paths were decided in different places: the upgrade validation gate,
the start path, the unrelated-transition write gate and two browser helpers. A
Setup that owned nothing, or an upgrade transition that was perfectly
cancellable, could therefore leave the console with no supported way forward.

These tests pin the one switch operation: it terminates the previous owner
exactly, through the owner's own service, or refuses without changing anything.

See ``docs/technical/admin-workflow-state.md``.
"""

import json
import threading

import pytest

from admin.guided_setup_workflow import GuidedSetupWorkflowStore
from admin.setup_intent import SetupIntentError, SetupIntentStore
from admin.setup_workflow import SetupWorkflowArtifacts
from admin.workflow_lifecycle import (
    AdminWorkflowLifecycleError,
    OWNER_GUIDED_SETUP,
    OWNER_GUIDED_UPGRADE,
    OWNER_NONE,
    TARGET_GUIDED_SETUP,
    TARGET_GUIDED_UPGRADE,
    TARGET_NONE,
    WORKFLOW_LIFECYCLE_CHANGED,
    WORKFLOW_OPERATION_IN_PROGRESS,
    WORKFLOW_RECOVERY_REQUIRED,
    WORKFLOW_SWITCH_BLOCKED,
)
from tests.test_admin_workflow_lifecycle import (
    FakeAlignment,
    build_service,
    write_upgrade_context,
)

pytestmark = pytest.mark.simulation

PRESERVED_INSTALL_STATE = (
    "live EMS configuration",
    "runtime data",
    "deployment marker",
    "containers",
    "volumes",
    "backups",
)


def _service(tmp_path, alignment=None, **kwargs):
    kwargs.setdefault("setup_intents", SetupIntentStore())
    return build_service(tmp_path, alignment, **kwargs)


def _start_setup(service, *, operation_id=None, mode="fresh_install"):
    record = service._workflows.ensure_active()
    if operation_id is not None:
        service._workflows.record_transition(
            record["workflow_id"], operation_id=operation_id, transition_mode=mode
        )
    return record["workflow_id"]


def _claim_artifacts(service, workflow_id):
    """Give the workflow a generated config it can prove it owns."""

    artifacts = SetupWorkflowArtifacts(
        service._workflows.admin_data_dir, workflow_id=workflow_id
    )
    artifacts.generated_config_path.parent.mkdir(parents=True, exist_ok=True)
    artifacts.generated_config_path.write_text('{"devices": []}\n', encoding="utf-8")
    artifacts.record_generated(
        workflow_id=workflow_id,
        preview_id="pv-" + "0" * 16,
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision={"expected_revision": None, "expect_absent": True},
        prepared_config_sha256="0" * 64,
    )
    service._workflows.bind_generated_artifacts(
        workflow_id, preview_id="pv-" + "0" * 16
    )
    return artifacts.generated_config_path


def _switch(service, target, **kwargs):
    kwargs.setdefault("confirm", True)
    kwargs.setdefault("expected_fingerprint", service.inspect()["fingerprint"])
    return service.switch(target, **kwargs)


# --- Guided Setup -> Guided Upgrade ------------------------------------------


def test_empty_setup_switches_to_upgrade_and_leaves_the_install_alone(tmp_path):
    service = _service(tmp_path)
    workflow_id = _start_setup(service)
    marker = tmp_path / "state" / ".admin-deployment.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"release": "v0.8.0"}\n', encoding="utf-8")

    result = _switch(service, TARGET_GUIDED_UPGRADE)

    assert result["ok"] is True
    assert result["action"] == "discard_guided_setup"
    assert result["lifecycle"]["owner"] == OWNER_NONE
    assert result["lifecycle"]["switchable"] is True
    stored = service._workflows.load()
    assert stored["workflow_id"] == workflow_id
    assert stored["status"] == "abandoned"
    assert stored["cleanup"]["state"] == "complete"
    assert marker.read_text(encoding="utf-8") == '{"release": "v0.8.0"}\n'


def test_setup_with_owned_artifacts_cleans_up_then_switches(tmp_path):
    service = _service(tmp_path)
    workflow_id = _start_setup(service)
    generated = _claim_artifacts(service, workflow_id)

    result = _switch(service, TARGET_GUIDED_UPGRADE)

    assert result["ok"] is True
    assert generated.exists() is False
    assert service._workflows.load()["cleanup"]["state"] == "complete"


def test_switch_cancels_only_the_setup_owned_operation(tmp_path):
    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    _start_setup(service, operation_id="op-1")

    _switch(service, TARGET_GUIDED_UPGRADE)

    assert alignment.cancelled == ["op-1"]


def test_pre_existing_unclaimed_files_are_preserved_by_a_switch(tmp_path):
    service = _service(tmp_path)
    _start_setup(service)
    legacy = tmp_path / "generated" / "config.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('{"devices": ["installed"]}\n', encoding="utf-8")

    result = _switch(service, TARGET_GUIDED_UPGRADE)

    assert result["ok"] is True
    assert legacy.read_text(encoding="utf-8") == '{"devices": ["installed"]}\n'
    assert service._workflows.load()["cleanup"]["state"] == "complete"


def test_a_running_setup_mutation_blocks_the_switch(tmp_path):
    service = _service(tmp_path)
    workflow_id = _start_setup(service)
    fingerprint = service.inspect()["fingerprint"]

    with service._lifecycle.claim_mutation(
        workflow_id=workflow_id, operation="config_apply"
    ):
        with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
            service.switch(
                TARGET_GUIDED_UPGRADE, expected_fingerprint=fingerprint, confirm=True
            )

    assert excinfo.value.code == "setup_operation_in_progress"
    assert service._workflows.load()["status"] == "active"


def test_a_setup_transition_mismatch_fails_the_switch_closed(tmp_path):
    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    _start_setup(service, operation_id="op-other")

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _switch(service, TARGET_GUIDED_UPGRADE)

    assert excinfo.value.code == WORKFLOW_SWITCH_BLOCKED
    assert excinfo.value.detail == "setup_transition_context_mismatch"
    assert alignment.cancelled == []
    assert service._workflows.load()["status"] == "active"


def test_a_genuine_review_required_state_refuses_an_automatic_switch(tmp_path):
    service = _service(tmp_path)
    workflow_id = _start_setup(service)
    _claim_artifacts(service, workflow_id)
    service._workflows.finish(
        workflow_id,
        status="abandoned",
        cleanup={
            "state": "review_required",
            "attempted_at": "2026-01-01T00:00:00Z",
            "failed_count": 0,
            "review_count": 1,
            "artifacts": [{"kind": "generated_config", "status": "review_required"}],
        },
    )

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _switch(service, TARGET_GUIDED_UPGRADE)

    assert excinfo.value.code == WORKFLOW_RECOVERY_REQUIRED
    assert service._workflows.load()["cleanup"]["state"] == "review_required"


def test_repeating_the_switch_is_idempotent(tmp_path):
    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    _start_setup(service, operation_id="op-1")

    first = _switch(service, TARGET_GUIDED_UPGRADE)
    second = _switch(service, TARGET_GUIDED_UPGRADE)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["action"] == "none"
    assert alignment.cancelled == ["op-1"]


def test_a_stale_fingerprint_refuses_the_switch(tmp_path):
    service = _service(tmp_path)
    _start_setup(service)
    stale = service.inspect()["fingerprint"]
    _start_setup(service)
    service._workflows.finish(
        service._workflows.load()["workflow_id"], status="abandoned"
    )

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        service.switch(
            TARGET_GUIDED_UPGRADE, expected_fingerprint=stale, confirm=True
        )

    assert excinfo.value.code == WORKFLOW_LIFECYCLE_CHANGED


def test_switch_requires_explicit_confirmation(tmp_path):
    service = _service(tmp_path)
    _start_setup(service)

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        service.switch(
            TARGET_GUIDED_UPGRADE,
            expected_fingerprint=service.inspect()["fingerprint"],
            confirm=False,
        )

    assert excinfo.value.code == "confirmation_required"
    assert service._workflows.load()["status"] == "active"


def test_two_concurrent_switches_terminate_the_setup_once(tmp_path):
    """Interleaving: the second switch enters while the first holds the claim.

    The first request is parked inside ``alignment.cancel`` — after the switch
    decision, before the durable cancel — and the second runs to completion
    from another thread. Exactly one termination and one cancel may happen.
    """

    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    entered = threading.Event()
    release = threading.Event()
    real_cancel = alignment.cancel

    def blocking_cancel(*, operation_id, coordinator=None):
        entered.set()
        assert release.wait(timeout=5)
        return real_cancel(operation_id=operation_id, coordinator=coordinator)

    alignment.cancel = blocking_cancel
    service = _service(tmp_path, alignment)
    _start_setup(service, operation_id="op-1")
    fingerprint = service.inspect()["fingerprint"]
    outcomes = {}

    def run(name):
        try:
            outcomes[name] = service.switch(
                TARGET_GUIDED_UPGRADE, expected_fingerprint=fingerprint, confirm=True
            )
        except AdminWorkflowLifecycleError as exc:
            outcomes[name] = exc

    first = threading.Thread(target=run, args=("first",))
    first.start()
    assert entered.wait(timeout=5)
    second = threading.Thread(target=run, args=("second",))
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert alignment.cancelled == ["op-1"]
    succeeded = [name for name, value in outcomes.items() if isinstance(value, dict)]
    assert succeeded == ["first"]
    assert isinstance(outcomes["second"], AdminWorkflowLifecycleError)
    assert outcomes["second"].code == WORKFLOW_LIFECYCLE_CHANGED
    assert service._workflows.load()["status"] == "abandoned"


# --- Guided Upgrade -> Guided Setup ------------------------------------------


@pytest.mark.parametrize(
    "stage", ["validated", "admin_aligned", "resources_verified", "failed_recoverable"]
)
def test_a_cancellable_upgrade_is_cancelled_and_setup_starts(tmp_path, stage):
    alignment = FakeAlignment(
        mode="guided_upgrade", stage=stage, cancel_available=True
    )
    service = _service(tmp_path, alignment)
    write_upgrade_context(tmp_path, "op-1")

    result = _switch(service, TARGET_GUIDED_SETUP, session_id="session-a")

    assert result["ok"] is True
    assert result["action"] == "cancel_guided_upgrade"
    assert alignment.cancelled == ["op-1"]
    assert result["cleared_upgrade_context"] is True
    assert (tmp_path / "state" / "guided-upgrade-context.json").exists() is False
    assert result["setup_workflow_id"]
    assert result["setup_intent_id"]
    assert service._workflows.load()["status"] == "active"
    assert result["lifecycle"]["owner"] == OWNER_GUIDED_SETUP


@pytest.mark.parametrize(
    "stage",
    [
        "admin_reconnect_pending",
        "ems_operation_running",
        "healthcheck_pending",
    ],
)
def test_an_externally_mutating_upgrade_stage_blocks_the_switch(tmp_path, stage):
    alignment = FakeAlignment(mode="guided_upgrade", stage=stage)
    service = _service(tmp_path, alignment)
    write_upgrade_context(tmp_path, "op-1")

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _switch(service, TARGET_GUIDED_SETUP, session_id="session-a")

    assert excinfo.value.code == WORKFLOW_OPERATION_IN_PROGRESS
    assert alignment.cancelled == []
    assert (tmp_path / "state" / "guided-upgrade-context.json").exists()
    assert service._workflows.load() is None


def test_a_claimed_resource_verification_blocks_the_switch(tmp_path):
    alignment = FakeAlignment(
        mode="guided_upgrade", stage="admin_aligned", cancel_available=False
    )
    service = _service(tmp_path, alignment)

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _switch(service, TARGET_GUIDED_SETUP, session_id="session-a")

    assert excinfo.value.code == WORKFLOW_OPERATION_IN_PROGRESS
    assert alignment.cancelled == []


def test_a_foreign_upgrade_context_is_never_cleared(tmp_path):
    alignment = FakeAlignment(
        mode="guided_upgrade", stage="resources_verified", operation_id="op-2"
    )
    service = _service(tmp_path, alignment)
    context = write_upgrade_context(tmp_path, "op-1")

    result = _switch(service, TARGET_GUIDED_SETUP, session_id="session-a")

    assert alignment.cancelled == ["op-2"]
    assert result["cleared_upgrade_context"] is False
    assert context.exists()


def test_switching_to_setup_without_an_upgrade_just_starts_setup(tmp_path):
    service = _service(tmp_path)

    result = _switch(service, TARGET_GUIDED_SETUP, session_id="session-a")

    assert result["action"] == "start_guided_setup"
    assert result["setup_workflow_id"]
    assert result["setup_intent_id"]


def test_switching_to_setup_resumes_the_active_workflow(tmp_path):
    service = _service(tmp_path)
    workflow_id = _start_setup(service)

    result = _switch(service, TARGET_GUIDED_SETUP, session_id="session-a")

    assert result["action"] == "resume_guided_setup"
    assert result["setup_workflow_id"] == workflow_id


def test_two_concurrent_switches_to_setup_create_one_workflow(tmp_path):
    """Interleaving: both requests read one lifecycle state, only one may act."""

    alignment = FakeAlignment(mode="guided_upgrade", stage="resources_verified")
    entered = threading.Event()
    release = threading.Event()
    real_cancel = alignment.cancel

    def blocking_cancel(*, operation_id, coordinator=None):
        entered.set()
        assert release.wait(timeout=5)
        return real_cancel(operation_id=operation_id, coordinator=coordinator)

    alignment.cancel = blocking_cancel
    service = _service(tmp_path, alignment)
    fingerprint = service.inspect()["fingerprint"]
    outcomes = {}

    def run(name):
        try:
            outcomes[name] = service.switch(
                TARGET_GUIDED_SETUP,
                expected_fingerprint=fingerprint,
                confirm=True,
                session_id="session-" + name,
            )
        except AdminWorkflowLifecycleError as exc:
            outcomes[name] = exc

    first = threading.Thread(target=run, args=("first",))
    first.start()
    assert entered.wait(timeout=5)
    second = threading.Thread(target=run, args=("second",))
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert alignment.cancelled == ["op-1"]
    assert isinstance(outcomes["first"], dict)
    assert isinstance(outcomes["second"], AdminWorkflowLifecycleError)
    workflows = GuidedSetupWorkflowStore(tmp_path).load()
    assert workflows["workflow_id"] == outcomes["first"]["setup_workflow_id"]


# --- return to task selection -------------------------------------------------


def test_target_none_stops_the_current_workflow_without_a_replacement(tmp_path):
    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    workflow_id = _start_setup(service, operation_id="op-1")

    result = _switch(service, TARGET_NONE)

    assert result["ok"] is True
    assert result["action"] == "discard_guided_setup"
    assert result.get("setup_workflow_id") is None
    assert service._workflows.load()["workflow_id"] == workflow_id
    assert service._workflows.load()["status"] == "abandoned"
    assert alignment.cancelled == ["op-1"]


def test_target_none_on_an_idle_console_changes_nothing(tmp_path):
    service = _service(tmp_path)

    result = _switch(service, TARGET_NONE)

    assert result["action"] == "none"
    assert service._workflows.load() is None


# --- switch preview -----------------------------------------------------------


def test_preview_names_what_is_reset_and_what_is_preserved(tmp_path):
    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    _start_setup(service, operation_id="op-1")

    plan = service.plan_switch(TARGET_GUIDED_UPGRADE)

    assert plan["ok"] is True
    assert plan["action"] == "discard_guided_setup"
    assert plan["confirmation_required"] is True
    assert plan["blocked"] is False
    assert "Guided Setup workflow" in plan["will_reset"]
    assert "Setup System Build transition" in plan["will_reset"]
    for preserved in PRESERVED_INSTALL_STATE:
        assert preserved in plan["will_preserve"]
    assert plan["fingerprint"] == service.inspect()["fingerprint"]


def test_preview_reports_a_blocked_switch_without_changing_anything(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="ems_operation_running")
    service = _service(tmp_path, alignment)

    plan = service.plan_switch(TARGET_GUIDED_SETUP)

    assert plan["blocked"] is True
    assert plan["blocking_reason"] == WORKFLOW_OPERATION_IN_PROGRESS
    assert plan["resume_available"] is False
    assert plan["confirmation_required"] is False
    assert alignment.cancelled == []


def test_preview_of_a_noop_switch_needs_no_confirmation(tmp_path):
    service = _service(tmp_path)

    plan = service.plan_switch(TARGET_GUIDED_UPGRADE)

    assert plan["action"] == "none"
    assert plan["confirmation_required"] is False
    assert plan["will_reset"] == []


def test_an_unsupported_target_is_refused(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ValueError):
        service.plan_switch("maintenance")


def test_preview_carries_no_absolute_path(tmp_path):
    service = _service(tmp_path)
    workflow_id = _start_setup(service)
    _claim_artifacts(service, workflow_id)

    plan = service.plan_switch(TARGET_GUIDED_UPGRADE)

    assert str(tmp_path) not in json.dumps(plan)


def test_switching_away_retires_the_setup_intents_of_that_workflow(tmp_path):
    intents = SetupIntentStore()
    service = _service(tmp_path, setup_intents=intents)
    workflow_id = _start_setup(service)
    intent = intents.issue(session_id="session-a", workflow_id=workflow_id)

    _switch(service, TARGET_GUIDED_UPGRADE)

    with pytest.raises(SetupIntentError):
        intents.validate(
            intent.intent_id, session_id="session-a", workflow_id=workflow_id
        )


def test_upgrade_owner_is_reported_before_and_setup_owner_after(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="resources_verified")
    service = _service(tmp_path, alignment)

    assert service.inspect()["owner"] == OWNER_GUIDED_UPGRADE
    _switch(service, TARGET_GUIDED_SETUP, session_id="session-a")
    assert service.inspect()["owner"] == OWNER_GUIDED_SETUP
