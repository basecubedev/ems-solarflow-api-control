# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contradictory, unsupported and unprovable lifecycle state must fail closed.

The arbiter reads several durable authorities together, and its first version
resolved them optimistically: the active transition decided the owner even when
a Guided Setup record still claimed the console, an unknown owner turned into a
successful no-op switch, a readable-but-unsupported record had no recovery at
all, and a Docker daemon that could not be reached read as "no replacement is
running".

Each of those is a state where the safe answer is to refuse and offer recovery.
These tests pin that: a contradiction is named, never silently resolved; a block
is never converted into success; anything normal domain operations cannot repair
becomes advanced-recovery material with an exact reason; and releasing durable
state requires positive proof that no Admin replacement is running.

See ``docs/technical/admin-workflow-state.md``.
"""

import json
import threading

import pytest

from admin.admin_update import ADMIN_UPDATER_CONTAINER_PREFIX
from admin.guided_upgrade_context import GuidedUpgradeContextStore
from admin.workflow_lifecycle import (
    AdminWorkflowLifecycleError,
    OWNER_CONFLICT,
    RECOVERY_MODE_RELEASE_STALE_STATE,
    RECOVERY_MODE_SAFE,
    REPLACEMENT_ACTIVE,
    ReplacementActivity,
    STALE_UNREADABLE_STATE,
    STALE_UNSUPPORTED_TRANSITION_MODE,
    STALE_UNUSABLE_UPGRADE_CONTEXT,
    STATE_CONFLICT,
    TARGET_GUIDED_SETUP,
    TARGET_GUIDED_UPGRADE,
    TARGET_NONE,
    OWNER_NONE,
    WORKFLOW_LIFECYCLE_CHANGED,
    WORKFLOW_OWNER_CONFLICT,
    WORKFLOW_OWNER_UNKNOWN,
    WORKFLOW_RECOVERY_UNSAFE,
    admin_replacement_activity,
)
from tests.test_admin_workflow_lifecycle import FakeAlignment
from tests.test_admin_workflow_switching import _claim_artifacts, _service, _start_setup

pytestmark = pytest.mark.simulation


def _terminal_setup_with_cleanup(service, state="pending"):
    workflow_id = _start_setup(service)
    _claim_artifacts(service, workflow_id)
    service._workflows.finish(
        workflow_id,
        status="abandoned",
        cleanup={
            "state": state,
            "attempted_at": "2026-01-01T00:00:00Z",
            "failed_count": 1 if state == "pending" else 0,
            "review_count": 0 if state == "pending" else 1,
            "artifacts": [
                {
                    "kind": "generated_config",
                    "status": "failed" if state == "pending" else "review_required",
                }
            ],
        },
    )
    return workflow_id


def _stale_names(plan):
    return [entry["name"] for entry in plan["advanced"]["files"]]


def _stale_reasons(plan):
    return {entry["name"]: entry["reason"] for entry in plan["advanced"]["files"]}


# --- contradictory durable owners --------------------------------------------


def test_active_setup_and_guided_upgrade_is_owner_conflict(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="resources_verified")
    service = _service(tmp_path, alignment)
    workflow_id = _start_setup(service)
    _claim_artifacts(service, workflow_id)

    view = service.inspect()

    assert view["owner"] == OWNER_CONFLICT
    assert view["state"] == STATE_CONFLICT
    assert view["switchable"] is False
    assert view["recoverable"] is True
    assert view["blocking_reason"] == WORKFLOW_OWNER_CONFLICT
    # Inspection names the contradiction; it never resolves it.
    stored = service._workflows.load()
    assert stored["workflow_id"] == workflow_id
    assert stored["status"] == "active"
    assert alignment.cancelled == []


def test_setup_cleanup_and_guided_upgrade_is_owner_conflict(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="resources_verified")
    service = _service(tmp_path, alignment)
    _terminal_setup_with_cleanup(service)

    view = service.inspect()

    assert view["owner"] == OWNER_CONFLICT
    assert view["blocking_reason"] == WORKFLOW_OWNER_CONFLICT
    assert view["switchable"] is False


def test_setup_and_align_existing_is_owner_conflict(tmp_path):
    alignment = FakeAlignment(
        mode="align_existing_install", stage="resources_verified"
    )
    service = _service(tmp_path, alignment)
    _start_setup(service)

    view = service.inspect()

    assert view["owner"] == OWNER_CONFLICT
    assert view["blocking_reason"] == WORKFLOW_OWNER_CONFLICT


def test_a_setup_owned_transition_is_not_a_conflict(tmp_path):
    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    record = service._workflows.ensure_active()
    service._workflows.record_transition(
        record["workflow_id"], operation_id="op-1", transition_mode="fresh_install"
    )

    view = service.inspect()

    assert view["owner"] == "guided_setup"
    assert view["blocking_reason"] is None


def test_an_operation_mismatch_stays_its_own_fail_closed_contract(tmp_path):
    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    _start_setup(service, operation_id="op-other")

    view = service.inspect()

    assert view["owner"] == "guided_setup"
    assert view["switchable"] is False
    assert view["blocking_reason"] == "setup_transition_context_mismatch"


# --- a block is never a successful no-op --------------------------------------


@pytest.mark.parametrize("target", [TARGET_GUIDED_SETUP, TARGET_GUIDED_UPGRADE, TARGET_NONE])
def test_unknown_owner_switch_is_blocked(tmp_path, target):
    alignment = FakeAlignment(mode="teleport_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    before = service.inspect()

    plan = service.plan_switch(target)
    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        service.switch(
            target,
            expected_fingerprint=before["fingerprint"],
            confirm=True,
            session_id="session-a",
        )

    assert plan["blocked"] is True
    assert plan["blocking_reason"] == WORKFLOW_OWNER_UNKNOWN
    assert excinfo.value.code == WORKFLOW_OWNER_UNKNOWN
    assert alignment.cancelled == []
    assert service._workflows.load() is None
    assert service.inspect()["fingerprint"] == before["fingerprint"]


def test_align_existing_switch_is_blocked(tmp_path):
    alignment = FakeAlignment(
        mode="align_existing_install", stage="resources_verified"
    )
    service = _service(tmp_path, alignment)

    plan = service.plan_switch(TARGET_GUIDED_UPGRADE)
    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        service.switch(
            TARGET_GUIDED_UPGRADE,
            expected_fingerprint=plan["fingerprint"],
            confirm=True,
        )

    assert plan["blocked"] is True
    assert excinfo.value.code == WORKFLOW_OWNER_UNKNOWN
    assert alignment.cancelled == []


def test_owner_conflict_switch_is_blocked(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="resources_verified")
    service = _service(tmp_path, alignment)
    workflow_id = _start_setup(service)

    plan = service.plan_switch(TARGET_GUIDED_UPGRADE)
    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        service.switch(
            TARGET_GUIDED_UPGRADE,
            expected_fingerprint=plan["fingerprint"],
            confirm=True,
        )

    assert plan["blocked"] is True
    assert plan["blocking_reason"] == WORKFLOW_OWNER_CONFLICT
    assert excinfo.value.code == WORKFLOW_OWNER_CONFLICT
    assert alignment.cancelled == []
    assert service._workflows.load()["workflow_id"] == workflow_id
    assert service._workflows.load()["status"] == "active"


def test_an_idle_console_still_answers_a_no_op_switch(tmp_path):
    service = _service(tmp_path)

    plan = service.plan_switch(TARGET_GUIDED_UPGRADE)
    result = service.switch(
        TARGET_GUIDED_UPGRADE,
        expected_fingerprint=plan["fingerprint"],
        confirm=True,
    )

    assert plan["blocked"] is False
    assert result["action"] == "none"


def test_the_same_owner_still_answers_a_no_op_switch(tmp_path):
    alignment = FakeAlignment(
        mode="guided_upgrade", stage="ems_operation_running", worker_active=True
    )
    service = _service(tmp_path, alignment)

    plan = service.plan_switch(TARGET_GUIDED_UPGRADE)
    result = service.switch(
        TARGET_GUIDED_UPGRADE,
        expected_fingerprint=plan["fingerprint"],
        confirm=True,
    )

    # Guided Upgrade already owns the console: asking for it again changes
    # nothing, even while its own operation is running.
    assert plan["blocked"] is False
    assert result["action"] == "none"
    assert alignment.cancelled == []


# --- advanced recovery for readable but unusable state ------------------------


def test_unknown_readable_transition_offers_advanced_recovery(tmp_path):
    alignment = FakeAlignment(mode="teleport_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    transition = tmp_path / "state" / "pending-transition.json"
    transition.parent.mkdir(parents=True, exist_ok=True)
    transition.write_text('{"mode": "teleport_install"}\n', encoding="utf-8")

    plan = service.plan_recovery()

    assert plan["safe"]["available"] is False
    assert plan["advanced"]["available"] is True
    assert _stale_names(plan) == ["state/pending-transition.json"]
    assert (
        _stale_reasons(plan)["state/pending-transition.json"]
        == STALE_UNSUPPORTED_TRANSITION_MODE
    )


def test_owner_conflict_offers_advanced_recovery(tmp_path):
    alignment = FakeAlignment(mode="teleport_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    _start_setup(service)
    transition = tmp_path / "state" / "pending-transition.json"
    transition.parent.mkdir(parents=True, exist_ok=True)
    transition.write_text('{"mode": "teleport_install"}\n', encoding="utf-8")

    plan = service.plan_recovery()

    assert plan["advanced"]["available"] is True
    assert set(_stale_names(plan)) == {
        "state/guided-setup-workflow.json",
        "state/pending-transition.json",
    }
    assert (
        _stale_reasons(plan)["state/guided-setup-workflow.json"]
        == WORKFLOW_OWNER_CONFLICT
    )


def test_a_healthy_transition_is_never_stale(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="resources_verified")
    service = _service(tmp_path, alignment)
    transition = tmp_path / "state" / "pending-transition.json"
    transition.parent.mkdir(parents=True, exist_ok=True)
    transition.write_text('{"mode": "guided_upgrade"}\n', encoding="utf-8")

    plan = service.plan_recovery()

    assert plan["advanced"]["available"] is False
    assert _stale_names(plan) == []
    assert plan["safe"]["available"] is True


def test_an_unprovable_setup_transition_owner_offers_advanced_recovery(tmp_path):
    alignment = FakeAlignment(mode="fresh_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    _start_setup(service, operation_id="op-other")
    transition = tmp_path / "state" / "pending-transition.json"
    transition.parent.mkdir(parents=True, exist_ok=True)
    transition.write_text('{"mode": "fresh_install"}\n', encoding="utf-8")

    plan = service.plan_recovery()

    # Abandonment refuses a transition this workflow cannot name, so the normal
    # operations converge on nothing here.
    assert plan["safe"]["available"] is False
    assert plan["advanced"]["available"] is True
    assert (
        _stale_reasons(plan)["state/guided-setup-workflow.json"]
        == "setup_transition_context_mismatch"
    )


def test_the_advanced_plan_still_reports_unreadable_state(tmp_path):
    service = _service(tmp_path)
    service._workflows.path.parent.mkdir(parents=True, exist_ok=True)
    service._workflows.path.write_text("{ not json", encoding="utf-8")

    plan = service.plan_recovery()

    assert _stale_names(plan) == ["state/guided-setup-workflow.json"]
    assert (
        _stale_reasons(plan)["state/guided-setup-workflow.json"]
        == STALE_UNREADABLE_STATE
    )


def test_the_manifest_records_why_each_file_was_released(tmp_path):
    alignment = FakeAlignment(mode="teleport_install", stage="resources_verified")
    service = _service(tmp_path, alignment)
    transition = tmp_path / "state" / "pending-transition.json"
    transition.parent.mkdir(parents=True, exist_ok=True)
    transition.write_text('{"mode": "teleport_install"}\n', encoding="utf-8")
    fingerprint = service.inspect()["fingerprint"]

    result = service.recover(
        mode=RECOVERY_MODE_RELEASE_STALE_STATE,
        expected_fingerprint=fingerprint,
        confirm=True,
        reason="unsupported transition mode",
    )

    backup = tmp_path / "state" / "workflow-recovery"
    directory = next(path for path in backup.iterdir() if path.is_dir())
    manifest = json.loads(
        (directory / "recovery-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["files"][0]["reason"] == STALE_UNSUPPORTED_TRANSITION_MODE
    assert manifest["lifecycle_fingerprint"] == fingerprint
    assert manifest["reason"] == "unsupported transition mode"
    assert result["released"] == ["state/pending-transition.json"]
    assert transition.exists() is False


# --- replacement activity must be proven, never assumed ----------------------


class _Docker:
    """Deterministic container inspector: answer, absence or failure."""

    def __init__(self, *, container=None, error=None, listing=None):
        self.container = container
        self.error = error
        self.listing = listing
        self.inspected = []
        self.listed = []

    def inspect_container(self, container_name):
        self.inspected.append(container_name)
        if self.error is not None:
            raise self.error
        return self.container

    def list_containers(self, name_prefix):
        self.listed.append(name_prefix)
        if self.error is not None:
            raise self.error
        return list(self.listing or [])


def test_replacement_activity_reports_three_distinct_states():
    running = _Docker(container={"status": "running"})
    absent = _Docker(container=None)
    unreachable = _Docker(error=RuntimeError("docker daemon unreachable"))

    assert admin_replacement_activity(running, "op-1") is ReplacementActivity.ACTIVE
    assert admin_replacement_activity(absent, "op-1") is ReplacementActivity.INACTIVE
    assert (
        admin_replacement_activity(unreachable, "op-1") is ReplacementActivity.UNKNOWN
    )
    assert admin_replacement_activity(None, "op-1") is ReplacementActivity.UNKNOWN


def test_replacement_activity_without_an_operation_scans_the_updater_prefix():
    empty = _Docker(listing=[])
    busy = _Docker(listing=[{"container_name": "ems-admin-updater-op-9", "status": "running"}])

    assert admin_replacement_activity(empty, None) is ReplacementActivity.INACTIVE
    assert admin_replacement_activity(busy, None) is ReplacementActivity.ACTIVE
    assert empty.listed == ["ems-admin-updater-"]


def test_replacement_activity_without_a_listing_capability_is_unknown():
    class _NoListing:
        @staticmethod
        def inspect_container(container_name):
            del container_name
            return None

    assert admin_replacement_activity(_NoListing(), None) is ReplacementActivity.UNKNOWN


@pytest.mark.parametrize(
    "container",
    [
        {},
        {"container_name": "ems-admin-updater-op-1"},
        {"status": ""},
        {"status": None},
        {"status": "some-state-this-build-does-not-know"},
    ],
)
def test_a_container_of_unknown_state_is_never_proof_of_inactivity(container):
    """A destructive recovery needs a proof, and this answer is not one.

    Only a container whose reported state is recognised may settle the
    question. Anything else — a row without a state, or one naming a state this
    build does not classify — leaves the question open, so the recovery has to
    treat it as UNKNOWN rather than release the operation.
    """

    docker = _Docker(container=container)

    assert admin_replacement_activity(docker, "op-1") is ReplacementActivity.UNKNOWN


@pytest.mark.parametrize(
    "listing",
    [
        [{}],
        [{"container_name": "ems-admin-updater-op-9"}],
        [
            {"container_name": "ems-admin-updater-op-9", "status": "exited"},
            {"container_name": "ems-admin-updater-op-8", "status": "?"},
        ],
    ],
)
def test_a_prefix_scan_of_unknown_state_is_never_proof_of_inactivity(listing):
    """One unclassifiable row is enough to lose the proof for the whole scan."""

    docker = _Docker(listing=listing)

    assert admin_replacement_activity(docker, None) is ReplacementActivity.UNKNOWN


def test_a_recognised_dead_state_still_proves_inactivity():
    """Fail-closed must not swallow the answer a recovery is waiting for."""

    docker = _Docker(container={"container_name": "u", "status": "exited"})
    scan = _Docker(listing=[{"container_name": "u", "status": "dead"}])

    assert admin_replacement_activity(docker, "op-1") is ReplacementActivity.INACTIVE
    assert admin_replacement_activity(scan, None) is ReplacementActivity.INACTIVE


def _corrupt_service(tmp_path, probe):
    service = _service(tmp_path, install_state_probe=probe)
    service._workflows.path.parent.mkdir(parents=True, exist_ok=True)
    service._workflows.path.write_text("{ not json", encoding="utf-8")
    return service


def _release(service, tmp_path):
    return service.recover(
        mode=RECOVERY_MODE_RELEASE_STALE_STATE,
        expected_fingerprint=service.inspect()["fingerprint"],
        confirm=True,
        reason="corrupt workflow metadata",
    )


def test_advanced_recovery_fails_closed_when_docker_is_unavailable(tmp_path):
    service = _corrupt_service(tmp_path, lambda _op: ReplacementActivity.UNKNOWN)

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _release(service, tmp_path)

    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert excinfo.value.detail == "install_state_unavailable"
    assert service._workflows.path.exists()


def test_advanced_recovery_fails_closed_when_a_replacement_is_active(tmp_path):
    service = _corrupt_service(tmp_path, lambda _op: ReplacementActivity.ACTIVE)

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _release(service, tmp_path)

    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert excinfo.value.detail == "replacement_active"
    assert service._workflows.path.exists()


def test_advanced_recovery_fails_closed_when_the_probe_raises(tmp_path):
    def probe(_operation_id):
        raise RuntimeError("docker socket gone")

    service = _corrupt_service(tmp_path, probe)

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _release(service, tmp_path)

    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert excinfo.value.detail == "install_state_unavailable"
    assert service._workflows.path.exists()


def test_advanced_recovery_continues_when_docker_proves_no_replacement(tmp_path):
    seen = []

    def probe(operation_id):
        seen.append(operation_id)
        return ReplacementActivity.INACTIVE

    service = _corrupt_service(tmp_path, probe)

    result = _release(service, tmp_path)

    assert result["released"] == ["state/guided-setup-workflow.json"]
    assert service._workflows.path.exists() is False
    assert seen == [None]


def test_a_missing_operation_identity_still_reaches_the_probe(tmp_path):
    alignment = FakeAlignment(ok=False)
    seen = []

    def probe(operation_id):
        seen.append(operation_id)
        return ReplacementActivity.UNKNOWN

    service = _service(tmp_path, alignment, install_state_probe=probe)
    transition = tmp_path / "state" / "pending-transition.json"
    transition.parent.mkdir(parents=True, exist_ok=True)
    transition.write_text("}{ corrupt", encoding="utf-8")

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _release(service, tmp_path)

    # An unreadable transition names no operation, which is exactly the case the
    # old comment assumed the durable stage would cover.
    assert seen == [None]
    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert transition.exists()


# --- Guided Upgrade context identity vs domain validity ----------------------


def _write_context(tmp_path, payload):
    path = tmp_path / "state" / "guided-upgrade-context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def test_context_describe_distinguishes_identity_from_domain_validity(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    _write_context(
        tmp_path,
        {"format_version": 1, "operation_id": "op-1", "target_system_tag": "v0.9.0"},
    )

    described = store.describe()

    assert described["present"] is True
    assert described["identity_readable"] is True
    assert described["domain_valid"] is False
    assert described["operation_id"] == "op-1"
    assert described["target_system_tag"] == "v0.9.0"
    assert described["reason"]


def test_context_describe_accepts_a_reproducible_context(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    from admin.guided_upgrade import guided_upgrade_request_fingerprint

    options = GuidedUpgradeContextStore._normalize_options({})
    store.save(
        operation_id="op-1",
        target_system_tag="v0.9.0",
        options=options,
        request_fingerprint=guided_upgrade_request_fingerprint("v0.9.0", options),
    )

    described = store.describe()

    assert described["identity_readable"] is True
    assert described["domain_valid"] is True
    assert described["reason"] is None


def test_an_invalid_context_is_not_cleared_by_safe_recovery(tmp_path):
    service = _service(tmp_path)
    context = _write_context(
        tmp_path,
        {"format_version": 1, "operation_id": "op-gone", "target_system_tag": "v0.9.0"},
    )

    plan = service.plan_recovery()
    result = service.recover(
        mode=RECOVERY_MODE_SAFE,
        expected_fingerprint=plan["fingerprint"],
        confirm=True,
    )

    assert "upgrade_context_clear" not in result["actions"]
    assert context.exists()


def test_an_invalid_context_is_backed_up_by_advanced_recovery(tmp_path):
    service = _service(tmp_path)
    context = _write_context(
        tmp_path,
        {"format_version": 1, "operation_id": "op-gone", "target_system_tag": "v0.9.0"},
    )
    original = context.read_bytes()

    plan = service.plan_recovery()
    assert _stale_reasons(plan)["state/guided-upgrade-context.json"] == (
        STALE_UNUSABLE_UPGRADE_CONTEXT
    )
    service.recover(
        mode=RECOVERY_MODE_RELEASE_STALE_STATE,
        expected_fingerprint=plan["fingerprint"],
        confirm=True,
        reason="unusable upgrade context",
    )

    backup = tmp_path / "state" / "workflow-recovery"
    directory = next(path for path in backup.iterdir() if path.is_dir())
    assert (directory / "guided-upgrade-context.json").read_bytes() == original
    assert context.exists() is False


def test_advanced_recovery_blocks_any_updater_when_operation_id_is_unreadable(
    tmp_path,
):
    """An unreadable transition names no operation, so every updater counts.

    This is the case the durable stage cannot cover: with no operation id there
    is nothing to inspect by name, so the canonical updater prefix decides — and
    a live one there refuses the release.
    """

    alignment = FakeAlignment(ok=False)
    docker = _Docker(
        listing=[{"container_name": "ems-admin-updater-op-9", "status": "running"}]
    )
    service = _service(
        tmp_path,
        alignment,
        install_state_probe=lambda operation_id: admin_replacement_activity(
            docker, operation_id
        ),
    )
    transition = tmp_path / "state" / "pending-transition.json"
    transition.parent.mkdir(parents=True, exist_ok=True)
    transition.write_text("}{ corrupt", encoding="utf-8")

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _release(service, tmp_path)

    assert docker.listed == [ADMIN_UPDATER_CONTAINER_PREFIX]
    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert excinfo.value.detail == REPLACEMENT_ACTIVE
    assert transition.exists()


# --- races that must never leave two silent owners ----------------------------


def test_a_transition_created_during_setup_entry_ends_as_a_named_conflict(tmp_path):
    """Interleaving: Guided Setup is created while an upgrade transition commits.

    The arbiter serializes its own decide-then-act, but a transition is
    committed by another service outside that lock. The window is real, so the
    contract is not that it cannot happen — it is that the result is a *named*
    conflict that blocks both workflows, never two silent owners.
    """

    alignment = FakeAlignment()
    service = _service(tmp_path, alignment)
    entered = threading.Event()
    release = threading.Event()
    store = service._workflows
    real_ensure_active = store.ensure_active

    def parked_ensure_active(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return real_ensure_active(**kwargs)

    store.ensure_active = parked_ensure_active
    outcome = {}

    def enter_setup():
        try:
            outcome["result"] = service.switch(
                TARGET_GUIDED_SETUP,
                expected_fingerprint=service.inspect()["fingerprint"],
                confirm=True,
                session_id="session-a",
            )
        except AdminWorkflowLifecycleError as exc:
            outcome["result"] = exc

    worker = threading.Thread(target=enter_setup)
    worker.start()
    assert entered.wait(timeout=5)
    # The upgrade transition becomes durable while Setup entry is mid-flight.
    alignment.mode = "guided_upgrade"
    alignment.stage = "resources_verified"
    release.set()
    worker.join(timeout=5)

    view = service.inspect()
    assert view["owner"] == OWNER_CONFLICT
    assert view["state"] == STATE_CONFLICT
    assert view["switchable"] is False
    assert view["recoverable"] is True
    # Neither side was cancelled or cleaned behind the operator's back.
    assert alignment.cancelled == []
    assert service._workflows.load()["status"] == "active"


def test_a_recovery_racing_setup_entry_leaves_one_coherent_owner(tmp_path):
    """Interleaving: Setup entry arrives while a safe recovery is cancelling.

    Both take the arbiter's lock, so the loser re-reads a changed state and is
    refused by its own fingerprint rather than acting on the state it saw.
    """

    alignment = FakeAlignment(mode="guided_upgrade", stage="resources_verified")
    service = _service(tmp_path, alignment)
    entered = threading.Event()
    release = threading.Event()
    real_cancel = alignment.cancel

    def parked_cancel(*, operation_id, coordinator=None):
        entered.set()
        assert release.wait(timeout=5)
        return real_cancel(operation_id=operation_id, coordinator=coordinator)

    alignment.cancel = parked_cancel
    fingerprint = service.inspect()["fingerprint"]
    outcomes = {}

    def recover():
        try:
            outcomes["recovery"] = service.recover(
                mode=RECOVERY_MODE_SAFE,
                expected_fingerprint=fingerprint,
                confirm=True,
            )
        except AdminWorkflowLifecycleError as exc:
            outcomes["recovery"] = exc

    def enter_setup():
        try:
            outcomes["setup"] = service.switch(
                TARGET_GUIDED_SETUP,
                expected_fingerprint=fingerprint,
                confirm=True,
                session_id="session-a",
            )
        except AdminWorkflowLifecycleError as exc:
            outcomes["setup"] = exc

    first = threading.Thread(target=recover)
    first.start()
    assert entered.wait(timeout=5)
    second = threading.Thread(target=enter_setup)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert isinstance(outcomes["recovery"], dict)
    assert isinstance(outcomes["setup"], AdminWorkflowLifecycleError)
    assert outcomes["setup"].code == WORKFLOW_LIFECYCLE_CHANGED
    # One cancellation, and no Setup record minted on the refused path.
    assert alignment.cancelled == ["op-1"]
    assert service._workflows.load() is None
    assert service.inspect()["owner"] == OWNER_NONE


def test_a_conflict_appearing_after_the_preview_refuses_the_switch(tmp_path):
    """A preview taken on a healthy state cannot execute against a conflict."""

    alignment = FakeAlignment()
    service = _service(tmp_path, alignment)
    workflow_id = _start_setup(service)
    plan = service.plan_switch(TARGET_GUIDED_UPGRADE)
    assert plan["blocked"] is False

    # The contradiction appears between preview and execute.
    alignment.mode = "guided_upgrade"
    alignment.stage = "resources_verified"

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        service.switch(
            TARGET_GUIDED_UPGRADE,
            expected_fingerprint=plan["fingerprint"],
            confirm=True,
        )

    assert excinfo.value.code == WORKFLOW_LIFECYCLE_CHANGED
    assert alignment.cancelled == []
    assert service._workflows.load()["workflow_id"] == workflow_id
    assert service._workflows.load()["status"] == "active"
    assert service.inspect()["blocking_reason"] == WORKFLOW_OWNER_CONFLICT
