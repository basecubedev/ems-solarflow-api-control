# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin discovery HTTP server / API tests (no real network scans)."""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.config_apply import ConfigApplyService
from admin.config_export import ConfigExportService, ConfigExportValidationError
from admin.config_preview import ConfigPreviewGenerator
from admin.install_context import detect_install_context
from admin.models import DiscoveredDevice
from admin.mqtt_discovery import MqttBrokerDiscovery
from admin.operation_coordinator import OperationCoordinator
from admin.secret_store import ZendureTokenStore
from admin.system_alignment import (
    SystemAlignmentError,
    terminal_system_build_action_state,
)
from admin.system_build import SystemBuildError
from admin.zendure_cloud_mqtt import FakeCloudMqttListener, ZendureCloudDiscovery
from admin.server import (
    SECURITY_HEADERS,
    ScanRegistry,
    _result_view,
    create_admin_runtime,
    create_server,
)
from admin import zendure_mqtt_config_proposals
from tests.admin_auth_helpers import auth_headers, authenticate, raw_request
from tests.helpers.system_alignment import SetupReadySystemAlignment
from tests.helpers.setup_config import authorize_setup_mutation

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate_install_root(isolated_install_root):
    """Keep these tests off the developer's real repo-local config/data.

    ``create_server`` builds the config preview/export/apply services with the
    default install-context provider, so without isolation a developer's local
    config/config.json would leak into the server's setup endpoints. Tests that
    intentionally exercise path resolution re-point ``BASE_DIR``/``EMS_INSTALL_DIR``
    themselves on top of this baseline.
    """

    return isolated_install_root


def _fake_scan(cidr, timeout_ms=600, max_workers=32, progress_callback=None):
    if progress_callback is not None:
        progress_callback({
            "total_hosts": 2,
            "checked_hosts": 1,
            "found_devices": 0,
            "failed_hosts": 0,
            "current_ip": "192.168.178.41",
        })
    device = DiscoveredDevice(
        ip="192.168.178.42",
        api_family="zendure_local_http",
        device_type="zendure_solarflow_unknown",
        role_suggestion="inverter",
        display_name="Zendure SolarFlow device",
        serial_number="SN123456",
        confidence=0.95,
        config_ready=True,
    )
    return [device], []


def _fake_gateway_prober(timeout_ms=None, max_workers=None):
    return {
        "candidates": [
            {
                "network": "192.168.20.0/24",
                "gateway_candidate": "192.168.20.1",
                "status": "reachable",
                "signals": ["tcp_443_open"],
                "confidence": 0.8,
                "source": "gateway_candidate_probe",
                "scan_supported": True,
            }
        ],
        "source": "gateway_candidate_probe",
        "probed": 1,
    }


@pytest.fixture()
def server():
    registry = ScanRegistry(scan_runner=_fake_scan)
    srv = create_server(
        "127.0.0.1", 0, registry=registry, gateway_prober=_fake_gateway_prober
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


def _request(url, method="GET", body=None, extra_headers=None):
    data = None
    headers = dict(auth_headers(url, method))
    headers.update(extra_headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, json.loads(exc.read() or b"null")


def _own_active_setup_transition(srv, base, workflow_id):
    """Give ``workflow_id`` the transition ownership production writes pre-commit.

    Production links a Setup transition into its workflow record inside the System
    Alignment pre-commit boundary, so a workflow that reached a transition always
    names its exact ``operation_id``. A harness that *pre-seeds* an active Setup
    transition has to establish the same fact, or the workflow it starts afterwards
    is an unlinked owner — a state the ownership rules refuse on purpose.
    """

    _status, _headers, payload = _request(f"{base}/api/admin/system-alignment/status")
    transition = (payload or {}).get("transition") or {}
    if transition.get("mode") not in {"fresh_install", "automated_setup"}:
        return None
    return srv.setup_workflows.record_transition(
        workflow_id,
        operation_id=transition.get("operation_id"),
        transition_mode=transition["mode"],
        selected_system_tag=transition.get("system_tag"),
    )


def _setup_build_authority(srv, base, *, confirm=False):
    """The exact workflow id and one-shot intent a System Build route requires."""

    status, _, payload = _request(
        f"{base}/api/admin/start-path",
        method="POST",
        body={"choice": "setup_new", "confirm": confirm},
    )
    assert status == 200, payload
    assert payload["ok"] is True, payload
    workflow_id = payload["setup_workflow_id"]
    _own_active_setup_transition(srv, base, workflow_id)
    return workflow_id, payload["setup_intent_id"]


def test_root_serves_html(server):
    with urllib.request.urlopen(f"{server}/") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        body = resp.read().decode("utf-8")
        assert "EMS SolarFlow Admin" in body
        assert "Guided Docker setup for local EMS deployments." in body


def test_static_assets_have_content_types(server):
    with urllib.request.urlopen(f"{server}/admin.css") as resp:
        assert resp.headers["Content-Type"].startswith("text/css")
    with urllib.request.urlopen(f"{server}/admin.js") as resp:
        assert resp.headers["Content-Type"].startswith("application/javascript")


def test_path_traversal_blocked(server):
    status, _, _ = _request(f"{server}/..%2f..%2fadmin/server.py")
    assert status == 404


def test_admin_security_headers_are_hardened():
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"

    csp = SECURITY_HEADERS["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "img-src 'self' data:" in csp
    assert "connect-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'self'" in csp

    assert (
        SECURITY_HEADERS["Permissions-Policy"]
        == "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    )


def test_admin_auth_status_response_has_security_headers():
    # A genuinely unauthenticated public request must still carry the hardening
    # headers, so raw_request (no session attached) is used deliberately.
    srv, base = _serve()
    try:
        status, headers, _ = raw_request(f"{base}/api/admin/auth/status")
        assert status == 200
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert "object-src 'none'" in headers["Content-Security-Policy"]
        assert (
            headers["Permissions-Policy"]
            == "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_auth_status_exposes_process_scoped_instance_id():
    first, first_base = _serve()
    second, second_base = _serve()
    try:
        first_status, _, first_payload = raw_request(
            f"{first_base}/api/admin/auth/status"
        )
        repeat_status, _, repeat_payload = raw_request(
            f"{first_base}/api/admin/auth/status"
        )
        second_status, _, second_payload = raw_request(
            f"{second_base}/api/admin/auth/status"
        )
    finally:
        first.shutdown()
        first.server_close()
        second.shutdown()
        second.server_close()

    assert first_status == repeat_status == second_status == 200
    instance_id = first_payload["admin_instance_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", instance_id)
    assert repeat_payload["admin_instance_id"] == instance_id
    assert second_payload["admin_instance_id"] != instance_id
    assert set(first_payload) <= {
        "admin_instance_id",
        "auth_configured",
        "authenticated",
        "requires_initial_password",
        "recovery_required",
        "error",
        "message",
        "shared_password_file",
        "warning",
        "csrf_token",
        "session_expires_in_seconds",
    }


def test_admin_unauthenticated_error_has_security_headers():
    # Auth failures and JSON error responses must be hardened too; raw_request
    # skips the cached session so the protected endpoint rejects the request.
    srv, base = _serve()
    try:
        status, headers, _ = raw_request(f"{base}/api/admin/install-state")
        assert status in (401, 403)
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "object-src 'none'" in headers["Content-Security-Policy"]
        assert "Permissions-Policy" in headers
    finally:
        srv.shutdown()
        srv.server_close()


def test_status_endpoint_reports_config_apply_capability(server):
    status, _, payload = _request(f"{server}/api/admin/status")
    assert status == 200
    assert payload["service"] == "ems-solarflow-admin"
    assert payload["writes_config"] is True
    assert "discovery" in payload["capabilities"]
    assert "release_resources" in payload["capabilities"]
    assert "config_export" in payload["capabilities"]
    assert "config_apply" in payload["capabilities"]
    assert "deployment_start" in payload["capabilities"]
    assert payload["writes_generated_config"] is True


def test_status_endpoint_reports_current_capabilities_not_mvp(server):
    status, _, payload = _request(f"{server}/api/admin/status")
    assert status == 200
    # The status metadata no longer advertises MVP/planned placeholder state.
    assert payload["version"] != "mvp"
    assert "active_device_list" not in payload
    for capability in (
        "guided_upgrade",
        "backup_restore",
        "backup_delete",
        "restore_preview",
    ):
        assert capability in payload["capabilities"], capability


def test_status_endpoint_reports_admin_https_transport(server):
    # This fixture server is HTTP-only and did not configure an HTTPS listener.
    status, _, payload = _request(f"{server}/api/admin/status")
    assert status == 200
    assert payload["admin_https"] == {
        "configured": False,
        "current_request_https": False,
        "port": 8091,
    }


# --- shared runtime for parallel HTTP/HTTPS listeners ----------------------


def test_admin_runtime_can_be_shared_by_http_and_https_servers():
    runtime = create_admin_runtime()
    http_server = create_server("127.0.0.1", 0, runtime=runtime, https_active=False)
    https_server = create_server("127.0.0.1", 0, runtime=runtime, https_active=True)

    try:
        assert http_server.auth_sessions is https_server.auth_sessions
        assert http_server.auth_login_limiter is https_server.auth_login_limiter
        assert http_server.auth_setup_limiter is https_server.auth_setup_limiter
        assert http_server.registry is https_server.registry
        assert http_server.mqtt_discovery is https_server.mqtt_discovery
        assert http_server.mdns_provider is https_server.mdns_provider
        assert http_server.upgrade_jobs is https_server.upgrade_jobs
        assert http_server.backup_jobs is https_server.backup_jobs
        assert http_server.release_manager is https_server.release_manager
        assert http_server.deployment is https_server.deployment
        assert http_server.https_active is False
        assert https_server.https_active is True
    finally:
        http_server.server_close()
        https_server.server_close()


def test_admin_http_server_does_not_set_secure_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    runtime = create_admin_runtime()
    server = create_server("127.0.0.1", 0, runtime=runtime, https_active=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, headers, _ = raw_request(
            f"{base}/api/admin/auth/setup",
            method="POST",
            body={
                "password": "secret-password",
                "confirm_password": "secret-password",
            },
        )
        assert status == 200
        assert "Secure" not in headers["Set-Cookie"]
    finally:
        server.shutdown()
        server.server_close()


def test_admin_https_server_sets_secure_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    # The https_active flag alone controls the Secure cookie attribute; the test
    # server speaks plain HTTP so no real TLS handshake is needed.
    runtime = create_admin_runtime()
    server = create_server("127.0.0.1", 0, runtime=runtime, https_active=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        status, headers, _ = raw_request(
            f"{base}/api/admin/auth/setup",
            method="POST",
            body={
                "password": "secret-password",
                "confirm_password": "secret-password",
            },
        )
        assert status == 200
        assert "Secure" in headers["Set-Cookie"]
    finally:
        server.shutdown()
        server.server_close()


def test_setup_config_catalog_endpoint_returns_setup_sections(server):
    status, _, payload = _request(f"{server}/api/setup/config/catalog")

    assert status == 200
    assert payload["mode"] == "setup"
    ids = [section["id"] for section in payload["sections"]]
    assert ids[:3] == ["system", "grid_meter", "devices"]
    assert "ha" not in ids
    assert set(payload["grid_meter_variants"]) >= {"shelly", "mqtt"}


def test_setup_config_catalog_endpoint_never_exposes_secret_values(server):
    _, _, payload = _request(f"{server}/api/setup/config/catalog")
    secrets = [
        field
        for section in payload["sections"]
        for field in section["fields"]
        if field.get("secret")
    ]

    assert secrets  # at least the MQTT password is marked secret
    assert all("default" not in field for field in secrets)


def test_config_preview_endpoint_accepts_features_object(server):
    status, _, payload = _request(
        f"{server}/api/setup/config-preview/validate",
        method="POST",
        body={"devices": [], "features": {"winter.enabled": True}},
    )

    assert status == 200
    assert "validation" in payload


class _FakeReleaseManager:
    def __init__(self, data_dir=None, template=None):
        self.data_dir = Path(data_dir or "/unused-admin-data")
        self.template = template if template is not None else {"devices": []}

    def list_releases(self, *, for_upgrade=True):
        self.last_for_upgrade = for_upgrade
        return {
            "active_release": None,
            "prepared_release": "v0.6.0",
            "latest": "latest",
            "latest_stable": "v0.6.0",
            "default_release": "v0.6.0",
            "releases": [
                {
                    "tag": "v0.6.0",
                    "channel": "stable",
                    "docker_supported": True,
                    "admin_supported": True,
                    "prepared": True,
                    "selectable": True,
                }
            ],
            "warnings": [],
        }

    def prepare(self, tag, *, revision=None):
        return {
            "status": "ready",
            "tag": tag,
            "manifest": {"tag": tag},
            "resources": {"config_template_available": True},
            "warnings": [],
        }

    def config_template(self):
        return {
            "tag": "v0.6.0",
            "template": self.template,
            "source": "/cache/v0.6.0/config.template.json",
        }


def _control_export_manager(data_dir):
    return _FakeReleaseManager(
        data_dir,
        template={
            "devices": [
                {
                    "name": "WR1",
                    "ip": "192.168.1.100",
                    "sn": "SN1",
                    "max_power": 800,
                }
            ]
        },
    )


def _control_export_body():
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


def _authorized_body(base, body=None, **kwargs):
    """A mutation body carrying real workflow + exact-preview authority."""

    return authorize_setup_mutation(
        base, _request, body if body is not None else _control_export_body(), **kwargs
    )


def _setup_body(srv, **extra):
    """A Setup deployment body carrying the server's exact workflow id.

    Prepare, start, permission repair and container-conflict resolution all
    require the workflow they belong to by name, so tests present it the same way
    the browser does.
    """

    return {
        "setup_workflow_id": srv.setup_workflows.ensure_active()["workflow_id"],
        **extra,
    }


def _abandon(base, srv, workflow_id="current"):
    """Discard Setup by name: the stored workflow id is always required.

    Also establishes the transition ownership production writes pre-commit, so a
    harness that pre-seeded an active Setup transition is discarded by a workflow
    that can prove it owns it (an unlinked owner is refused on purpose).
    """

    if workflow_id == "current":
        workflow_id = (srv.setup_workflows.load() or {}).get("workflow_id")
    if workflow_id:
        _own_active_setup_transition(srv, base, workflow_id)
    return _request(
        f"{base}/api/setup/abandon",
        method="POST",
        body={"setup_workflow_id": workflow_id},
    )


def test_release_endpoint_defaults_to_setup_flow_and_honours_upgrade_flag(tmp_path):
    # Guided Setup (no query) lists releases without the upgrade gate; the
    # maintenance flow passes ?flow=upgrade to enable it.
    manager = _FakeReleaseManager(tmp_path)
    srv, base = _serve(release_manager=manager)
    try:
        status, _, _ = _request(f"{base}/api/setup/releases")
        assert status == 200
        assert manager.last_for_upgrade is False

        status, _, _ = _request(f"{base}/api/setup/releases?flow=upgrade")
        assert status == 200
        assert manager.last_for_upgrade is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_release_setup_endpoints_use_alignment_then_release_resources(tmp_path):
    alignment = _FakeSystemAlignment(stage="resources_verified")
    srv, base = _serve(release_manager=_FakeReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, releases = _request(f"{base}/api/setup/releases")
        assert status == 200
        assert releases["prepared_release"] == "v0.6.0"
        assert releases["default_release"] == "v0.6.0"
        assert releases["releases"][0]["docker_supported"] is True

        status, _, prepared = _request(
            f"{base}/api/setup/releases/prepare",
            method="POST",
            body={"tag": "v0.6.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
        assert status == 200
        assert prepared["status"] == "ready_for_ems"
        assert alignment.prepare_calls == [
            {"requested_tag": "v0.6.0", "mode": "fresh_install"}
        ]
        assert alignment.start_calls == []

        status, _, template = _request(f"{base}/api/setup/config-template")
        assert status == 200
        assert template["template"] == {"devices": []}

        status, _, preview = _request(
            f"{base}/api/setup/config-preview/validate",
            method="POST",
            body={"devices": [], "supported_grid_meter_count": 0},
        )
        assert status == 200
        assert preview["release"] == "v0.6.0"
        assert preview["template_loaded"] is True
        assert preview["config"] == {"devices": []}
    finally:
        srv.shutdown()
        srv.server_close()


_ORDERED_TEMPLATE = {
    "_comment": "release template",
    "_comment_docs": "https://example.invalid/docs",
    "config_schema_version": 3,
    "config_upgrade": {"enabled": True},
    "system": {
        "output_control": {
            "max_output_w": 800,
            "min_output_w": 0,
            "deadband_w": 15,
        }
    },
    "influxdb": {
        "retention": {
            "raw": "30d",
            "downsampled": "365d",
        }
    },
    "_comment_devices": "device prototypes",
    "battery_full_charge_assist": {"enabled": False},
    "devices": [],
}


def test_config_template_response_preserves_template_key_order(tmp_path):
    srv, base = _serve(
        release_manager=_FakeReleaseManager(tmp_path, template=_ORDERED_TEMPLATE)
    )
    try:
        status, _, body = _request(f"{base}/api/setup/config-template")
        assert status == 200
        assert list(body["template"].keys()) == list(_ORDERED_TEMPLATE.keys())
        assert list(body["template"]["system"]["output_control"].keys()) == [
            "max_output_w",
            "min_output_w",
            "deadband_w",
        ]
        assert list(body["template"]["influxdb"]["retention"].keys()) == [
            "raw",
            "downsampled",
        ]
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_preview_response_preserves_template_key_order(tmp_path):
    srv, base = _serve(
        release_manager=_FakeReleaseManager(tmp_path, template=_ORDERED_TEMPLATE)
    )
    try:
        status, _, body = _request(
            f"{base}/api/setup/config-preview/validate",
            method="POST",
            body={"devices": [], "supported_grid_meter_count": 0},
        )
        assert status == 200
        config = body["config"]
        assert config is not None
        assert list(config.keys()) == list(_ORDERED_TEMPLATE.keys())
        assert list(config["system"]["output_control"].keys()) == [
            "max_output_w",
            "min_output_w",
            "deadband_w",
        ]
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_preview_get_masks_cloud_route_and_issues_identity_token(
    tmp_path, monkeypatch
):
    route = "ACCOUNT_ROUTE_7501"
    product = "PRODUCT_KEY_7501"
    topic = f"iot/{product}/{route}/properties/write"
    template = {
        "zendure_mqtt": {
            "brokers": {
                "cloud_a": {
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtt.example.invalid",
                    "port": 8883,
                    "tls": True,
                }
            }
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": f"Cloud shed {route}",
                "mqtt": {
                    "broker_ref": "cloud_a",
                    "topic_family": "legacy_zendure_json_alt",
                    "device_id": route,
                    "product_key": product,
                    "write_topic": topic,
                },
                "capabilities": {"write_output_limit": False},
            }
        ],
    }
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(template), encoding="utf-8"
    )
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    srv, base = _serve(
        release_manager=_FakeReleaseManager(tmp_path, template=template)
    )
    try:
        status, _, payload = _request(f"{base}/api/setup/config-preview")
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    flattened = json.dumps(payload)
    assert route not in flattened
    assert product not in flattened
    assert topic not in flattened
    device = payload["config"]["devices"][0]
    assert device["physical_identity_token"].startswith("opaque:v1:")


def test_release_prepare_returns_clean_embedded_resource_error(tmp_path):
    class _UnwritableAlignment(_FakeSystemAlignment):
        def prepare_setup_resources(
            self, *, requested_tag, mode, development_risk_acknowledged=False,
            pre_launch=None,
        ):
            raise SystemAlignmentError(
                "system_build_resources_invalid",
                "Admin data directory is not writable: /data. "
                "Check the Docker volume mount for ./data/admin:/data.",
            )

    srv, base = _serve(release_manager=_FakeReleaseManager(tmp_path))
    _attach_system_alignment(srv, _UnwritableAlignment())
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, payload = _request(
            f"{base}/api/setup/releases/prepare",
            method="POST",
            body={"tag": "v0.6.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
        assert status == 409
        assert payload == {
            "ok": False,
            "error": "system_build_resources_invalid",
            "message": (
                "Admin data directory is not writable: /data. "
                "Check the Docker volume mount for ./data/admin:/data."
            ),
            "action_state": terminal_system_build_action_state(
                "v0.6.0",
                "system_build_resources_invalid",
                "Admin data directory is not writable: /data. "
                "Check the Docker volume mount for ./data/admin:/data.",
            ),
        }
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_download_returns_attachment_from_generated_preview(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        status, headers, payload = _request(
            f"{base}/api/setup/config/download",
            method="POST",
            body=_control_export_body(),
        )
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert headers["Content-Disposition"] == 'attachment; filename="config.json"'
        assert payload["devices"][0]["name"] == "WR1"
        assert not (tmp_path / "generated" / "config.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_download_is_blocked_when_validation_fails(tmp_path):
    manager = _FakeReleaseManager(
        tmp_path,
        template={"devices": [], "grid_meter": {"type": "shelly", "ip": "x"}},
    )
    srv, base = _serve(release_manager=manager)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/config/download",
            method="POST",
            body={"devices": [], "supported_grid_meter_count": 0},
        )
        assert status == 422
        assert payload["reason"] == "validation_failed"
        assert payload["validation"]["errors"][0]["code"] == "grid_meter_missing"
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_validation_failure_masks_installed_cloud_route_name(
    tmp_path, monkeypatch
):
    route = "ACCOUNT_ROUTE_7501"
    product = "PRODUCT_KEY_7501"
    raw_name = f"Cloud shed {route}"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "zendure_mqtt": {
                    "brokers": {
                        "cloud_a": {
                            "source": "zendure_cloud_mqtt",
                            "host": "mqtt.example.invalid",
                        }
                    }
                },
                "devices": [
                    {
                        "type": "zendure_mqtt",
                        "name": raw_name,
                        "mqtt": {
                            "broker_ref": "cloud_a",
                            "device_id": route,
                            "product_key": product,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    class RejectingExport:
        def serialize(self, *_args, **_kwargs):
            raise ConfigExportValidationError(
                {
                    "validation": {
                        "errors": [
                            {
                                "code": "device_name_duplicate",
                                "message": f"Config names must be unique: {raw_name}.",
                            }
                        ],
                        "warnings": [],
                        "info": [],
                    }
                }
            )

    srv, base = _serve(config_export=RejectingExport())
    try:
        status, _, payload = _request(
            f"{base}/api/setup/config/download",
            method="POST",
            body={"devices": [], "supported_grid_meter_count": 0},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 422
    assert payload["reason"] == "validation_failed"
    flattened = json.dumps(payload)
    assert route not in flattened
    assert product not in flattened
    assert raw_name not in flattened


def test_config_write_endpoint_protects_overwrite_and_rejects_paths(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        body = _authorized_body(base)
        status, _, first = _request(
            f"{base}/api/setup/config/write", method="POST", body=body
        )
        assert status == 200
        assert first["ok"] is True
        target = Path(first["path"])
        assert target == tmp_path / "workflows" / "guided-setup" / body[
            "setup_workflow_id"
        ] / "generated" / "config.json"
        assert target.exists()

        status, _, existing = _request(
            f"{base}/api/setup/config/write", method="POST", body=body
        )
        assert status == 409
        assert existing["reason"] == "target_exists"

        status, _, overwritten = _request(
            f"{base}/api/setup/config/write",
            method="POST",
            body={**body, "overwrite": True},
        )
        assert status == 200
        assert overwritten["ok"] is True

        status, _, rejected = _request(
            f"{base}/api/setup/config/write",
            method="POST",
            body={**body, "path": str(tmp_path / "outside.json")},
        )
        assert status == 400
        assert "custom output paths" in rejected["error"]
        assert not (tmp_path / "outside.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_status_reports_generated_config_presence(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        status, _, before = _request(f"{base}/api/setup/config/status")
        assert status == 200
        legacy = tmp_path / "generated" / "config.json"
        assert before == {"path": str(legacy), "exists": False}

        body = _authorized_body(base)
        write_status, _, written = _request(
            f"{base}/api/setup/config/write", method="POST", body=body
        )
        assert write_status == 200

        status, _, after = _request(f"{base}/api/setup/config/status")
        assert status == 200
        assert after == {"path": written["path"], "exists": True}
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_preview_endpoint_ignores_local_repo_config(tmp_path, monkeypatch):
    # Regression: even when the resolved repo root holds a gitignored local
    # config/config.json, the setup preview endpoint must stay isolated to the
    # explicit EMS_INSTALL_DIR and use the release template, never reading or
    # touching the developer's local runtime config.
    from ems import paths

    repo_root = tmp_path / "repo"
    (repo_root / "config").mkdir(parents=True)
    local_config = repo_root / "config" / "config.json"
    local_config.write_bytes(b'{"operator_only": "do-not-touch"}')
    original = local_config.read_bytes()
    monkeypatch.setattr(paths, "BASE_DIR", str(repo_root))

    install_root = tmp_path / "install"
    install_root.mkdir()
    monkeypatch.setenv("EMS_INSTALL_DIR", str(install_root))

    srv, base = _serve(
        release_manager=_FakeReleaseManager(tmp_path / "admin", template={"devices": []})
    )
    try:
        status, _, preview = _request(
            f"{base}/api/setup/config-preview/validate",
            method="POST",
            body={"devices": [], "supported_grid_meter_count": 0},
        )
        assert status == 200
        assert preview["base"] == {"source": "release_template"}
        assert preview["config"] == {"devices": []}
    finally:
        srv.shutdown()
        srv.server_close()

    assert local_config.read_bytes() == original


def _apply_service(manager, admin_data_dir, install_root):
    def provider():
        return detect_install_context(base_dir=str(install_root))

    preview = ConfigPreviewGenerator(manager, install_context_provider=provider)
    export = ConfigExportService(preview, admin_data_dir)
    return ConfigApplyService(export, admin_data_dir, install_context_provider=provider)


def test_config_apply_writes_resolved_install_config(tmp_path):
    manager = _control_export_manager(tmp_path)
    install_root = tmp_path / "ems"
    apply = _apply_service(manager, tmp_path / "admin", install_root)
    srv, base = _serve(release_manager=manager, config_apply=apply)
    try:
        body = _authorized_body(base)
        status, _, payload = _request(
            f"{base}/api/setup/config/apply", method="POST", body=body
        )
        target = install_root / "config" / "config.json"
        assert status == 200
        assert payload["ok"] is True
        assert payload["created"] is True
        assert payload["backup_path"] is None
        assert payload["path"] == str(target)
        assert target.is_file()
        assert not (tmp_path / "app" / "config" / "config.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_apply_backs_up_existing_config(tmp_path):
    manager = _control_export_manager(tmp_path)
    install_root = tmp_path / "ems"
    target = install_root / "config" / "config.json"
    target.parent.mkdir(parents=True)
    old_bytes = b'{"devices": [], "system": {"max_total_power": 1}}\n'
    target.write_bytes(old_bytes)
    apply = _apply_service(manager, tmp_path / "admin", install_root)
    srv, base = _serve(release_manager=manager, config_apply=apply)
    try:
        body = _authorized_body(base)
        status, _, payload = _request(
            f"{base}/api/setup/config/apply", method="POST", body=body
        )
        assert status == 200
        assert payload["created"] is False
        assert payload["backup_path"] is not None
        assert Path(payload["backup_path"]).read_bytes() == old_bytes
        assert target.read_bytes() != old_bytes
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_apply_is_blocked_when_validation_fails(tmp_path):
    manager = _FakeReleaseManager(
        tmp_path,
        template={"devices": [], "grid_meter": {"type": "shelly", "ip": "x"}},
    )
    install_root = tmp_path / "ems"
    target = install_root / "config" / "config.json"
    target.parent.mkdir(parents=True)
    original = b'{"keep": true}\n'
    target.write_bytes(original)
    apply = _apply_service(manager, tmp_path / "admin", install_root)
    srv, base = _serve(release_manager=manager, config_apply=apply)
    try:
        from tests.helpers.setup_config import start_setup_workflow

        workflow_id = start_setup_workflow(base, _request)
        invalid = {"devices": [], "supported_grid_meter_count": 0}
        status, _, preview = _request(
            f"{base}/api/setup/config-preview",
            method="POST",
            body={**invalid, "setup_workflow_id": workflow_id},
        )
        assert status == 200
        assert "config_preview_id" not in preview
        assert preview["validation"]["errors"][0]["code"] == "grid_meter_missing"

        status, _, payload = _request(
            f"{base}/api/setup/config/apply",
            method="POST",
            body={**invalid, "setup_workflow_id": workflow_id},
        )
        assert status == 409
        assert payload["error"] == "setup_preview_required"
        assert target.read_bytes() == original
    finally:
        srv.shutdown()
        srv.server_close()


def _seed_mqtt_device_discovery(serial="ABC123", host="10.0.0.9"):
    """A discovery instance holding one trusted scalar device proposal.

    The browser never authors proposal content: a submitted selection carries
    only the proposal id/broker_ref, resolved back to this stored proposal, so
    tests must seed real discovery state instead of posting a synthetic proposal.
    """

    from admin.mqtt_topic_discovery import MqttTopicAggregator

    agg = MqttTopicAggregator({"id": f"mqtt:{host}:1883", "host": host, "port": 1883})
    for metric in ("outputPackPower", "electricLevel", "packInputPower"):
        agg.observe(f"Zendure/sensor/{serial}/{metric}", None)
    mqtt = MqttBrokerDiscovery(connector=lambda *args, **kwargs: True)
    generation = mqtt.store.begin_refresh()
    mqtt.store.complete_refresh(
        generation,
        [{"id": f"mqtt:{host}:1883", "host": host, "port": 1883, "devices": agg.results()}],
        success=True,
    )
    return mqtt


def _mqtt_selection(mqtt, serial="ABC123", **overrides):
    """The trusted proposal's id/broker_ref selection a browser would submit."""

    proposal = next(
        p
        for p in zendure_mqtt_config_proposals.proposals_from_brokers(mqtt.candidates())
        if p["serial_number"] == serial
    )
    selection = {
        "id": proposal["id"],
        "broker_ref": proposal["broker_ref"],
        "target": "device",
    }
    selection.update(overrides)
    return proposal, selection


def test_config_preview_accepts_zendure_mqtt_proposals(tmp_path):
    mqtt = _seed_mqtt_device_discovery()
    _proposal, selection = _mqtt_selection(mqtt)
    srv, base = _serve(release_manager=_FakeReleaseManager(tmp_path), mqtt_discovery=mqtt)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/config-preview/validate",
            method="POST",
            body={
                "devices": [],
                "supported_grid_meter_count": 0,
                "zendure_mqtt_proposals": [selection],
            },
        )
        assert status == 200
        entries = [
            d for d in payload["config"]["devices"] if d.get("type") == "zendure_mqtt"
        ]
        assert len(entries) == 1
        assert "preview_only" not in entries[0]
        assert entries[0]["capabilities"]["write_output_limit"] is False
        codes = [issue["code"] for issue in payload["validation"]["warnings"]]
        assert "zendure_mqtt_telemetry_only" in codes
        assert payload["summary"]["zendure_mqtt_devices"] == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_preview_rejects_malformed_zendure_mqtt_proposals(tmp_path):
    srv, base = _serve(release_manager=_FakeReleaseManager(tmp_path))
    try:
        for bad in ("not-a-list", [["not-an-object"]], [{"ok": True}] * 21):
            status, _, payload = _request(
                f"{base}/api/setup/config-preview",
                method="POST",
                body={"devices": [], "zendure_mqtt_proposals": bad},
            )
            assert status == 400
            assert "zendure_mqtt_proposals" in payload["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_preview_accepts_broker_and_manual_zendure_mqtt_device(tmp_path):
    srv, base = _serve(release_manager=_FakeReleaseManager(tmp_path))
    try:
        status, _, payload = _request(
            f"{base}/api/setup/config-preview/validate",
            method="POST",
            body={
                "devices": [],
                "supported_grid_meter_count": 0,
                "zendure_mqtt_broker": {
                    "name": "local_mqtt",
                    "host": "192.168.1.20",
                    "port": 1883,
                    "security": "plain",
                },
                "zendure_mqtt_manual_devices": [
                    {
                        "name": "SolarFlow 800 Pro 2",
                        "serial_number": "DEVSN1",
                        "generation": "solarflow_zensdk",
                    }
                ],
            },
        )
        assert status == 200
        entries = [
            d for d in payload["config"]["devices"] if d.get("type") == "zendure_mqtt"
        ]
        assert len(entries) == 1
        assert entries[0]["mqtt"]["topic_family"] == "zensdk_ha_scalar"
        assert entries[0]["capabilities"]["write_output_limit"] is False
        assert "local_mqtt" in payload["config"]["zendure_mqtt"]["brokers"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_preview_rejects_malformed_manual_zendure_mqtt(tmp_path):
    srv, base = _serve(release_manager=_FakeReleaseManager(tmp_path))
    try:
        for field, bad in (
            ("zendure_mqtt_broker", "not-an-object"),
            ("zendure_mqtt_manual_devices", "not-a-list"),
            ("zendure_mqtt_manual_devices", ["not-an-object"]),
            ("zendure_mqtt_manual_devices", [{"ok": True}] * 21),
        ):
            status, _, payload = _request(
                f"{base}/api/setup/config-preview",
                method="POST",
                body={"devices": [], field: bad},
            )
            assert status == 400, (field, bad)
            assert field in payload["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_export_endpoints_include_telemetry_mqtt_alongside_control_device(tmp_path):
    manager = _control_export_manager(tmp_path)
    install_root = tmp_path / "ems"
    apply = _apply_service(manager, tmp_path / "admin", install_root)
    mqtt = _seed_mqtt_device_discovery()
    _proposal, selection = _mqtt_selection(mqtt)
    srv, base = _serve(release_manager=manager, config_apply=apply, mqtt_discovery=mqtt)
    # A browser cannot smuggle a secret through proposal content: the backend
    # resolves the id back to trusted stored state and ignores submitted content.
    selection["config_fragment"] = {"password": "hunter2"}
    try:
        body = _authorized_body(
            base,
            {**_control_export_body(), "zendure_mqtt_proposals": [selection]},
        )
        status, _, downloaded = _request(
            f"{base}/api/setup/config/download", method="POST", body=body
        )
        assert status == 200
        entry = [
            d for d in downloaded["devices"] if d.get("type") == "zendure_mqtt"
        ][0]
        assert entry["mqtt"]["device_id"] == "ABC123"
        assert entry["capabilities"]["write_output_limit"] is False

        status, _, written = _request(
            f"{base}/api/setup/config/write", method="POST", body=body
        )
        assert status == 200 and written["ok"] is True

        status, _, applied = _request(
            f"{base}/api/setup/config/apply", method="POST", body=body
        )
        assert status == 200 and applied["ok"] is True

        install_config = install_root / "config" / "config.json"
        assert install_config.exists()
        for path in (Path(written["path"]), install_config):
            blob = path.read_text(encoding="utf-8").lower()
            assert "zendure_mqtt" in blob
            for secret in ("password", "app_key", "hunter2", "token", "username"):
                assert secret not in blob
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_export_endpoints_still_reject_malformed_zendure_mqtt(tmp_path):
    srv, base = _serve(release_manager=_FakeReleaseManager(tmp_path))
    try:
        for bad in ("not-a-list", [["not-an-object"]], [{"ok": True}] * 21):
            for endpoint in ("config/download", "config/write", "config/apply"):
                status, _, payload = _request(
                    f"{base}/api/setup/{endpoint}",
                    method="POST",
                    body={"devices": [], "zendure_mqtt_proposals": bad},
                )
                assert status == 400, (endpoint, bad)
                assert "zendure_mqtt_proposals" in payload["error"]
    finally:
        srv.shutdown()
        srv.server_close()


class _FakeDeployment:
    def __init__(self):
        self.prepared_overwrite = None
        self.start_calls = 0
        self.repair_calls = 0

    def plan(self):
        return {
            "release": "v0.6.0",
            "bootstrap_source": "/data/admin/releases/v0.6.0",
            "generated_config": {"ready": True, "path": "/data/generated/config.json"},
            "workspace": "/data/deployment",
            "influxdb": {"enabled": False, "bundled": False, "planned": False,
                         "image": None, "reason": "InfluxDB: not enabled in generated config"},
            "images": [
                {"service": "ems",
                 "image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.0"}
            ],
            "docker": {"state": "ready", "code": None, "mode": "deployment_controller",
                       "message": "Docker is available.", "server_version": "27.5.1"},
            "prepared": None,
            "can_prepare": True,
        }

    def prepare(self, overwrite=False, *, workflow_id=None, on_settled=None):
        self.prepared_overwrite = overwrite
        self.prepare_workflow_id = workflow_id
        if on_settled is not None:
            on_settled()
        if overwrite is False and getattr(self, "existing_install", False):
            return {
                "ok": False,
                "reason": "existing_install_conflict",
                "message": "Existing EMS installation detected.",
                "status": 409,
                "paths": {
                    "config": "/data/config/config.json",
                    "compose": "/data/docker-compose.yml",
                    "data": "/data/data",
                },
                "existing": {"config": True, "compose": True},
                "requires_confirmation": True,
            }
        if overwrite is False and getattr(self, "conflict", False):
            return {
                "ok": False,
                "reason": "workspace_conflict",
                "message": "Confirm overwrite to replace it.",
                "status": 409,
            }
        return {
            "ok": True,
            "status": 202,
            "job": {
                "job_id": "job-1",
                "status": "running",
                "steps": [],
                "images": [],
                "workspace": "/data/deployment",
            },
        }

    def job(self, job_id):
        if job_id != "job-1":
            return None
        return {"job_id": "job-1", "status": "succeeded", "prepared": True,
                "steps": [], "images": []}

    def start(self, *, on_complete=None, workflow_id=None, on_settled=None):
        self.start_calls += 1
        self.start_workflow_id = workflow_id
        if on_settled is not None:
            on_settled()
        if getattr(self, "start_rejection", None):
            return {
                "ok": False,
                "reason": self.start_rejection,
                "message": "Prepare deployment first before starting EMS.",
                "status": 409,
            }
        result = {
            "ok": True,
            "status": 202,
            "job": {
                "job_id": "start-1",
                "status": "running",
                "phase": "Starting EMS containers",
                "steps": [],
                "services": [],
                "workspace": "/data/deployment",
            },
        }
        if on_complete is not None:
            on_complete(self.start_job("start-1"))
        return result

    def start_job(self, job_id):
        if job_id != "start-1":
            return None
        if getattr(self, "start_failed", False):
            return {
                "job_id": "start-1",
                "status": "failed",
                "error": {"code": "compose_failed", "message": "compose up failed"},
                "steps": [],
                "services": [],
                "dashboard_reachable": False,
            }
        return {
            "job_id": "start-1",
            "status": "succeeded",
            "phase": "EMS is running",
            "steps": [],
            "services": [{"service": "ems", "state": "running"}],
            "dashboard_url": "http://localhost:8080",
            "dashboard_reachable": not getattr(self, "dashboard_unreachable", False),
        }

    def resolve_container_conflict(self, container_name, action, *, workflow_id=None):
        self.resolved_conflict = (container_name, action)
        if container_name != "ems-solarflow-api-control":
            return {
                "ok": False,
                "reason": "unknown_container_name",
                "message": "Unknown container.",
                "status": 400,
            }
        return {"ok": True, "removed": container_name, "continue": True}

    def repair_workspace_permissions(self, *, workflow_id=None):
        self.repair_calls += 1
        return {"ok": True, "repaired": True}

    def status(self):
        return {
            "prepared": True,
            "running": True,
            "services": [
                {
                    "name": "ems",
                    "service": "ems",
                    "image": "ems:test",
                    "state": "running",
                    "status": "Up",
                    "ports": ["8080:8080/tcp"],
                }
            ],
            "docker": {"state": "ready", "code": None},
            "dashboard_url": "http://localhost:8080",
            "dashboard_reachable": True,
            "errors": [],
        }


def test_deployment_plan_endpoint_returns_images():
    srv, base = _serve(deployment=_FakeDeployment())
    try:
        status, _, plan = _request(f"{base}/api/setup/deployment/plan")
        assert status == 200
        assert plan["release"] == "v0.6.0"
        assert plan["images"][0]["service"] == "ems"
        assert plan["can_prepare"] is True
        # Host Docker access state is surfaced to Step 04 via the plan.
        assert plan["docker"]["state"] == "ready"
        assert plan["docker"]["mode"] == "deployment_controller"
        # The resolved real EMS install context is exposed alongside the plan.
        assert "config_path" in plan["install_context"]
        assert "config_source" in plan["install_context"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_prepare_endpoint_starts_job_and_passes_overwrite():
    deployment = _FakeDeployment()
    srv, base = _serve(deployment=deployment)
    try:
        status, _, job = _request(
            f"{base}/api/setup/deployment/prepare",
            method="POST",
            body=_setup_body(srv, overwrite=True),
        )
        assert status == 202
        assert job["job_id"] == "job-1"
        assert deployment.prepared_overwrite is True

        status, _, done = _request(f"{base}/api/setup/deployment/jobs/job-1")
        assert status == 200
        assert done["status"] == "succeeded"

        status, _, missing = _request(f"{base}/api/setup/deployment/jobs/nope")
        assert status == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_prepare_endpoint_surfaces_conflict():
    deployment = _FakeDeployment()
    deployment.conflict = True
    srv, base = _serve(deployment=deployment)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/deployment/prepare", method="POST", body=_setup_body(srv)
        )
        assert status == 409
        assert payload["reason"] == "workspace_conflict"
        assert payload["ok"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_prepare_endpoint_rejects_bad_overwrite():
    srv, base = _serve(deployment=_FakeDeployment())
    try:
        status, _, payload = _request(
            f"{base}/api/setup/deployment/prepare",
            method="POST",
            body={"overwrite": "yes"},
        )
        assert status == 400
        assert "error" in payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_prepare_endpoint_surfaces_existing_install_conflict():
    deployment = _FakeDeployment()
    deployment.existing_install = True
    srv, base = _serve(deployment=deployment)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/deployment/prepare", method="POST", body=_setup_body(srv)
        )
        assert status == 409
        assert payload["ok"] is False
        assert payload["reason"] == "existing_install_conflict"
        assert payload["requires_confirmation"] is True
        assert payload["existing"] == {"config": True, "compose": True}
        assert payload["paths"]["config"] == "/data/config/config.json"
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_prepare_attaches_current_durable_transition():
    deployment = _FakeDeployment()
    alignment = _FakeSystemAlignment()
    srv, base = _serve(deployment=deployment)
    _attach_system_alignment(srv, alignment)
    try:
        status, headers, payload = _request(
            f"{base}/api/setup/deployment/prepare", method="POST", body=_setup_body(srv)
        )
        assert status == 202
        assert headers["Content-Type"].startswith("application/json")
        assert payload["job_id"] == "job-1"
        assert payload["status"] == "running"
        assert payload["workspace"] == "/data/deployment"
        assert payload["transition"] == alignment.status()["transition"]
        assert payload["transition"]["operation_id"] == "op-1"
        assert payload["transition"]["stage"] == "resources_verified"
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_prepare_succeeds_when_transition_clears_during_prepare():
    alignment = _FakeSystemAlignment()

    class _TransitionClearingDeployment(_FakeDeployment):
        def prepare(self, overwrite=False, **kwargs):
            # A concurrent session cancels the System Build transition while
            # the workspace prepare is still running.
            alignment.active = False
            return super().prepare(overwrite, **kwargs)

    deployment = _TransitionClearingDeployment()
    srv, base = _serve(deployment=deployment)
    _attach_system_alignment(srv, alignment)
    try:
        status, headers, payload = _request(
            f"{base}/api/setup/deployment/prepare", method="POST", body=_setup_body(srv)
        )
        assert status == 202
        assert headers["Content-Type"].startswith("application/json")
        assert payload["job_id"] == "job-1"
        assert payload["status"] == "running"
        assert "transition" not in payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_prepare_transition_does_not_replace_job_payload():
    class _RecordingDeployment(_FakeDeployment):
        def prepare(self, overwrite=False, **kwargs):
            self.last_result = super().prepare(overwrite, **kwargs)
            return self.last_result

    deployment = _RecordingDeployment()
    alignment = _FakeSystemAlignment()
    srv, base = _serve(deployment=deployment)
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/deployment/prepare", method="POST", body=_setup_body(srv)
        )
        assert status == 202
        job = deployment.last_result["job"]
        assert "transition" not in job
        for key, value in job.items():
            assert payload[key] == value
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_start_and_status_endpoints():
    deployment = _FakeDeployment()
    srv, base = _serve(deployment=deployment)
    try:
        status, _, started = _request(
            f"{base}/api/setup/deployment/start", method="POST", body=_setup_body(srv)
        )
        assert status == 202
        assert started["job_id"] == "start-1"
        assert deployment.start_calls == 1

        status, _, done = _request(
            f"{base}/api/setup/deployment/start/jobs/start-1"
        )
        assert status == 200
        assert done["status"] == "succeeded"
        assert done["dashboard_reachable"] is True

        status, _, missing = _request(
            f"{base}/api/setup/deployment/start/jobs/missing"
        )
        assert status == 404

        status, _, current = _request(f"{base}/api/setup/deployment/status")
        assert status == 200
        assert current["prepared"] is True
        assert current["running"] is True
        assert current["services"][0]["service"] == "ems"
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_start_rejects_unprepared_and_parameters():
    deployment = _FakeDeployment()
    deployment.start_rejection = "deployment_not_prepared"
    srv, base = _serve(deployment=deployment)
    try:
        status, _, rejected = _request(
            f"{base}/api/setup/deployment/start", method="POST", body=_setup_body(srv)
        )
        assert status == 409
        assert rejected["reason"] == "deployment_not_prepared"

        status, _, unsafe = _request(
            f"{base}/api/setup/deployment/start",
            method="POST",
            body={"command": "docker compose down"},
        )
        assert status == 400
        assert "does not accept parameters" in unsafe["error"]
        assert deployment.start_calls == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_resolve_container_conflict_endpoint():
    deployment = _FakeDeployment()
    srv, base = _serve(deployment=deployment)
    try:
        status, _, resolved = _request(
            f"{base}/api/setup/deployment/resolve-container-conflict",
            method="POST",
            body=_setup_body(
                srv,
                container_name="ems-solarflow-api-control",
                action="remove_stopped_and_continue",
            ),
        )
        assert status == 200
        assert resolved["removed"] == "ems-solarflow-api-control"
        assert deployment.resolved_conflict == (
            "ems-solarflow-api-control",
            "remove_stopped_and_continue",
        )

        status, _, rejected = _request(
            f"{base}/api/setup/deployment/resolve-container-conflict",
            method="POST",
            body=_setup_body(
                srv,
                container_name="arbitrary",
                action="remove_stopped_and_continue",
            ),
        )
        assert status == 400
        assert rejected["reason"] == "unknown_container_name"
    finally:
        srv.shutdown()
        srv.server_close()


def test_deployment_conflict_action_recovers_only_matching_failed_transition():
    class RecoverableContainerConflict(_FakeSystemAlignment):
        def status(self, *, operation_active=None):
            result = super().status(operation_active=operation_active)
            result["transition"].update(
                {
                    "system_tag": _DEV_BUILD_TAG,
                    "error_code": "compose_container_name_conflict",
                    "development_risk_acknowledged": True,
                    "development_risk_acknowledged_for_tag": _DEV_BUILD_TAG,
                }
            )
            return result

        def retry(self, *, operation_id, development_risk_acknowledged=False):
            self.conflict_recovery_ack = development_risk_acknowledged
            return super().retry(
                operation_id=operation_id,
                development_risk_acknowledged=development_risk_acknowledged,
            )

    class ConflictingDeployment(_FakeDeployment):
        def status(self):
            result = super().status()
            result.update(
                {
                    "running": False,
                    "conflict": {
                        "container_name": "ems-solarflow-api-control",
                        "safe_fix_available": True,
                        "replace_available": False,
                    },
                }
            )
            return result

    deployment = ConflictingDeployment()
    alignment = RecoverableContainerConflict(stage="failed_recoverable")
    srv, base = _serve(deployment=deployment)
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/deployment/resolve-container-conflict",
            method="POST",
            body=_setup_body(
                srv,
                container_name="ems-solarflow-api-control",
                action="remove_stopped_and_continue",
            ),
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["removed"] == "ems-solarflow-api-control"
    assert deployment.resolved_conflict == (
        "ems-solarflow-api-control",
        "remove_stopped_and_continue",
    )
    assert alignment.stage == "ems_operation_pending"
    assert alignment.conflict_recovery_ack is True
    assert payload["transition"]["stage"] == "ems_operation_pending"


def test_deployment_conflict_action_does_not_recover_unrelated_failure():
    deployment = _FakeDeployment()
    alignment = _FakeSystemAlignment(stage="failed_recoverable")
    srv, base = _serve(deployment=deployment)
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/deployment/resolve-container-conflict",
            method="POST",
            body=_setup_body(
                srv,
                container_name="ems-solarflow-api-control",
                action="remove_stopped_and_continue",
            ),
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_alignment_incomplete"
    assert alignment.stage == "failed_recoverable"
    assert not hasattr(deployment, "resolved_conflict")


def test_networks_endpoint_returns_json(server):
    status, headers, payload = _request(f"{server}/api/discovery/networks")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert isinstance(payload["networks"], list)
    assert payload["manual_entry_supported"] is True


def test_networks_endpoint_uses_injected_detector():
    def _fake_detector():
        return {
            "networks": [
                {
                    "cidr": "192.168.178.0/24",
                    "interface": "eth0",
                    "address": "192.168.178.25",
                    "scan_recommended": True,
                    "reason": "Default route on a private IPv4 network",
                }
            ],
            "warnings": ["only docker networks"],
            "manual_entry_supported": True,
        }

    srv = create_server(
        "127.0.0.1", 0, registry=ScanRegistry(scan_runner=_fake_scan),
        network_detector=_fake_detector,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        status, _, payload = _request(f"{base}/api/discovery/networks")
        assert status == 200
        assert payload["networks"][0]["cidr"] == "192.168.178.0/24"
        assert payload["warnings"] == ["only docker networks"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_scan_rejects_public_cidr(server):
    status, _, payload = _request(
        f"{server}/api/discovery/scan", method="POST", body={"cidr": "8.8.8.0/24"}
    )
    assert status == 400
    assert "error" in payload


def test_scan_rejects_missing_cidr(server):
    status, _, _ = _request(
        f"{server}/api/discovery/scan", method="POST", body={"ports": [80]}
    )
    assert status == 400


def test_scan_and_result_flow(server):
    status, _, payload = _request(
        f"{server}/api/discovery/scan",
        method="POST",
        body={"cidr": "192.168.178.0/24"},
    )
    assert status == 202
    scan_id = payload["scan_id"]

    result = None
    for _ in range(50):
        _, _, result = _request(f"{server}/api/discovery/result/{scan_id}")
        if result["status"] == "finished":
            break
        time.sleep(0.05)

    assert result["status"] == "finished"
    assert result["cidr"] == "192.168.178.0/24"
    assert result["finished_at"]
    assert len(result["devices"]) == 1
    assert result["devices"][0]["id"] == "zendure_local_http:SN123456"


def test_unknown_scan_id_returns_404(server):
    status, _, _ = _request(f"{server}/api/discovery/result/does-not-exist")
    assert status == 404


def test_oversized_body_rejected(server):
    big = "1" * (8 * 1024)
    status, _, _ = _request(
        f"{server}/api/discovery/scan", method="POST", body={"cidr": big}
    )
    assert status == 413


def test_gateway_probe_endpoint_uses_injected_prober():
    srv = create_server(
        "127.0.0.1", 0, registry=ScanRegistry(scan_runner=_fake_scan),
        gateway_prober=_fake_gateway_prober,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    try:
        status, headers, payload = _request(
            f"{base}/api/discovery/gateway-probe", method="POST"
        )
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert payload["candidates"][0]["network"] == "192.168.20.0/24"
        assert payload["source"] == "gateway_candidate_probe"
    finally:
        srv.shutdown()
        srv.server_close()


def test_gateway_probe_accepts_optional_body(server):
    status, _, payload = _request(
        f"{server}/api/discovery/gateway-probe",
        method="POST",
        body={"timeout_ms": 300, "max_workers": 8},
    )
    assert status == 200
    assert isinstance(payload["candidates"], list)


def test_mdns_status_endpoint_reports_default_state(server):
    status, headers, payload = _request(f"{server}/api/discovery/mdns/status")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert payload["state"] in (
        "running_with_devices",
        "running_no_devices",
        "disabled",
        "unavailable_dependency",
        "unavailable_runtime",
    )
    assert "message" in payload
    assert "service" not in payload
    assert "verified_count" in payload


class _FakeMdnsProvider:
    def __init__(self):
        self.enabled = False
        self.refresh_calls = 0
        self._device = {
            "id": "zendure_local_http:SN123456789",
            "source": "mdns",
            "verified": True,
            "sources": ["mdns"],
            "ip": "192.168.178.42",
        }

    def status(self):
        return {
            "enabled": self.enabled,
            "available": True,
            "running": self.enabled,
            "state": "running_with_devices" if self.enabled else "disabled",
            "message": (
                "Automatic mDNS discovery is running."
                if self.enabled
                else "Automatic mDNS discovery is disabled."
            ),
            "last_event": None,
            "last_error": None,
            "verified_count": 1,
            "mdns_device_count": 1,
        }

    def devices(self):
        return [self._device]

    def ignored_devices(self):
        return [{"id": "mdns:printer", "verified": False, "reason": "unsupported"}]

    def enable(self):
        self.enabled = True
        return self.status()

    def disable(self):
        self.enabled = False
        return self.status()

    def refresh(self):
        self.refresh_calls += 1
        self.enabled = True
        return self.status()


def _serve(**kwargs):
    kwargs.setdefault("system_alignment", SetupReadySystemAlignment())
    srv = create_server("127.0.0.1", 0, registry=ScanRegistry(scan_runner=_fake_scan),
                        gateway_prober=_fake_gateway_prober, **kwargs)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def test_mdns_enable_disable_and_results_use_provider():
    provider = _FakeMdnsProvider()
    srv, base = _serve(mdns_provider=provider)
    try:
        status, _, payload = _request(f"{base}/api/discovery/mdns/enable", method="POST")
        assert status == 200
        assert payload["enabled"] is True

        status, _, results = _request(f"{base}/api/discovery/devices")
        assert status == 200
        assert results["devices"][0]["id"] == "zendure_local_http:SN123456789"
        assert results["ignored_devices"][0]["id"] == "mdns:printer"

        status, _, payload = _request(
            f"{base}/api/discovery/mdns/refresh", method="POST"
        )
        assert status == 200
        assert payload["enabled"] is True
        assert provider.refresh_calls == 1

        status, _, payload = _request(f"{base}/api/discovery/mdns/disable", method="POST")
        assert payload["enabled"] is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_mqtt_api_keeps_brokers_separate_from_devices():
    mqtt = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: port == 1883
    )
    provider = _FakeMdnsProvider()
    srv, base = _serve(mdns_provider=provider, mqtt_discovery=mqtt)
    try:
        status, _, result = _request(
            f"{base}/api/discovery/mqtt-brokers/probe",
            method="POST",
            body={"cidr": "192.168.178.10/32"},
        )
        assert status == 200
        assert result["candidates"][0]["id"] == "mqtt:192.168.178.10:1883"

        _, _, brokers = _request(f"{base}/api/discovery/mqtt-brokers")
        _, _, devices = _request(f"{base}/api/discovery/devices")
        assert brokers["candidates"][0]["port"] == 1883
        assert all(not item["id"].startswith("mqtt:") for item in devices["devices"])
    finally:
        srv.shutdown()
        srv.server_close()


def _hardware_candidate(broker):
    return {
        "id": f"mqtt-device:{broker['id']}:zensdk_ha_scalar:EOD123",
        "broker_id": broker["id"],
        "broker_host": broker["host"],
        "broker_port": broker["port"],
        "topic_family": "zensdk_ha_scalar",
        "device_id": "EOD123",
        "serial_number": "EOD123",
        "metrics_seen": ["packInputPower"],
        "topics_seen": ["Zendure/sensor/EOD123/packInputPower"],
        "confidence": 0.75,
    }


def test_mqtt_brokers_api_returns_hardware_candidates_after_refresh():
    mqtt = MqttBrokerDiscovery(
        connector=lambda host, port, timeout: port == 1883,
        topic_discoverer=lambda broker: [_hardware_candidate(broker)],
    )
    provider = _FakeMdnsProvider()
    srv, base = _serve(mdns_provider=provider, mqtt_discovery=mqtt)
    try:
        status, _, _ = _request(
            f"{base}/api/discovery/mqtt-brokers/probe",
            method="POST",
            body={"cidr": "192.168.178.10/32"},
        )
        assert status == 200

        status, _, refreshed = _request(
            f"{base}/api/discovery/mqtt-brokers/refresh", method="POST"
        )
        assert status == 200
        assert refreshed["devices_found"] == 1

        _, _, brokers = _request(f"{base}/api/discovery/mqtt-brokers")
        broker = brokers["candidates"][0]
        assert broker["devices"][0]["serial_number"] == "EOD123"
        assert broker["devices"][0]["broker_id"] == broker["id"]

        # MQTT hardware candidates must never leak into the HTTP device list.
        _, _, devices = _request(f"{base}/api/discovery/devices")
        ids = [item["id"] for item in devices["devices"]]
        assert all(not item.startswith("mqtt:") for item in ids)
        assert all(not item.startswith("mqtt-device:") for item in ids)
    finally:
        srv.shutdown()
        srv.server_close()


def test_mqtt_probe_api_rejects_unsafe_network():
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/discovery/mqtt-brokers/probe",
            method="POST",
            body={"cidr": "8.8.8.0/24"},
        )
        assert status == 400
        assert "error" in payload
    finally:
        srv.shutdown()
        srv.server_close()


# --- Zendure cloud MQTT discovery endpoints ------------------------------

_CLOUD_API_KEY = "app-key-secret"


def _cloud_device_list_result():
    return {
        "devices": [
            {
                "productKey": "PK-AAA",
                "deviceKey": "DK-BBB",
                "productModel": "SolarFlow 800",
                "snNumber": "SN-EOD123",
                "deviceName": "Balcony battery",
            }
        ],
        "mqtt": {
            "host": "mqtt.example.invalid",
            "port": 8883,
            "username": "mqtt-user",
            "password": "mqtt-secret",
            "client_id": "client-xyz",
        },
        "api_url": "https://app.zendure.tech",
        "app_key": "app-key-secret",
    }


def _cloud_discovery(tmp_path, *, messages=()):
    store = ZendureTokenStore(data_dir=tmp_path)
    return ZendureCloudDiscovery(
        store,
        device_list_fetcher=lambda _t, _to: _cloud_device_list_result(),
        listener_factory=lambda c: FakeCloudMqttListener(c, messages),
        timeout_s=0.0,
    )


def test_zendure_cloud_endpoints_require_authentication(tmp_path):
    srv, base = _serve(zendure_cloud_discovery=_cloud_discovery(tmp_path))
    try:
        status, _, _ = raw_request(f"{base}/api/discovery/zendure-cloud-mqtt/settings")
        assert status == 401
        status, _, _ = raw_request(
            f"{base}/api/discovery/zendure-cloud-mqtt/refresh", method="POST"
        )
        assert status in (401, 403)
    finally:
        srv.shutdown()
        srv.server_close()


def test_zendure_cloud_settings_never_returns_api_key(tmp_path):
    srv, base = _serve(zendure_cloud_discovery=_cloud_discovery(tmp_path))
    try:
        _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={"api_key": _CLOUD_API_KEY},
        )
        status, _, settings = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/settings"
        )
        assert status == 200
        assert settings["token_saved"] is True
        assert _CLOUD_API_KEY not in json.dumps(settings)
        assert "api_key" not in settings
        assert "token" not in [k for k in settings if k != "token_saved"]
    finally:
        srv.shutdown()
        srv.server_close()


def _poison_fernet_encrypt(monkeypatch):
    """Break Fernet.encrypt (leaving construction/decrypt intact) so an encryption
    fault can be simulated without corrupting existing readable records."""

    from cryptography.fernet import Fernet

    def _boom(self, data):
        raise RuntimeError("simulated cipher failure")

    monkeypatch.setattr(Fernet, "encrypt", _boom)


def test_zendure_cloud_token_save_fails_closed_on_encryption_error(tmp_path, monkeypatch):
    discovery = _cloud_discovery(tmp_path)
    srv, base = _serve(zendure_cloud_discovery=discovery)
    try:
        status, _, saved = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={"api_key": _CLOUD_API_KEY},
        )
        assert status == 200 and saved["token_saved"] is True

        # Encryption breaks: the rotation must fail closed, never store a base64
        # downgrade or leak the token in the response.
        _poison_fernet_encrypt(monkeypatch)
        status, _, payload = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={"api_key": "a-newer-rotated-secret"},
        )
        assert status >= 400, payload
        assert payload.get("ok") is False
        body = json.dumps(payload)
        assert "a-newer-rotated-secret" not in body
        assert _CLOUD_API_KEY not in body
    finally:
        srv.shutdown()
        srv.server_close()

    # The previous token survived the failed rotation and is still usable.
    assert discovery.store.load_token() == _CLOUD_API_KEY


def test_zendure_cloud_save_and_delete_api_key(tmp_path):
    discovery = _cloud_discovery(tmp_path)
    srv, base = _serve(zendure_cloud_discovery=discovery)
    try:
        status, _, saved = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={"api_key": _CLOUD_API_KEY},
        )
        assert status == 200
        assert saved["token_saved"] is True
        assert discovery.store.token_saved() is True
        assert discovery.store.load_token() == _CLOUD_API_KEY

        status, _, removed = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token", method="DELETE"
        )
        assert status == 200
        assert removed["token_saved"] is False
        assert discovery.store.token_saved() is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_zendure_cloud_save_accepts_token_alias(tmp_path):
    discovery = _cloud_discovery(tmp_path)
    srv, base = _serve(zendure_cloud_discovery=discovery)
    try:
        status, _, saved = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={"token": _CLOUD_API_KEY},
        )
        assert status == 200
        assert saved["token_saved"] is True
        assert discovery.store.load_token() == _CLOUD_API_KEY
    finally:
        srv.shutdown()
        srv.server_close()


def test_zendure_cloud_save_rejects_both_api_key_and_token(tmp_path):
    srv, base = _serve(zendure_cloud_discovery=_cloud_discovery(tmp_path))
    try:
        status, _, payload = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={"api_key": _CLOUD_API_KEY, "token": _CLOUD_API_KEY},
        )
        assert status == 400
        assert "error" in payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_zendure_cloud_save_accepts_ha_mode_and_rejects_manual_mode(tmp_path):
    srv, base = _serve(zendure_cloud_discovery=_cloud_discovery(tmp_path))
    try:
        status, _, payload = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={
                "api_key": _CLOUD_API_KEY,
                "credential_mode": "ha_device_list_token",
            },
        )
        assert status == 200
        assert payload["token_saved"] is True

        status, _, payload = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={
                "api_key": _CLOUD_API_KEY,
                "credential_mode": "manual_mqtt_credentials",
            },
        )
        assert status == 400
        assert payload["error"] == "unsupported_credential_mode"
        assert "Zendure API key" in payload["message"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_zendure_cloud_save_rejects_empty_api_key(tmp_path):
    srv, base = _serve(zendure_cloud_discovery=_cloud_discovery(tmp_path))
    try:
        status, _, payload = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={"api_key": "   "},
        )
        assert status == 400
        assert "error" in payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_zendure_cloud_test_returns_device_count(tmp_path):
    discovery = _cloud_discovery(tmp_path)
    discovery.store.save_token(_CLOUD_API_KEY)
    srv, base = _serve(zendure_cloud_discovery=discovery)
    try:
        status, _, result = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/test", method="POST", body={}
        )
        assert status == 200
        assert result["devices_found"] == 1
        assert result["broker"] == "mqtt.example.invalid:8883"
        assert result["tls_required"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_zendure_cloud_refresh_returns_candidates(tmp_path):
    messages = [
        ("iot/PK-AAA/DK-BBB/properties/report", json.dumps({"properties": {"soc": 55}}))
    ]
    discovery = _cloud_discovery(tmp_path, messages=messages)
    discovery.store.save_token(_CLOUD_API_KEY)
    srv, base = _serve(zendure_cloud_discovery=discovery)
    try:
        status, _, result = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/refresh", method="POST"
        )
        assert status == 200
        assert result["ok"] is True
        assert result["device_list_count"] == 1
        candidate = result["candidates"][0]
        assert candidate["source_type"] == "zendure_cloud_mqtt"
        assert candidate["serial_number"] == "SN-EOD123"
        blob = json.dumps(result["candidates"])
        assert "PK-AAA" not in blob and "DK-BBB" not in blob
    finally:
        srv.shutdown()
        srv.server_close()


def test_zendure_cloud_refresh_without_token_is_not_configured(tmp_path):
    srv, base = _serve(zendure_cloud_discovery=_cloud_discovery(tmp_path))
    try:
        status, _, result = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/refresh", method="POST"
        )
        assert status == 400
        assert result["error"] == "not_configured"
    finally:
        srv.shutdown()
        srv.server_close()


def test_mqtt_proposals_include_zendure_cloud_candidates(tmp_path):
    # Cloud-discovered devices must be part of the one trusted proposal set the
    # review UI reads, not stranded in the per-source candidate list.
    discovery = _cloud_discovery(
        tmp_path,
        messages=[
            (
                "/PK-AAA/DK-BBB/properties/report",
                json.dumps({"properties": {"electricLevel": 55}}),
            )
        ],
    )
    discovery.store.save_token(_CLOUD_API_KEY)
    assert discovery.refresh()["ok"] is True
    srv, base = _serve(zendure_cloud_discovery=discovery)
    try:
        status, _, payload = _request(f"{base}/api/discovery/mqtt-proposals")
        assert status == 200
        cloud = [
            p for p in payload["proposals"] if p["broker_ref"] == "zendure_cloud"
        ]
        assert len(cloud) == 1
        assert cloud[0]["serial_number"] == "SN-EOD123"
        assert cloud[0]["device_id"] == "SN-EOD123"
        assert cloud[0]["config_fragment"]["mqtt"]["source"] == "zendure_cloud_mqtt"
        assert "device_id" not in cloud[0]["config_fragment"]["mqtt"]
        blob = json.dumps(payload)
        for secret in (_CLOUD_API_KEY, "mqtt-secret", "mqtt-user", "PK-AAA", "DK-BBB"):
            assert secret not in blob
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_preview_accepts_zendure_cloud_mqtt_proposal(tmp_path):
    # The preview trust-resolve must see the same combined proposal set as the
    # GET endpoint, so a selected cloud proposal resolves instead of being
    # rejected as unknown. Token store and preview auth check share the
    # isolated EMS credential store.
    from admin.credential_store import CredentialStore

    messages = [
        (
            "iot/PK-AAA/DK-BBB/properties/report",
            json.dumps({"properties": {"electricLevel": 55, "outputHomePower": 120}}),
        )
    ]
    discovery = ZendureCloudDiscovery(
        CredentialStore().zendure,
        device_list_fetcher=lambda _t, _to: _cloud_device_list_result(),
        listener_factory=lambda c: FakeCloudMqttListener(c, messages),
        timeout_s=0.0,
    )
    discovery.save_token(_CLOUD_API_KEY)
    assert discovery.refresh()["ok"] is True
    srv, base = _serve(
        release_manager=_FakeReleaseManager(tmp_path),
        zendure_cloud_discovery=discovery,
    )
    try:
        _, _, proposals_payload = _request(f"{base}/api/discovery/mqtt-proposals")
        proposal = next(
            p
            for p in proposals_payload["proposals"]
            if p["broker_ref"] == "zendure_cloud"
        )
        selection = {
            "id": proposal["id"],
            "broker_ref": proposal["broker_ref"],
            "serial_number": proposal["serial_number"],
            "device_id": proposal["device_id"],
        }
        status, _, payload = _request(
            f"{base}/api/setup/config-preview/validate",
            method="POST",
            body={
                "devices": [],
                "supported_grid_meter_count": 0,
                "zendure_mqtt_proposals": [selection],
            },
        )
        assert status == 200
        entries = [
            d for d in payload["config"]["devices"] if d.get("type") == "zendure_mqtt"
        ]
        assert len(entries) == 1
        assert entries[0]["mqtt"]["broker_ref"] == "zendure_cloud"
        assert entries[0]["mqtt"]["device_id"] == "••••"
        assert entries[0]["capabilities"]["write_output_limit"] is True
        assert "zendure_cloud" in payload["config"]["zendure_mqtt"]["brokers"]
        blob = json.dumps(payload)
        for secret in (
            _CLOUD_API_KEY,
            "mqtt-secret",
            "mqtt-user",
            "PK-AAA",
            "DK-BBB",
        ):
            assert secret not in blob
    finally:
        srv.shutdown()
        srv.server_close()


def test_discovery_devices_excludes_cloud_candidates(tmp_path):
    discovery = _cloud_discovery(tmp_path)
    discovery.store.save_token(_CLOUD_API_KEY)
    provider = _FakeMdnsProvider()
    srv, base = _serve(mdns_provider=provider, zendure_cloud_discovery=discovery)
    try:
        _request(f"{base}/api/discovery/zendure-cloud-mqtt/refresh", method="POST")
        _, _, devices = _request(f"{base}/api/discovery/devices")
        ids = [item["id"] for item in devices["devices"]]
        assert all(not item.startswith("mqtt-device:") for item in ids)
    finally:
        srv.shutdown()
        srv.server_close()


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_install_state_endpoint_returns_contract_keys():
    srv, base = _serve()
    try:
        status, _, payload = _request(f"{base}/api/admin/install-state")
        assert status == 200
        for key in (
            "state",
            "recommended_path",
            "paths",
            "reasons",
            "warnings",
            "legacy_migration_available",
            "setup_requires_confirmation",
        ):
            assert key in payload
        assert payload["recommended_path"] in ("setup_new", "manage_existing")
        for key in ("legacy_config", "standard_config", "compose", "data"):
            assert key in payload["paths"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_start_path_rejects_unknown_choice():
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/start-path",
            method="POST",
            body={"choice": "docker_bootstrap"},
        )
        assert status == 400
        assert "error" in payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_start_path_setup_requires_confirmation_on_existing(monkeypatch, tmp_path):
    from ems import paths

    monkeypatch.setattr(paths, "BASE_DIR", str(tmp_path))
    _write_json(tmp_path / "config" / "config.json", {"a": 1})

    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/start-path",
            method="POST",
            body={"choice": "setup_new"},
        )
        assert status == 409
        assert payload["requires_confirmation"] is True

        status, _, confirmed = _request(
            f"{base}/api/admin/start-path",
            method="POST",
            body={"choice": "setup_new", "confirm": True},
        )
        assert status == 200
        assert confirmed["ok"] is True
        assert confirmed["route"] == "setup"
        assert confirmed["existing_install_confirmed"] is True
        assert confirmed["setup_intent_id"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_start_path_setup_issues_intent_for_empty_installation():
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/start-path",
            method="POST",
            body={"choice": "setup_new"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["route"] == "setup"
    assert payload["setup_intent_id"]
    assert payload["existing_install_confirmed"] is False


@pytest.mark.parametrize(
    "path",
    [
        "/api/setup/system-build/update-admin",
        "/api/setup/system-build/confirm",
        "/api/setup/releases/prepare",
        "/api/setup/automated/releases/prepare",
    ],
)
def test_setup_intent_is_required_by_setup_mutations(path, tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        # Naming the active workflow is not the user's confirmation: the route
        # still refuses without the one-shot intent issued for that workflow.
        workflow_id, _intent_id = _setup_build_authority(srv, base)
        status, _, payload = _request(
            f"{base}{path}",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "setup_intent_required"
    assert alignment.start_calls == []
    assert alignment.confirm_calls == []
    assert alignment.prepare_calls == []


def test_setup_intent_expires_before_confirm(tmp_path):
    from admin.setup_intent import SetupIntentStore

    now = [100.0]
    intents = SetupIntentStore(ttl_seconds=1200, time_fn=lambda: now[0])
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path), setup_intents=intents
    )
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        now[0] += 1201
        status, _, payload = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "setup_intent_expired"
    assert alignment.confirm_calls == []


def test_setup_intent_rejects_changed_install_state(tmp_path):
    from ems import paths

    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        config_path = Path(paths.BASE_DIR) / "config" / "config.json"
        _write_json(config_path, {"changed": True})
        status, _, payload = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload == {
        "error": "setup_state_changed",
        "message": "The installation state changed. Confirm Fresh Setup again.",
    }
    assert alignment.confirm_calls == []


def test_setup_intent_is_invalid_after_logout_and_new_session(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        logout_status, _, _ = _request(
            f"{base}/api/admin/auth/logout", method="POST"
        )
        authenticate(base)
        status, _, payload = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert logout_status == 200
    assert status == 409
    assert payload["error"] == "setup_intent_required"
    assert alignment.confirm_calls == []


def test_setup_intent_is_invalid_after_selecting_another_start_path(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        manage_status, _, _ = _request(
            f"{base}/api/admin/start-path",
            method="POST",
            body={"choice": "manage_existing"},
        )
        status, _, payload = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert manage_status == 200
    assert status == 409
    assert payload["error"] == "setup_intent_required"
    assert alignment.confirm_calls == []


def test_development_setup_intent_is_required_but_acknowledgement_is_not(tmp_path):
    # Fresh Setup still needs a valid one-shot intent, but selecting an
    # Experimental build is itself the decision: no second acknowledgement.
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        missing_intent_status, _, missing_intent = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": _DEV_BUILD_TAG, "setup_workflow_id": workflow_id},
        )
        accepted_status, _, accepted = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": _DEV_BUILD_TAG, "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert missing_intent_status == 409
    assert missing_intent["error"] == "setup_intent_required"
    assert accepted_status == 200
    assert accepted["resources_verified"] is True
    # The server authorised the Experimental build itself (never a browser flag).
    assert alignment.confirm_calls == [
        {"requested_tag": _DEV_BUILD_TAG, "mode": "fresh_install"}
    ]
    assert alignment.development_acknowledgements == [True]


def test_start_path_manage_routes_legacy_to_migration(monkeypatch, tmp_path):
    from ems import paths

    monkeypatch.setattr(paths, "BASE_DIR", str(tmp_path))
    _write_json(tmp_path / "config.json", {"a": 1})

    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/start-path",
            method="POST",
            body={"choice": "manage_existing"},
        )
        assert status == 200
        assert payload["route"] == "maintenance"
        assert payload["migrate_legacy_config"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_migrate_legacy_endpoint_migrates_and_backs_up(monkeypatch, tmp_path):
    from ems import paths

    monkeypatch.setattr(paths, "BASE_DIR", str(tmp_path))
    _write_json(tmp_path / "config.json", {"a": 1})
    (tmp_path / "data").mkdir()

    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/config/migrate-legacy", method="POST"
        )
        assert status == 200
        assert payload["ok"] is True and payload["migrated"] is True
        assert (tmp_path / "config" / "config.json").exists()
        assert (tmp_path / "config.json").exists(), "legacy source preserved"
        assert (tmp_path / "data").is_dir(), "runtime data preserved"
    finally:
        srv.shutdown()
        srv.server_close()


def test_migrate_legacy_endpoint_missing_source_returns_404(monkeypatch, tmp_path):
    from ems import paths

    monkeypatch.setattr(paths, "BASE_DIR", str(tmp_path))
    _write_json(tmp_path / "config" / "config.json", {"a": 1})

    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/config/migrate-legacy", method="POST"
        )
        assert status == 404
        assert payload["reason"] == "legacy_config_missing"
    finally:
        srv.shutdown()
        srv.server_close()


def test_install_state_endpoint_uses_env_install_dir(monkeypatch, tmp_path):
    from ems import paths

    monkeypatch.setattr(paths, "BASE_DIR", str(tmp_path / "app"))
    (tmp_path / "app").mkdir()
    root = tmp_path / "install"
    _write_json(root / "config" / "config.json", {"a": 1})
    (root / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))

    srv, base = _serve()
    try:
        status, _, payload = _request(f"{base}/api/admin/install-state")
        assert status == 200
        assert payload["state"] == "standard_install"
        assert payload["paths"]["install_root"] == str(root)
        assert payload["paths"]["standard_config"] == str(
            root / "config" / "config.json"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_migrate_legacy_endpoint_uses_env_install_dir(monkeypatch, tmp_path):
    from ems import paths

    monkeypatch.setattr(paths, "BASE_DIR", str(tmp_path / "app"))
    (tmp_path / "app").mkdir()
    root = tmp_path / "install"
    _write_json(root / "config.json", {"a": 1})
    (root / "data").mkdir()
    monkeypatch.setenv("EMS_INSTALL_DIR", str(root))

    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/config/migrate-legacy", method="POST"
        )
        assert status == 200
        assert payload["migrated"] is True
        assert payload["target"] == str(root / "config" / "config.json")
        assert (root / "config" / "config.json").exists()
        assert (root / "config.json").exists(), "legacy source preserved"
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_container_plan_endpoint_returns_plan(monkeypatch):
    from admin import server as server_module

    fake_plan = {
        "ok": True,
        "available": True,
        "install_root": "/install",
        "compose_path": "/install/docker-compose.yml",
        "requires_confirmation": True,
        "desired": {
            "ems": {"service": "ems", "desired": "running", "reason": "present"},
            "influxdb": {"service": "influxdb", "desired": "running", "reason": "on"},
        },
        "current": {"ems": {}, "influxdb": {}},
        "actions": [{"service": "ems", "action": "recreate", "label": "Recreate EMS", "reason": "x"}],
        "summary": "EMS will be recreated so it reads the new config.",
    }
    monkeypatch.setattr(server_module, "build_maintenance_container_plan", lambda: fake_plan)
    srv, base = _serve()
    try:
        status, _, payload = _request(f"{base}/api/admin/maintenance/containers/plan")
        assert status == 200
        assert payload["ok"] is True
        assert payload["desired"]["ems"]["desired"] == "running"
        assert payload["actions"][0]["action"] == "recreate"
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_container_sync_requires_confirm(monkeypatch):
    from admin import server as server_module

    called = {"count": 0}

    def _fake_sync():
        called["count"] += 1
        return {"ok": True, "status": "completed", "steps": [], "plan": {}}

    monkeypatch.setattr(server_module, "run_maintenance_container_sync", _fake_sync)
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/maintenance/containers/sync",
            method="POST",
            body={"reason": "config_apply"},
        )
        assert status == 400
        assert called["count"] == 0

        status, _, ok = _request(
            f"{base}/api/admin/maintenance/containers/sync",
            method="POST",
            body={"confirm": True, "reason": "config_apply"},
        )
        assert status == 200
        assert ok["ok"] is True
        assert called["count"] == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_container_sync_rejects_unsupported_fields(monkeypatch):
    from admin import server as server_module

    monkeypatch.setattr(
        server_module,
        "run_maintenance_container_sync",
        lambda: pytest.fail("sync must not run for a rejected request"),
    )
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/maintenance/containers/sync",
            method="POST",
            body={"confirm": True, "path": "/custom/config.json"},
        )
        assert status == 400
        assert "unsupported" in payload["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_container_sync_returns_409_when_unavailable(monkeypatch):
    from admin import server as server_module

    monkeypatch.setattr(
        server_module,
        "run_maintenance_container_sync",
        lambda: {
            "ok": False,
            "status": "unavailable",
            "message": "Docker/Compose is not available. Re-check the Admin deployment.",
            "plan": {},
            "steps": [],
        },
    )
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/maintenance/containers/sync",
            method="POST",
            body={"confirm": True, "reason": "config_apply"},
        )
        assert status == 409
        assert payload["status"] == "unavailable"
    finally:
        srv.shutdown()
        srv.server_close()


def test_maintenance_container_sync_relays_influx_schema_step(monkeypatch):
    from admin import server as server_module

    monkeypatch.setattr(
        server_module,
        "run_maintenance_container_sync",
        lambda: {
            "ok": True,
            "status": "completed",
            "plan": {},
            "steps": [
                {"service": "influxdb", "action": "init", "status": "ok"},
                {"service": "influxdb", "action": "start", "status": "ok"},
                {"service": "influxdb", "action": "sync", "status": "ok"},
                {"service": "ems", "action": "recreate", "status": "ok"},
            ],
        },
    )
    srv, base = _serve()
    try:
        status, _, payload = _request(
            f"{base}/api/admin/maintenance/containers/sync",
            method="POST",
            body={"confirm": True, "reason": "config_apply"},
        )
        assert status == 200
        assert payload["ok"] is True
        assert {"service": "influxdb", "action": "sync", "status": "ok"} in payload["steps"]
    finally:
        srv.shutdown()
        srv.server_close()


# --- scan progress ---------------------------------------------------------

def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


def _progress_device():
    return DiscoveredDevice(
        ip="192.168.1.73",
        api_family="zendure_local_http",
        device_type="zendure_solarflow_unknown",
        role_suggestion="inverter",
        display_name="Zendure SolarFlow device",
        serial_number="SN73",
        confidence=0.95,
        config_ready=True,
    )


def test_scan_registry_result_includes_running_progress():
    release = threading.Event()

    def runner(cidr, timeout_ms=600, max_workers=32, progress_callback=None):
        progress_callback({
            "total_hosts": 254,
            "checked_hosts": 73,
            "found_devices": 1,
            "failed_hosts": 0,
            "current_ip": "192.168.1.73",
        })
        release.wait(2)
        return [_progress_device()], []

    registry = ScanRegistry(scan_runner=runner)
    try:
        record = registry.start("192.168.1.0/24", [80], 600, 32)
        scan_id = record["scan_id"]
        current = _wait_for(
            lambda: registry.get(scan_id)
            if registry.get(scan_id)["progress"]["checked_hosts"] == 73
            else None
        )
        assert current["status"] == "running"
        assert current["progress"]["total_hosts"] == 254
        assert current["progress"]["checked_hosts"] == 73
        assert current["progress"]["found_devices"] == 1
        assert current["progress"]["percent"] == 28
    finally:
        release.set()


def test_scan_registry_marks_progress_complete_on_finish():
    def runner(cidr, timeout_ms=600, max_workers=32, progress_callback=None):
        progress_callback({
            "total_hosts": 254,
            "checked_hosts": 200,
            "found_devices": 1,
            "failed_hosts": 0,
            "current_ip": "192.168.1.73",
        })
        return [_progress_device()], []

    registry = ScanRegistry(scan_runner=runner)
    record = registry.start("192.168.1.0/24", [80], 600, 32)
    scan_id = record["scan_id"]
    finished = _wait_for(
        lambda: registry.get(scan_id)
        if registry.get(scan_id)["status"] == "finished"
        else None
    )
    progress = finished["progress"]
    assert progress["percent"] == 100
    assert progress["checked_hosts"] == progress["total_hosts"] == 254
    assert progress["found_devices"] == len(finished["devices"]) == 1


def test_scan_registry_failed_scan_keeps_known_progress():
    def runner(cidr, timeout_ms=600, max_workers=32, progress_callback=None):
        progress_callback({
            "total_hosts": 254,
            "checked_hosts": 40,
            "found_devices": 0,
            "failed_hosts": 0,
            "current_ip": "192.168.1.40",
        })
        raise RuntimeError("boom")

    registry = ScanRegistry(scan_runner=runner)
    record = registry.start("192.168.1.0/24", [80], 600, 32)
    scan_id = record["scan_id"]
    failed = _wait_for(
        lambda: registry.get(scan_id)
        if registry.get(scan_id)["status"] == "failed"
        else None
    )
    assert failed["progress"]["checked_hosts"] == 40
    assert failed["progress"]["percent"] < 100


def test_scan_result_view_keeps_existing_fields_and_adds_progress():
    record = {
        "scan_id": "abc",
        "status": "running",
        "cidr": "192.168.1.0/24",
        "started_at": "t0",
        "finished_at": None,
        "devices": [],
        "errors": [],
        "progress": {
            "total_hosts": 254,
            "checked_hosts": 73,
            "found_devices": 1,
            "failed_hosts": 0,
            "percent": 28,
        },
    }
    view = _result_view(record)
    for field in ("scan_id", "status", "cidr", "started_at", "finished_at",
                  "devices", "errors"):
        assert field in view
    assert view["progress"]["checked_hosts"] == 73
    assert view["progress"]["percent"] == 28


def test_scan_result_view_defaults_progress_for_legacy_record():
    record = {
        "scan_id": "abc",
        "status": "finished",
        "cidr": "192.168.1.0/24",
        "started_at": "t0",
        "finished_at": "t1",
        "devices": [],
        "errors": [],
    }
    view = _result_view(record)
    assert view["progress"]["percent"] == 0
    assert view["progress"]["total_hosts"] == 0


# --- paired System Build productive route contracts ----------------------


_DEV_BUILD_TAG = "dev-feature-zendure-mqtt-f7265fc-123456789-1"
_DEV_FLOATING_TAG = "dev-feature-zendure-mqtt"


class _FakeSystemAlignment:
    """Route-shaped alignment double with an explicit persisted stage.

    The production service owns Docker/image identities and the transition
    store.  Browser requests therefore carry only a tag/operation id; this fake
    deliberately exposes no way for a request to choose an image repository.
    """

    def __init__(
        self, stage="resources_verified", *, active=True, mode="fresh_install"
    ):
        self.stage = stage
        self.active = active
        self.mode = mode
        self.start_calls = []
        self.prepare_calls = []
        self.confirm_calls = []
        self.validate_calls = []
        self.resume_calls = []
        self.verify_calls = []
        self.return_calls = []
        self.ems_calls = []
        self.health_calls = []
        self.development_acknowledgements = []
        self._ems_claimed = False

    def validate_setup_discovery_operation(self, *, operation_id):
        if not operation_id:
            raise SystemAlignmentError(
                "setup_operation_required", "a Setup operation id is required"
            )
        if operation_id != "op-1":
            raise SystemAlignmentError("operation_mismatch", "operation differs")
        if (
            not self.active
            or self.mode not in {"fresh_install", "automated_setup"}
            or self.stage != "resources_verified"
        ):
            raise SystemAlignmentError(
                "system_alignment_incomplete", "resources are not verified"
            )
        return self.status()["transition"]

    @staticmethod
    def _system_build(tag="v0.8.0"):
        return {
            "requested_tag": tag,
            "canonical_tag": tag,
            "channel": "dev" if tag.startswith("dev-") else "stable",
            "revision": "f7265fc747c2223f126f0ee7801e030c6226edf4",
            "build_id": (
                "dev-feature-zendure-mqtt-f7265fc-123456789-1"
                if tag.startswith("dev-")
                else "v0.8.0-f7265fc"
            ),
            "admin_image": f"ghcr.io/basecubedev/ems-solarflow-admin:{tag}",
            "admin_digest": "sha256:admin",
            "ems_image": (
                "ghcr.io/basecubedev/ems-solarflow-api-control:" + tag
            ),
            "ems_digest": "sha256:ems",
            "release_tag": None if tag.startswith("dev-") else tag,
        }

    @staticmethod
    def selection_fingerprint(build):
        return ":".join(
            str(build.get(key) or "")
            for key in (
                "canonical_tag",
                "channel",
                "revision",
                "build_id",
                "admin_digest",
                "ems_digest",
            )
        )

    def validate(self, *, requested_tag):
        self.validate_calls.append(requested_tag)
        if requested_tag == _DEV_FLOATING_TAG:
            raise SystemBuildError(
                "system_build_dev_floating",
                "floating development aliases are not install targets",
            )
        build = self._system_build(requested_tag)
        return {
            "ok": True,
            "status": "validated",
            "valid": True,
            "validation_state": "valid",
            "selected_tag": requested_tag,
            "system_build": build,
            "alignment": "aligned",
            "admin_update_required": False,
            "embedded_resources_valid": True,
            "resources_verified": False,
            "next_allowed": True,
            "summary": {
                "channel": build["channel"],
                "revision": build["revision"],
                "build_id": build["build_id"],
                "admin_image": build["admin_image"],
                "ems_image": build["ems_image"],
            },
            "checks": {
                "admin_image_available": True,
                "ems_image_available": True,
                "revision_matches": True,
                "build_id_matches": True,
                "channel_matches": True,
                "embedded_resources_match": True,
            },
        }

    @staticmethod
    def _reject_floating(requested_tag):
        # The real resolver refuses floating development aliases in every start
        # path, so server-side validation stays enforced with or without an ack.
        if requested_tag == _DEV_FLOATING_TAG:
            raise SystemBuildError(
                "system_build_dev_floating",
                "floating development aliases are not install targets",
            )

    def start(
        self, *, requested_tag, mode, development_risk_acknowledged=False
    ):
        self._reject_floating(requested_tag)
        self.mode = mode
        self.development_acknowledgements.append(development_risk_acknowledged)
        self.start_calls.append(
            {"requested_tag": requested_tag, "mode": mode}
        )
        status = (
            "ready_for_ems"
            if self.stage == "resources_verified"
            else "admin_alignment_started"
        )
        return {
            "ok": True,
            "status": status,
            "stage": self.stage,
            "reconnect": status == "admin_alignment_started",
            "operation_id": "op-1",
            "system_build": self._system_build(requested_tag),
        }

    def resolve(self, requested_tag):
        return self._system_build(requested_tag)

    def start_resolved(
        self,
        *,
        system_build,
        mode,
        request_fingerprint=None,
        development_risk_acknowledged=False,
        pre_launch=None,
    ):
        self.request_fingerprint = request_fingerprint
        if pre_launch is not None:
            pre_launch(SimpleNamespace(operation_id="op-1"))
        return self.start(
            requested_tag=system_build["canonical_tag"],
            mode=mode,
            development_risk_acknowledged=development_risk_acknowledged,
        )

    def prepare_setup_resources(
        self, *, requested_tag, mode, development_risk_acknowledged=False,
        pre_launch=None,
    ):
        # Aligned-only: verify resources without ever launching an Admin update.
        # A not-yet-aligned Admin is refused with the alignment-required error.
        self._reject_floating(requested_tag)
        self.mode = mode
        self.development_acknowledgements.append(development_risk_acknowledged)
        self.prepare_calls.append({"requested_tag": requested_tag, "mode": mode})
        if self.stage in {"resources_verified", "admin_aligned"}:
            if pre_launch is not None:
                pre_launch(SimpleNamespace(operation_id="op-1"))
            self.stage = "resources_verified"
            return {
                "ok": True,
                "status": "ready_for_ems",
                "stage": "resources_verified",
                "operation_id": "op-1",
                "resources_verified": True,
                "next_allowed": True,
                "system_build": self._system_build(requested_tag),
            }
        raise SystemAlignmentError(
            "system_build_alignment_required",
            "align the Admin to the selected System Build before preparing resources",
        )

    def confirm_setup_build(
        self, *, requested_tag, mode, development_risk_acknowledged=False,
        pre_launch=None,
    ):
        self._reject_floating(requested_tag)
        self.development_acknowledgements.append(development_risk_acknowledged)
        self.confirm_calls.append({"requested_tag": requested_tag, "mode": mode})
        # Production persists the workflow link before committing the transition.
        if pre_launch is not None:
            pre_launch(SimpleNamespace(operation_id="op-1"))
        self.stage = "resources_verified"
        return {
            "ok": True,
            "status": "resources_verified",
            "stage": "resources_verified",
            "operation_id": "op-1",
            "resources_verified": True,
            "next_allowed": True,
            "system_build": self._system_build(requested_tag),
        }

    def status(self, *, operation_active=None):
        del operation_active
        transition = None
        if self.active:
            transition = {
                "operation_id": "op-1",
                "mode": self.mode,
                "stage": self.stage,
                "system_tag": "v0.8.0",
                "build_id": "v0.8.0-f7265fc",
                "revision": "f7265fc747c2223f126f0ee7801e030c6226edf4",
                "admin_image": "ghcr.io/basecubedev/ems-solarflow-admin:v0.8.0",
                "ems_image": (
                    "ghcr.io/basecubedev/ems-solarflow-api-control:v0.8.0"
                ),
                "failed_stage": "ems_operation_running"
                if self.stage == "failed_recoverable"
                else None,
                "resume_stage": "ems_operation_pending"
                if self.stage == "failed_recoverable"
                else None,
                "error_code": "ems_deployment_failed"
                if self.stage == "failed_recoverable"
                else None,
                "error_message": "EMS deployment failed"
                if self.stage == "failed_recoverable"
                else None,
                "resume_available": self.stage == "failed_recoverable",
            }
        return {
            "ok": True,
            "active": self.active,
            "transition": transition,
            "known_good": {
                "system_tag": "v0.7.0",
                "build_id": "v0.7.0-aaaaaaa",
                "revision": "aaaaaaa000000000000000000000000000000000",
                "admin_image": "ghcr.io/basecubedev/ems-solarflow-admin:v0.7.0",
                "ems_image": (
                    "ghcr.io/basecubedev/ems-solarflow-api-control:v0.7.0"
                ),
            },
        }

    def resume(self, *, operation_id, **kwargs):
        self.resume_calls.append(
            {"operation_id": operation_id, "extra": dict(kwargs)}
        )
        self.stage = "admin_aligned"
        return {
            "ok": True,
            "operation_id": operation_id,
            "stage": self.stage,
        }

    def retry(self, *, operation_id, development_risk_acknowledged=False):
        if self.stage != "failed_recoverable":
            raise SystemAlignmentError("not_resumable", "transition is not failed")
        self.stage = "ems_operation_pending"
        self._ems_claimed = False
        return {"ok": True, "operation_id": operation_id, "stage": self.stage}

    def recover_ems_operation(
        self, *, operation_id, healthcheck_passed=None, **_kwargs
    ):
        if self.stage in {"resources_verified", "ems_operation_pending"}:
            return {"ok": True, "operation_id": operation_id, "stage": self.stage}
        if self.stage == "healthcheck_pending" and healthcheck_passed is not None:
            return self.finish_healthcheck(
                operation_id=operation_id,
                passed=healthcheck_passed,
            )
        return {"ok": True, "operation_id": operation_id, "stage": self.stage}

    def return_to_running_build(self, *, operation_id, confirm):
        self.return_calls.append(
            {"operation_id": operation_id, "confirm": confirm}
        )
        return {
            "ok": True,
            "operation_id": operation_id,
            "status": "admin_return_started",
            "target_system_tag": "v0.7.0",
            "reconnect": True,
        }

    def verify_resources(self, *, operation_id):
        self.verify_calls.append(operation_id)
        if self.stage != "admin_aligned":
            raise SystemAlignmentError(
                "invalid_transition", "Admin must be aligned first"
            )
        self.stage = "resources_verified"
        return {
            "ok": True,
            "operation_id": operation_id,
            "stage": self.stage,
            "status": self.stage,
        }

    def is_transition_pending(self):
        return self.active and self.stage not in {"completed", "cancelled"}

    def resources_verified(self):
        return self.stage in {
            "resources_verified",
            "ems_operation_pending",
            "ems_operation_running",
            "healthcheck_pending",
            "completed",
        }

    def begin_ems_operation(self, *, operation_id):
        self.ems_calls.append(("begin", operation_id))
        if self.stage == "resources_verified":
            self.stage = "ems_operation_pending"
        return {"status": self.stage, "operation_id": operation_id}

    def claim_ems_operation(self, *, operation_id):
        self.ems_calls.append(("claim", operation_id))
        if self.stage != "ems_operation_pending" or self._ems_claimed:
            return False
        self._ems_claimed = True
        self.stage = "ems_operation_running"
        return True

    def finish_ems_operation(
        self,
        *,
        operation_id,
        succeeded,
        error_code=None,
        error_message=None,
    ):
        self.ems_calls.append(("finish", operation_id, succeeded, error_code))
        self.stage = "healthcheck_pending" if succeeded else "failed_recoverable"
        return {
            "status": self.stage,
            "stage": self.stage,
            "operation_id": operation_id,
        }

    def finish_healthcheck(
        self,
        *,
        operation_id,
        passed,
        system_build=None,
        error_code=None,
        error_message=None,
    ):
        self.health_calls.append((operation_id, passed, error_code))
        self.stage = "completed" if passed else "failed_recoverable"
        self.active = not passed
        # The real service reports the transition stage under both keys; the
        # server's terminal bookkeeping reads "stage".
        return {
            "status": self.stage,
            "stage": self.stage,
            "operation_id": operation_id,
        }


class _TrackingReleaseManager(_FakeReleaseManager):
    def __init__(self, data_dir, template=None):
        super().__init__(data_dir=data_dir, template=template)
        self.prepare_calls = []

    def prepare(self, tag, *, revision=None):
        self.prepare_calls.append(tag)
        return super().prepare(tag, revision=revision)


class _AlignmentGatedUpgrade:
    def __init__(self):
        self.preflight_calls = []
        self.prepare_calls = []
        self.run_calls = []
        self.run_called = threading.Event()

    def preflight(self, target_release, options, *, confirm=False, system_build=None):
        self.preflight_calls.append(
            {
                "target_release": target_release,
                "options": options,
                "confirm": confirm,
            }
        )
        return None, SimpleNamespace(
            options=dict(options),
            migration={"required": False, "revision": None, "review": None},
        )

    @staticmethod
    def request_fingerprint(target_release, options):
        # Match what the durable context store recomputes on save, so the
        # persist-before-launch step succeeds rather than failing closed.
        from admin.guided_upgrade import guided_upgrade_request_fingerprint

        return guided_upgrade_request_fingerprint(target_release, options)

    def prepare_alignment(self, run_context):
        self.prepare_calls.append(run_context)
        return None, object()

    def run(self, run_context, *, pre_alignment, progress):
        self.run_calls.append((run_context, pre_alignment, progress))
        self.run_called.set()
        return {
            "ok": False,
            "status": "failed",
            "reason": "sentinel_run",
            "message": "executor reached after alignment",
            "steps": [],
            "warnings": [],
        }


class _ReadOnlyDiagnostics:
    def __init__(self):
        self.calls = 0

    def run(self):
        self.calls += 1
        return {
            "available": True,
            "mode": "container",
            "checks": [],
            "summary": {
                "status": "ok",
                "ok": 1,
                "warning": 0,
                "failed": 0,
                "unavailable": 0,
            },
        }


def _attach_system_alignment(server, alignment):
    # The route tests attach the injected dependency explicitly so each missing
    # route/gate fails for its own contract, rather than every test collapsing at
    # the currently-missing create_server keyword.
    server.system_alignment = alignment
    server.runtime.system_alignment = alignment


def test_admin_runtime_accepts_one_shared_system_alignment_service(tmp_path):
    alignment = _FakeSystemAlignment()
    runtime = create_admin_runtime(
        release_manager=_TrackingReleaseManager(tmp_path),
        system_alignment=alignment,
    )

    assert runtime.system_alignment is alignment


def test_productive_alignment_service_shares_the_runtime_worker_coordinator(
    tmp_path, monkeypatch
):
    # Worker liveness only means something when the service that claims for a
    # resource import and the route that abandons the operation consult the same
    # coordinator, so the productive graph must hand over its own instance.
    monkeypatch.setenv(
        "EMS_ADMIN_DEVELOPMENT_CATALOGUE", str(tmp_path / "missing-catalogue.json")
    )
    runtime = create_admin_runtime()

    assert (
        getattr(runtime.system_alignment, "_coordinator", None)
        is runtime.operation_coordinator
    )


def test_admin_runtime_accepts_an_injected_worker_coordinator(tmp_path):
    # The path the deterministic E2E runtime uses: build the alignment service
    # against the coordinator the runtime will hold.
    coordinator = OperationCoordinator()
    runtime = create_admin_runtime(
        release_manager=_TrackingReleaseManager(tmp_path),
        system_alignment=_FakeSystemAlignment(),
        operation_coordinator=coordinator,
    )

    assert runtime.operation_coordinator is coordinator


def test_production_server_wires_development_catalogue_source(tmp_path, monkeypatch):
    # The productive server builds its own ReleaseManager with a real development
    # catalogue source (not the test-only injected item list), so a published
    # development build appears without any test fake.
    monkeypatch.setenv(
        "EMS_ADMIN_DEVELOPMENT_CATALOGUE", str(tmp_path / "missing-catalogue.json")
    )
    runtime = create_admin_runtime()
    source = getattr(runtime.release_manager, "_development_source", None)

    assert callable(source)
    # A missing fixture source degrades to an empty group without network access.
    assert list(source()) == []


def test_setup_release_prepare_verifies_resources_without_updating_admin(tmp_path):
    # Resource preparation for an aligned Admin verifies resources only — it never
    # starts alignment (no Admin container update / launcher).
    alignment = _FakeSystemAlignment(stage="resources_verified")
    manager = _TrackingReleaseManager(tmp_path)
    srv, base = _serve(release_manager=manager)
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, payload = _request(
            f"{base}/api/setup/releases/prepare",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert alignment.prepare_calls == [
        {"requested_tag": "v0.8.0", "mode": "fresh_install"}
    ]
    # The resource-prepare endpoint must never start an Admin alignment.
    assert alignment.start_calls == []
    assert payload["status"] in {"ready", "ready_for_ems"}
    assert payload["resources_verified"] is True
    assert manager.prepare_calls == []


def test_setup_release_prepare_refuses_to_update_an_unaligned_admin(tmp_path):
    # A not-yet-aligned Admin blocks resource preparation with the alignment
    # error and never launches an Admin update from the prepare endpoint.
    alignment = _FakeSystemAlignment(stage="admin_update_pending")
    manager = _TrackingReleaseManager(tmp_path)
    srv, base = _serve(release_manager=manager)
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, payload = _request(
            f"{base}/api/setup/releases/prepare",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_build_alignment_required"
    assert alignment.start_calls == []
    assert manager.prepare_calls == []


def test_setup_release_prepare_authorizes_development_build_without_ack(tmp_path):
    alignment = _FakeSystemAlignment(stage="resources_verified")
    manager = _TrackingReleaseManager(tmp_path)
    srv, base = _serve(release_manager=manager)
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        accepted_status, _, accepted = _request(
            f"{base}/api/setup/releases/prepare",
            method="POST",
            body={"tag": _DEV_BUILD_TAG, "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert accepted_status == 200
    assert accepted["resources_verified"] is True
    # Selecting the build server-side authorises it; no browser acknowledgement.
    assert alignment.development_acknowledgements == [True]


def test_development_transition_recovery_ignores_browser_acknowledgement(tmp_path):
    # A dev transition with no stored, tag-bound authorization cannot be
    # recovered — and a fresh browser acknowledgement is never trusted to grant
    # that authorization during recovery.
    class UnauthorizedDevTransition(_FakeSystemAlignment):
        def status(self, *, operation_active=None):
            result = super().status(operation_active=operation_active)
            transition = result["transition"]
            transition["system_tag"] = _DEV_BUILD_TAG
            transition["build_id"] = _DEV_BUILD_TAG
            transition["revision"] = "f7265fc747c2223f126f0ee7801e030c6226edf4"
            return result

    alignment = UnauthorizedDevTransition(stage="failed_recoverable")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        no_input_status, _, no_input = _request(
            f"{base}/api/admin/system-alignment/resume",
            method="POST",
            body={"operation_id": "op-1"},
        )
        wrong_status, _, wrong = _request(
            f"{base}/api/admin/system-alignment/resume",
            method="POST",
            body={"operation_id": "op-1", "tag": "v0.8.0"},
        )
        browser_ack_status, _, browser_ack = _request(
            f"{base}/api/admin/system-alignment/resume",
            method="POST",
            body={
                "operation_id": "op-1",
                "tag": _DEV_BUILD_TAG,
                "acknowledge_risk": True,
            },
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert no_input_status == 400
    assert no_input["error"] == "acknowledgement_required"
    assert wrong_status == 409
    assert wrong["error"] == "transition_context_mismatch"
    # The browser-provided acknowledgement does not authorize recovery.
    assert browser_ack_status == 400
    assert browser_ack["error"] == "acknowledgement_required"


def test_development_transition_recovery_uses_stored_ack(tmp_path):
    class AuthorizedDevTransition(_FakeSystemAlignment):
        def status(self, *, operation_active=None):
            result = super().status(operation_active=operation_active)
            result["transition"].update(
                {
                    "system_tag": _DEV_BUILD_TAG,
                    "build_id": _DEV_BUILD_TAG,
                    "revision": "f7265fc747c2223f126f0ee7801e030c6226edf4",
                    "development_risk_acknowledged": True,
                    "development_risk_acknowledged_for_tag": _DEV_BUILD_TAG,
                }
            )
            return result

    alignment = AuthorizedDevTransition(stage="failed_recoverable")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        # No browser acknowledgement: the stored, tag-bound authorization is used.
        status, _, payload = _request(
            f"{base}/api/admin/system-alignment/resume",
            method="POST",
            body={"operation_id": "op-1", "tag": _DEV_BUILD_TAG},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["stage"] == "ems_operation_pending"


def test_development_admin_reconnect_uses_stored_ack_without_checkbox(tmp_path):
    class DevelopmentReconnect(_FakeSystemAlignment):
        def status(self, *, operation_active=None):
            result = super().status(operation_active=operation_active)
            transition = result["transition"]
            transition.update(
                {
                    "system_tag": _DEV_BUILD_TAG,
                    "build_id": _DEV_BUILD_TAG,
                    "development_risk_acknowledged": True,
                    "development_risk_acknowledged_for_tag": _DEV_BUILD_TAG,
                }
            )
            return result

    alignment = DevelopmentReconnect(stage="admin_reconnect_pending")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/system-alignment/resume",
            method="POST",
            body={"operation_id": "op-1", "tag": _DEV_BUILD_TAG},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["stage"] == "resources_verified"
    assert alignment.resume_calls == [{"operation_id": "op-1", "extra": {}}]
    assert alignment.verify_calls == ["op-1"]


def test_setup_update_admin_revalidates_and_starts_alignment_with_reconnect(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_update_pending")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, payload = _request(
            f"{base}/api/setup/system-build/update-admin",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 202
    assert payload["status"] == "admin_alignment_started"
    assert payload["reconnect"] is True
    assert payload["operation_id"] == "op-1"
    # The transition/service is created by the alignment start (not legacy update).
    assert alignment.start_calls == [
        {"requested_tag": "v0.8.0", "mode": "fresh_install"}
    ]


def test_setup_update_admin_authorizes_development_build_without_ack(tmp_path):
    # An Experimental build no longer needs a checkbox: with a valid setup intent
    # the update starts, and the server authorises the transition itself.
    alignment = _FakeSystemAlignment(stage="admin_update_pending")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, _payload = _request(
            f"{base}/api/setup/system-build/update-admin",
            method="POST",
            body={"tag": _DEV_BUILD_TAG, "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 202
    assert alignment.start_calls == [
        {"requested_tag": _DEV_BUILD_TAG, "mode": "fresh_install"}
    ]
    assert alignment.development_acknowledgements == [True]


def test_setup_update_admin_requires_session_and_csrf(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_update_pending")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    url = f"{base}/api/setup/system-build/update-admin"
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        body = {"tag": "v0.8.0", "setup_workflow_id": workflow_id}
        unauthenticated, _, _ = raw_request(url, method="POST", body=body)
        cookie_only = auth_headers(url, "GET")
        cookie_only["X-Setup-Intent-ID"] = intent_id
        no_csrf, _, _ = raw_request(url, method="POST", body=body, headers=cookie_only)
        authenticated, _, _ = _request(
            url,
            method="POST",
            body=body,
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert unauthenticated == 401
    assert no_csrf == 403
    assert authenticated == 202


def test_automated_setup_prepare_uses_the_same_alignment_service(tmp_path):
    alignment = _FakeSystemAlignment(stage="resources_verified")
    manager = _TrackingReleaseManager(tmp_path)
    srv, base = _serve(release_manager=manager)
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, payload = _request(
            f"{base}/api/setup/automated/releases/prepare",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["status"] in {"ready", "ready_for_ems"}
    assert alignment.prepare_calls == [
        {"requested_tag": "v0.8.0", "mode": "automated_setup"}
    ]
    assert alignment.start_calls == []
    assert manager.prepare_calls == []


def test_development_pair_validation_needs_no_acknowledgement(tmp_path):
    alignment = _FakeSystemAlignment()
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/system-alignment/validate",
            method="POST",
            body={"tag": _DEV_BUILD_TAG},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert alignment.validate_calls == [_DEV_BUILD_TAG]
    assert payload["checks"] == {
        "admin_image_available": True,
        "ems_image_available": True,
        "revision_matches": True,
        "build_id_matches": True,
        "channel_matches": True,
        "embedded_resources_match": True,
    }


def test_system_build_validation_returns_alignment_button_state(tmp_path):
    alignment = _FakeSystemAlignment()
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/system-alignment/validate",
            method="POST",
            body={"tag": "v0.8.0"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["valid"] is True
    assert payload["validation_state"] == "valid"
    assert payload["selected_tag"] == "v0.8.0"
    assert payload["alignment"] == "aligned"
    assert payload["admin_update_required"] is False
    assert payload["embedded_resources_valid"] is True
    assert payload["resources_verified"] is False
    assert payload["next_allowed"] is True
    assert alignment.start_calls == []
    assert alignment.prepare_calls == []
    assert set(payload["summary"]) == {
        "channel",
        "revision",
        "build_id",
        "admin_image",
        "ems_image",
    }


def test_stable_build_validation_does_not_require_acknowledgement(tmp_path):
    alignment = _FakeSystemAlignment()
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/system-alignment/validate",
            method="POST",
            body={"tag": "v0.8.0"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert alignment.validate_calls == ["v0.8.0"]
    assert payload["next_allowed"] is True


def test_setup_system_build_confirm_creates_one_idempotent_operation(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        first_status, _, first = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.8.0", "acknowledge_risk": False,
                  "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
        # A one-shot intent authorizes exactly one mutation, so the idempotent
        # re-confirm needs its own fresh confirmation.
        _same, second_intent = _setup_build_authority(srv, base)
        second_status, _, second = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.8.0", "acknowledge_risk": False,
                  "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": second_intent},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert first_status == second_status == 200
    assert first["operation_id"] == second["operation_id"] == "op-1"
    assert first["resources_verified"] is True
    assert alignment.confirm_calls == [
        {"requested_tag": "v0.8.0", "mode": "fresh_install"},
        {"requested_tag": "v0.8.0", "mode": "fresh_install"},
    ]


def test_setup_intent_is_consumed_after_the_first_mutation(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        first_status, _, _ = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
        second_status, _, second = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert first_status == 200
    assert second_status == 409
    assert second["error"] == "setup_intent_consumed"
    # The mutating service ran exactly once — the reused intent never reached it.
    assert alignment.confirm_calls == [
        {"requested_tag": "v0.8.0", "mode": "fresh_install"}
    ]


def test_setup_intent_stays_consumed_even_when_the_mutation_fails(tmp_path):
    class FailingConfirm(_FakeSystemAlignment):
        def confirm_setup_build(
            self, *, requested_tag, mode, development_risk_acknowledged=False,
            pre_launch=None,
        ):
            raise SystemAlignmentError(
                "transition_context_mismatch",
                "the active transition targets another System Build",
            )

    alignment = FailingConfirm(stage="resources_verified")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        first_status, _, _ = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.9.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
        second_status, _, second = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.9.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    # The first mutation failed, but the intent is not re-released: a fresh Fresh
    # Setup confirmation is required, which is safer than reusing a partly-used one.
    assert first_status == 409
    assert second_status == 409
    assert second["error"] == "setup_intent_consumed"


def test_parallel_confirm_and_update_admin_share_one_intent(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        results = {}
        barrier = threading.Barrier(2)

        def fire(key, path):
            barrier.wait()
            status, _, payload = _request(
                f"{base}{path}",
                method="POST",
                body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
                extra_headers={"X-Setup-Intent-ID": intent_id},
            )
            results[key] = (status, payload)

        threads = [
            threading.Thread(
                target=fire,
                args=("confirm", "/api/setup/system-build/confirm"),
            ),
            threading.Thread(
                target=fire,
                args=("update", "/api/setup/system-build/update-admin"),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
    finally:
        srv.shutdown()
        srv.server_close()

    statuses = sorted(status for status, _ in results.values())
    # Exactly one request proceeds. The loser is refused either because the
    # winner already spent the one-shot intent, or because the winner still owns
    # the workflow's lifecycle claim — never because both were allowed to run.
    assert statuses[0] in {200, 202}
    refused = [payload for status, payload in results.values() if status == 409]
    assert len(refused) == 1
    assert refused[0]["error"] in {
        "setup_intent_consumed",
        "setup_operation_in_progress",
    }


def test_setup_system_build_confirm_authorizes_development_build_without_ack(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, payload = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": _DEV_BUILD_TAG, "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["resources_verified"] is True
    assert alignment.confirm_calls == [
        {"requested_tag": _DEV_BUILD_TAG, "mode": "fresh_install"}
    ]
    assert alignment.development_acknowledgements == [True]


@pytest.mark.parametrize(
    "tag",
    ["v0.8.0", "v0.9.0-rc.1", _DEV_BUILD_TAG],
)
def test_fresh_install_confirm_starts_without_acknowledgement(tmp_path, tag):
    # Stable, Unstable (rc) and Experimental (development) builds all confirm from
    # Fresh Setup with only a valid setup intent — never a risk acknowledgement.
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, payload = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": tag, "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["resources_verified"] is True
    assert alignment.confirm_calls == [{"requested_tag": tag, "mode": "fresh_install"}]


def test_fresh_install_confirm_still_validates_build_server_side(tmp_path):
    # Dropping the acknowledgement never weakens server-side validation: a
    # floating development alias is still refused before any transition starts.
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, payload = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": _DEV_FLOATING_TAG, "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 400
    assert payload["error"] == "system_build_dev_floating"
    assert alignment.confirm_calls == []


def test_development_confirm_does_not_reauthorize_a_consumed_setup_intent(tmp_path):
    # The one-shot setup intent authorises exactly one mutation even for an
    # Experimental build; a repeated confirm never revives a consumed intent.
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        first_status, _, _ = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": _DEV_BUILD_TAG, "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
        second_status, _, second = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": _DEV_BUILD_TAG, "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert first_status == 200
    assert second_status == 409
    assert second["error"] == "setup_intent_consumed"
    assert len(alignment.confirm_calls) == 1


def test_confirm_reports_a_different_active_transition_context(tmp_path):
    class MismatchedTransition(_FakeSystemAlignment):
        def confirm_setup_build(
            self, *, requested_tag, mode, development_risk_acknowledged=False,
            pre_launch=None,
        ):
            raise SystemAlignmentError(
                "transition_context_mismatch",
                "the active transition targets another System Build",
            )

    alignment = MismatchedTransition(stage="resources_verified")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _setup_build_authority(srv, base)
        status, _, payload = _request(
            f"{base}/api/setup/system-build/confirm",
            method="POST",
            body={"tag": "v0.9.0", "acknowledge_risk": False, "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "transition_context_mismatch"
    assert payload["transition"]["system_tag"] == "v0.8.0"


@pytest.mark.parametrize(
    "body, expected_error",
    [
        # A floating development alias is never an install target: server-side
        # validation still rejects it even though no acknowledgement is required.
        ({"tag": _DEV_FLOATING_TAG}, "system_build_dev_floating"),
        (
            {
                "tag": _DEV_BUILD_TAG,
                "admin_image": "ghcr.io/evil/admin:latest",
            },
            "unsupported_field",
        ),
    ],
)
def test_development_pair_validation_rejects_unsafe_requests(
    tmp_path, body, expected_error
):
    alignment = _FakeSystemAlignment()
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/system-alignment/validate",
            method="POST",
            body=body,
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 400
    assert payload.get("error") == expected_error


def test_setup_config_compose_and_ems_are_blocked_before_resources_verified(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    deployment = _FakeDeployment()
    manager = _TrackingReleaseManager(tmp_path)
    srv, base = _serve(release_manager=manager, deployment=deployment)
    _attach_system_alignment(srv, alignment)
    try:
        config_status, _, config_payload = _request(
            f"{base}/api/setup/config/write",
            method="POST",
            body=_authorized_body(base, _control_export_body()),
        )
        compose_status, _, compose_payload = _request(
            f"{base}/api/setup/deployment/prepare",
            method="POST",
            body=_setup_body(srv),
        )
        ems_status, _, ems_payload = _request(
            f"{base}/api/setup/deployment/start",
            method="POST",
            body=_setup_body(srv),
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert (config_status, compose_status, ems_status) == (409, 409, 409)
    for payload in (config_payload, compose_payload, ems_payload):
        assert payload["error"] == "system_alignment_incomplete"
        assert payload["transition"]["stage"] == "admin_aligned"
    assert not list(tmp_path.rglob("generated/config.json"))
    assert deployment.prepared_overwrite is None
    assert deployment.start_calls == 0


def test_fresh_setup_deployment_worker_finishes_before_terminal_job_poll(tmp_path):
    alignment = _FakeSystemAlignment(stage="resources_verified")
    deployment = _FakeDeployment()
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        deployment=deployment,
    )
    _attach_system_alignment(srv, alignment)
    try:
        start_status, _, started = _request(
            f"{base}/api/setup/deployment/start", method="POST", body=_setup_body(srv)
        )
        assert start_status == 202
        assert started["job_id"] == "start-1"
        assert alignment.stage == "completed"

        job_status, _, job = _request(
            f"{base}/api/setup/deployment/start/jobs/start-1"
        )
        assert job_status == 200
        assert job["status"] == "succeeded"
        assert alignment.stage == "completed"

        # Repeated status polling is read-only with respect to committed stages.
        again_status, _, _ = _request(
            f"{base}/api/setup/deployment/start/jobs/start-1"
        )
        assert again_status == 200
    finally:
        srv.shutdown()
        srv.server_close()

    assert alignment.ems_calls == [
        ("begin", "op-1"),
        ("claim", "op-1"),
        ("finish", "op-1", True, None),
    ]
    assert alignment.health_calls == [("op-1", True, None)]


def test_recoverable_pending_ems_stage_can_be_completed_after_restart(tmp_path):
    alignment = _FakeSystemAlignment(stage="ems_operation_pending")
    deployment = _FakeDeployment()
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        deployment=deployment,
    )
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/deployment/start", method="POST", body=_setup_body(srv)
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 202
    assert payload["job_id"] == "start-1"
    assert deployment.start_calls == 1
    assert alignment.stage == "completed"


@pytest.mark.parametrize(
    ("failure", "expected_ems_success", "expected_health"),
    (
        ("start_rejection", False, []),
        ("start_failed", False, []),
        ("dashboard_unreachable", True, [("op-1", False, "healthcheck_failed")]),
    ),
)
def test_fresh_setup_deployment_failures_remain_recoverable(
    tmp_path, failure, expected_ems_success, expected_health
):
    alignment = _FakeSystemAlignment(stage="resources_verified")
    deployment = _FakeDeployment()
    setattr(deployment, failure, True if failure != "start_rejection" else "not_ready")
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        deployment=deployment,
    )
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/deployment/start", method="POST", body=_setup_body(srv)
        )
        if failure == "start_rejection":
            assert status == 409
        else:
            assert status == 202
            status, _, payload = _request(
                f"{base}/api/setup/deployment/start/jobs/start-1"
            )
            assert status == 200
    finally:
        srv.shutdown()
        srv.server_close()

    assert alignment.stage == "failed_recoverable"
    finish = [call for call in alignment.ems_calls if call[0] == "finish"]
    assert finish and finish[-1][2] is expected_ems_success
    assert alignment.health_calls == expected_health


# The verified selection fingerprint a confirmed Guided Upgrade must submit.
_UPGRADE_FINGERPRINT = _FakeSystemAlignment.selection_fingerprint(
    _FakeSystemAlignment._system_build("v0.8.0")
)
_DEV_UPGRADE_FINGERPRINT = _FakeSystemAlignment.selection_fingerprint(
    _FakeSystemAlignment._system_build(_DEV_BUILD_TAG)
)


def test_guided_upgrade_uses_alignment_and_does_not_reach_legacy_executor_early(tmp_path):
    class _ResourceMismatchAlignment(_FakeSystemAlignment):
        def verify_resources(self, *, operation_id):
            raise SystemAlignmentError(
                "system_build_resources_invalid",
                "embedded resources do not match the running Admin",
            )

    alignment = _ResourceMismatchAlignment(stage="admin_aligned", active=False)
    executor = _AlignmentGatedUpgrade()
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        guided_upgrade=executor,
    )
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/maintenance/upgrade/execute",
            method="POST",
            body={"confirm": True, "target_release": "v0.8.0", "options": {}, "selection_fingerprint": _UPGRADE_FINGERPRINT},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_build_resources_invalid"
    assert alignment.start_calls == [
        {"requested_tag": "v0.8.0", "mode": "guided_upgrade"}
    ]
    assert len(executor.preflight_calls) == 1
    assert len(executor.prepare_calls) == 1
    assert executor.run_calls == []


def test_guided_upgrade_hands_off_only_after_alignment_resources_verified(tmp_path):
    alignment = _FakeSystemAlignment(stage="resources_verified", active=False)
    executor = _AlignmentGatedUpgrade()
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        guided_upgrade=executor,
    )
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/maintenance/upgrade/execute",
            method="POST",
            body={"confirm": True, "target_release": "v0.8.0", "options": {}, "selection_fingerprint": _UPGRADE_FINGERPRINT},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 202
    assert payload["job_id"]
    assert alignment.start_calls == [
        {"requested_tag": "v0.8.0", "mode": "guided_upgrade"}
    ]
    assert len(executor.preflight_calls) == 1
    assert len(executor.prepare_calls) == 1
    assert executor.run_called.wait(2)
    assert len(executor.run_calls) == 1


def test_guided_upgrade_still_requires_acknowledgement_for_development_builds(tmp_path):
    # Guided Upgrade policy is unchanged by the Fresh Setup simplification: an
    # Experimental target still needs an explicit acknowledgement to execute.
    alignment = _FakeSystemAlignment(stage="admin_aligned", active=False)
    executor = _AlignmentGatedUpgrade()
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        guided_upgrade=executor,
    )
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/maintenance/upgrade/execute",
            method="POST",
            body={"confirm": True, "target_release": _DEV_BUILD_TAG, "options": {}, "selection_fingerprint": _DEV_UPGRADE_FINGERPRINT},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 400
    assert payload["error"] == "acknowledgement_required"
    assert alignment.start_calls == []
    assert executor.run_calls == []


class _ReasonUpgrade(_AlignmentGatedUpgrade):
    def __init__(self, reason, message="pull failed"):
        super().__init__()
        self._reason = reason
        self._message = message

    def run(self, run_context, *, pre_alignment, progress):
        self.run_calls.append((run_context, pre_alignment, progress))
        self.run_called.set()
        return {
            "ok": False,
            "status": "failed",
            "reason": self._reason,
            "message": self._message,
            "steps": [],
            "warnings": [],
        }


def _run_failing_guided_upgrade(tmp_path, executor):
    alignment = _FakeSystemAlignment(stage="resources_verified", active=False)
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        guided_upgrade=executor,
    )
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/maintenance/upgrade/execute",
            method="POST",
            body={
                "confirm": True,
                "target_release": "v0.8.0",
                "options": {},
                "selection_fingerprint": _UPGRADE_FINGERPRINT,
            },
        )
        assert status == 202, payload
        finishes = _wait_for(
            lambda: [call for call in alignment.ems_calls if call[0] == "finish"]
        )
    finally:
        srv.shutdown()
        srv.server_close()
    assert finishes, "finish_ems_operation was never called"
    return finishes[-1]


@pytest.mark.parametrize(
    "reason",
    [
        "system_build_registry_rate_limited",
        "image_pull_rate_limited",
        "image_pull_network_error",
        "image_pull_failed",
        "target_digest_mismatch",
    ],
)
def test_guided_upgrade_preserves_trusted_failure_reason_in_transition(tmp_path, reason):
    finish = _run_failing_guided_upgrade(tmp_path, _ReasonUpgrade(reason))

    assert finish[2] is False
    assert finish[3] == reason


def test_guided_upgrade_normalizes_untrusted_failure_reason(tmp_path):
    finish = _run_failing_guided_upgrade(tmp_path, _ReasonUpgrade("something_untrusted"))

    assert finish[2] is False
    assert finish[3] == "ems_upgrade_failed"


def test_system_alignment_status_resume_and_return_routes_are_productive(tmp_path):
    alignment = _FakeSystemAlignment(stage="failed_recoverable")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status_code, _, status_payload = _request(
            f"{base}/api/admin/system-alignment/status"
        )
        resume_code, _, resume_payload = _request(
            f"{base}/api/admin/system-alignment/resume",
            method="POST",
            body={"operation_id": "op-1"},
        )
        return_code, _, return_payload = _request(
            f"{base}/api/admin/system-alignment/return-to-running-build",
            method="POST",
            body={"operation_id": "op-1", "confirm": True},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status_code == resume_code == return_code == 200
    assert status_payload["transition"]["stage"] == "failed_recoverable"
    assert status_payload["transition"]["resume_available"] is True
    assert status_payload["known_good"]["system_tag"] == "v0.7.0"
    assert resume_payload["stage"] == "ems_operation_pending"
    assert return_payload["target_system_tag"] == "v0.7.0"
    assert alignment.resume_calls == []
    assert alignment.return_calls == [{"operation_id": "op-1", "confirm": True}]


def test_system_alignment_resource_verification_has_productive_route(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_aligned")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/system-alignment/verify-resources",
            method="POST",
            body={"operation_id": "op-1"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["stage"] == "resources_verified"
    assert alignment.verify_calls == ["op-1"]


def test_system_alignment_status_read_requires_session_authentication(tmp_path):
    alignment = _FakeSystemAlignment(stage="failed_recoverable")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    url = f"{base}/api/admin/system-alignment/status"
    try:
        unauthenticated, _, _ = raw_request(url)
        authenticated, _, payload = _request(url)
    finally:
        srv.shutdown()
        srv.server_close()

    assert unauthenticated == 401
    assert authenticated == 200
    assert payload["transition"]["operation_id"] == "op-1"


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/admin/system-alignment/resume", {"operation_id": "op-1"}),
        (
            "/api/admin/system-alignment/verify-resources",
            {"operation_id": "op-1"},
        ),
        (
            "/api/admin/system-alignment/return-to-running-build",
            {"operation_id": "op-1", "confirm": True},
        ),
        (
            "/api/admin/system-alignment/validate",
            {"tag": _DEV_BUILD_TAG, "acknowledge_risk": True},
        ),
    ],
)
def test_system_alignment_mutations_keep_session_and_csrf_protection(
    tmp_path, path, body
):
    alignment = _FakeSystemAlignment(stage="failed_recoverable")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    url = base + path
    try:
        unauthenticated, _, _ = raw_request(url, method="POST", body=body)
        cookie_only = auth_headers(url, "GET")
        no_csrf, _, _ = raw_request(
            url, method="POST", body=body, headers=cookie_only
        )
        authenticated, _, _ = _request(url, method="POST", body=body)
    finally:
        srv.shutdown()
        srv.server_close()

    assert unauthenticated == 401
    assert no_csrf == 403
    assert authenticated in {200, 202, 409}


@pytest.mark.parametrize(
    "stage, active, blocked",
    [
        ("resources_verified", True, False),
        ("admin_aligned", True, True),
        ("admin_update_pending", True, True),
        ("admin_reconnect_pending", True, True),
        ("failed_recoverable", True, True),
        ("completed", False, True),
    ],
    ids=[
        "verified-permits",
        "aligned-not-verified-blocks",
        "update-pending-blocks",
        "reconnect-blocks",
        "failed-blocks",
        "completed-blocks",
    ],
)
def test_guided_setup_discovery_gate_requires_system_build_alignment(
    tmp_path, stage, active, blocked
):
    alignment = _FakeSystemAlignment(stage=stage, active=active)
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        run_status, _, run_payload = _request(
            f"{base}/api/setup/discovery/run",
            method="POST",
            body={"refresh": True},
            extra_headers={"X-Setup-Operation-ID": "op-1"},
        )
        prep_status, _, prep_payload = _request(
            f"{base}/api/setup/discovery/preparation",
            method="POST",
            body={},
            extra_headers={"X-Setup-Operation-ID": "op-1"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    for status, payload in ((run_status, run_payload), (prep_status, prep_payload)):
        if blocked:
            assert status == 409
            assert payload["error"] == "system_alignment_incomplete"
            assert payload.get("return_to") == "system_build"
        else:
            assert status == 200


def test_guided_setup_discovery_gate_keeps_read_only_status_available(tmp_path):
    # An unaligned setup transition must not block read-only discovery status.
    alignment = _FakeSystemAlignment(stage="admin_update_pending", active=True)
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, _ = _request(f"{base}/api/discovery/preparation")
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200


def test_guided_setup_discovery_gate_blocks_mqtt_and_broker_writes(tmp_path):
    alignment = _FakeSystemAlignment(stage="failed_recoverable", active=True)
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        broker_status, _, broker_payload = _request(
            f"{base}/api/setup/discovery/connections/mqtt-brokers",
            method="POST",
            body={"host": "192.168.1.60", "port": 1883},
            extra_headers={"X-Setup-Operation-ID": "op-1"},
        )
        mdns_status, _, mdns_payload = _request(
            f"{base}/api/setup/discovery/mdns/refresh",
            method="POST",
            body={},
            extra_headers={"X-Setup-Operation-ID": "op-1"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert broker_status == 409
    assert broker_payload["error"] == "system_alignment_incomplete"
    assert mdns_status == 409
    assert mdns_payload["error"] == "system_alignment_incomplete"


def test_guided_setup_discovery_gate_returns_to_step_one(tmp_path):
    # A refusal names Step 1 explicitly so the browser can send the user back.
    alignment = _FakeSystemAlignment(stage="admin_update_pending", active=True)
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/discovery/run",
            method="POST",
            body={"refresh": True},
            extra_headers={"X-Setup-Operation-ID": "op-1"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_alignment_incomplete"
    assert payload.get("return_to") == "system_build"
    assert payload.get("return_to_step") == 1


def test_discovery_gate_fails_closed_on_internal_gate_error(tmp_path):
    # An alignment service that raises while reporting its state must block the
    # discovery write, never silently allow it (fail-closed, not fail-open).
    class _RaisingAlignment(_FakeSystemAlignment):
        def validate_setup_discovery_operation(self, *, operation_id):
            raise RuntimeError("transition state unreadable")

    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, _RaisingAlignment())
    try:
        status, _, payload = _request(
            f"{base}/api/setup/discovery/run",
            method="POST",
            body={"refresh": True},
            extra_headers={"X-Setup-Operation-ID": "op-1"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_alignment_incomplete"


def test_discovery_gate_fails_closed_when_alignment_state_unavailable(tmp_path):
    # A misconfigured alignment service missing the gate predicates must block the
    # discovery write rather than defaulting open.
    class _NoGateMethods:
        @staticmethod
        def status(*, operation_active=None):
            return {"ok": True, "active": False, "transition": None, "known_good": None}

    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, _NoGateMethods())
    try:
        status, _, payload = _request(
            f"{base}/api/setup/discovery/run",
            method="POST",
            body={"refresh": True},
            extra_headers={"X-Setup-Operation-ID": "op-1"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_alignment_incomplete"


@pytest.mark.parametrize("operation_id", [None, "wrong-operation"])
def test_setup_discovery_requires_the_confirmed_operation_id(tmp_path, operation_id):
    alignment = _FakeSystemAlignment(stage="resources_verified", active=True)
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    headers = {"X-Setup-Operation-ID": operation_id} if operation_id else {}
    try:
        status, _, payload = _request(
            f"{base}/api/setup/discovery/run",
            method="POST",
            body={"refresh": True},
            extra_headers=headers,
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] in {"setup_operation_required", "operation_mismatch"}


def test_maintenance_discovery_remains_available_without_setup_operation(tmp_path):
    alignment = _FakeSystemAlignment(stage="failed_recoverable", active=True)
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/discovery/run", method="POST", body={"refresh": True}
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload.get("error") is None


# --- Maintenance discovery credentials: independent of Setup operations -------
# Maintenance manages discovery credentials for an existing installation via the
# generic /api/discovery routes. Those routes require the Admin session and CSRF
# only — they must never consult the Guided Setup operation validator, so they
# keep working after Setup transition state was completed, cleaned up or deleted
# during recovery. The /api/setup/discovery aliases keep their confirmed-
# operation gate unchanged.


class _RejectingAlignment(_FakeSystemAlignment):
    """Alignment double for a system without usable Setup transition state.

    Every Setup discovery operation is refused (the live failure mode after
    transition JSON files were removed); the counter proves generic Maintenance
    routes never even consult the validator.
    """

    def __init__(self):
        super().__init__(stage="failed_recoverable", active=False)
        self.discovery_validation_calls = 0

    def validate_setup_discovery_operation(self, *, operation_id):
        self.discovery_validation_calls += 1
        raise SystemAlignmentError(
            "setup_operation_required", "a confirmed Setup operation id is required"
        )


def test_maintenance_zendure_credential_lifecycle_needs_no_setup_operation(tmp_path):
    alignment = _RejectingAlignment()
    discovery = _cloud_discovery(tmp_path)
    srv, base = _serve(zendure_cloud_discovery=discovery)
    _attach_system_alignment(srv, alignment)
    base_path = f"{base}/api/discovery/zendure-cloud-mqtt"
    try:
        test_status, _, tested = _request(
            f"{base_path}/test", method="POST", body={"api_key": _CLOUD_API_KEY}
        )
        save_status, _, saved = _request(
            f"{base_path}/token", method="POST", body={"api_key": _CLOUD_API_KEY}
        )
        refresh_status, _, refreshed = _request(f"{base_path}/refresh", method="POST")
        delete_status, _, deleted = _request(f"{base_path}/token", method="DELETE")
    finally:
        srv.shutdown()
        srv.server_close()

    assert test_status == 200 and tested["ok"] is True
    assert save_status == 200 and saved["token_saved"] is True
    assert refresh_status == 200 and refreshed["ok"] is True
    assert delete_status == 200 and deleted["token_saved"] is False
    assert alignment.discovery_validation_calls == 0
    for payload in (tested, saved, refreshed, deleted):
        assert _CLOUD_API_KEY not in json.dumps(payload)


def test_maintenance_local_mqtt_credential_lifecycle_needs_no_setup_operation(
    tmp_path,
):
    alignment = _RejectingAlignment()
    mqtt = MqttBrokerDiscovery(connector=lambda host, port, timeout: port == 1883)
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        mqtt_discovery=mqtt,
        mdns_provider=_FakeMdnsProvider(),
    )
    _attach_system_alignment(srv, alignment)
    try:
        save_status, _, saved = _request(
            f"{base}/api/discovery/connections/mqtt-credentials",
            method="POST",
            body={
                "label": "Maintenance broker",
                "username": "svc",
                "password": "maintenance-secret",
            },
        )
        credentials = (saved.get("local_mqtt") or {}).get("credentials") or []
        assert credentials, saved
        credential_id = credentials[0]["id"]
        probe_status, _, probed = _request(
            f"{base}/api/discovery/mqtt-brokers/probe",
            method="POST",
            body={"cidr": "192.168.178.10/32"},
        )
        refresh_status, _, refreshed = _request(
            f"{base}/api/discovery/mqtt-brokers/refresh", method="POST"
        )
        delete_status, _, deleted = _request(
            f"{base}/api/discovery/connections/mqtt-credentials/{credential_id}",
            method="DELETE",
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert save_status == 200
    assert probe_status == 200 and probed["found"] == 1
    assert refresh_status == 200
    assert delete_status == 200 and deleted["ok"] is True
    assert alignment.discovery_validation_calls == 0
    assert "maintenance-secret" not in json.dumps(saved)


@pytest.mark.parametrize(
    ("method", "alias_path", "body", "confirmed_status"),
    (
        ("POST", "/api/setup/discovery/zendure-cloud-mqtt/test", {"api_key": "k"}, 200),
        ("POST", "/api/setup/discovery/zendure-cloud-mqtt/token", {"api_key": "k"}, 200),
        # No token is saved in this fresh store, so the handler itself answers
        # not_configured once the operation gate has passed.
        ("POST", "/api/setup/discovery/zendure-cloud-mqtt/refresh", {}, 400),
        ("DELETE", "/api/setup/discovery/zendure-cloud-mqtt/token", None, 200),
        (
            "POST",
            "/api/setup/discovery/connections/mqtt-credentials",
            {"label": "L", "username": "u", "password": "p"},
            200,
        ),
        ("DELETE", "/api/setup/discovery/connections/mqtt-credentials/l", None, 200),
        (
            "POST",
            "/api/setup/discovery/mqtt-brokers/probe",
            {"cidr": "192.168.178.10/32"},
            200,
        ),
        ("POST", "/api/setup/discovery/mqtt-brokers/refresh", {}, 200),
    ),
    ids=(
        "zendure-test",
        "zendure-token-save",
        "zendure-refresh",
        "zendure-token-delete",
        "mqtt-credential-save",
        "mqtt-credential-delete",
        "mqtt-broker-probe",
        "mqtt-broker-refresh",
    ),
)
def test_setup_discovery_credential_aliases_keep_the_operation_gate(
    tmp_path, method, alias_path, body, confirmed_status
):
    # Connectivity probes included: the setup alias of every discovery write is
    # operation-gated because probe/test persist discovery store state.
    alignment = _FakeSystemAlignment(stage="resources_verified", active=True)
    srv, base = _serve(
        zendure_cloud_discovery=_cloud_discovery(tmp_path),
        mqtt_discovery=MqttBrokerDiscovery(
            connector=lambda host, port, timeout: port == 1883
        ),
        mdns_provider=_FakeMdnsProvider(),
    )
    _attach_system_alignment(srv, alignment)
    try:
        missing_status, _, missing = _request(
            f"{base}{alias_path}", method=method, body=body
        )
        mismatch_status, _, mismatched = _request(
            f"{base}{alias_path}",
            method=method,
            body=body,
            extra_headers={"X-Setup-Operation-ID": "someone-elses-operation"},
        )
        gated_status, _, gated = _request(
            f"{base}{alias_path}",
            method=method,
            body=body,
            extra_headers={"X-Setup-Operation-ID": "op-1"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert missing_status == 409
    assert missing["error"] == "setup_operation_required"
    assert missing.get("return_to") == "system_build"
    assert mismatch_status == 409
    assert mismatched["error"] == "operation_mismatch"
    assert gated_status == confirmed_status, gated
    assert gated.get("error") not in {
        "setup_operation_required",
        "operation_mismatch",
        "system_alignment_incomplete",
    }


def test_no_dormant_discovery_diagnostic_allowlist_remains():
    # The one-time _DISCOVERY_DIAGNOSTIC_PATHS allowlist was never enforced and
    # its endpoints are not read-only (broker probe merges candidates, the
    # Zendure test persists status metadata), so the Setup alias gates them like
    # every other discovery write; Maintenance uses the generic routes instead.
    # An unused security-policy constant must not linger and suggest otherwise.
    from admin import server as server_module

    assert not hasattr(server_module, "_DISCOVERY_DIAGNOSTIC_PATHS")


def test_setup_discovery_credential_alias_rejects_unverified_transition(tmp_path):
    alignment = _FakeSystemAlignment(stage="admin_aligned", active=True)
    srv, base = _serve(zendure_cloud_discovery=_cloud_discovery(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            f"{base}/api/setup/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={"api_key": "k"},
            extra_headers={"X-Setup-Operation-ID": "op-1"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_alignment_incomplete"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    (
        ("POST", "/api/discovery/zendure-cloud-mqtt/test", {"api_key": "k"}),
        ("POST", "/api/discovery/zendure-cloud-mqtt/token", {"api_key": "k"}),
        ("DELETE", "/api/discovery/zendure-cloud-mqtt/token", None),
        (
            "POST",
            "/api/discovery/connections/mqtt-credentials",
            {"label": "L", "username": "u", "password": "p"},
        ),
        ("DELETE", "/api/discovery/connections/mqtt-credentials/l", None),
        (
            "POST",
            "/api/discovery/mqtt-brokers/probe",
            {"cidr": "192.168.178.10/32"},
        ),
    ),
    ids=(
        "zendure-test",
        "zendure-token-save",
        "zendure-token-delete",
        "mqtt-credential-save",
        "mqtt-credential-delete",
        "mqtt-broker-probe",
    ),
)
def test_maintenance_credential_routes_still_require_auth_and_csrf(
    tmp_path, method, path, body
):
    # Setup-operation independence must not loosen the session/CSRF gate.
    srv, base = _serve(zendure_cloud_discovery=_cloud_discovery(tmp_path))
    try:
        anon_status, _, _ = raw_request(f"{base}{path}", method=method, body=body)
        cookie_only = {
            key: value
            for key, value in auth_headers(f"{base}{path}", method).items()
            if key != "X-CSRF-Token"
        }
        csrf_status, _, csrf_payload = raw_request(
            f"{base}{path}", method=method, body=body, headers=cookie_only
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert anon_status in (401, 403)
    assert csrf_status == 403
    assert csrf_payload.get("error") == "csrf_failed"


def test_recovered_install_without_transition_state_supports_maintenance(
    tmp_path, monkeypatch, isolated_install_root
):
    # Real-world recovery: an existing installation (manual install, older
    # Admin, or transition/state JSON removed during recovery). The *real*
    # alignment service runs over an empty state dir, so no historical Setup
    # operation can be validated — Maintenance must still load the config,
    # test/save the Zendure credential and keep preview available.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "system": {"max_total_power": 1600},
                "devices": [
                    {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}
                ],
                "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))
    cloud_dir = tmp_path / "cloud-store"
    cloud_dir.mkdir()
    srv, base = _serve(
        system_alignment=None,
        zendure_cloud_discovery=_cloud_discovery(cloud_dir),
    )
    state_dir = Path(isolated_install_root) / "admin-data" / "state"
    assert not (state_dir / "pending-transition.json").exists()
    try:
        config_status, _, loaded = _request(f"{base}/api/admin/maintenance/config")
        test_status, _, tested = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/test",
            method="POST",
            body={"api_key": _CLOUD_API_KEY},
        )
        save_status, _, saved = _request(
            f"{base}/api/discovery/zendure-cloud-mqtt/token",
            method="POST",
            body={"api_key": _CLOUD_API_KEY},
        )
        preview_status, _, preview = _request(
            f"{base}/api/admin/maintenance/config/preview",
            method="POST",
            body={"draft": loaded["draft"]},
        )
        # The historical Setup path stays firmly closed — reproducing the exact
        # refusal Maintenance used to trip over — without blocking the above.
        alias_status, _, alias = _request(
            f"{base}/api/setup/discovery/zendure-cloud-mqtt/test",
            method="POST",
            body={"api_key": _CLOUD_API_KEY},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert config_status == 200 and loaded["status"] == "ok"
    assert loaded["summary"]["device_count"] == 1
    assert test_status == 200 and tested["ok"] is True
    assert save_status == 200 and saved["token_saved"] is True
    assert preview_status == 200
    assert preview.get("validation") is not None
    assert alias_status == 409
    assert alias["error"] == "setup_operation_required"


def test_unresolved_transition_blocks_unrelated_write_but_allows_diagnostics(
    tmp_path, monkeypatch
):
    from admin import server as server_module

    alignment = _FakeSystemAlignment(stage="failed_recoverable")
    diagnostics = _ReadOnlyDiagnostics()
    sync_calls = []
    monkeypatch.setattr(
        server_module,
        "run_maintenance_container_sync",
        lambda: sync_calls.append(True) or {"ok": True, "status": "completed"},
    )
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        ems_cli=diagnostics,
    )
    _attach_system_alignment(srv, alignment)
    try:
        write_status, _, write_payload = _request(
            f"{base}/api/admin/maintenance/containers/sync",
            method="POST",
            body={"confirm": True},
        )
        diagnostics_status, _, diagnostics_payload = _request(
            f"{base}/api/admin/maintenance/diagnostics/run",
            method="POST",
            body={},
        )
        overview_status, _, overview_payload = _request(
            f"{base}/api/admin/maintenance/overview"
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert write_status == 409
    assert write_payload["error"] == "system_transition_in_progress"
    assert write_payload["transition"]["stage"] == "failed_recoverable"
    assert sync_calls == []
    assert diagnostics_status == 200
    assert diagnostics_payload["summary"]["status"] == "ok"
    assert diagnostics.calls == 1
    assert overview_status == 200
    assert isinstance(overview_payload, dict)


@pytest.mark.parametrize(
    ("path", "body"),
    (
        (
            "/api/setup/config/write",
            {"devices": [], "supported_grid_meter_count": 0},
        ),
        (
            "/api/setup/config/apply",
            {"devices": [], "supported_grid_meter_count": 0},
        ),
        ("/api/setup/deployment/prepare", {}),
        ("/api/setup/deployment/start", {}),
        ("/api/setup/deployment/repair-permissions", {}),
        (
            "/api/setup/deployment/resolve-container-conflict",
            {
                "container_name": "ems-solarflow-api-control",
                "action": "remove_stopped_and_continue",
            },
        ),
    ),
    ids=(
        "config-write",
        "config-apply",
        "compose-prepare",
        "ems-start",
        "permission-repair",
        "container-conflict",
    ),
)
def test_setup_mutations_require_an_active_resource_verified_transition(
    tmp_path, path, body
):
    """No transition is not equivalent to a verified System Build operation."""

    alignment = _FakeSystemAlignment(stage="admin_aligned", active=False)
    deployment = _FakeDeployment()
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        deployment=deployment,
    )
    _attach_system_alignment(srv, alignment)
    try:
        if path.startswith("/api/setup/config/"):
            body = _authorized_body(base, _control_export_body())
        else:
            # Present valid workflow authority so the alignment gate — not a
            # missing workflow id — is what refuses the mutation.
            body = _setup_body(srv, **body)
        status, _, payload = _request(base + path, method="POST", body=body)
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_alignment_incomplete"
    assert payload["transition"] is None
    assert not list(tmp_path.rglob("generated/config.json"))
    assert deployment.prepared_overwrite is None
    assert deployment.start_calls == 0
    assert deployment.repair_calls == 0
    assert not hasattr(deployment, "resolved_conflict")


@pytest.mark.parametrize(
    ("stage", "active", "mode"),
    (
        ("resources_verified", True, "guided_upgrade"),
        ("completed", False, "fresh_install"),
        ("cancelled", False, "fresh_install"),
    ),
)
def test_setup_config_write_requires_current_setup_operation(
    tmp_path, stage, active, mode
):
    alignment = _FakeSystemAlignment(stage=stage, active=active, mode=mode)
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            base + "/api/setup/config/write",
            method="POST",
            body=_authorized_body(base, _control_export_body()),
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_alignment_incomplete"
    assert not list(tmp_path.rglob("generated/config.json"))


def test_fresh_deployment_worker_completes_transition_without_terminal_get_poll(
    tmp_path,
):
    class CompletingDeployment(_FakeDeployment):
        def start(self, *, on_complete=None, **kwargs):
            result = super().start(**kwargs)
            if on_complete is not None:
                on_complete(
                    {
                        "job_id": "start-1",
                        "status": "succeeded",
                        "dashboard_reachable": True,
                        "error": None,
                    }
                )
            return result

    alignment = _FakeSystemAlignment(stage="resources_verified")
    deployment = CompletingDeployment()
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        deployment=deployment,
    )
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            base + "/api/setup/deployment/start", method="POST", body=_setup_body(srv)
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 202
    assert payload["job_id"] == "start-1"
    assert alignment.stage == "completed"
    assert alignment.health_calls == [("op-1", True, None)]


def test_fresh_deployment_start_exception_leaves_recoverable_transition(tmp_path):
    class RaisingDeployment(_FakeDeployment):
        def start(self, **kwargs):
            del kwargs
            self.start_calls += 1
            raise RuntimeError("unexpected deployment launcher failure")

    alignment = _FakeSystemAlignment(stage="resources_verified")
    deployment = RaisingDeployment()
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        deployment=deployment,
    )
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            base + "/api/setup/deployment/start", method="POST", body=_setup_body(srv)
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 500
    assert payload["reason"] == "ems_deployment_unexpected_failure"
    assert alignment.stage == "failed_recoverable"
    assert alignment.ems_calls[-1] == (
        "finish",
        "op-1",
        False,
        "ems_deployment_unexpected_failure",
    )


class _TransitionGateAdminUpdate:
    def __init__(self):
        self.plan_calls = []
        self.execute_calls = []

    def plan(self, target_release):
        self.plan_calls.append(target_release)
        return {"ok": True, "plan_id": "plan-1"}

    def execute(self, plan_id, confirm):
        self.execute_calls.append((plan_id, confirm))
        return {"ok": True, "status": "admin_update_started", "reconnect": True}


class _TransitionGateRestorePlan:
    blocked = False
    block_reason = None


class _TransitionGateBackupService:
    def __init__(self):
        self.calls = []
        self.create_finished = threading.Event()
        self.plans = {"restore-1": _TransitionGateRestorePlan()}

    @staticmethod
    def plan_create_steps(scope):
        return [{"key": f"create_{scope}", "label": "Create backup"}]

    def create_backup(self, body, progress=None):
        self.calls.append(("create", body.get("scope")))
        self.create_finished.set()
        return {"ok": True, "status": "completed", "archives": []}

    def inspect_backup(self, backup_id, *, password=None):
        self.calls.append(("inspect", backup_id, password))
        return {"ok": True, "id": backup_id, "files": []}

    def diff_backup_file(self, backup_id, file_name, *, password=None):
        self.calls.append(("diff", backup_id, file_name, password))
        return {"ok": True, "id": backup_id, "file": file_name, "diff": []}

    def create_restore_plan(self, body):
        self.calls.append(("restore_preview", body.get("id"), body.get("scope")))
        return {
            "ok": True,
            "plan_id": "restore-1",
            "scope": body.get("scope"),
            "blocked": False,
        }

    @staticmethod
    def plan_restore_steps(_plan):
        return [{"key": "restore", "label": "Restore backup"}]

    def restore_from_plan(self, plan_id, *, confirm, progress=None):
        self.calls.append(("restore_execute", plan_id, confirm))
        return {"ok": True, "status": "completed"}

    def delete_backup(self, backup_id, *, confirm, mode):
        self.calls.append(("delete", backup_id, confirm, mode))
        return {"ok": True, "deleted": backup_id, "mode": mode}


@pytest.mark.parametrize(
    ("path", "body", "blocked_call"),
    (
        (
            "/api/admin/maintenance/admin-update/plan",
            {"target_release": "v0.8.0"},
            "admin_update_plan",
        ),
        (
            "/api/admin/maintenance/admin-update/execute",
            {"plan_id": "plan-1", "confirm": True},
            "admin_update",
        ),
        (
            "/api/admin/maintenance/backups/restore/execute",
            {"plan_id": "restore-1", "confirm": True},
            "restore_execute",
        ),
        (
            "/api/admin/maintenance/backups/delete",
            {"id": "backup-1", "confirm": True},
            "delete",
        ),
    ),
    ids=("admin-update-plan", "admin-update", "backup-restore", "backup-delete"),
)
def test_failed_transition_blocks_unrelated_maintenance_mutations(
    tmp_path, path, body, blocked_call
):
    alignment = _FakeSystemAlignment(stage="failed_recoverable")
    admin_update = _TransitionGateAdminUpdate()
    backups = _TransitionGateBackupService()
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        admin_update=admin_update,
        backup_service=backups,
    )
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(base + path, method="POST", body=body)
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_transition_in_progress"
    assert payload["transition"]["stage"] == "failed_recoverable"
    assert admin_update.plan_calls == []
    assert admin_update.execute_calls == []
    assert not any(call[0] == blocked_call for call in backups.calls)


def test_failed_transition_blocks_legacy_config_migration(tmp_path, monkeypatch):
    from admin import server as server_module

    calls = []
    monkeypatch.setattr(
        server_module,
        "migrate_legacy_root_config",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )
    alignment = _FakeSystemAlignment(stage="failed_recoverable")
    srv, base = _serve(release_manager=_TrackingReleaseManager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(
            base + "/api/admin/config/migrate-legacy",
            method="POST",
            body={"overwrite": True},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 409
    assert payload["error"] == "system_transition_in_progress"
    assert calls == []


@pytest.mark.parametrize(
    ("path", "body", "expected_status", "expected_call"),
    (
        (
            "/api/admin/maintenance/backups/create",
            {"scope": "config"},
            202,
            "create",
        ),
        (
            "/api/admin/maintenance/backups/inspect",
            {"id": "backup-1"},
            200,
            "inspect",
        ),
        (
            "/api/admin/maintenance/backups/diff",
            {"id": "backup-1", "file": "config/config.json"},
            200,
            "diff",
        ),
        (
            "/api/admin/maintenance/backups/restore/preview",
            {"id": "backup-1", "scope": "config"},
            200,
            "restore_preview",
        ),
        (
            "/api/admin/maintenance/diagnostics/run",
            {},
            200,
            None,
        ),
    ),
    ids=("backup-create", "backup-inspect", "backup-diff", "restore-preview", "diagnostics"),
)
def test_failed_transition_keeps_recovery_reads_and_backup_creation_available(
    tmp_path, path, body, expected_status, expected_call
):
    alignment = _FakeSystemAlignment(stage="failed_recoverable")
    diagnostics = _ReadOnlyDiagnostics()
    backups = _TransitionGateBackupService()
    srv, base = _serve(
        release_manager=_TrackingReleaseManager(tmp_path),
        ems_cli=diagnostics,
        backup_service=backups,
    )
    _attach_system_alignment(srv, alignment)
    try:
        status, _, payload = _request(base + path, method="POST", body=body)
        if expected_call == "create":
            assert backups.create_finished.wait(1)
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == expected_status
    assert payload.get("error") != "system_transition_in_progress"
    if expected_call is None:
        assert diagnostics.calls == 1
    else:
        assert any(call[0] == expected_call for call in backups.calls)


# --- production status responses are always worker-aware --------------------


def test_production_alignment_status_calls_are_worker_aware():
    """Contract: every production system-alignment status read injects the
    server coordinator's liveness probe.

    The transition dict is embedded in many response families (dedicated
    status, job polls, start accepts, transition-in-progress rejections). A
    single bare ``status()`` call would let one of them report a live worker
    as proven inactive, so the only permitted call site is the
    ``_alignment_status`` helper and it must always pass
    ``operation_active=self._operation_active``.
    """

    import ast

    import admin.server

    admin_dir = Path(admin.server.__file__).resolve().parent

    def _touches_system_alignment(node):
        return any(
            isinstance(sub, ast.Attribute) and sub.attr == "system_alignment"
            for sub in ast.walk(node)
        )

    def _status_calls(func):
        return [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "status"
            and _touches_system_alignment(node.func.value)
        ]

    offenders = []
    helper_calls = []
    helper_def = None
    for path in sorted(admin_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = _status_calls(func)
            if not calls:
                continue
            if path.name == "server.py" and func.name == "_alignment_status":
                helper_def = func
                helper_calls.extend(calls)
            else:
                offenders.append(f"{path.name}:{func.name}")

    assert offenders == [], (
        "system_alignment.status() must only be called through the "
        f"_alignment_status helper; bypasses: {offenders}"
    )
    assert helper_def is not None, "_alignment_status helper not found"

    args = helper_def.args
    assert [arg.arg for arg in args.args] == ["self"], (
        "_alignment_status must not let callers alter the liveness probe"
    )
    assert args.kwonlyargs == [] and args.vararg is None and args.kwarg is None

    assert len(helper_calls) == 1
    keywords = {kw.arg: kw.value for kw in helper_calls[0].keywords}
    probe = keywords.get("operation_active")
    assert isinstance(probe, ast.Attribute) and probe.attr == "_operation_active", (
        "_alignment_status must pass operation_active=self._operation_active"
    )


def test_setup_abandon_clears_generated_config_and_reports_state(tmp_path):
    """The backend-owned Start over: one idempotent, authoritative reset."""

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        written, _, first = _request(
            f"{base}/api/setup/config/write",
            method="POST",
            body=_authorized_body(base),
        )
        assert written == 200
        _, _, before = _request(f"{base}/api/setup/config/status")
        assert before["exists"] is True

        status, _, payload = _abandon(base, srv)
        assert status == 200
        assert payload["ok"] is True
        assert payload["generated_config"]["exists"] is False
        assert payload["transition"]["stage"] == "cancelled"
        assert payload["workflow"]["status"] == "abandoned"
        assert first["path"] in payload["removed"]

        _, _, after = _request(f"{base}/api/setup/config/status")
        assert after["exists"] is False

        # Idempotent: a retry after a dropped response changes nothing further.
        again, _, repeat = _abandon(base, srv)
        assert again == 200
        assert repeat["ok"] is True
        assert repeat["removed"] == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_abandon_leaves_the_live_config_untouched(tmp_path):
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    live = Path(detect_install_context().config_path)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text('{"live": true}\n', encoding="utf-8")
    try:
        status, _, payload = _abandon(base, srv)
        assert status == 200
        assert payload["ok"] is True
        assert live.read_text(encoding="utf-8") == '{"live": true}\n'
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_abandon_reports_partial_cleanup_and_converges_on_retry(tmp_path):
    """A failed removal must be explicit and retryable, never a silent success."""

    import shutil
    from unittest import mock

    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    real_rmtree = shutil.rmtree

    try:
        written, _, first = _request(
            f"{base}/api/setup/config/write",
            method="POST",
            body=_authorized_body(base),
        )
        assert written == 200
        generated = Path(first["path"])

        def guarded(target, *args, **kwargs):
            if Path(target) == generated.parent.parent:
                raise PermissionError(13, "Permission denied")
            return real_rmtree(target, *args, **kwargs)

        with mock.patch.object(shutil, "rmtree", guarded):
            status, _, payload = _abandon(base, srv)
        assert status == 500
        assert payload["ok"] is False
        assert payload["error"] == "abandon_cleanup_incomplete"
        assert payload["generated_config"]["exists"] is True
        assert generated.exists()

        retried, _, done = _abandon(base, srv)
        assert retried == 200
        assert done["ok"] is True
        assert done["generated_config"]["exists"] is False
        assert not generated.exists()
    finally:
        srv.shutdown()
        srv.server_close()
