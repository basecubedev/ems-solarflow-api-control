# SPDX-License-Identifier: AGPL-3.0-or-later
"""An incomplete Guided Setup cleanup stays owned, and keeps blocking.

Terminalizing a workflow revokes its mutation authority immediately, but that
does not mean its files are gone. Archive 99 marked the workflow terminal and
returned ``abandon_cleanup_incomplete`` — and then let a new Fresh Setup replace
the only ownership record for the files that were left behind, while Guided
Upgrade validation proceeded as if nothing remained.

These tests pin the durable cleanup state: the same workflow ID keeps owning its
failed cleanup across an Admin restart, a replacement Setup and both Guided
Upgrade phases stay blocked with ``setup_cleanup_required``, the retry must name
that exact workflow, and a successful retry converges and unblocks the next
action.

See ``docs/technical/admin-workflow-state.md``.
"""

import json
import shutil
from pathlib import Path
from unittest import mock

import pytest

from tests.test_admin_server import (
    _attach_system_alignment,
    _authorized_body,
    _control_export_manager,
    _own_active_setup_transition,
    _request,
    _serve,
)
from tests.test_admin_setup_cancellation_ownership import _CancelRecordingAlignment

pytestmark = [
    pytest.mark.admin,
    pytest.mark.backup_restore,
    pytest.mark.setup,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _start_workflow(base, srv=None):
    """Confirm Fresh Setup; with ``srv`` also take ownership of its transition.

    Production links a Setup transition into its workflow inside the pre-commit
    boundary, so a workflow that reached the harness's pre-seeded transition has
    to name its exact operation id before it may cancel it.
    """

    status, _, payload = _request(
        f"{base}/api/admin/start-path",
        method="POST",
        body={"choice": "setup_new", "confirm": True},
    )
    if srv is not None and payload.get("setup_workflow_id"):
        _own_active_setup_transition(srv, base, payload["setup_workflow_id"])
    return status, payload


def _write_generated(base):
    """Give the active workflow a real generated config to clean up."""

    status, _, payload = _request(
        f"{base}/api/setup/config/write",
        method="POST",
        body=_authorized_body(base),
    )
    assert status == 200, payload
    return Path(payload["path"])


def _abandon(base, workflow_id):
    return _request(
        f"{base}/api/setup/abandon",
        method="POST",
        body={"setup_workflow_id": workflow_id},
    )


def _workflow_view(base):
    status, _, payload = _request(f"{base}/api/setup/workflow")
    assert status == 200, payload
    return payload.get("workflow")


def _rmtree_fails_for(target):
    real_rmtree = shutil.rmtree

    def guarded(path, *args, **kwargs):
        if Path(path) == Path(target):
            raise PermissionError(13, "Permission denied")
        return real_rmtree(path, *args, **kwargs)

    return mock.patch.object(shutil, "rmtree", guarded)


def _fail_cleanup_once(base, workflow_id, generated):
    """Abandon with exactly one removal failing; returns the abandon response."""

    with _rmtree_fails_for(generated.parent.parent):
        status, _, payload = _abandon(base, workflow_id)
    assert status == 500, payload
    assert payload["error"] == "abandon_cleanup_incomplete"
    assert generated.exists()
    return payload


def _upgrade_validate(base):
    return _request(
        f"{base}/api/admin/maintenance/upgrade/validate",
        method="POST",
        body={"tag": "v0.9.0"},
    )


def _upgrade_execute(base):
    return _request(
        f"{base}/api/admin/maintenance/upgrade/execute",
        method="POST",
        body={
            "confirm": True,
            "target_release": "v0.9.0",
            "selection_fingerprint": "verified-selection",
        },
    )


# --- ownership survives the failure ------------------------------------------


def test_cleanup_failure_remains_owned_after_restart(tmp_path):
    manager = _control_export_manager(tmp_path)
    srv, base = _serve(release_manager=manager)
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated = _write_generated(base)
        _fail_cleanup_once(base, workflow_id, generated)

        view = _workflow_view(base)
        assert view["workflow_id"] == workflow_id
        assert view["status"] == "abandoned"
        assert view["cleanup"]["state"] == "pending"
        assert view["cleanup"]["blocking"] is True
    finally:
        srv.shutdown()
        srv.server_close()

    restarted, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        view = _workflow_view(base)
        assert view["workflow_id"] == workflow_id, (
            "the failed cleanup must keep its exact owner across a restart"
        )
        assert view["cleanup"]["state"] == "pending"
        assert generated.exists()
    finally:
        restarted.shutdown()
        restarted.server_close()


def test_cleanup_summary_redacts_server_paths(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated = _write_generated(base)
        _fail_cleanup_once(base, workflow_id, generated)

        view = _workflow_view(base)
        summary = json.dumps(view["cleanup"])
        assert str(generated) not in summary
        assert str(tmp_path) not in summary
        assert "Permission denied" not in summary
        # Kinds and statuses are enough for the recovery UI to name the action.
        kinds = {entry["kind"] for entry in view["cleanup"]["artifacts"]}
        assert "generated_config" in kinds
        assert all(
            entry["status"] == "failed" for entry in view["cleanup"]["artifacts"]
        )
        assert view["cleanup"]["failed_count"] == len(view["cleanup"]["artifacts"])
    finally:
        srv.shutdown()
        srv.server_close()


def test_cleanup_record_validation_fails_closed(tmp_path):
    """A tampered cleanup state makes the whole record unreadable, not trusted."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        record_path = srv.setup_workflows.path
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["cleanup"]["state"] = "definitely_fine"
        record_path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        assert _workflow_view(base) is None
        status, _, refused = _request(
            f"{base}/api/setup/config/apply",
            method="POST",
            body={"devices": [], "setup_workflow_id": workflow_id},
        )
        assert status == 409, refused
        assert refused["error"] == "setup_workflow_not_active"
    finally:
        srv.shutdown()
        srv.server_close()


# --- blocking while cleanup is pending ---------------------------------------


def test_cleanup_pending_blocks_new_setup(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated = _write_generated(base)
        _fail_cleanup_once(base, workflow_id, generated)

        status, payload = _start_workflow(base, srv)

        assert status == 409, payload
        assert payload["error"] == "setup_cleanup_required"
        assert "setup_workflow_id" not in payload
        assert "setup_intent_id" not in payload, (
            "a blocked start-path must not issue a new setup intent"
        )
        assert _workflow_view(base)["workflow_id"] == workflow_id
    finally:
        srv.shutdown()
        srv.server_close()


def test_cleanup_pending_blocks_upgrade_validation(tmp_path):
    alignment = _UpgradeRecordingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated = _write_generated(base)
        _fail_cleanup_once(base, workflow_id, generated)

        status, _, payload = _upgrade_validate(base)

        assert status == 409, payload
        assert payload["error"] == "setup_cleanup_required"
        assert alignment.upgrade_validate_calls == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_cleanup_pending_blocks_upgrade_execution(tmp_path):
    alignment = _UpgradeRecordingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated = _write_generated(base)
        _fail_cleanup_once(base, workflow_id, generated)

        status, _, payload = _upgrade_execute(base)

        assert status == 409, payload
        assert payload["error"] == "setup_cleanup_required"
        assert alignment.resolve_calls == [], (
            "a blocked upgrade must not even resolve its target build"
        )
    finally:
        srv.shutdown()
        srv.server_close()


# --- retry converges ---------------------------------------------------------


def test_retry_requires_matching_terminal_workflow_id(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated = _write_generated(base)
        _fail_cleanup_once(base, workflow_id, generated)

        status, _, refused = _request(
            f"{base}/api/setup/abandon", method="POST", body={}
        )
        assert status == 409, refused
        assert refused["error"] == "setup_workflow_required"

        status, _, foreign = _abandon(base, "some-other-workflow-id")
        assert status == 409, foreign
        assert foreign["error"] == "setup_workflow_not_active"
        assert generated.exists(), "a refused retry must remove nothing"
        assert _workflow_view(base)["cleanup"]["state"] == "pending"
    finally:
        srv.shutdown()
        srv.server_close()


def test_retry_cleanup_converges_and_unblocks_setup(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated = _write_generated(base)
        _fail_cleanup_once(base, workflow_id, generated)

        status, _, retried = _abandon(base, workflow_id)
        assert status == 200, retried
        assert retried["ok"] is True
        assert not generated.exists()
        assert _workflow_view(base)["cleanup"]["state"] == "complete"

        # A second retry is idempotent, then a new Setup is allowed again.
        status, _, again = _abandon(base, workflow_id)
        assert status == 200, again
        assert again["ok"] is True

        status, payload = _start_workflow(base, srv)
        assert status == 200, payload
        assert payload["setup_workflow_id"] != workflow_id
        assert payload.get("setup_intent_id")
    finally:
        srv.shutdown()
        srv.server_close()


def test_retry_cleanup_converges_and_unblocks_upgrade(tmp_path):
    alignment = _UpgradeRecordingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated = _write_generated(base)
        _fail_cleanup_once(base, workflow_id, generated)

        status, _, retried = _abandon(base, workflow_id)
        assert status == 200, retried

        status, _, validated = _upgrade_validate(base)
        assert status == 200, validated
        assert alignment.upgrade_validate_calls == ["v0.9.0"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_retry_after_restart_uses_the_same_workflow_id(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated = _write_generated(base)
        _fail_cleanup_once(base, workflow_id, generated)
    finally:
        srv.shutdown()
        srv.server_close()

    restarted, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        status, _, retried = _abandon(base, workflow_id)
        assert status == 200, retried
        assert retried["ok"] is True
        assert not generated.exists()
        assert _workflow_view(base)["cleanup"]["state"] == "complete"
    finally:
        restarted.shutdown()
        restarted.server_close()


class _UpgradeRecordingAlignment(_CancelRecordingAlignment):
    """Setup-owned transition that records which upgrade phases were reached."""

    def __init__(self):
        super().__init__(mode="fresh_install")
        self.resolve_calls = []

    def resolve(self, requested_tag):
        self.resolve_calls.append(requested_tag)
        raise AssertionError("a blocked upgrade must not resolve a build")


# --- migration: records stranded by the pre-claim cleanup ---------------------


_STRANDED_CLEANUP = {
    "state": "review_required",
    "attempted_at": "2026-07-31T21:15:47Z",
    "failed_count": 0,
    "review_count": 2,
    "artifacts": [
        {"kind": "legacy_generated_config", "status": "review_required"},
        {"kind": "deployment_marker", "status": "review_required"},
    ],
}


def _strand_workflow(srv, workflow_id, cleanup=None):
    """Write the exact record shape the old cleanup left behind."""

    store = srv.setup_workflows
    record = store.load()
    record["status"] = "abandoned"
    record["preview"] = None
    record["cleanup"] = dict(cleanup or _STRANDED_CLEANUP)
    assert record["workflow_id"] == workflow_id
    store.path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return record


def _seed_installed_files(srv):
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


def test_stranded_zero_claim_review_is_reconciled_without_touching_files(tmp_path):
    alignment = _UpgradeRecordingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated, marker = _seed_installed_files(srv)
        generated_bytes = generated.read_bytes()
        marker_bytes = marker.read_bytes()
        _strand_workflow(srv, workflow_id)

        view = _workflow_view(base)

        assert view["cleanup"]["state"] == "complete"
        assert generated.read_bytes() == generated_bytes
        assert marker.read_bytes() == marker_bytes

        status, _, payload = _request(
            f"{base}/api/admin/maintenance/upgrade/validate",
            method="POST",
            body={"tag": "v0.9.0"},
        )
        assert payload.get("error") != "setup_cleanup_required", payload

        status, payload = _start_workflow(base, srv)
        assert status == 200, payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_reconciled_state_survives_a_fresh_read(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        _seed_installed_files(srv)
        _strand_workflow(srv, workflow_id)

        _workflow_view(base)

        assert srv.setup_workflows.load()["cleanup"]["state"] == "complete"
        assert _workflow_view(base)["cleanup"]["state"] == "complete"
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_claimed_review_state_is_never_reconciled(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        _seed_installed_files(srv)
        srv.setup_workflows.record_deployment_marker(workflow_id)
        _strand_workflow(srv, workflow_id)

        view = _workflow_view(base)

        assert view["cleanup"]["state"] == "review_required"

        status, payload = _start_workflow(base, srv)
        assert status == 409, payload
        assert payload["error"] == "setup_cleanup_required"
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_workflow_directory_review_is_never_reconciled(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        _strand_workflow(
            srv,
            workflow_id,
            cleanup={
                "state": "review_required",
                "attempted_at": "2026-07-31T21:15:47Z",
                "failed_count": 0,
                "review_count": 1,
                "artifacts": [
                    {"kind": "workflow_directory", "status": "review_required"}
                ],
            },
        )

        assert _workflow_view(base)["cleanup"]["state"] == "review_required"
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_stranded_record_does_not_block_a_new_setup_on_first_contact(tmp_path):
    """The recovery must not depend on the browser reading the workflow first."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _, payload = _start_workflow(base, srv)
        workflow_id = payload["setup_workflow_id"]
        generated, marker = _seed_installed_files(srv)
        generated_bytes = generated.read_bytes()
        marker_bytes = marker.read_bytes()
        _strand_workflow(srv, workflow_id)

        status, payload = _start_workflow(base, srv)

        assert status == 200, payload
        assert generated.read_bytes() == generated_bytes
        assert marker.read_bytes() == marker_bytes
    finally:
        srv.shutdown()
        srv.server_close()
