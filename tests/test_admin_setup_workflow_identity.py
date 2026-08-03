# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every Guided Setup mutation belongs to one server-owned workflow identity.

The one-shot ``setup_intent_id`` proves a user confirmed Fresh Setup once; it is
consumed by the first mutation and cannot identify the workflow afterwards.
These tests pin the durable ``setup_workflow_id``: issued by the backend at
start-path, persisted across an Admin restart, required by every Setup config
mutation, and never transferable between an old browser tab and the current
workflow.

See ``docs/technical/admin-workflow-state.md``.
"""

from pathlib import Path

import pytest

from admin.install_context import detect_install_context
from tests.helpers.setup_config import current_device_plan_id
from tests.test_admin_server import (
    _control_export_manager,
    _request,
    _serve,
)
from tests.test_admin_setup_preview_authority import (
    _draft_a,
    _preview,
    _start_workflow,
)

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _live_path():
    return Path(detect_install_context().config_path)


def _write_live(payload):
    path = _live_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def _mutate(base, path, body):
    request = dict(body)
    request.setdefault(
        "device_plan_id",
        current_device_plan_id(base, _request, devices=body.get("devices")),
    )
    return _request(f"{base}{path}", method="POST", body=request)


# --- missing / wrong workflow identity --------------------------------------


def test_preview_without_a_workflow_id_is_refused(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        _start_workflow(base)
        status, _, payload = _mutate(
            base, "/api/setup/config-preview", _draft_a()
        )
        assert status == 409, payload
        assert payload["error"] == "setup_workflow_required"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(
    "path", ["/api/setup/config/apply", "/api/setup/config/write"]
)
def test_mutations_without_a_workflow_id_are_refused(tmp_path, path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _draft_a())["config_preview_id"]

        status, _, payload = _mutate(
            base, path, {**_draft_a(), "config_preview_id": preview_id}
        )

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_required"
        assert _live_path().read_text(encoding="utf-8") == '{"live": "A"}\n'
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(
    "path", ["/api/setup/config/apply", "/api/setup/config/write"]
)
def test_mutations_with_a_foreign_workflow_id_are_refused(tmp_path, path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _draft_a())["config_preview_id"]

        status, _, payload = _mutate(
            base,
            path,
            {
                **_draft_a(),
                "setup_workflow_id": "forged-or-expired-workflow",
                "config_preview_id": preview_id,
            },
        )

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
        assert _live_path().read_text(encoding="utf-8") == '{"live": "A"}\n'
    finally:
        srv.shutdown()
        srv.server_close()


# --- old tabs cannot mutate the current workflow -----------------------------


def test_an_abandoned_workflows_tab_cannot_mutate_the_replacement(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        old_workflow = _start_workflow(base, srv)
        old_preview = _preview(base, old_workflow, _draft_a())["config_preview_id"]

        status, _, payload = _request(
            f"{base}/api/setup/abandon",
            method="POST",
            body={"setup_workflow_id": old_workflow},
        )
        assert status == 200, payload
        assert payload["ok"] is True

        new_workflow = _start_workflow(base)
        assert new_workflow != old_workflow

        status, _, payload = _mutate(
            base,
            "/api/setup/config/apply",
            {
                **_draft_a(),
                "setup_workflow_id": old_workflow,
                "config_preview_id": old_preview,
            },
        )
        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
        assert _live_path().read_text(encoding="utf-8") == '{"live": "A"}\n'

        # The current workflow keeps full mutation authority.
        preview_id = _preview(base, new_workflow, _draft_a())["config_preview_id"]
        status, _, payload = _mutate(
            base,
            "/api/setup/config/apply",
            {
                **_draft_a(),
                "setup_workflow_id": new_workflow,
                "config_preview_id": preview_id,
            },
        )
        assert status == 200, payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_start_path_returns_the_existing_active_workflow(tmp_path):
    """Re-entering Fresh Setup continues the active workflow, it does not fork
    a second identity that would race the first one."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        first = _start_workflow(base, srv)
        second = _start_workflow(base, srv)
        assert first == second
    finally:
        srv.shutdown()
        srv.server_close()


# --- Admin restart persistence -----------------------------------------------


def test_an_admin_restart_preserves_workflow_and_preview_authority(tmp_path):
    _write_live('{"live": "A"}\n')
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        workflow_id = _start_workflow(base)
        # The plan a browser would hold when it reviewed this preview. The
        # process that issued it is about to disappear; the authority it granted
        # must not, so the same id is presented after the restart.
        device_plan_id = current_device_plan_id(
            base, _request, devices=_draft_a().get("devices")
        )
        preview_id = _preview(base, workflow_id, _draft_a(), device_plan_id)[
            "config_preview_id"
        ]
    finally:
        srv.shutdown()
        srv.server_close()

    restarted, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        status, _, payload = _request(f"{base}/api/setup/workflow")
        assert status == 200, payload
        workflow = payload.get("workflow") or {}
        assert workflow.get("workflow_id") == workflow_id
        assert workflow.get("status") == "active"
        assert (workflow.get("preview") or {}).get("preview_id") == preview_id

        status, _, payload = _request(
            f"{base}/api/setup/config/apply",
            method="POST",
            body={
                **_draft_a(),
                "setup_workflow_id": workflow_id,
                "config_preview_id": preview_id,
                "device_plan_id": device_plan_id,
            },
        )
        assert status == 200, payload
        assert payload["ok"] is True
    finally:
        restarted.shutdown()
        restarted.server_close()
