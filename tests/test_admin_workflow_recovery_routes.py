# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HTTP surface of the unified workflow lifecycle.

Preview and mutation are separate endpoints on purpose: a confirmation is only
ever shown against the state it was computed for, and the exact fingerprint is
what proves the browser acted on that state. The recovery routes additionally
never accept a filesystem path — the server derives every recoverable file from
its own stores.

See ``docs/technical/admin-workflow-state.md``.
"""

import json
from pathlib import Path

import pytest

from admin.workflow_lifecycle import ReplacementActivity
from tests.admin_auth_helpers import raw_request
from tests.test_admin_setup_cancellation_ownership import _CancelRecordingAlignment
from tests.test_admin_server import (
    _FakeSystemAlignment,
    _attach_system_alignment,
    _control_export_manager,
    _own_active_setup_transition,
    _request,
    _serve,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.backup_restore,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
]

LIFECYCLE = "/api/admin/workflow-lifecycle"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


class _UpgradeAlignment(_FakeSystemAlignment):
    """A cancellable Guided Upgrade transition that records its cancels."""

    def __init__(self, stage="resources_verified"):
        super().__init__(stage=stage, active=True, mode="guided_upgrade")
        self.cancelled = []

    def cancel(self, *, operation_id, coordinator=None):
        del coordinator
        self.cancelled.append(operation_id)
        self.stage = "cancelled"
        self.active = False
        return {"ok": True, "operation_id": operation_id, "stage": "cancelled"}


def _start_setup(base, srv):
    status, _, payload = _request(
        f"{base}/api/admin/start-path",
        method="POST",
        body={"choice": "setup_new", "confirm": True},
    )
    assert status == 200, payload
    if payload.get("setup_workflow_id"):
        _own_active_setup_transition(srv, base, payload["setup_workflow_id"])
    return payload["setup_workflow_id"]


def _inspect(base):
    status, _, payload = _request(f"{base}{LIFECYCLE}")
    assert status == 200, payload
    return payload


def _switch_preview(base, target):
    return _request(
        f"{base}{LIFECYCLE}/switch/preview", method="POST", body={"target": target}
    )


def _switch(base, target, *, fingerprint=None, confirm=True):
    body = {"target": target, "confirm": confirm}
    body["fingerprint"] = (
        fingerprint if fingerprint is not None else _inspect(base)["fingerprint"]
    )
    return _request(f"{base}{LIFECYCLE}/switch", method="POST", body=body)


def _recovery_preview(base):
    return _request(f"{base}{LIFECYCLE}/recovery/preview", method="POST")


def _recover(base, mode, *, fingerprint=None, confirm=True, reason="stale metadata"):
    body = {"mode": mode, "confirm": confirm, "reason": reason}
    body["fingerprint"] = (
        fingerprint if fingerprint is not None else _inspect(base)["fingerprint"]
    )
    return _request(f"{base}{LIFECYCLE}/recovery", method="POST", body=body)


# --- inspection ---------------------------------------------------------------


def test_lifecycle_status_reports_the_owner_and_a_fingerprint(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _start_setup(base, srv)

        payload = _inspect(base)

        assert payload["ok"] is True
        assert payload["owner"] == "guided_setup"
        assert payload["fingerprint"].startswith("sha256:")
        assert str(tmp_path) not in json.dumps(payload)
    finally:
        srv.shutdown()
        srv.server_close()


def test_lifecycle_routes_require_authentication(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        read, _, _ = raw_request(f"{base}{LIFECYCLE}")
        preview, _, _ = raw_request(
            f"{base}{LIFECYCLE}/switch/preview",
            method="POST",
            body={"target": "guided_upgrade"},
        )
        recovery, _, _ = raw_request(
            f"{base}{LIFECYCLE}/recovery/preview", method="POST"
        )

        assert read == 401
        assert preview == 401
        assert recovery == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_lifecycle_mutations_require_csrf(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _start_setup(base, srv)
        fingerprint = _inspect(base)["fingerprint"]
        session = _request(f"{base}{LIFECYCLE}")

        del session
        status, _, _ = _request(
            f"{base}{LIFECYCLE}/switch",
            method="POST",
            body={
                "target": "guided_upgrade",
                "confirm": True,
                "fingerprint": fingerprint,
            },
            extra_headers={"X-CSRF-Token": "wrong"},
        )

        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()


# --- switch preview and execution --------------------------------------------


def test_switch_preview_is_read_only_and_names_the_reset_scope(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        workflow_id = _start_setup(base, srv)

        status, _, payload = _switch_preview(base, "guided_upgrade")

        assert status == 200, payload
        assert payload["action"] == "discard_guided_setup"
        assert payload["confirmation_required"] is True
        assert "Guided Setup workflow" in payload["will_reset"]
        assert "live EMS configuration" in payload["will_preserve"]
        assert payload["fingerprint"] == _inspect(base)["fingerprint"]
        assert srv.setup_workflows.load()["workflow_id"] == workflow_id
        assert srv.setup_workflows.load()["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()


def test_switch_discards_the_setup_and_unblocks_the_upgrade(tmp_path):
    alignment = _CancelRecordingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        _start_setup(base, srv)
        blocked, _, conflict = _request(
            f"{base}/api/admin/maintenance/upgrade/validate",
            method="POST",
            body={"tag": "v0.9.0"},
        )
        assert blocked == 409
        assert conflict["error"] == "setup_abandon_required"

        status, _, payload = _switch(base, "guided_upgrade")

        assert status == 200, payload
        assert payload["ok"] is True
        assert payload["lifecycle"]["owner"] == "none"
        assert alignment.cancelled == ["op-1"]
        assert srv.setup_workflows.load()["status"] == "abandoned"
        validated, _, upgrade = _request(
            f"{base}/api/admin/maintenance/upgrade/validate",
            method="POST",
            body={"tag": "v0.9.0"},
        )
        assert validated == 200, upgrade
    finally:
        srv.shutdown()
        srv.server_close()


def test_switch_to_setup_cancels_the_upgrade_and_returns_a_fresh_intent(tmp_path):
    alignment = _UpgradeAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _switch(base, "guided_setup")

        assert status == 200, payload
        assert payload["action"] == "cancel_guided_upgrade"
        assert alignment.cancelled == ["op-1"]
        assert payload["setup_workflow_id"]
        assert payload["setup_intent_id"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_stale_fingerprint_is_refused_with_a_stable_code(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _start_setup(base, srv)

        status, _, payload = _switch(base, "guided_upgrade", fingerprint="sha256:stale")

        assert status == 409
        assert payload["error"] == "workflow_lifecycle_changed"
        assert payload["lifecycle"]["owner"] == "guided_setup"
        assert srv.setup_workflows.load()["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_unconfirmed_switch_changes_nothing(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        _start_setup(base, srv)

        status, _, payload = _switch(base, "guided_upgrade", confirm=False)

        assert status == 400
        assert payload["error"] == "confirmation_required"
        assert srv.setup_workflows.load()["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()


def test_unsupported_switch_fields_and_targets_are_refused(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        extra, _, extra_payload = _request(
            f"{base}{LIFECYCLE}/switch",
            method="POST",
            body={
                "target": "guided_upgrade",
                "confirm": True,
                "fingerprint": _inspect(base)["fingerprint"],
                "force": True,
            },
        )
        target, _, target_payload = _switch_preview(base, "maintenance")
        missing, _, missing_payload = _request(
            f"{base}{LIFECYCLE}/switch",
            method="POST",
            body={"target": "guided_upgrade", "confirm": True},
        )

        assert extra == 400 and extra_payload["error"] == "unsupported_field"
        assert target == 400
        assert target_payload["error"] == "unsupported_workflow_target"
        assert missing == 400
        assert missing_payload["error"] == "workflow_fingerprint_required"
    finally:
        srv.shutdown()
        srv.server_close()


# --- the start path cannot bypass the arbiter ---------------------------------


def _start_path(base, confirm=True):
    return _request(
        f"{base}/api/admin/start-path",
        method="POST",
        body={"choice": "setup_new", "confirm": confirm},
    )


def test_start_path_cannot_bypass_active_guided_upgrade(tmp_path):
    """A direct API client must not create Setup beside a live Guided Upgrade.

    The console asks the arbiter first, but the route is what has to be safe: an
    old frontend, a script or a retry reaches it without any lifecycle call.
    """

    alignment = _UpgradeAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _start_path(base)

        assert status == 409, payload
        assert payload["error"] == "workflow_switch_required"
        assert payload["lifecycle"]["owner"] == "guided_upgrade"
        assert payload["switch_preview"] == (
            "/api/admin/workflow-lifecycle/switch/preview"
        )
        assert payload.get("setup_workflow_id") is None
        assert payload.get("setup_intent_id") is None
        # Neither authority was touched: no Setup record, upgrade still live.
        assert srv.setup_workflows.load() is None
        assert alignment.cancelled == []
        assert alignment.active is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_start_path_cannot_bypass_a_workflow_owner_conflict(tmp_path):
    alignment = _UpgradeAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        srv.setup_workflows.ensure_active()

        status, _, payload = _start_path(base)

        assert status == 409, payload
        assert payload["error"] == "workflow_switch_required"
        assert payload["lifecycle"]["blocking_reason"] == "workflow_owner_conflict"
    finally:
        srv.shutdown()
        srv.server_close()


def test_start_path_still_starts_setup_on_a_free_console(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        status, _, payload = _start_path(base)

        assert status == 200, payload
        assert payload["setup_workflow_id"]
        assert payload["setup_intent_id"]
        assert srv.setup_workflows.load()["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()


def test_start_path_keeps_the_cleanup_conflict_contract(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        workflow_id = _start_setup(base, srv)
        srv.setup_workflows.finish(
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

        status, _, payload = _start_path(base)

        assert status == 409, payload
        assert payload["error"] == "setup_cleanup_required"
        assert srv.setup_workflows.load()["workflow_id"] == workflow_id
    finally:
        srv.shutdown()
        srv.server_close()


def test_start_path_resumes_the_active_setup_workflow(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        first = _start_setup(base, srv)

        status, _, payload = _start_path(base)

        assert status == 200, payload
        assert payload["setup_workflow_id"] == first
        assert payload["setup_intent_id"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_direct_start_path_race_never_creates_a_second_owner(tmp_path):
    """Interleaving: a start-path request and a lifecycle switch overlap.

    Both read one lifecycle state; the arbiter serializes them and the loser is
    refused, so an active Setup can never end up beside an active Upgrade.
    """

    import threading

    alignment = _UpgradeAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        fingerprint = _inspect(base)["fingerprint"]
        ready = threading.Barrier(2, timeout=10)
        outcomes = {}

        def run_start_path():
            ready.wait()
            outcomes["start_path"] = _start_path(base)[0]

        def run_switch():
            ready.wait()
            outcomes["switch"] = _switch(base, "guided_setup", fingerprint=fingerprint)[
                0
            ]

        threads = [
            threading.Thread(target=run_start_path),
            threading.Thread(target=run_switch),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        record = srv.setup_workflows.load()
        view = _inspect(base)
        # Either the switch won (Setup owns the console, upgrade cancelled) or
        # the start path was refused; never both owners at once.
        assert view["owner"] in {"guided_setup", "guided_upgrade"}
        if view["owner"] == "guided_setup":
            assert alignment.cancelled == ["op-1"]
        else:
            assert record is None
    finally:
        srv.shutdown()
        srv.server_close()


# --- recovery ------------------------------------------------------------------


def test_recovery_preview_reports_no_action_on_a_healthy_console(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        status, _, payload = _recovery_preview(base)

        assert status == 200, payload
        assert payload["blocking"] is False
        assert payload["advanced"]["available"] is False
        assert str(tmp_path) not in json.dumps(payload)
    finally:
        srv.shutdown()
        srv.server_close()


def test_safe_recovery_resets_a_stranded_setup_through_the_normal_owner(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        workflow_id = _start_setup(base, srv)

        status, _, payload = _recover(base, "safe")

        assert status == 200, payload
        assert payload["ok"] is True
        stored = srv.setup_workflows.load()
        assert stored["workflow_id"] == workflow_id
        assert stored["status"] == "abandoned"
        assert payload["lifecycle"]["switchable"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def _prove_no_replacement(srv):
    """Bind a replacement probe that answers without a real Docker daemon."""

    srv.runtime.workflow_lifecycle.bind_install_state_probe(
        lambda _operation_id: ReplacementActivity.INACTIVE
    )


def test_advanced_release_backs_up_and_preserves_the_installed_system(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _prove_no_replacement(srv)
    try:
        data_dir = Path(srv.setup_workflows.admin_data_dir)
        record = srv.setup_workflows.path
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{ corrupt", encoding="utf-8")
        marker = data_dir / "state" / ".admin-deployment.json"
        marker.write_text('{"release": "v0.8.0"}\n', encoding="utf-8")

        preview, _, plan = _recovery_preview(base)
        status, _, payload = _recover(base, "release_stale_state")

        assert preview == 200, plan
        assert plan["advanced"]["files"] == [
            {
                "name": "state/guided-setup-workflow.json",
                "reason": "unreadable_state",
            }
        ]
        assert status == 200, payload
        assert payload["released"] == ["state/guided-setup-workflow.json"]
        backup_root = data_dir / "state" / "workflow-recovery"
        backups = sorted(path for path in backup_root.iterdir() if path.is_dir())
        assert len(backups) == 1
        manifest = json.loads(
            (backups[0] / "recovery-manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["reason"] == "stale metadata"
        assert manifest["files"][0]["name"] == "state/guided-setup-workflow.json"
        assert marker.read_text(encoding="utf-8") == '{"release": "v0.8.0"}\n'
        assert record.exists() is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_recovery_route_never_accepts_a_filesystem_path(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _prove_no_replacement(srv)
    try:
        record = srv.setup_workflows.path
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{ corrupt", encoding="utf-8")
        victim = Path(srv.setup_workflows.admin_data_dir) / "state" / "keep.json"
        victim.write_text('{"kept": true}\n', encoding="utf-8")

        status, _, payload = _request(
            f"{base}{LIFECYCLE}/recovery",
            method="POST",
            body={
                "mode": "release_stale_state",
                "confirm": True,
                "reason": "stale metadata",
                "fingerprint": _inspect(base)["fingerprint"],
                "files": ["../state/keep.json"],
            },
        )

        assert status == 400
        assert payload["error"] == "unsupported_field"
        assert victim.read_text(encoding="utf-8") == '{"kept": true}\n'
        assert record.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_unknown_recovery_mode_is_refused(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        status, _, payload = _request(
            f"{base}{LIFECYCLE}/recovery",
            method="POST",
            body={
                "mode": "delete_everything",
                "confirm": True,
                "fingerprint": _inspect(base)["fingerprint"],
            },
        )

        assert status == 400
        assert payload["error"] == "unsupported_recovery_mode"
    finally:
        srv.shutdown()
        srv.server_close()


def test_advanced_release_requires_a_reason(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _prove_no_replacement(srv)
    try:
        record = srv.setup_workflows.path
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{ corrupt", encoding="utf-8")

        status, _, payload = _recover(base, "release_stale_state", reason="  ")

        assert status == 400
        assert payload["error"] == "recovery_reason_required"
        assert record.exists()
    finally:
        srv.shutdown()
        srv.server_close()
