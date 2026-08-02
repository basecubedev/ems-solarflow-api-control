# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup mutations are bound to the exact previewed request, not just a revision.

Archive 98 bound Setup write/apply to the live config revision the browser
reviewed. That proves the *live file* is unchanged — it does not prove the
submitted draft is the one the user reviewed: a preview for draft A returned a
revision that authorized applying a different valid draft B. These tests pin
the exact-preview contract: the server issues an opaque ``config_preview_id``
bound to the active Setup workflow, a fingerprint of the full mutation input,
and the live baseline; write/apply must present the workflow and preview IDs
and match all of them.

See ``docs/technical/admin-workflow-state.md``.
"""

import hashlib
from pathlib import Path

import pytest

from admin.install_context import detect_install_context
from tests.helpers.setup_config import current_device_plan_id
from tests.test_admin_server import (
    _control_export_manager,
    _own_active_setup_transition,
    _request,
    _serve,
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


def _install_tree_snapshot():
    """Byte-exact snapshot of the live config and credential store."""

    root = _live_path().parent.parent
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((root / "config").rglob("*"))
        if path.is_file()
    }


def _draft_a():
    return {
        "devices": [
            {
                "role": "inverter",
                "enabled": True,
                "config_name": "WR1",
                "display_name": "Balcony inverter",
                "ip": "192.168.1.100",
                "serial_number": "SN1",
            }
        ],
        "supported_grid_meter_count": 0,
    }


def _draft_b():
    """A second valid draft that generates a different config than draft A."""

    body = _draft_a()
    body["devices"][0]["ip"] = "192.168.1.101"
    return body


def _broker_body(password):
    """A draft whose manual MQTT broker carries a credential-affecting secret."""

    return {
        **_draft_a(),
        "zendure_mqtt_broker": {
            "name": "local_mqtt",
            "host": "192.168.1.20",
            "port": 1883,
            "security": "plain",
            "username": "ems",
            "password": password,
        },
        "zendure_mqtt_manual_devices": [
            {
                "name": "SolarFlow 800 Pro 2",
                "serial_number": "DEVSN1",
                "generation": "solarflow_zensdk",
            }
        ],
    }


def _start_workflow(base, srv=None):
    """Confirm Fresh Setup and return the durable workflow id.

    Pass ``srv`` when the test later terminates or advances the harness's
    pre-seeded Setup transition: production links a transition into its workflow
    inside the pre-commit boundary, so a workflow that reached a transition always
    names its exact ``operation_id``, and a workflow that cannot is refused.
    """

    # ``confirm`` acknowledges an existing install; harmless on a fresh one.
    status, _, payload = _request(
        f"{base}/api/admin/start-path",
        method="POST",
        body={"choice": "setup_new", "confirm": True},
    )
    assert status == 200, payload
    assert payload["ok"] is True
    workflow_id = payload.get("setup_workflow_id")
    assert workflow_id, "start-path must issue a durable setup_workflow_id"
    if srv is not None:
        _own_active_setup_transition(srv, base, workflow_id)
    return workflow_id


def _device_plan_id(base):
    """The current device plan, exactly as the browser obtains one."""

    return current_device_plan_id(base, _request)


def _preview(base, workflow_id, body, device_plan_id=None):
    status, _, payload = _request(
        f"{base}/api/setup/config-preview",
        method="POST",
        body={
            **body,
            "setup_workflow_id": workflow_id,
            "device_plan_id": device_plan_id or _device_plan_id(base),
        },
    )
    assert status == 200, payload
    preview_id = payload.get("config_preview_id")
    assert preview_id, "a ready preview must issue a config_preview_id"
    assert payload.get("setup_workflow_id") == workflow_id
    return payload


def _mutate(base, path, body, workflow_id, preview_id):
    request = dict(body)
    if workflow_id is not None:
        request["setup_workflow_id"] = workflow_id
    if preview_id is not None:
        request["config_preview_id"] = preview_id
    request.setdefault("device_plan_id", _device_plan_id(base))
    return _request(f"{base}{path}", method="POST", body=request)


def _apply(base, body, workflow_id, preview_id):
    return _mutate(base, "/api/setup/config/apply", body, workflow_id, preview_id)


def _write(base, body, workflow_id, preview_id):
    return _mutate(base, "/api/setup/config/write", body, workflow_id, preview_id)


# --- a preview for A must never authorize B --------------------------------


def test_a_previews_authority_cannot_apply_draft_b(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _draft_a())["config_preview_id"]
        before = _install_tree_snapshot()

        status, _, payload = _apply(base, _draft_b(), workflow_id, preview_id)

        assert status == 409, payload
        assert payload["error"] == "setup_preview_mismatch"
        assert _install_tree_snapshot() == before, (
            "a rejected apply must leave the live config and credential store "
            "byte-exact unchanged"
        )

        # The rejection must not consume the preview: the exact reviewed draft
        # still applies.
        status, _, payload = _apply(base, _draft_a(), workflow_id, preview_id)
        assert status == 200, payload
        assert payload["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_previews_authority_cannot_write_draft_b(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _draft_a())["config_preview_id"]
        before = _install_tree_snapshot()

        status, _, payload = _write(base, _draft_b(), workflow_id, preview_id)

        assert status == 409, payload
        assert payload["error"] == "setup_preview_mismatch"
        assert _install_tree_snapshot() == before
        generated = [
            path
            for path in tmp_path.rglob("config.json")
            if "releases" not in path.parts
        ]
        assert generated == [], "a rejected write must not create artifacts"
    finally:
        srv.shutdown()
        srv.server_close()


def test_credential_affecting_change_requires_a_new_preview(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _broker_body("first-secret"))[
            "config_preview_id"
        ]
        before = _install_tree_snapshot()

        status, _, payload = _apply(
            base, _broker_body("changed-secret"), workflow_id, preview_id
        )

        assert status == 409, payload
        assert payload["error"] == "setup_preview_mismatch"
        assert _install_tree_snapshot() == before, (
            "no credential may be staged for a mutation that was not previewed"
        )

        # Re-previewing the changed input restores mutation authority.
        renewed = _preview(base, workflow_id, _broker_body("changed-secret"))
        status, _, payload = _apply(
            base,
            _broker_body("changed-secret"),
            workflow_id,
            renewed["config_preview_id"],
        )
        assert status == 200, payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_exact_previewed_draft_applies_while_the_live_config_is_unchanged(
    tmp_path,
):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _draft_a())["config_preview_id"]

        status, _, payload = _apply(base, _draft_a(), workflow_id, preview_id)

        assert status == 200, payload
        assert payload["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_maintenance_edit_rejects_even_a_matching_draft_fingerprint(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview_id = _preview(base, workflow_id, _draft_a())["config_preview_id"]
        maintenance = '{"live": "B — edited by Maintenance"}\n'
        _write_live(maintenance)

        status, _, payload = _apply(base, _draft_a(), workflow_id, preview_id)

        assert status == 409, payload
        assert payload["error"] == "stale_setup_config"
        assert _live_path().read_text(encoding="utf-8") == maintenance

        # The stale response invalidates the preview: the browser must review
        # the current configuration again before mutating.
        status, _, payload = _apply(base, _draft_a(), workflow_id, preview_id)
        assert status == 409, payload
        assert payload["error"] in {"setup_preview_required", "setup_preview_mismatch"}
        assert _live_path().read_text(encoding="utf-8") == maintenance
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_newer_preview_invalidates_the_older_preview(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        old_preview = _preview(base, workflow_id, _draft_a())["config_preview_id"]
        new_preview = _preview(base, workflow_id, _draft_b())["config_preview_id"]
        assert new_preview != old_preview

        status, _, payload = _apply(base, _draft_a(), workflow_id, old_preview)
        assert status == 409, payload
        assert payload["error"] in {"setup_preview_mismatch", "setup_preview_required"}
        assert _live_path().read_text(encoding="utf-8") == '{"live": "A"}\n'

        status, _, payload = _apply(base, _draft_b(), workflow_id, new_preview)
        assert status == 200, payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_preview_reports_the_live_revision_for_explanation_only(tmp_path):
    """``config_revision`` stays in the response, but presenting it back is not
    mutation authority: without the exact preview ID the mutation is refused."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _write_live('{"live": "A"}\n')
    try:
        workflow_id = _start_workflow(base)
        preview = _preview(base, workflow_id, _draft_a())
        revision = preview["config_revision"]
        assert revision["expected_revision"] == hashlib.sha256(
            _live_path().read_bytes()
        ).hexdigest()

        status, _, payload = _mutate(
            base,
            "/api/setup/config/apply",
            {**_draft_b(), "config_revision": revision},
            workflow_id,
            None,
        )
        assert status == 409, payload
        assert payload["error"] == "setup_preview_required"
        assert _live_path().read_text(encoding="utf-8") == '{"live": "A"}\n'
    finally:
        srv.shutdown()
        srv.server_close()
