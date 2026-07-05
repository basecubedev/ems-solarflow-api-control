# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin discovery HTTP server / API tests (no real network scans)."""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from admin.config_apply import ConfigApplyService
from admin.config_export import ConfigExportService
from admin.config_preview import ConfigPreviewGenerator
from admin.install_context import detect_install_context
from admin.models import DiscoveredDevice
from admin.mqtt_discovery import MqttBrokerDiscovery
from admin.releases import ReleaseError
from admin.server import ScanRegistry, create_server

pytestmark = pytest.mark.simulation


def _fake_scan(cidr, timeout_ms=600, max_workers=32):
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
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


def _request(url, method="GET", body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, json.loads(exc.read() or b"null")


def test_root_serves_html(server):
    with urllib.request.urlopen(f"{server}/") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        body = resp.read().decode("utf-8")
        assert "EMS SolarFlow Control Admin" in body
        assert "Guided local setup" in body


def test_static_assets_have_content_types(server):
    with urllib.request.urlopen(f"{server}/admin.css") as resp:
        assert resp.headers["Content-Type"].startswith("text/css")
    with urllib.request.urlopen(f"{server}/admin.js") as resp:
        assert resp.headers["Content-Type"].startswith("application/javascript")


def test_path_traversal_blocked(server):
    status, _, _ = _request(f"{server}/..%2f..%2fadmin/server.py")
    assert status == 404


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


class _FakeReleaseManager:
    def __init__(self, data_dir=None, template=None):
        self.data_dir = Path(data_dir or "/unused-admin-data")
        self.template = template if template is not None else {"devices": []}

    def list_releases(self):
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

    def prepare(self, tag):
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


def test_release_setup_endpoints_use_release_manager():
    srv, base = _serve(release_manager=_FakeReleaseManager())
    try:
        status, _, releases = _request(f"{base}/api/setup/releases")
        assert status == 200
        assert releases["prepared_release"] == "v0.6.0"
        assert releases["default_release"] == "v0.6.0"
        assert releases["releases"][0]["docker_supported"] is True

        status, _, prepared = _request(
            f"{base}/api/setup/releases/prepare",
            method="POST",
            body={"tag": "v0.6.0"},
        )
        assert status == 200
        assert prepared["status"] == "ready"

        status, _, template = _request(f"{base}/api/setup/config-template")
        assert status == 200
        assert template["template"] == {"devices": []}

        status, _, preview = _request(
            f"{base}/api/setup/config-preview",
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


def test_config_template_response_preserves_template_key_order():
    srv, base = _serve(
        release_manager=_FakeReleaseManager(template=_ORDERED_TEMPLATE)
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


def test_config_preview_response_preserves_template_key_order():
    srv, base = _serve(
        release_manager=_FakeReleaseManager(template=_ORDERED_TEMPLATE)
    )
    try:
        status, _, body = _request(
            f"{base}/api/setup/config-preview",
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


def test_release_prepare_returns_clean_data_directory_error():
    class _UnwritableReleaseManager(_FakeReleaseManager):
        def prepare(self, tag):
            raise ReleaseError(
                "Admin data directory is not writable: /data. "
                "Check the Docker volume mount for ./data/admin:/data.",
                500,
            )

    srv, base = _serve(release_manager=_UnwritableReleaseManager())
    try:
        status, _, payload = _request(
            f"{base}/api/setup/releases/prepare",
            method="POST",
            body={"tag": "v0.6.0"},
        )
        assert status == 500
        assert payload == {
            "error": (
                "Admin data directory is not writable: /data. "
                "Check the Docker volume mount for ./data/admin:/data."
            )
        }
    finally:
        srv.shutdown()
        srv.server_close()


def test_config_download_returns_attachment_from_generated_preview(tmp_path):
    srv, base = _serve(release_manager=_FakeReleaseManager(tmp_path))
    try:
        status, headers, payload = _request(
            f"{base}/api/setup/config/download",
            method="POST",
            body={"devices": [], "supported_grid_meter_count": 0},
        )
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert headers["Content-Disposition"] == 'attachment; filename="config.json"'
        assert payload == {"devices": []}
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


def test_config_write_endpoint_protects_overwrite_and_rejects_paths(tmp_path):
    srv, base = _serve(release_manager=_FakeReleaseManager(tmp_path))
    body = {"devices": [], "supported_grid_meter_count": 0}
    try:
        status, _, first = _request(
            f"{base}/api/setup/config/write", method="POST", body=body
        )
        assert status == 200
        assert first["ok"] is True
        target = tmp_path / "generated" / "config.json"
        assert first["path"] == str(target)
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
    srv, base = _serve(release_manager=_FakeReleaseManager(tmp_path))
    body = {"devices": [], "supported_grid_meter_count": 0}
    try:
        status, _, before = _request(f"{base}/api/setup/config/status")
        assert status == 200
        target = tmp_path / "generated" / "config.json"
        assert before == {"path": str(target), "exists": False}

        write_status, _, _ = _request(
            f"{base}/api/setup/config/write", method="POST", body=body
        )
        assert write_status == 200

        status, _, after = _request(f"{base}/api/setup/config/status")
        assert status == 200
        assert after == {"path": str(target), "exists": True}
    finally:
        srv.shutdown()
        srv.server_close()


def _apply_service(manager, admin_data_dir, install_root):
    provider = lambda: detect_install_context(base_dir=str(install_root))
    preview = ConfigPreviewGenerator(manager, install_context_provider=provider)
    export = ConfigExportService(preview, admin_data_dir)
    return ConfigApplyService(export, admin_data_dir, install_context_provider=provider)


def test_config_apply_writes_resolved_install_config(tmp_path):
    manager = _FakeReleaseManager(tmp_path)
    install_root = tmp_path / "ems"
    apply = _apply_service(manager, tmp_path / "admin", install_root)
    srv, base = _serve(release_manager=manager, config_apply=apply)
    body = {"devices": [], "supported_grid_meter_count": 0}
    try:
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
    manager = _FakeReleaseManager(tmp_path)
    install_root = tmp_path / "ems"
    target = install_root / "config" / "config.json"
    target.parent.mkdir(parents=True)
    old_bytes = b'{"devices": [], "system": {"max_total_power": 1}}\n'
    target.write_bytes(old_bytes)
    apply = _apply_service(manager, tmp_path / "admin", install_root)
    srv, base = _serve(release_manager=manager, config_apply=apply)
    body = {"devices": [], "supported_grid_meter_count": 0}
    try:
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
    body = {"devices": [], "supported_grid_meter_count": 0}
    try:
        status, _, payload = _request(
            f"{base}/api/setup/config/apply", method="POST", body=body
        )
        assert status == 422
        assert payload["reason"] == "validation_failed"
        assert target.read_bytes() == original
    finally:
        srv.shutdown()
        srv.server_close()


class _FakeDeployment:
    def __init__(self):
        self.prepared_overwrite = None
        self.start_calls = 0

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

    def prepare(self, overwrite=False):
        self.prepared_overwrite = overwrite
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

    def start(self):
        self.start_calls += 1
        if getattr(self, "start_rejection", None):
            return {
                "ok": False,
                "reason": self.start_rejection,
                "message": "Prepare deployment first before starting EMS.",
                "status": 409,
            }
        return {
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

    def start_job(self, job_id):
        if job_id != "start-1":
            return None
        return {
            "job_id": "start-1",
            "status": "succeeded",
            "phase": "EMS is running",
            "steps": [],
            "services": [{"service": "ems", "state": "running"}],
            "dashboard_url": "http://localhost:8080",
            "dashboard_reachable": True,
        }

    def resolve_container_conflict(self, container_name, action):
        self.resolved_conflict = (container_name, action)
        if container_name != "ems-solarflow-api-control":
            return {
                "ok": False,
                "reason": "unknown_container_name",
                "message": "Unknown container.",
                "status": 400,
            }
        return {"ok": True, "removed": container_name, "continue": True}

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
            body={"overwrite": True},
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
            f"{base}/api/setup/deployment/prepare", method="POST", body={}
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
            f"{base}/api/setup/deployment/prepare", method="POST", body={}
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


def test_deployment_start_and_status_endpoints():
    deployment = _FakeDeployment()
    srv, base = _serve(deployment=deployment)
    try:
        status, _, started = _request(
            f"{base}/api/setup/deployment/start", method="POST", body={}
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
            f"{base}/api/setup/deployment/start", method="POST", body={}
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
            body={
                "container_name": "ems-solarflow-api-control",
                "action": "remove_stopped_and_continue",
            },
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
            body={
                "container_name": "arbitrary",
                "action": "remove_stopped_and_continue",
            },
        )
        assert status == 400
        assert rejected["reason"] == "unknown_container_name"
    finally:
        srv.shutdown()
        srv.server_close()


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
    srv = create_server("127.0.0.1", 0, registry=ScanRegistry(scan_runner=_fake_scan),
                        gateway_prober=_fake_gateway_prober, **kwargs)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


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
    finally:
        srv.shutdown()
        srv.server_close()


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
