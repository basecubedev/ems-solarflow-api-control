# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP tests for the admin backup/restore API endpoints."""

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from datetime import datetime, timedelta, timezone

from ems import backup as backup_mod
from admin.backup_restore import (
    BackupRestoreService,
    RestorePlan,
    RestorePlanTarget,
)
from admin.install_context import detect_install_context
from admin.server import create_server
from tests.test_admin_backup_restore import (
    _build_install,
    _make_config_archive,
    _make_influxdb_archive,
)

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


@pytest.fixture()
def install(tmp_path):
    return _build_install(tmp_path)


@pytest.fixture()
def server(install):
    service = BackupRestoreService(
        context_provider=lambda: detect_install_context(base_dir=str(install))
    )
    srv = create_server("127.0.0.1", 0, backup_service=service)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def backup_server(install):
    """A running server plus a handle to its backup service (for plan injection)."""

    service = BackupRestoreService(
        context_provider=lambda: detect_install_context(base_dir=str(install))
    )
    srv = create_server("127.0.0.1", 0, backup_service=service)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base, service
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
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _poll_job(base, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, data = _request(
            base + "/api/admin/maintenance/backups/jobs/" + job_id
        )
        assert status == 200
        if data["status"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def _first_backup_id(base):
    status, data = _request(base + "/api/admin/maintenance/backups")
    assert status == 200
    return data["backups"][0]["id"]


def test_backups_list_endpoint_returns_summary(server, install):
    _make_config_archive(install)
    status, data = _request(server + "/api/admin/maintenance/backups")
    assert status == 200
    assert data["ok"] is True
    assert set(data["summary"]) >= {"total", "encrypted", "invalid", "latest_created_at"}
    assert data["summary"]["total"] == 1


def test_create_backup_endpoint_starts_job(server, install):
    status, data = _request(
        server + "/api/admin/maintenance/backups/create",
        method="POST",
        body={"scope": "config"},
    )
    assert status == 202
    assert data["ok"] is True
    assert data["job_id"]
    result = _poll_job(server, data["job_id"])
    assert result["result"]["ok"] is True


def test_backup_job_endpoint_reports_steps_and_result(server, install):
    _, data = _request(
        server + "/api/admin/maintenance/backups/create",
        method="POST",
        body={"scope": "config"},
    )
    job = _poll_job(server, data["job_id"])
    assert job["status"] == "succeeded"
    assert any(step["key"] == "create_config" for step in job["steps"])
    assert job["result"]["archives"][0]["verified"] is True


def test_inspect_endpoint_rejects_unknown_fields(server, install):
    _make_config_archive(install)
    backup_id = _first_backup_id(server)
    status, data = _request(
        server + "/api/admin/maintenance/backups/inspect",
        method="POST",
        body={"id": backup_id, "bogus": 1},
    )
    assert status == 400
    assert "error" in data


def test_restore_preview_endpoint_returns_plan_id(server, install):
    _make_config_archive(install)
    (install / "config" / "config.json").write_text('{"changed": true}')
    backup_id = _first_backup_id(server)
    status, data = _request(
        server + "/api/admin/maintenance/backups/restore/preview",
        method="POST",
        body={"id": backup_id, "scope": "config", "conflict_policy": "replace"},
    )
    assert status == 200
    assert data["ok"] is True
    assert data["plan_id"]
    # Preview writes nothing.
    assert (install / "config" / "config.json").read_text() == '{"changed": true}'


def test_restore_preview_endpoint_defaults_to_replace(server, install):
    _make_config_archive(install)
    (install / "config" / "config.json").write_text('{"changed": true}')
    backup_id = _first_backup_id(server)
    status, data = _request(
        server + "/api/admin/maintenance/backups/restore/preview",
        method="POST",
        body={"id": backup_id, "scope": "config"},
    )
    assert status == 200
    assert data["ok"] is True
    assert data["conflict_policy"] == "replace"
    assert data["blocked"] is False
    assert data["summary"]["would_replace"] >= 1


def test_restore_execute_endpoint_requires_confirm_and_plan(server, install):
    status, data = _request(
        server + "/api/admin/maintenance/backups/restore/execute",
        method="POST",
        body={"plan_id": "anything"},
    )
    assert status == 400

    status, data = _request(
        server + "/api/admin/maintenance/backups/restore/execute",
        method="POST",
        body={"plan_id": "missing", "confirm": True},
    )
    assert status == 409
    assert "error" in data


def test_delete_endpoint_requires_confirm(server, install):
    _make_config_archive(install)
    backup_id = _first_backup_id(server)
    status, data = _request(
        server + "/api/admin/maintenance/backups/delete",
        method="POST",
        body={"id": backup_id},
    )
    assert status == 400
    assert "error" in data
    # The archive is still present.
    status, listing = _request(server + "/api/admin/maintenance/backups")
    assert listing["summary"]["total"] == 1


def test_delete_endpoint_rejects_unknown_id(server, install):
    _make_config_archive(install)
    status, data = _request(
        server + "/api/admin/maintenance/backups/delete",
        method="POST",
        body={"id": "does-not-exist", "confirm": True},
    )
    assert status == 400
    assert isinstance(data, dict)
    assert data.get("ok") is False
    assert "error" in data
    assert "Traceback" not in json.dumps(data)
    # The real archive is untouched by the rejected delete.
    status, listing = _request(server + "/api/admin/maintenance/backups")
    assert listing["summary"]["total"] == 1


def test_delete_endpoint_removes_archive_and_refreshed_list_is_empty(server, install):
    _make_config_archive(install)
    backup_id = _first_backup_id(server)
    status, data = _request(
        server + "/api/admin/maintenance/backups/delete",
        method="POST",
        body={"id": backup_id, "confirm": True},
    )
    assert status == 200
    assert data["ok"] is True
    assert data["mode"] == "archive"
    assert data["deleted"]
    # A fresh listing no longer shows the deleted archive.
    status, listing = _request(server + "/api/admin/maintenance/backups")
    assert status == 200
    assert listing["summary"]["total"] == 0
    assert listing["backups"] == []


def test_api_errors_are_json_not_tracebacks(server, install):
    _make_config_archive(install)
    status, data = _request(
        server + "/api/admin/maintenance/backups/inspect",
        method="POST",
        body={"id": "unknown-id"},
    )
    assert status == 400
    assert isinstance(data, dict)
    assert "error" in data
    assert "Traceback" not in json.dumps(data)


def test_restore_preview_endpoint_blocks_influxdb_archive(server, install):
    _make_influxdb_archive(install)
    backup_id = _first_backup_id(server)
    status, data = _request(
        server + "/api/admin/maintenance/backups/restore/preview",
        method="POST",
        body={"id": backup_id, "scope": "influxdb"},
    )
    assert status == 400
    assert "InfluxDB restore" in data["error"]
    assert "plan_id" not in data
    assert "Traceback" not in json.dumps(data)


def test_restore_execute_endpoint_blocks_influxdb_plan(backup_server, install):
    base, service = backup_server
    path = _make_influxdb_archive(install)
    backup_id = service.list_backups()["backups"][0]["id"]

    now = datetime.now(timezone.utc)
    target = RestorePlanTarget(
        archive_id=backup_id, name=os.path.basename(path),
        backup_type="influxdb", archive_sha256=backup_mod._sha256_file(path),
    )
    plan = RestorePlan(
        plan_id="crafted", kind="archive", backup_id=backup_id, scope="influxdb",
        conflict_policy="replace", rollback_enabled=False,
        auto_rollback_enabled=False, password=None,
        created_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        targets=[target], manifest_summary=None, files=[], summary={},
        warnings=[], blocked=False, block_reason=None,
    )
    service.plans.put(plan)

    status, data = _request(
        base + "/api/admin/maintenance/backups/restore/execute",
        method="POST",
        body={"plan_id": "crafted", "confirm": True},
    )
    assert status == 409
    assert "InfluxDB restore" in data["error"]
    assert "job_id" not in data
