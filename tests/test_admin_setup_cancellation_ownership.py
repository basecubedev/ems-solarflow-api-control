# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every Setup-owned termination runs through the Setup lifecycle owner.

The narrow ``/api/admin/system-alignment/cancel`` primitive cancels a transition
and nothing else. For Setup-owned transitions that split the lifecycle: the
transition died while the generated config, its metadata and the deployment
marker survived with no owner. These tests pin the ownership contract: the
public primitive refuses Setup-owned modes, build supersede is one backend
operation, and Guided Upgrade cannot start while Setup state is unresolved.

See ``docs/technical/admin-workflow-state.md``.
"""

import json
from pathlib import Path

import pytest

from tests.test_admin_server import (
    _FakeSystemAlignment,
    _attach_system_alignment,
    _control_export_manager,
    _request,
    _serve,
)
from tests.test_admin_setup_preview_authority import (
    _draft_a,
    _start_workflow,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


class _CancelRecordingAlignment(_FakeSystemAlignment):
    """Fake alignment that records primitive cancels instead of hiding them."""

    def __init__(self, stage="resources_verified", *, mode="fresh_install"):
        super().__init__(stage=stage, active=True, mode=mode)
        self.cancelled = []
        self.upgrade_validate_calls = []

    def cancel(self, *, operation_id, coordinator=None):
        del coordinator
        self.cancelled.append(operation_id)
        self.stage = "cancelled"
        self.active = False
        return {"ok": True, "operation_id": operation_id, "stage": "cancelled"}

    def validate_upgrade_target(self, *, requested_tag):
        self.upgrade_validate_calls.append(requested_tag)
        return {
            "ok": True,
            "valid": True,
            "selected_tag": requested_tag,
            "upgrade_allowed": True,
            "selection_fingerprint": "fp",
        }


def _seed_legacy_artifacts(data_dir):
    """Pre-workflow singletons: present, but owned by nothing on record."""

    generated = Path(data_dir) / "generated" / "config.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text('{"devices": []}\n', encoding="utf-8")
    marker = Path(data_dir) / "state" / ".admin-deployment.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"release": "v0.8.0"}) + "\n", encoding="utf-8")
    return generated, marker


def _seed_owned_artifacts(base, srv):
    """Start a workflow and give it artifacts it can prove it owns."""

    from admin.setup_workflow import SetupWorkflowArtifacts

    workflow_id = _start_workflow(base, srv)
    artifacts = SetupWorkflowArtifacts(
        srv.setup_workflows.admin_data_dir, workflow_id=workflow_id
    )
    generated = artifacts.generated_config_path
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text('{"devices": []}\n', encoding="utf-8")
    artifacts.record_generated(
        workflow_id=workflow_id,
        preview_id="pv-" + "0" * 16,
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision={"expected_revision": None, "expect_absent": True},
        prepared_config_sha256="0" * 64,
    )
    marker = artifacts.deployment_marker_path
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "release": "v0.8.0",
                "owner": "guided_setup",
                "workflow_id": workflow_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return workflow_id, generated, marker


def _cancel(base, operation_id="op-1"):
    return _request(
        f"{base}/api/admin/system-alignment/cancel",
        method="POST",
        body={"operation_id": operation_id, "confirm": True},
    )


# --- the public primitive refuses Setup-owned transitions --------------------


@pytest.mark.parametrize("mode", ["fresh_install", "automated_setup"])
def test_public_cancel_rejects_setup_owned_transitions(tmp_path, mode):
    alignment = _CancelRecordingAlignment(mode=mode)
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    generated, marker = _seed_legacy_artifacts(tmp_path)
    try:
        status, _, payload = _cancel(base)

        assert status == 409, payload
        assert payload["error"] == "setup_abandon_required"
        assert alignment.cancelled == [], (
            "the public primitive must not cancel a Setup-owned transition"
        )
        assert alignment.stage == "resources_verified"
        assert generated.exists() and marker.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_public_cancel_fails_closed_for_unknown_transition_modes(tmp_path):
    alignment = _CancelRecordingAlignment(mode="mystery_future_mode")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _cancel(base)

        assert status == 409, payload
        assert payload["error"] == "transition_cancel_unsupported"
        assert alignment.cancelled == []
        assert alignment.stage == "resources_verified"
    finally:
        srv.shutdown()
        srv.server_close()


# --- build supersede is one backend operation --------------------------------


def test_superseding_the_selected_build_is_backend_owned(tmp_path):
    alignment = _CancelRecordingAlignment(mode="fresh_install")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        old_workflow, generated, marker = _seed_owned_artifacts(base, srv)
        status, _, payload = _request(
            f"{base}/api/setup/system-build/supersede",
            method="POST",
            body={"setup_workflow_id": old_workflow, "tag": "v0.9.0"},
        )

        assert status == 200, payload
        assert payload["ok"] is True
        assert payload["superseded_workflow_id"] == old_workflow
        new_workflow = payload["setup_workflow_id"]
        assert new_workflow and new_workflow != old_workflow
        assert payload.get("setup_intent_id"), (
            "the replacement workflow needs a fresh one-shot setup intent"
        )
        assert alignment.cancelled == ["op-1"]
        assert not generated.exists() and not marker.exists(), (
            "no artifact from the superseded build may stay active"
        )

        status, _, payload = _request(
            f"{base}/api/setup/config/apply",
            method="POST",
            body={
                **_draft_a(),
                "setup_workflow_id": old_workflow,
                "config_preview_id": "stale",
            },
        )
        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
    finally:
        srv.shutdown()
        srv.server_close()


def test_supersede_refuses_a_stale_workflow_id(tmp_path):
    alignment = _CancelRecordingAlignment(mode="fresh_install")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        current = _start_workflow(base)
        status, _, payload = _request(
            f"{base}/api/setup/system-build/supersede",
            method="POST",
            body={"setup_workflow_id": "an-older-tab", "tag": "v0.9.0"},
        )
        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
        assert alignment.cancelled == []

        status, _, payload = _request(f"{base}/api/setup/workflow")
        assert status == 200, payload
        assert payload["workflow"]["workflow_id"] == current
    finally:
        srv.shutdown()
        srv.server_close()


# --- Guided Upgrade must resolve active Setup state first --------------------


def test_upgrade_validation_is_blocked_until_setup_is_abandoned(tmp_path):
    alignment = _CancelRecordingAlignment(mode="fresh_install")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, generated, marker = _seed_owned_artifacts(base, srv)
        status, _, payload = _request(
            f"{base}/api/admin/maintenance/upgrade/validate",
            method="POST",
            body={"tag": "v0.9.0"},
        )
        assert status == 409, payload
        assert payload["error"] == "setup_abandon_required"
        assert alignment.upgrade_validate_calls == [], (
            "upgrade validation must not start while Setup owns the transition"
        )

        status, _, payload = _request(
            f"{base}/api/setup/abandon",
            method="POST",
            body={"setup_workflow_id": workflow_id},
        )
        assert status == 200, payload
        assert payload["ok"] is True
        assert not generated.exists() and not marker.exists()

        status, _, payload = _request(
            f"{base}/api/admin/maintenance/upgrade/validate",
            method="POST",
            body={"tag": "v0.9.0"},
        )
        assert status == 200, payload
        assert alignment.upgrade_validate_calls == ["v0.9.0"]
    finally:
        srv.shutdown()
        srv.server_close()


# --- worker-active refusals remove nothing -----------------------------------


class _WorkerActiveAlignment(_CancelRecordingAlignment):
    def cancel(self, *, operation_id, coordinator=None):
        from admin.system_alignment import SystemAlignmentError

        raise SystemAlignmentError(
            "transition_worker_active",
            "The System Build operation is still running.",
        )


def test_worker_active_abandon_refusal_removes_nothing(tmp_path):
    alignment = _WorkerActiveAlignment(mode="fresh_install")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    generated, marker = _seed_legacy_artifacts(tmp_path)
    try:
        workflow_id = _start_workflow(base, srv)
        status, _, payload = _request(
            f"{base}/api/setup/abandon",
            method="POST",
            body={"setup_workflow_id": workflow_id},
        )

        assert status == 409, payload
        assert payload["error"] == "transition_worker_active"
        assert generated.exists() and marker.exists()

        status, _, payload = _request(f"{base}/api/setup/workflow")
        assert payload["workflow"]["workflow_id"] == workflow_id
        assert payload["workflow"]["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()


def test_worker_active_supersede_refusal_keeps_the_old_workflow(tmp_path):
    alignment = _WorkerActiveAlignment(mode="fresh_install")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    generated, marker = _seed_legacy_artifacts(tmp_path)
    try:
        workflow_id = _start_workflow(base, srv)
        status, _, payload = _request(
            f"{base}/api/setup/system-build/supersede",
            method="POST",
            body={"setup_workflow_id": workflow_id, "tag": "v0.9.0"},
        )

        assert status == 409, payload
        assert payload["error"] == "transition_worker_active"
        assert generated.exists() and marker.exists()

        status, _, payload = _request(f"{base}/api/setup/workflow")
        assert payload["workflow"]["workflow_id"] == workflow_id
        assert payload["workflow"]["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()


# --- an active workflow blocks on what it owns, not on what exists ------------


def _seed_installed_artifacts(srv):
    """Installed-system files in the server's real Admin data directory."""

    data_dir = Path(srv.setup_workflows.admin_data_dir)
    generated = data_dir / "generated" / "config.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text('{"devices": [{"name": "INV_1"}]}\n', encoding="utf-8")
    marker = data_dir / "state" / ".admin-deployment.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"release": "v0.6.0-rc", "source": "admin_install"}) + "\n",
        encoding="utf-8",
    )
    return generated, marker


def test_empty_active_workflow_does_not_block_on_installed_artifacts(tmp_path):
    alignment = _CancelRecordingAlignment(stage="cancelled", mode="fresh_install")
    alignment.active = False
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        _start_workflow(base, srv)
        generated, marker = _seed_installed_artifacts(srv)
        generated_bytes = generated.read_bytes()
        marker_bytes = marker.read_bytes()

        status, _, payload = _request(
            f"{base}/api/admin/maintenance/upgrade/validate",
            method="POST",
            body={"tag": "v0.9.0"},
        )

        assert payload.get("error") != "setup_abandon_required", payload
        assert status != 409, payload
        assert generated.read_bytes() == generated_bytes
        assert marker.read_bytes() == marker_bytes
    finally:
        srv.shutdown()
        srv.server_close()


def test_active_workflow_with_a_recorded_artifact_still_blocks(tmp_path):
    alignment = _CancelRecordingAlignment(stage="cancelled", mode="fresh_install")
    alignment.active = False
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id = _start_workflow(base, srv)
        _seed_installed_artifacts(srv)
        srv.setup_workflows.bind_generated_artifacts(
            workflow_id, preview_id="pv-" + "0" * 16
        )

        status, _, payload = _request(
            f"{base}/api/admin/maintenance/upgrade/validate",
            method="POST",
            body={"tag": "v0.9.0"},
        )

        assert status == 409, payload
        assert payload["error"] == "setup_abandon_required"
    finally:
        srv.shutdown()
        srv.server_close()
