# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stranded Admin workflow has a supported recovery path, not an SSH session.

Old Admin versions, a corrupt workflow record and an orphaned transition after a
crash all produced the same advice: delete a JSON file on the host. These tests
pin the two supported recoveries instead.

``safe`` uses nothing but the normal domain operations — exact cancellation,
claim-aware cleanup, normal terminalization, operation-bound context clearing.
``release_stale_state`` may quarantine durable Admin workflow metadata, but only
after proving no mutation can still be running, only after backing the files up
with their hashes, and never anything belonging to the installed system.

See ``docs/technical/admin-workflow-state.md``.
"""

import hashlib
import json
import threading

import pytest

from admin.workflow_lifecycle import (
    AdminWorkflowLifecycleError,
    OWNER_NONE,
    RECOVERY_MODE_RELEASE_STALE_STATE,
    RECOVERY_MODE_SAFE,
    WORKFLOW_LIFECYCLE_CHANGED,
    WORKFLOW_RECOVERY_UNSAFE,
)
from admin.guided_upgrade import guided_upgrade_request_fingerprint
from admin.guided_upgrade_context import GuidedUpgradeContextStore
from tests.test_admin_workflow_lifecycle import FakeAlignment, write_upgrade_context
from tests.test_admin_workflow_switching import _claim_artifacts, _start_setup, _service

pytestmark = pytest.mark.simulation


def _corrupt_setup_record(service):
    path = service._workflows.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    return path


def _corrupt_transition(tmp_path):
    path = tmp_path / "state" / "pending-transition.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("}{ corrupt", encoding="utf-8")
    return path


def _seed_installed_system(tmp_path):
    """Files the recovery may never touch, whatever the workflow state is."""

    seeded = {}
    for relative, content in (
        ("state/.admin-deployment.json", '{"release": "v0.8.0"}\n'),
        ("config/config.json", '{"devices": ["live"]}\n'),
        ("docker-compose.yml", "services: {}\n"),
        ("data/runtime-state.json", '{"mode": "auto"}\n'),
        ("backups/config/backup-1.json", '{"kept": true}\n'),
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        seeded[relative] = content
    return seeded


def _assert_installed_system_intact(tmp_path, seeded):
    for relative, content in seeded.items():
        assert (tmp_path / relative).read_text(encoding="utf-8") == content


def _recover(service, mode, **kwargs):
    kwargs.setdefault("confirm", True)
    kwargs.setdefault("expected_fingerprint", service.inspect()["fingerprint"])
    if mode == RECOVERY_MODE_RELEASE_STALE_STATE:
        kwargs.setdefault("reason", "corrupt workflow metadata")
    return service.recover(mode=mode, **kwargs)


def _backup_dirs(tmp_path):
    root = tmp_path / "state" / "workflow-recovery"
    return sorted(path for path in root.iterdir() if path.is_dir()) if root.exists() else []


# --- safe recovery ------------------------------------------------------------


def test_safe_recovery_converges_a_pending_setup_cleanup(tmp_path):
    service = _service(tmp_path)
    workflow_id = _start_setup(service)
    generated = _claim_artifacts(service, workflow_id)
    service._workflows.finish(
        workflow_id,
        status="abandoned",
        cleanup={
            "state": "pending",
            "attempted_at": "2026-01-01T00:00:00Z",
            "failed_count": 1,
            "review_count": 0,
            "artifacts": [{"kind": "generated_config", "status": "failed"}],
        },
    )

    result = _recover(service, RECOVERY_MODE_SAFE)

    assert result["ok"] is True
    assert result["mode"] == RECOVERY_MODE_SAFE
    assert "setup_cleanup" in result["actions"]
    assert generated.exists() is False
    assert service._workflows.load()["cleanup"]["state"] == "complete"
    assert result["lifecycle"]["owner"] == OWNER_NONE
    assert result["lifecycle"]["switchable"] is True


def test_safe_recovery_normalizes_an_old_zero_claim_review_record(tmp_path):
    service = _service(tmp_path)
    workflow_id = _start_setup(service)
    marker = tmp_path / "state" / ".admin-deployment.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"release": "v0.8.0"}\n', encoding="utf-8")
    service._workflows.finish(
        workflow_id,
        status="abandoned",
        cleanup={
            "state": "review_required",
            "attempted_at": "2026-01-01T00:00:00Z",
            "failed_count": 0,
            "review_count": 1,
            "artifacts": [{"kind": "deployment_marker", "status": "review_required"}],
        },
    )

    result = _recover(service, RECOVERY_MODE_SAFE)

    assert result["ok"] is True
    assert service._workflows.load()["cleanup"]["state"] == "complete"
    assert marker.read_text(encoding="utf-8") == '{"release": "v0.8.0"}\n'


def test_safe_recovery_cancels_an_expired_upgrade_transition(tmp_path):
    alignment = FakeAlignment(
        mode="guided_upgrade", stage="admin_reconnect_pending", cancel_available=True
    )
    service = _service(tmp_path, alignment)
    write_upgrade_context(tmp_path, "op-1")

    result = _recover(service, RECOVERY_MODE_SAFE)

    assert result["ok"] is True
    assert alignment.cancelled == ["op-1"]
    assert "transition_cancel" in result["actions"]
    assert (tmp_path / "state" / "guided-upgrade-context.json").exists() is False


def test_safe_recovery_clears_a_usable_orphaned_upgrade_context(tmp_path):
    """Only a context the loader still accepts is an ordinary orphan.

    One that no longer reproduces is evidence, not litter: it goes through the
    advanced release so an operator gets it backed up first.
    """

    service = _service(tmp_path)
    store = GuidedUpgradeContextStore(tmp_path / "state")
    options = GuidedUpgradeContextStore._normalize_options({})
    store.save(
        operation_id="op-gone",
        target_system_tag="v0.9.0",
        options=options,
        request_fingerprint=guided_upgrade_request_fingerprint("v0.9.0", options),
    )

    result = _recover(service, RECOVERY_MODE_SAFE)

    assert result["ok"] is True
    assert "upgrade_context_clear" in result["actions"]
    assert (tmp_path / "state" / "guided-upgrade-context.json").exists() is False


def test_safe_recovery_never_deletes_a_state_file(tmp_path):
    service = _service(tmp_path)
    workflow_id = _start_setup(service)

    _recover(service, RECOVERY_MODE_SAFE)

    stored = service._workflows.load()
    assert stored["workflow_id"] == workflow_id
    assert stored["status"] == "abandoned"
    assert _backup_dirs(tmp_path) == []


def test_safe_recovery_is_refused_while_a_worker_is_active(tmp_path):
    alignment = FakeAlignment(
        mode="guided_upgrade", stage="ems_operation_running", worker_active=True
    )
    service = _service(tmp_path, alignment)

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _recover(service, RECOVERY_MODE_SAFE)

    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert alignment.cancelled == []


def test_safe_recovery_cannot_repair_a_corrupt_record(tmp_path):
    service = _service(tmp_path)
    _corrupt_setup_record(service)

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _recover(service, RECOVERY_MODE_SAFE)

    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert excinfo.value.detail == "workflow_state_malformed"


def test_safe_recovery_is_idempotent(tmp_path):
    service = _service(tmp_path)
    _start_setup(service)

    first = _recover(service, RECOVERY_MODE_SAFE)
    second = _recover(service, RECOVERY_MODE_SAFE)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["actions"] == []


# --- recovery planning --------------------------------------------------------


def test_a_corrupt_setup_record_produces_an_advanced_plan(tmp_path):
    service = _service(tmp_path)
    _corrupt_setup_record(service)

    plan = service.plan_recovery()

    assert plan["ok"] is True
    assert plan["safe"]["available"] is False
    assert plan["advanced"]["available"] is True
    assert plan["advanced"]["files"] == [
        {"name": "state/guided-setup-workflow.json", "reason": "unreadable_state"}
    ]
    assert plan["advanced"]["confirmation_required"] is True
    for preserved in ("config/config.json", "docker-compose.yml"):
        assert preserved in plan["will_preserve"]
    assert plan["fingerprint"] == service.inspect()["fingerprint"]


def test_a_corrupt_transition_produces_an_advanced_plan(tmp_path):
    alignment = FakeAlignment(ok=False)
    service = _service(tmp_path, alignment)
    _corrupt_transition(tmp_path)

    plan = service.plan_recovery()

    assert plan["advanced"]["available"] is True
    assert {
        "name": "state/pending-transition.json",
        "reason": "unreadable_state",
    } in plan["advanced"]["files"]


def test_a_healthy_console_offers_no_recovery(tmp_path):
    plan = _service(tmp_path).plan_recovery()

    assert plan["safe"]["available"] is False
    assert plan["advanced"]["available"] is False
    assert plan["blocking"] is False


def test_a_blocked_console_offers_safe_recovery(tmp_path):
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

    plan = service.plan_recovery()

    assert plan["blocking"] is True
    assert plan["safe"]["available"] is True


def test_the_plan_exposes_no_absolute_path(tmp_path):
    service = _service(tmp_path)
    _corrupt_setup_record(service)

    assert str(tmp_path) not in json.dumps(service.plan_recovery())


# --- advanced stale-state release ---------------------------------------------


def test_advanced_release_backs_up_before_it_quarantines(tmp_path):
    service = _service(tmp_path)
    path = _corrupt_setup_record(service)
    original = path.read_bytes()
    seeded = _seed_installed_system(tmp_path)

    result = _recover(service, RECOVERY_MODE_RELEASE_STALE_STATE)

    assert result["ok"] is True
    assert result["mode"] == RECOVERY_MODE_RELEASE_STALE_STATE
    assert path.exists() is False
    backups = _backup_dirs(tmp_path)
    assert len(backups) == 1
    saved = backups[0] / "guided-setup-workflow.json"
    assert saved.read_bytes() == original
    _assert_installed_system_intact(tmp_path, seeded)


def test_the_recovery_manifest_records_reason_hashes_and_fingerprint(tmp_path):
    service = _service(tmp_path)
    path = _corrupt_setup_record(service)
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    fingerprint = service.inspect()["fingerprint"]

    result = _recover(
        service, RECOVERY_MODE_RELEASE_STALE_STATE, reason="corrupt workflow metadata"
    )

    manifest = json.loads(
        (_backup_dirs(tmp_path)[0] / "recovery-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["mode"] == RECOVERY_MODE_RELEASE_STALE_STATE
    assert manifest["reason"] == "corrupt workflow metadata"
    assert manifest["lifecycle_fingerprint"] == fingerprint
    assert manifest["created_at"].endswith("Z")
    assert manifest["files"] == [
        {
            "name": "state/guided-setup-workflow.json",
            "reason": "unreadable_state",
            "sha256": digest,
            "bytes": len(original),
        }
    ]
    assert result["backup"]["files"] == ["state/guided-setup-workflow.json"]


def test_advanced_release_unblocks_both_guided_workflows(tmp_path):
    service = _service(tmp_path)
    _corrupt_setup_record(service)

    result = _recover(service, RECOVERY_MODE_RELEASE_STALE_STATE)

    assert result["lifecycle"]["owner"] == OWNER_NONE
    assert result["lifecycle"]["switchable"] is True
    assert result["lifecycle"]["state"] == "idle"


def test_advanced_release_is_refused_while_a_worker_is_active(tmp_path):
    alignment = FakeAlignment(
        mode="guided_upgrade", stage="ems_operation_running", worker_active=True
    )
    service = _service(tmp_path, alignment)
    _corrupt_setup_record(service)

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _recover(service, RECOVERY_MODE_RELEASE_STALE_STATE)

    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert _backup_dirs(tmp_path) == []
    assert service._workflows.path.exists()


def test_advanced_release_is_refused_while_a_replacement_may_run(tmp_path):
    alignment = FakeAlignment(mode="guided_upgrade", stage="admin_reconnect_pending")
    service = _service(tmp_path, alignment)
    _corrupt_setup_record(service)

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _recover(service, RECOVERY_MODE_RELEASE_STALE_STATE)

    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert _backup_dirs(tmp_path) == []


def test_advanced_release_is_refused_while_a_setup_mutation_is_claimed(tmp_path):
    service = _service(tmp_path)
    workflow_id = _start_setup(service)

    with service._lifecycle.claim_mutation(
        workflow_id=workflow_id, operation="config_apply"
    ):
        with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
            _recover(service, RECOVERY_MODE_RELEASE_STALE_STATE)

    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert _backup_dirs(tmp_path) == []


def test_advanced_release_is_refused_after_the_state_changed(tmp_path):
    service = _service(tmp_path)
    _corrupt_setup_record(service)
    stale = service.inspect()["fingerprint"]
    service._workflows.path.write_text("{ still not json but different", encoding="utf-8")

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        service.recover(
            mode=RECOVERY_MODE_RELEASE_STALE_STATE,
            expected_fingerprint=stale,
            confirm=True,
            reason="corrupt workflow metadata",
        )

    assert excinfo.value.code == WORKFLOW_LIFECYCLE_CHANGED
    assert _backup_dirs(tmp_path) == []


def test_advanced_release_requires_confirmation_and_a_reason(tmp_path):
    service = _service(tmp_path)
    _corrupt_setup_record(service)

    with pytest.raises(AdminWorkflowLifecycleError) as unconfirmed:
        service.recover(
            mode=RECOVERY_MODE_RELEASE_STALE_STATE,
            expected_fingerprint=service.inspect()["fingerprint"],
            confirm=False,
            reason="corrupt workflow metadata",
        )
    with pytest.raises(AdminWorkflowLifecycleError) as unexplained:
        service.recover(
            mode=RECOVERY_MODE_RELEASE_STALE_STATE,
            expected_fingerprint=service.inspect()["fingerprint"],
            confirm=True,
            reason="   ",
        )

    assert unconfirmed.value.code == "confirmation_required"
    assert unexplained.value.code == "recovery_reason_required"
    assert _backup_dirs(tmp_path) == []


def test_a_second_advanced_release_finds_nothing_left_to_release(tmp_path):
    service = _service(tmp_path)
    _corrupt_setup_record(service)

    first = _recover(service, RECOVERY_MODE_RELEASE_STALE_STATE)
    second = _recover(service, RECOVERY_MODE_RELEASE_STALE_STATE)

    assert first["released"] == ["state/guided-setup-workflow.json"]
    assert second["released"] == []
    assert len(_backup_dirs(tmp_path)) == 1


def test_advanced_release_refuses_a_symlinked_state_directory(tmp_path):
    service = _service(tmp_path)
    _corrupt_setup_record(service)
    state = tmp_path / "state"
    elsewhere = tmp_path / "elsewhere"
    state.rename(elsewhere)
    state.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(AdminWorkflowLifecycleError) as excinfo:
        _recover(service, RECOVERY_MODE_RELEASE_STALE_STATE)

    assert excinfo.value.code == WORKFLOW_RECOVERY_UNSAFE
    assert (elsewhere / "guided-setup-workflow.json").exists()


def test_advanced_release_ignores_a_browser_supplied_path(tmp_path):
    service = _service(tmp_path)
    _corrupt_setup_record(service)
    victim = tmp_path / "config" / "config.json"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text('{"devices": ["live"]}\n', encoding="utf-8")

    with pytest.raises(TypeError):
        service.recover(
            mode=RECOVERY_MODE_RELEASE_STALE_STATE,
            expected_fingerprint=service.inspect()["fingerprint"],
            confirm=True,
            reason="corrupt workflow metadata",
            files=["../config/config.json"],
        )

    assert victim.read_text(encoding="utf-8") == '{"devices": ["live"]}\n'


def test_two_concurrent_advanced_releases_produce_one_backup(tmp_path):
    """Interleaving: both requests carry the same fingerprint, one may act."""

    service = _service(tmp_path)
    _corrupt_setup_record(service)
    fingerprint = service.inspect()["fingerprint"]
    ready = threading.Barrier(2, timeout=5)
    outcomes = {}

    def run(name):
        ready.wait()
        try:
            outcomes[name] = service.recover(
                mode=RECOVERY_MODE_RELEASE_STALE_STATE,
                expected_fingerprint=fingerprint,
                confirm=True,
                reason="corrupt workflow metadata",
            )
        except AdminWorkflowLifecycleError as exc:
            outcomes[name] = exc

    threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    released = [
        value
        for value in outcomes.values()
        if isinstance(value, dict) and value["released"]
    ]
    assert len(released) == 1
    assert len(_backup_dirs(tmp_path)) == 1


def test_the_manifest_copies_no_credential_material(tmp_path):
    service = _service(tmp_path)
    service._workflows.path.parent.mkdir(parents=True, exist_ok=True)
    service._workflows.path.write_text(
        json.dumps({"format_version": 1, "password": "super-secret"}), encoding="utf-8"
    )
    secrets_file = tmp_path / "state" / "credentials.json"
    secrets_file.write_text('{"password": "super-secret"}\n', encoding="utf-8")

    _recover(service, RECOVERY_MODE_RELEASE_STALE_STATE)

    backup = _backup_dirs(tmp_path)[0]
    manifest = (backup / "recovery-manifest.json").read_text(encoding="utf-8")
    assert "super-secret" not in manifest
    assert (backup / "credentials.json").exists() is False
    assert secrets_file.exists()
