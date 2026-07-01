# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import io
import json
import os
import shutil
import tarfile

import pytest

import dashboard.server as server_module
from dashboard import maintenance
from dashboard.auth import write_password_file
from ems import backup as backup_mod
from ems import config as config_mod
from test_dashboard_server import (
    StoreStub,
    json_response,
    with_server,
)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET_TOKEN = "leaky-ha-token-SECRET-42"


def write_config(path, *, influx_enabled=False, influx_mode="bundled", minimal=False):
    if minimal:
        body = {"system": {"enabled": True}, "ha": {"token": SECRET_TOKEN}, "devices": []}
    else:
        body = {
            "system": {
                "enabled": True,
                "max_total_power": 900,
                "max_device_power": 800,
                "loop_interval": 5,
                "min_output_limit": 35,
            },
            "ha": {"enabled": False, "token": SECRET_TOKEN},
            "winter": {"enabled": False},
            "devices": [{"name": "WR1", "max_power": 800, "pv_priority_factor": 1.0}],
            "influxdb": {"enabled": influx_enabled, "mode": influx_mode},
        }
    path.write_text(json.dumps(body))


def maint_server(tmp_path, monkeypatch, *, configured=True, **config_kwargs):
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    monkeypatch.setattr(server_module, "BASE_DIR", str(tmp_path))

    config_path = tmp_path / "config.json"
    runtime_path = tmp_path / "runtime-state.json"
    auth_file = tmp_path / "dashboard-auth.json"
    write_config(config_path, **config_kwargs)
    runtime_path.write_text(json.dumps({"system": {}, "devices": {}}))
    shutil.copy(
        os.path.join(ROOT, "config.template.json"),
        str(tmp_path / "config.template.json"),
    )
    if configured:
        write_password_file(auth_file, "secret-password")

    server, base_url = with_server(
        StoreStub(),
        auth_file=str(auth_file),
        config_path=str(config_path),
        runtime_state_path=str(runtime_path),
    )
    return server, base_url


def login(base_url):
    _, headers, payload = json_response(
        f"{base_url}/api/auth/login",
        method="POST",
        payload={"password": "secret-password"},
    )
    return headers["Set-Cookie"], payload["csrf_token"]


def create_backup_via_api(base_url, cookie, csrf, btype="config"):
    status, _, payload = json_response(
        f"{base_url}/api/maintenance/backups/create",
        method="POST",
        payload={"type": btype},
        headers={"Cookie": cookie, "X-CSRF-Token": csrf},
    )
    assert status == 200, payload
    return payload["backup"]["name"]


def write_crafted_backup(path, backup_type, archive_path, data):
    manifest = {
        "backup_format": 1,
        "backup_type": backup_type,
        "backup_purpose": "manual",
        "files": [
            {
                "path": archive_path,
                "kind": "config",
                "sensitive": False,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    }
    with tarfile.open(path, "w:gz") as tar:
        manifest_data = json.dumps(manifest).encode()
        manifest_info = tarfile.TarInfo(backup_mod.MANIFEST_NAME)
        manifest_info.size = len(manifest_data)
        tar.addfile(manifest_info, io.BytesIO(manifest_data))
        file_info = tarfile.TarInfo(archive_path)
        file_info.size = len(data)
        tar.addfile(file_info, io.BytesIO(data))


@pytest.mark.parametrize("operation", ["preview", "restore"])
def test_restore_service_rejects_unsupported_manifest_path_before_writing(
    tmp_path, monkeypatch, operation
):
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    target = tmp_path / "dashboard" / "server.py"
    target.parent.mkdir()
    target.write_bytes(b"original")
    backup_dir = tmp_path / "data" / "backups"
    backup_dir.mkdir(parents=True)
    name = "ems-config-manual-2026-01-01-000000.tar.gz"
    write_crafted_backup(
        backup_dir / name,
        "config",
        "dashboard/server.py",
        b"malicious replacement",
    )
    config = {"system": {}, "dashboard": {}, "influxdb": {"enabled": False}}

    with pytest.raises(maintenance.MaintenanceError) as exc_info:
        if operation == "preview":
            maintenance.restore_plan(str(tmp_path), name, None, config)
        else:
            maintenance.restore(
                str(tmp_path),
                name,
                None,
                config,
                confirm_preview=True,
                confirm_restore=True,
                confirm_replace=True,
            )

    assert exc_info.value.code == "unsupported_restore_path"
    assert target.read_bytes() == b"original"
    assert not list(backup_dir.glob("ems-config-rollback-*"))


# ---------------------------------------------------------------------------
# Auth / CSRF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/api/maintenance/status",
        "/api/maintenance/backups",
        "/api/maintenance/config-upgrade",
    ],
)
def test_read_endpoints_require_authentication(tmp_path, monkeypatch, path):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        status, _, payload = json_response(f"{base_url}{path}")
        assert status == 401
        assert payload["error"] == "not_authenticated"
    finally:
        server.shutdown()
        server.server_close()


def test_read_endpoint_forbidden_when_auth_not_configured(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch, configured=False)
    try:
        status, _, payload = json_response(f"{base_url}/api/maintenance/status")
        assert status == 403
        assert payload["error"] == "auth_not_configured"
    finally:
        server.shutdown()
        server.server_close()


WRITE_ENDPOINTS = [
    ("/api/maintenance/backups/create", {"type": "config"}),
    ("/api/maintenance/backups/inspect", {"file": "ems-config-manual-x.tar.gz"}),
    ("/api/maintenance/backups/restore-plan", {"file": "ems-config-manual-x.tar.gz"}),
    (
        "/api/maintenance/backups/restore",
        {"file": "ems-config-manual-x.tar.gz", "confirm_preview": True,
         "confirm_restore": True, "confirm_replace": True},
    ),
    ("/api/maintenance/config-upgrade/apply", {"confirm_apply": True}),
]


@pytest.mark.parametrize("path,body", WRITE_ENDPOINTS)
def test_write_endpoints_require_session(tmp_path, monkeypatch, path, body):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        status, _, payload = json_response(f"{base_url}{path}", method="POST", payload=body)
        assert status == 401
        assert payload["error"] == "not_authenticated"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("path,body", WRITE_ENDPOINTS)
def test_write_endpoints_require_csrf(tmp_path, monkeypatch, path, body):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, _ = login(base_url)
        status, _, payload = json_response(
            f"{base_url}{path}", method="POST", payload=body, headers={"Cookie": cookie}
        )
        assert status == 403
        assert payload["error"] == "csrf_failed"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def test_status_reports_restore_available(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, _ = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/status", headers={"Cookie": cookie}
        )
        assert status == 200
        assert payload["config_path"] == "config.json"
        assert payload["backup_dir"] == os.path.join("data", "backups")
        assert payload["restore_available_in_dashboard"] is True
        assert "restore_supported" in payload["influxdb"]
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Backup create / list / inspect
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_name",
    [
        None,
        7,
        "",
        "ems-config-manual-x.tar.gz\n",
        "../ems-config-manual-x.tar.gz",
        "nested/ems-config-manual-x.tar.gz",
        "nested\\ems-config-manual-x.tar.gz",
        "/tmp/ems-config-manual-x.tar.gz",
    ],
)
def test_safe_backup_path_rejects_invalid_names(tmp_path, monkeypatch, bad_name):
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    with pytest.raises(maintenance.MaintenanceError) as exc_info:
        maintenance._safe_backup_path(str(tmp_path), bad_name)
    assert exc_info.value.code == "invalid_backup_name"


def test_safe_backup_path_accepts_valid_file_and_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    backup_dir = tmp_path / "data" / "backups"
    backup_dir.mkdir(parents=True)
    valid_name = "ems-config-manual-2026-01-01-000000.tar.gz"
    valid_path = backup_dir / valid_name
    valid_path.write_bytes(b"backup")
    assert maintenance._safe_backup_path(str(tmp_path), valid_name) == str(valid_path)

    link_name = "ems-config-manual-2026-01-01-000001.tar.gz"
    (backup_dir / link_name).symlink_to(valid_path)
    with pytest.raises(maintenance.MaintenanceError) as exc_info:
        maintenance._safe_backup_path(str(tmp_path), link_name)
    assert exc_info.value.code == "invalid_backup_name"


def test_safe_backup_path_joins_sanitized_basename(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    backup_dir = tmp_path / "data" / "backups"
    backup_dir.mkdir(parents=True)
    expected_path = str(
        backup_dir / "ems-config-manual-2026-01-01-000000.tar.gz"
    )
    joined_names = []
    original_join = os.path.join

    def recording_join(*parts):
        if len(parts) == 2 and parts[0] == str(backup_dir):
            joined_names.append(parts[1])
        return original_join(*parts)

    monkeypatch.setattr(maintenance.os.path, "join", recording_join)

    class ArchiveName(str):
        pass

    raw_name = ArchiveName("ems-config-manual-2026-01-01-000000.tar.gz")
    resolved = maintenance._safe_backup_path(str(tmp_path), raw_name)

    assert resolved == expected_path
    assert len(joined_names) == 1
    assert joined_names[0] == raw_name
    assert joined_names[0] is not raw_name


def test_inspect_passes_backup_root_to_downstream_checks(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    backup_dir = tmp_path / "data" / "backups"
    backup_dir.mkdir(parents=True)
    name = "ems-config-manual-2026-01-01-000000.tar.gz"
    path = backup_dir / name
    path.write_bytes(b"backup")
    calls = {}

    def fake_is_encrypted(candidate, allowed_root=None):
        calls["encrypted"] = (candidate, allowed_root)
        return False

    def fake_inspect(candidate, password=None, base_dir=None, allowed_root=None):
        calls["inspect"] = (candidate, password, allowed_root)
        return {"manifest": None}

    monkeypatch.setattr(backup_mod, "is_encrypted", fake_is_encrypted)
    monkeypatch.setattr(backup_mod, "inspect_backup", fake_inspect)

    maintenance.inspect_backup(str(tmp_path), name, password="pw")

    assert calls["encrypted"] == (str(path), str(backup_dir))
    assert calls["inspect"] == (str(path), "pw", str(backup_dir))


def test_backup_listing_skips_symlinks_and_unsafe_names(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    backup_dir = tmp_path / "data" / "backups"
    backup_dir.mkdir(parents=True)
    valid_name = "ems-config-manual-2026-01-01-000000.tar.gz"
    valid_path = backup_dir / valid_name
    write_crafted_backup(valid_path, "config", "config.json", b"{}")
    link_name = "ems-config-manual-2026-01-01-000001.tar.gz"
    (backup_dir / link_name).symlink_to(valid_path)
    (backup_dir / "not-a-backup.tar.gz").write_bytes(b"ignored")

    names = [item["name"] for item in maintenance.list_backups(str(tmp_path))["items"]]

    assert names == [valid_name]


def test_backup_inspect_rejects_invalid_name(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")

    with pytest.raises(maintenance.MaintenanceError) as exc_info:
        maintenance.inspect_backup(
            str(tmp_path), "../ems-config-manual-2026-01-01-000000.tar.gz"
        )

    assert exc_info.value.code == "invalid_backup_name"


def test_config_backup_create_and_list(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, csrf = login(base_url)
        name = create_backup_via_api(base_url, cookie, csrf, "config")
        assert name.startswith("ems-config-manual-")
        assert os.path.isfile(tmp_path / "data" / "backups" / name)

        status, _, listing = json_response(
            f"{base_url}/api/maintenance/backups", headers={"Cookie": cookie}
        )
        assert status == 200
        assert name in [item["name"] for item in listing["items"]]
    finally:
        server.shutdown()
        server.server_close()


def test_database_backup_create(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, csrf = login(base_url)
        name = create_backup_via_api(base_url, cookie, csrf, "databases")
        assert name.startswith("ems-databases-manual-")
    finally:
        server.shutdown()
        server.server_close()


def test_unknown_backup_type_is_bad_request(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, csrf = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/create",
            method="POST",
            payload={"type": "bogus"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert payload["error"] == "unknown_backup_type"
    finally:
        server.shutdown()
        server.server_close()


def test_influxdb_disabled_is_clean_noop(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch, influx_enabled=False)
    try:
        cookie, csrf = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/create",
            method="POST",
            payload={"type": "influxdb"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload["created"] is False
        assert payload["reason"] == "influxdb_disabled"
    finally:
        server.shutdown()
        server.server_close()


def test_inspect_is_post_and_returns_manifest(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, csrf = login(base_url)
        name = create_backup_via_api(base_url, cookie, csrf, "config")
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/inspect",
            method="POST",
            payload={"file": name},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload["manifest_available"] is True
        for entry in payload["manifest"]["files"]:
            assert "sha256" not in entry
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "bad",
    ["../config.json", "/etc/passwd", "ems-x.tar.gz/../y", "notabackup.txt"],
)
def test_inspect_rejects_path_traversal(tmp_path, monkeypatch, bad):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, csrf = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/inspect",
            method="POST",
            payload={"file": bad},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert payload["error"] == "invalid_backup_name"
    finally:
        server.shutdown()
        server.server_close()


def test_backups_list_only_includes_backup_archives(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        backup_dir = tmp_path / "data" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "secrets.txt").write_text("nope")
        (backup_dir / "ems-config-manual-2026-01-01-000000.tar.gz").write_text("x")
        (backup_dir / "ems-config-manual-2026-01-01-000001.tar.gz").symlink_to(
            backup_dir / "ems-config-manual-2026-01-01-000000.tar.gz"
        )

        cookie, _ = login(base_url)
        status, _, listing = json_response(
            f"{base_url}/api/maintenance/backups", headers={"Cookie": cookie}
        )
        assert status == 200
        names = [item["name"] for item in listing["items"]]
        assert "secrets.txt" not in names
        assert "ems-config-manual-2026-01-01-000000.tar.gz" in names
        assert "ems-config-manual-2026-01-01-000001.tar.gz" not in names
    finally:
        server.shutdown()
        server.server_close()


def test_encrypted_inspect_without_password_hides_manifest(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        from ems import backup_crypto

        backup_dir = tmp_path / "data" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        name = "ems-config-manual-2026-01-01-000000.tar.gz.enc"
        (backup_dir / name).write_bytes(backup_crypto.MAGIC + b"\x00" * 32)

        cookie, csrf = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/inspect",
            method="POST",
            payload={"file": name},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload["encrypted"] is True
        assert payload["manifest_available"] is False
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Restore (wraps the shared ems.backup core used by the CLI)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "endpoint,body",
    [
        ("/api/maintenance/backups/restore-plan", {}),
        (
            "/api/maintenance/backups/restore",
            {
                "confirm_preview": True,
                "confirm_restore": True,
                "confirm_replace": True,
            },
        ),
    ],
)
def test_dashboard_restore_rejects_unsupported_manifest_path(
    tmp_path, monkeypatch, endpoint, body
):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        target = tmp_path / "dashboard" / "server.py"
        target.parent.mkdir()
        target.write_bytes(b"original")
        backup_dir = tmp_path / "data" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        name = "ems-config-manual-2026-01-01-000000.tar.gz"
        write_crafted_backup(
            backup_dir / name,
            "config",
            "dashboard/server.py",
            b"malicious replacement",
        )

        cookie, csrf = login(base_url)
        status, _, payload = json_response(
            f"{base_url}{endpoint}",
            method="POST",
            payload={"file": name, **body},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert payload["error"] == "unsupported_restore_path"
        assert target.read_bytes() == b"original"
        assert not list(backup_dir.glob("ems-config-rollback-*"))
    finally:
        server.shutdown()
        server.server_close()


def test_restore_plan_returns_dry_run_actions_without_writing(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, csrf = login(base_url)
        name = create_backup_via_api(base_url, cookie, csrf, "config")
        # Modify config so the restore plan sees a conflict.
        (tmp_path / "config.json").write_text(json.dumps({"system": {"enabled": False}}))

        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/restore-plan",
            method="POST",
            payload={"file": name},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload["backup_type"] == "config"
        assert any(a["action"].startswith("would_") for a in payload["actions"])
        assert payload["requires_restart"] is True
        # Plan must not have written anything: config stays modified.
        assert json.loads((tmp_path / "config.json").read_text())["system"]["enabled"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_restore_requires_all_confirmations(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, csrf = login(base_url)
        name = create_backup_via_api(base_url, cookie, csrf, "config")
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/restore",
            method="POST",
            payload={"file": name, "confirm_preview": True, "confirm_restore": False,
                     "confirm_replace": True},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert payload["error"] == "confirmation_required"
    finally:
        server.shutdown()
        server.server_close()


def test_restore_creates_rollback_before_restoring(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, csrf = login(base_url)
        name = create_backup_via_api(base_url, cookie, csrf, "config")
        (tmp_path / "config.json").write_text(json.dumps({"system": {"enabled": False}}))

        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/restore",
            method="POST",
            payload={"file": name, "confirm_preview": True, "confirm_restore": True,
                     "confirm_replace": True},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload["restored"] is True
        assert payload["rollback_backup"].startswith(os.path.join("data", "backups"))
        assert payload["requires_relogin"] is True
        # A rollback archive exists and config was restored.
        rollbacks = list((tmp_path / "data" / "backups").glob("ems-config-rollback-*.tar.gz"))
        assert rollbacks
        assert json.loads((tmp_path / "config.json").read_text())["system"]["enabled"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_restore_does_not_start_if_rollback_fails(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        cookie, csrf = login(base_url)
        name = create_backup_via_api(base_url, cookie, csrf, "config")
        (tmp_path / "config.json").write_text(json.dumps({"system": {"enabled": False}}))

        def boom(*args, **kwargs):
            raise backup_mod.BackupError("rollback exploded")

        monkeypatch.setattr(backup_mod, "create_rollback_backup", boom)

        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/restore",
            method="POST",
            payload={"file": name, "confirm_preview": True, "confirm_restore": True,
                     "confirm_replace": True},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 500
        assert payload["error"] == "rollback_failed"
        # Restore must not have run: config stays modified.
        assert json.loads((tmp_path / "config.json").read_text())["system"]["enabled"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_restore_rejects_wrong_password_without_writing(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        # Create an encrypted config backup directly in the backup dir.
        cfg = json.loads((tmp_path / "config.json").read_text())
        archive = backup_mod.create_config_backup(
            cfg,
            base_dir=str(tmp_path),
            config_path=str(tmp_path / "config.json"),
            password="correct-pw",
        )
        name = os.path.basename(archive)
        (tmp_path / "config.json").write_text(json.dumps({"system": {"enabled": False}}))

        cookie, csrf = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/restore",
            method="POST",
            payload={"file": name, "password": "wrong-pw", "confirm_preview": True,
                     "confirm_restore": True, "confirm_replace": True},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status in (400, 500)
        # No rollback created, config untouched.
        assert not list((tmp_path / "data" / "backups").glob("ems-config-rollback-*"))
        assert json.loads((tmp_path / "config.json").read_text())["system"]["enabled"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_restore_external_influxdb_is_rejected(tmp_path, monkeypatch):
    server, base_url = maint_server(
        tmp_path, monkeypatch, influx_enabled=True, influx_mode="external"
    )
    try:
        bundled = json.loads((tmp_path / "config.json").read_text())
        bundled["influxdb"]["mode"] = "bundled"

        def backup_runner(output_dir):
            with open(os.path.join(output_dir, "backup.bolt.gz"), "wb") as handle:
                handle.write(b"mock-influx-backup")

        archive = backup_mod.create_influxdb_backup(
            bundled,
            base_dir=str(tmp_path),
            backup_runner=backup_runner,
        )
        cookie, csrf = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/restore-plan",
            method="POST",
            payload={"file": os.path.basename(archive)},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert payload["error"] == "influxdb_restore_unsupported"
        assert "external" in payload["message"].lower()
    finally:
        server.shutdown()
        server.server_close()


def test_restore_bundled_influxdb_uses_mocked_runners(tmp_path, monkeypatch):
    import emsctl

    server, base_url = maint_server(
        tmp_path, monkeypatch, influx_enabled=True, influx_mode="bundled"
    )
    received = {}

    def backup_runner(output_dir):
        with open(os.path.join(output_dir, "backup.bolt.gz"), "wb") as handle:
            handle.write(b"mock-influx-backup")

    def restore_runner(influx_dir):
        received["files"] = sorted(os.listdir(influx_dir))

    monkeypatch.setattr(
        emsctl, "make_influx_backup_runner", lambda config, **kwargs: backup_runner
    )
    monkeypatch.setattr(
        emsctl, "make_influx_restore_runner", lambda config, **kwargs: restore_runner
    )

    try:
        config = json.loads((tmp_path / "config.json").read_text())
        archive = backup_mod.create_influxdb_backup(
            config,
            base_dir=str(tmp_path),
            backup_runner=backup_runner,
        )
        cookie, csrf = login(base_url)

        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/restore",
            method="POST",
            payload={
                "file": os.path.basename(archive),
                "confirm_preview": True,
                "confirm_restore": True,
                "confirm_replace": False,
            },
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert payload["error"] == "confirmation_required"
        assert received == {}

        status, _, payload = json_response(
            f"{base_url}/api/maintenance/backups/restore",
            method="POST",
            payload={
                "file": os.path.basename(archive),
                "confirm_preview": True,
                "confirm_restore": True,
                "confirm_replace": True,
            },
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload["backup_type"] == "influxdb"
        assert payload["restored"] is True
        assert payload["rollback_backup"].startswith(
            os.path.join("data", "backups", "ems-influxdb-rollback-")
        )
        assert received["files"] == ["backup.bolt.gz"]
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Config upgrade
# ---------------------------------------------------------------------------

def test_config_upgrade_preview_redacts(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch, minimal=True)
    try:
        cookie, _ = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/config-upgrade", headers={"Cookie": cookie}
        )
        assert status == 200
        assert payload["changed"] is True
        assert payload["add_count"] > 0
        assert payload["plan_id"]
        assert payload["apply_available"] is True
        assert {"add", "comment_add"}.issubset(
            {item["kind"] for item in payload["items"]}
        )
        suspicious = [
            item for item in payload["items"]
            if "value" in item
            and any(
                tok in item["path"].lower()
                for tok in ("auth", "key", "token", "secret")
            )
        ]
        assert suspicious
        assert all(item["value"] == "<redacted>" for item in suspicious)
        assert SECRET_TOKEN not in json.dumps(payload)
    finally:
        server.shutdown()
        server.server_close()


def test_config_upgrade_apply_requires_confirmation(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch, minimal=True)
    try:
        cookie, csrf = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/config-upgrade/apply",
            method="POST",
            payload={"refresh_comments": True},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert payload["error"] == "confirmation_required"
        assert not list((tmp_path / "data" / "backups").glob("*"))
    finally:
        server.shutdown()
        server.server_close()


def test_config_upgrade_apply_rejects_invalid_refresh_comments(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch, minimal=True)
    try:
        cookie, csrf = login(base_url)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/config-upgrade/apply",
            method="POST",
            payload={"refresh_comments": "yes", "confirm_apply": True},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 400
        assert payload["error"] == "bad_request"
        assert not list((tmp_path / "data" / "backups").glob("*"))
    finally:
        server.shutdown()
        server.server_close()


def test_config_upgrade_apply_creates_backup_first(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch, minimal=True)
    try:
        cookie, csrf = login(base_url)
        _, _, plan = json_response(
            f"{base_url}/api/maintenance/config-upgrade",
            headers={"Cookie": cookie},
        )
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/config-upgrade/apply",
            method="POST",
            payload={
                "refresh_comments": True,
                "confirm_apply": True,
                "plan_id": plan["plan_id"],
            },
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert payload["changed"] is True
        assert payload["backup"].startswith(os.path.join("data", "backups"))
        assert payload["backup_name"].startswith("ems-config-manual-")
        assert payload["requires_restart"] is True
        assert payload["requires_relogin"] is False
        assert payload["applied"]["keys_added"] > 0
        assert payload["applied_count"] > 0
        assert list((tmp_path / "data" / "backups").glob("ems-config-manual-*.tar.gz"))
        assert "config_schema_version" in json.loads((tmp_path / "config.json").read_text())
    finally:
        server.shutdown()
        server.server_close()


def test_config_upgrade_comment_only_preview_and_refresh_modes(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch)
    try:
        template = json.loads((tmp_path / "config.template.json").read_text())
        expected_comment = template["config_upgrade"]["_comment"]
        template["config_upgrade"]["_comment"] = ["Outdated comment."]
        plan = config_mod.build_config_upgrade_plan(template, str(tmp_path))
        (tmp_path / "config.json").write_text(
            config_mod.render_config_json(template, plan["template_layout"])
        )

        cookie, csrf = login(base_url)
        status, _, preview = json_response(
            f"{base_url}/api/maintenance/config-upgrade",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert preview["changed"] is False
        assert preview["apply_available"] is True
        assert preview["comment_refresh_count"] == 1
        assert any(
            item["kind"] == "comment_refresh"
            and item["path"] == "config_upgrade._comment"
            for item in preview["items"]
        )

        status, _, skipped = json_response(
            f"{base_url}/api/maintenance/config-upgrade/apply",
            method="POST",
            payload={
                "refresh_comments": False,
                "confirm_apply": True,
                "plan_id": preview["plan_id"],
            },
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert skipped["changed"] is False
        assert not list((tmp_path / "data" / "backups").glob("*"))

        status, _, applied = json_response(
            f"{base_url}/api/maintenance/config-upgrade/apply",
            method="POST",
            payload={
                "refresh_comments": True,
                "confirm_apply": True,
                "plan_id": preview["plan_id"],
            },
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 200
        assert applied["changed"] is True
        assert applied["applied"]["comments_refreshed"] == 1
        updated = json.loads((tmp_path / "config.json").read_text())
        assert updated["config_upgrade"]["_comment"] == expected_comment
    finally:
        server.shutdown()
        server.server_close()


def test_config_upgrade_apply_rejects_stale_preview(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch, minimal=True)
    try:
        cookie, csrf = login(base_url)
        _, _, preview = json_response(
            f"{base_url}/api/maintenance/config-upgrade",
            headers={"Cookie": cookie},
        )
        config_path = tmp_path / "config.json"
        config_path.write_text(config_path.read_text() + "\n")

        status, _, payload = json_response(
            f"{base_url}/api/maintenance/config-upgrade/apply",
            method="POST",
            payload={
                "refresh_comments": True,
                "confirm_apply": True,
                "plan_id": preview["plan_id"],
            },
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 409
        assert payload["error"] == "config_upgrade_plan_changed"
        assert not list((tmp_path / "data" / "backups").glob("*"))
    finally:
        server.shutdown()
        server.server_close()


def test_config_upgrade_apply_rejects_changed_template(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch, minimal=True)
    try:
        cookie, csrf = login(base_url)
        _, _, preview = json_response(
            f"{base_url}/api/maintenance/config-upgrade",
            headers={"Cookie": cookie},
        )
        template_path = tmp_path / "config.template.json"
        template = json.loads(template_path.read_text())
        template["system"]["_comment"] = ["Changed after preview."]
        template_path.write_text(json.dumps(template))

        status, _, payload = json_response(
            f"{base_url}/api/maintenance/config-upgrade/apply",
            method="POST",
            payload={
                "refresh_comments": True,
                "confirm_apply": True,
                "plan_id": preview["plan_id"],
            },
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 409
        assert payload["error"] == "config_upgrade_plan_changed"
        assert not list((tmp_path / "data" / "backups").glob("*"))
    finally:
        server.shutdown()
        server.server_close()


def test_config_upgrade_backup_failure_does_not_write(tmp_path, monkeypatch):
    server, base_url = maint_server(tmp_path, monkeypatch, minimal=True)
    try:
        cookie, csrf = login(base_url)
        _, _, preview = json_response(
            f"{base_url}/api/maintenance/config-upgrade",
            headers={"Cookie": cookie},
        )
        config_path = tmp_path / "config.json"
        original = config_path.read_text()

        def fail_backup(*args, **kwargs):
            raise backup_mod.BackupError("disk full")

        monkeypatch.setattr(backup_mod, "create_config_backup", fail_backup)
        status, _, payload = json_response(
            f"{base_url}/api/maintenance/config-upgrade/apply",
            method="POST",
            payload={
                "refresh_comments": True,
                "confirm_apply": True,
                "plan_id": preview["plan_id"],
            },
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 500
        assert payload["error"] == "backup_failed"
        assert config_path.read_text() == original
    finally:
        server.shutdown()
        server.server_close()
