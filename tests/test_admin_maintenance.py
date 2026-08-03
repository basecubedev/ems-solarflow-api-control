# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance diagnostics API tests (read-only, allowlisted, degrades cleanly)."""

import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from admin.ems_cli import EmsCliDiagnostics
from admin.server import ScanRegistry, create_server
from tests.admin_auth_helpers import auth_headers, authenticate

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


COMPOSE_TEXT = """
services:
  ems:
    image: ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1
    container_name: ems-solarflow-api-control
"""


class FakeDocker:
    def __init__(self, container=None):
        self._container = container

    def probe(self):
        return {"state": "ready"}

    def inspect_container(self, name):
        return dict(self._container) if self._container else None


def _running_ems():
    return {
        "container_name": "ems-solarflow-api-control",
        "image": "ghcr.io/basecubedev/ems-solarflow-api-control:v0.6.1",
        "status": "running",
    }


def _standard_install(base_dir):
    (base_dir / "config").mkdir()
    (base_dir / "config" / "config.json").write_text("{}", encoding="utf-8")
    (base_dir / "data").mkdir()
    (base_dir / "docker-compose.yml").write_text(COMPOSE_TEXT, encoding="utf-8")


def _make_ems_cli(base_dir, docker, run):
    from admin.install_context import detect_install_context

    return EmsCliDiagnostics(
        install_context_provider=lambda: detect_install_context(base_dir=str(base_dir)),
        docker=docker,
        run=run,
    )


def _server(ems_cli):
    registry = ScanRegistry(scan_runner=lambda *a, **k: ([], []))
    srv = create_server("127.0.0.1", 0, registry=registry, ems_cli=ems_cli)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    authenticate(base)
    return srv, base


def _post(url, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = dict(auth_headers(url, "POST"))
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_diagnostics_endpoint_runs_allowlisted_checks(tmp_path):
    _standard_install(tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return _completed(0, "{}")

    ems_cli = _make_ems_cli(tmp_path, FakeDocker(_running_ems()), run)
    srv, base = _server(ems_cli)
    try:
        status, payload = _post(f"{base}/api/admin/maintenance/diagnostics/run")
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["available"] is True
    assert payload["mode"] == "container"
    ids = [check["id"] for check in payload["checks"]]
    assert ids == ["quick_diagnose", "config_upgrade_dry_run", "influx_status", "runtime_status"]
    # Only the allowlisted suffixes were executed; nothing mutating.
    suffixes = {tuple(argv[5:]) for argv in calls}
    assert ("config", "upgrade", "--dry-run") in suffixes
    assert not any("--yes" in argv for argv in calls)
    assert not any("backup" in argv for argv in calls)


def test_diagnostics_endpoint_rejects_no_command_input(tmp_path):
    _standard_install(tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return _completed(0, "{}")

    ems_cli = _make_ems_cli(tmp_path, FakeDocker(_running_ems()), run)
    srv, base = _server(ems_cli)
    try:
        # A body attempting to inject a command is ignored: the allowlist runs.
        status, payload = _post(
            f"{base}/api/admin/maintenance/diagnostics/run",
            body={"command": "rm -rf /", "check": "backup create"},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    ids = [check["id"] for check in payload["checks"]]
    assert ids == ["quick_diagnose", "config_upgrade_dry_run", "influx_status", "runtime_status"]
    assert not any("rm" in argv for argv in calls)


def test_diagnostics_endpoint_unavailable_degrades_cleanly(tmp_path):
    # No compose, no emsctl, no container -> unavailable, but a clean 200.
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text("{}", encoding="utf-8")

    def run(argv, **kwargs):
        raise AssertionError("no command should run when unavailable")

    ems_cli = _make_ems_cli(tmp_path, FakeDocker(None), run)
    srv, base = _server(ems_cli)
    try:
        status, payload = _post(f"{base}/api/admin/maintenance/diagnostics/run")
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["available"] is False
    assert payload["mode"] == "unavailable"
    assert payload["checks"] == []
    assert payload["summary"]["status"] == "unavailable"


def test_diagnostics_endpoint_failing_check_is_not_a_route_crash(tmp_path):
    _standard_install(tmp_path)

    def run(argv, **kwargs):
        if argv[-1] == "status":
            return _completed(3, "", "runtime state unreadable")
        return _completed(0, "{}")

    ems_cli = _make_ems_cli(tmp_path, FakeDocker(_running_ems()), run)
    srv, base = _server(ems_cli)
    try:
        status, payload = _post(f"{base}/api/admin/maintenance/diagnostics/run")
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    runtime = next(c for c in payload["checks"] if c["id"] == "runtime_status")
    assert runtime["status"] == "failed"
    assert payload["summary"]["failed"] == 1


def test_diagnostics_endpoint_survives_runner_exception(tmp_path):
    class ExplodingEmsCli:
        def run(self, check_ids=None):
            raise RuntimeError("unexpected")

    srv, base = _server(ExplodingEmsCli())
    try:
        status, payload = _post(f"{base}/api/admin/maintenance/diagnostics/run")
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200
    assert payload["available"] is False
    assert payload["summary"]["status"] == "unavailable"


def test_diagnostics_endpoint_masks_cloud_identity_in_every_result_field(
    tmp_path, monkeypatch
):
    route = "ADMIN_DIAG_CLOUD_ROUTE_7501"
    product = "ADMIN_DIAG_PRODUCT_ACCOUNT"
    topic = f"iot/{product}/{route}/properties/write"
    _standard_install(tmp_path)
    (tmp_path / "config" / "config.json").write_text(
        json.dumps(
            {
                "zendure_mqtt": {
                    "brokers": {
                        "cloud_a": {
                            "source": "zendure_cloud_mqtt",
                            "host": "mqtt.example.invalid",
                            "password": "CONFIG_BROKER_PASSWORD",
                        }
                    }
                },
                "devices": [
                    {
                        "type": "zendure_mqtt",
                        "name": f"Cloud device {route}",
                        "mqtt": {
                            "broker_ref": "cloud_a",
                            "product_key": product,
                            "device_id": route,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    class RawDiagnostics:
        def run(self, check_ids=None):
            return {
                "available": True,
                "mode": "container",
                "checks": [
                    {
                        "id": "quick_diagnose",
                        "name": f"Device {route}",
                        "stdout": f"publish pending for {topic}",
                        "stderr": f"route={route} product={product}",
                        "parsed": {route: {"topic": topic}},
                        "runtime_status": {
                            "broker_ref": "cloud_a",
                            "device_id": route,
                            "write_topic": topic,
                        },
                        "app_key": "RESULT_APP_KEY_SECRET",
                        "authorization_code": "RESULT_AUTH_SECRET",
                    }
                ],
                "summary": {"status": "ok"},
            }

    srv, base = _server(RawDiagnostics())
    try:
        status, payload = _post(f"{base}/api/admin/maintenance/diagnostics/run")
    finally:
        srv.shutdown()
        srv.server_close()

    flattened = json.dumps(payload)
    assert status == 200
    for raw in (
        route,
        product,
        topic,
        "RESULT_APP_KEY_SECRET",
        "RESULT_AUTH_SECRET",
        "CONFIG_BROKER_PASSWORD",
    ):
        assert raw not in flattened


def test_overview_endpoint_still_works_alongside_diagnostics(tmp_path, monkeypatch):
    _standard_install(tmp_path)
    monkeypatch.setenv("EMS_INSTALL_DIR", str(tmp_path))

    ems_cli = _make_ems_cli(tmp_path, FakeDocker(_running_ems()), lambda *a, **k: _completed())
    srv, base = _server(ems_cli)
    try:
        overview_url = f"{base}/api/admin/maintenance/overview"
        overview_req = urllib.request.Request(
            overview_url, headers=auth_headers(overview_url, "GET"), method="GET"
        )
        with urllib.request.urlopen(overview_req) as resp:
            overview = json.loads(resp.read())
    finally:
        srv.shutdown()
        srv.server_close()

    assert "install_state" in overview
    assert "paths" in overview
