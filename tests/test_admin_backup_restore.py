# SPDX-License-Identifier: AGPL-3.0-or-later
"""Service-level tests for admin/backup_restore.py.

These prove the user-facing behaviour (list/inspect/create/preview/restore/
delete + rollback safety) using the real EMS backup core; no Docker/network.
"""

import io
import json
import os
import sqlite3
import tarfile

import pytest

from datetime import datetime, timedelta, timezone

from ems import backup as backup_mod
from admin.backup_restore import (
    BackupInspector,
    BackupRecord,
    BackupRestoreError,
    BackupRestoreService,
    BackupStore,
    RestorePlan,
    RestorePlanTarget,
    resolve_env,
)
from admin.install_context import detect_install_context

pytestmark = pytest.mark.simulation


def _build_install(tmp_path, *, influx=None):
    """Create a standard EMS install layout and return its root path."""

    root = tmp_path / "install"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "data" / "backups").mkdir()

    config = {
        "system": {"runtime_state_path": "data/runtime-state.json"},
        "dashboard": {
            "auth_file": "config/dashboard-auth.json",
            "ssl_cert_file": "config/dashboard.crt",
            "ssl_key_file": "config/dashboard.key",
            "database_path": "data/ems_dashboard.sqlite",
        },
        "influxdb": influx if influx is not None else {"enabled": False},
        "devices": [],
    }
    (root / "config" / "config.json").write_text(json.dumps(config, indent=2))
    (root / "data" / "runtime-state.json").write_text('{"runtime": true}\n')
    (root / "config" / "dashboard-auth.json").write_text('{"hash": "secret"}')
    (root / "config" / "dashboard.crt").write_text("CERT")
    (root / "config" / "dashboard.key").write_text("KEY")
    (root / "docker-compose.yml").write_text(
        "services:\n  ems:\n    image: ghcr.io/basecubedev/ems:v0.6.0\n"
    )

    db_path = root / "data" / "ems_dashboard.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return root


def _service(root):
    return BackupRestoreService(
        context_provider=lambda: detect_install_context(base_dir=str(root))
    )


def _backup_dir(root):
    return root / "data" / "backups"


def _make_config_archive(root, **kwargs):
    return backup_mod.create_config_backup(
        _load(root),
        base_dir=str(root),
        config_path=str(root / "config" / "config.json"),
        backup_dir=str(_backup_dir(root)),
        **kwargs,
    )


def _make_database_archive(root, **kwargs):
    return backup_mod.create_database_backup(
        _load(root), base_dir=str(root), backup_dir=str(_backup_dir(root)), **kwargs
    )


def _make_influxdb_archive(root, **kwargs):
    """Create a real EMS InfluxDB archive in the backup dir (no Docker/network).

    The backup *runner* owns the Docker orchestration, so a local fake that drops
    an output file is enough to produce a genuine ``ems-influxdb-*`` archive.
    """

    config = {
        "influxdb": {"enabled": True, "mode": "bundled", "org": "ems",
                     "bucket_prefix": "ems"},
        "devices": [],
    }

    def _runner(out_dir):
        with open(os.path.join(out_dir, "20260618T000000Z.bolt.gz"), "wb") as handle:
            handle.write(b"INFLUX-BACKUP")

    return backup_mod.create_influxdb_backup(
        config, base_dir=str(root), backup_dir=str(_backup_dir(root)),
        backup_runner=_runner, **kwargs
    )


def _load(root):
    return json.loads((root / "config" / "config.json").read_text())


def _tamper_manifest_sha(path):
    """Rewrite an archive so one file's manifest checksum no longer matches."""

    members = []
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            data = tar.extractfile(member).read() if member.isfile() else b""
            members.append((member, data))
    manifest = None
    for member, data in members:
        if member.name == backup_mod.MANIFEST_NAME:
            manifest = json.loads(data.decode("utf-8"))
    manifest["files"][0]["sha256"] = "0" * 64
    with tarfile.open(path, "w:gz") as tar:
        for member, data in members:
            if member.name == backup_mod.MANIFEST_NAME:
                data = json.dumps(manifest).encode("utf-8")
                member.size = len(data)
            tar.addfile(member, io.BytesIO(data))


# --- listing and safety -----------------------------------------------------

def test_list_backups_returns_valid_ems_archives(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    data = _service(root).list_backups()

    assert data["ok"] is True
    assert data["safe_location"] is True
    assert data["summary"]["total"] == 1
    record = data["backups"][0]
    assert record["backup_type"] == "config"
    assert record["manifest_available"] is True
    assert record["size_bytes"] > 0
    assert record["mtime"]
    assert "path" not in record
    # A local/dev build has no release version, but a build/revision label and a
    # short commit are still surfaced for the row facts.
    assert record["source_version"] is None
    assert record["source_build"]
    assert record["source_commit"]


def test_list_backups_ignores_non_backup_files(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    (_backup_dir(root) / "notes.txt").write_text("hello")
    (_backup_dir(root) / "random.tar.gz").write_text("nope")

    data = _service(root).list_backups()
    names = [b["name"] for b in data["backups"]]
    assert data["summary"]["total"] == 1
    assert all(n.startswith("ems-") for n in names)


def test_record_exposes_source_version_and_build():
    record = BackupRecord(id="x", name="ems-config-manual.tar.gz", path="/x")
    BackupStore._apply_manifest(record, {
        "source": {
            "ems_version": "0.6.3",
            "git_commit_short": "abcdef123456",
            "git_describe": "v0.6.3-2-gabcdef1",
        },
        "files": [],
    })
    data = record.to_dict()
    assert data["source_version"] == "0.6.3"
    # A git describe is the preferred compact build label.
    assert data["source_build"] == "v0.6.3-2-gabcdef1"
    assert data["source_commit"] == "abcdef123456"


def test_record_source_build_prefers_build_label():
    record = BackupRecord(id="x", name="ems-config-manual.tar.gz", path="/x")
    BackupStore._apply_manifest(record, {
        "source": {
            "ems_version": None,
            "build_label": "v0.6.3-12-gabcdef-dirty",
            "git_describe": "ignored-when-build-label-present",
            "git_commit_short": "abcdef123456",
        },
        "files": [],
    })
    # New-style manifest: build_label is the honest compact build identity, and
    # a non-release build carries no historic version.
    assert record.source_build == "v0.6.3-12-gabcdef-dirty"
    assert record.source_version is None


def test_record_source_build_falls_back_to_commit_and_drops_unknown():
    record = BackupRecord(id="x", name="ems-config-manual.tar.gz", path="/x")
    BackupStore._apply_manifest(record, {
        "source": {"git_commit_short": "abcdef123456", "git_describe": "unknown"},
        "files": [],
    })
    assert record.source_build == "abcdef123456"

    blank = BackupRecord(id="y", name="ems-config-manual.tar.gz", path="/y")
    BackupStore._apply_manifest(blank, {
        "source": {
            "build_label": None,
            "git_commit_short": "null",
            "git_describe": "unknown",
        },
        "files": [],
    })
    assert blank.source_build is None
    assert blank.source_commit is None


def test_inspect_summary_exposes_git_describe_and_dirty():
    summary = BackupInspector._summarize({
        "source": {
            "ems_version": "0.6.3",
            "build_label": "v0.6.3-2-gabcdef1",
            "git_commit_short": "abcdef123456",
            "git_describe": "v0.6.3-2-gabcdef1",
            "git_dirty": True,
        },
        "files": [],
    })
    assert summary["source"]["build_label"] == "v0.6.3-2-gabcdef1"
    assert summary["source"]["git_describe"] == "v0.6.3-2-gabcdef1"
    assert summary["source"]["git_commit_short"] == "abcdef123456"
    assert summary["source"]["git_dirty"] is True


def test_backup_id_resolution_rejects_path_traversal(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    service = _service(root)
    for bad in ("../evil.tar.gz", "/etc/passwd", "unknown-id"):
        with pytest.raises(BackupRestoreError):
            service.inspect_backup(bad)


def test_backup_id_resolution_rejects_symlink(tmp_path):
    root = _build_install(tmp_path)
    real = _make_config_archive(root)
    link = _backup_dir(root) / "ems-config-manual-9999-99-99-000000.tar.gz"
    os.symlink(real, link)

    data = _service(root).list_backups()
    # Only the real archive is listed; the symlink is never a backup archive.
    assert data["summary"]["total"] == 1
    assert data["backups"][0]["name"] == os.path.basename(real)


# --- inspect and details ----------------------------------------------------

def test_inspect_backup_returns_manifest_and_file_list(tmp_path):
    root = _build_install(tmp_path)
    path = _make_config_archive(root)
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    result = service.inspect_backup(backup_id)
    manifest = result["manifest"]
    assert result["locked"] is False
    assert manifest["backup_type"] == "config"
    assert manifest["backup_purpose"] == "manual"
    assert manifest["created_at"]
    paths = {f["path"] for f in manifest["files"]}
    assert "config/config.json" in paths
    for entry in manifest["files"]:
        assert "content" not in entry and "body" not in entry and "data" not in entry


def test_inspect_encrypted_backup_without_password_is_locked(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root, password="hunter2",
                         encryption_options=backup_mod.build_encryption_options())
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    result = service.inspect_backup(backup_id)
    assert result["ok"] is True
    assert result["locked"] is True
    assert result["manifest"] is None


def test_inspect_encrypted_backup_wrong_password_fails_friendly(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root, password="hunter2",
                         encryption_options=backup_mod.build_encryption_options())
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    with pytest.raises(BackupRestoreError) as exc:
        service.inspect_backup(backup_id, password="wrong")
    assert "password" in str(exc.value).lower()


def test_backup_details_never_return_sensitive_file_contents(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    result = service.inspect_backup(backup_id)
    auth = [f for f in result["manifest"]["files"] if f["kind"] == "dashboard_auth"]
    assert auth and auth[0]["sensitive"] is True
    blob = json.dumps(result)
    assert "secret" not in blob  # the dashboard-auth hash value never leaks


# --- create -----------------------------------------------------------------

def test_create_config_backup_creates_and_verifies_archive(tmp_path):
    root = _build_install(tmp_path)
    result = _service(root).create_backup({"scope": "config"})

    assert result["ok"] is True
    archive = result["archives"][0]
    assert archive["type"] == "config"
    assert archive["verified"] is True
    assert (_backup_dir(root) / archive["name"]).is_file()


def test_create_database_backup_creates_and_verifies_archive(tmp_path):
    root = _build_install(tmp_path)
    result = _service(root).create_backup({"scope": "databases"})

    assert result["ok"] is True
    archive = result["archives"][0]
    assert archive["type"] == "databases"
    assert archive["verified"] is True
    info = backup_mod.inspect_backup(str(_backup_dir(root) / archive["name"]))
    assert info["manifest"]["backup_type"] == "databases"


def test_create_system_backup_set_groups_archives(tmp_path):
    root = _build_install(tmp_path)  # influx disabled by default
    result = _service(root).create_backup({"scope": "system"})

    assert result["ok"] is True
    types = {a["type"] for a in result["archives"]}
    assert types == {"config", "databases"}
    assert any("InfluxDB" in w for w in result.get("warnings", []))

    backup_set = result["backup_set"]
    assert backup_set["status"] == "complete"
    member_names = {a["name"] for a in backup_set["archives"]}
    assert member_names == {a["name"] for a in result["archives"]}
    # The set metadata only references real EMS archives on disk.
    for name in member_names:
        assert (_backup_dir(root) / name).is_file()


def test_create_backup_failure_does_not_write_complete_set_metadata(tmp_path):
    root = _build_install(tmp_path)
    service = _service(root)

    original = service._create_archive

    def _fail_databases(env, scope, password):
        if scope == "databases":
            raise backup_mod.BackupError("database snapshot failed")
        return original(env, scope, password)

    service._create_archive = _fail_databases
    result = service.create_backup({"scope": "system"})

    assert result["ok"] is False
    # A failed member means the grouping metadata is never written.
    assert service.list_backups()["sets"] == []


# --- restore preview --------------------------------------------------------

def test_restore_preview_reports_new_conflict_identical_without_writing(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    config_file = root / "config" / "config.json"
    original_config = config_file.read_text()
    config_file.write_text('{"changed": true}')          # -> conflict
    (root / "data" / "runtime-state.json").unlink()       # -> new

    plan = service.create_restore_plan(
        {"id": backup_id, "scope": "config", "conflict_policy": "replace"}
    )
    assert plan["summary"]["would_restore"] >= 1
    assert plan["summary"]["would_replace"] >= 1
    assert plan["summary"]["would_skip"] >= 1
    # Preview must not write anything.
    assert config_file.read_text() == '{"changed": true}'
    assert not (root / "data" / "runtime-state.json").exists()
    assert original_config  # sanity


def test_restore_preview_defaults_to_replace_without_blocking(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    (root / "config" / "config.json").write_text('{"changed": true}')  # -> conflict

    plan = service.create_restore_plan({"id": backup_id, "scope": "config"})
    assert plan["conflict_policy"] == "replace"
    assert plan["blocked"] is False
    assert plan["summary"]["would_replace"] >= 1
    assert any(f["action"] == "would_replace_conflict" for f in plan["files"])


def test_restore_preview_blocks_invalid_checksum(tmp_path):
    root = _build_install(tmp_path)
    path = _make_config_archive(root)
    _tamper_manifest_sha(path)
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    plan = service.create_restore_plan(
        {"id": backup_id, "scope": "config", "conflict_policy": "replace"}
    )
    assert plan["blocked"] is True
    assert plan["block_reason"] == "checksum_invalid"


def test_restore_preview_requires_password_for_encrypted_backup(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root, password="hunter2",
                         encryption_options=backup_mod.build_encryption_options())
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    with pytest.raises(BackupRestoreError):
        service.create_restore_plan({"id": backup_id, "scope": "config"})

    plan = service.create_restore_plan(
        {"id": backup_id, "scope": "config", "password": "hunter2"}
    )
    assert plan["ok"] is True
    assert plan["plan_id"]


def test_restore_plan_records_archive_hash(tmp_path):
    root = _build_install(tmp_path)
    path = _make_config_archive(root)
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    plan = service.create_restore_plan({"id": backup_id, "scope": "config"})
    assert plan["archive_sha256"] == backup_mod._sha256_file(path)

    with open(path, "ab") as handle:  # mutate archive after preview
        handle.write(b"x")
    with pytest.raises(BackupRestoreError) as exc:
        service.restore_from_plan(plan["plan_id"], confirm=True)
    assert "changed" in str(exc.value).lower()


# --- restore execute and rollback -------------------------------------------

def _plan_replace(service, backup_id, **overrides):
    request = {"id": backup_id, "scope": "config", "conflict_policy": "replace"}
    request.update(overrides)
    return service.create_restore_plan(request)


def test_restore_execute_requires_confirm(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]
    plan = _plan_replace(service, backup_id)

    with pytest.raises(BackupRestoreError):
        service.restore_from_plan(plan["plan_id"], confirm=False)


def test_restore_execute_creates_rollback_before_write(tmp_path):
    root = _build_install(tmp_path)
    original = (root / "config" / "config.json").read_text()
    _make_config_archive(root)
    (root / "config" / "config.json").write_text('{"changed": true}')
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]
    plan = _plan_replace(service, backup_id)

    result = service.restore_from_plan(plan["plan_id"], confirm=True)
    assert result["ok"] is True
    assert result["rollback_backup"].startswith("ems-config-rollback-")
    assert (root / "config" / "config.json").read_text() == original


def test_restore_execute_aborts_when_rollback_creation_fails(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    (root / "config" / "config.json").write_text('{"changed": true}')
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]
    plan = _plan_replace(service, backup_id)

    def _boom(env, target):
        raise backup_mod.BackupError("disk full")

    service._create_rollback = _boom
    result = service.restore_from_plan(plan["plan_id"], confirm=True)
    assert result["ok"] is False
    assert "rollback" in result["message"].lower()
    # Restore never started, so the changed file is untouched.
    assert (root / "config" / "config.json").read_text() == '{"changed": true}'


def test_restore_execute_replace_conflict_restores_file(tmp_path):
    root = _build_install(tmp_path)
    original = (root / "config" / "config.json").read_text()
    _make_config_archive(root)
    (root / "config" / "config.json").write_text('{"changed": true}')
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]
    plan = _plan_replace(service, backup_id)

    result = service.restore_from_plan(plan["plan_id"], confirm=True)
    assert result["ok"] is True
    assert any(a.get("action") == "restored" for a in result["actions"])
    assert (root / "config" / "config.json").read_text() == original


def test_restore_execute_keep_conflict_preserves_current_file(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    (root / "config" / "config.json").write_text('{"changed": true}')
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]
    plan = _plan_replace(service, backup_id, conflict_policy="keep")

    result = service.restore_from_plan(plan["plan_id"], confirm=True)
    assert result["ok"] is True
    assert any(a.get("action") == "kept_current" for a in result["actions"])
    assert (root / "config" / "config.json").read_text() == '{"changed": true}'


def test_restore_execute_aborts_on_conflict_policy_abort(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    (root / "config" / "config.json").write_text('{"changed": true}')
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]
    plan = service.create_restore_plan(
        {"id": backup_id, "scope": "config", "conflict_policy": "abort"}
    )
    assert plan["blocked"] is True

    with pytest.raises(BackupRestoreError):
        service.restore_from_plan(plan["plan_id"], confirm=True)
    assert (root / "config" / "config.json").read_text() == '{"changed": true}'


def test_restore_auto_rollback_restores_original_after_postcheck_failure(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    (root / "config" / "config.json").write_text('{"changed": true}')
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]
    plan = _plan_replace(service, backup_id)

    service._post_restore_check = lambda env, target: (False, "simulated failure")
    result = service.restore_from_plan(plan["plan_id"], confirm=True)
    assert result["ok"] is False
    assert result["status"] == "rolled_back"
    # Auto rollback returns the file to its pre-restore (changed) content.
    assert (root / "config" / "config.json").read_text() == '{"changed": true}'


def test_restore_auto_rollback_failure_is_reported(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    (root / "config" / "config.json").write_text('{"changed": true}')
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]
    plan = _plan_replace(service, backup_id)

    service._post_restore_check = lambda env, target: (False, "simulated failure")

    def _boom(env, rollback_path, password):
        raise backup_mod.BackupError("rollback archive unreadable")

    service._apply_rollback = _boom
    result = service.restore_from_plan(plan["plan_id"], confirm=True)
    assert result["status"] == "rollback_failed"
    assert "manual recovery" in result["message"].lower()


def test_restore_does_not_switch_docker_image_from_manifest(tmp_path):
    root = _build_install(tmp_path)
    compose_before = (root / "docker-compose.yml").read_text()
    _make_config_archive(root)
    (root / "config" / "config.json").write_text('{"changed": true}')
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]
    plan = _plan_replace(service, backup_id)

    result = service.restore_from_plan(plan["plan_id"], confirm=True)
    assert result["ok"] is True
    # Restore only touches backed-up files; compose/image is never changed.
    assert (root / "docker-compose.yml").read_text() == compose_before


# --- InfluxDB restore is blocked in Admin -----------------------------------

def _future_plan_times():
    now = datetime.now(timezone.utc)
    return (
        now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        (now + timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def test_restore_preview_blocks_influxdb_archive_without_writing(tmp_path):
    root = _build_install(tmp_path)
    _make_influxdb_archive(root)
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    config_before = (root / "config" / "config.json").read_text()
    runtime_before = (root / "data" / "runtime-state.json").read_text()

    with pytest.raises(BackupRestoreError) as exc:
        service.create_restore_plan({"id": backup_id, "scope": "influxdb"})
    message = str(exc.value)
    assert "InfluxDB restore" in message
    assert "Admin" in message

    # No plan is ever stored, so execute stays naturally safe.
    assert service.plans._plans == {}
    # Preview must not write anything to the live install.
    assert (root / "config" / "config.json").read_text() == config_before
    assert (root / "data" / "runtime-state.json").read_text() == runtime_before


def test_restore_execute_rejects_plan_containing_influxdb_target(tmp_path, monkeypatch):
    root = _build_install(tmp_path)
    path = _make_influxdb_archive(root)
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    # A hand-crafted plan must not bypass the preview guard.
    created_at, expires_at = _future_plan_times()
    target = RestorePlanTarget(
        archive_id=backup_id, name=os.path.basename(path),
        backup_type="influxdb", archive_sha256=backup_mod._sha256_file(path),
    )
    plan = RestorePlan(
        plan_id="crafted", kind="archive", backup_id=backup_id, scope="influxdb",
        conflict_policy="replace", rollback_enabled=False,
        auto_rollback_enabled=False, password=None, created_at=created_at,
        expires_at=expires_at, targets=[target], manifest_summary=None, files=[],
        summary={}, warnings=[], blocked=False, block_reason=None,
    )
    service.plans.put(plan)

    def fail_generic_restore(*args, **kwargs):
        raise AssertionError("generic restore must not be used for influxdb")

    monkeypatch.setattr(backup_mod, "restore_backup", fail_generic_restore)

    with pytest.raises(BackupRestoreError) as exc:
        service.restore_from_plan("crafted", confirm=True)
    assert "InfluxDB restore" in str(exc.value)


def test_system_set_restore_blocks_when_influxdb_member_is_present(tmp_path):
    root = _build_install(tmp_path)
    config_path = _make_config_archive(root)
    db_path = _make_database_archive(root)
    influx_path = _make_influxdb_archive(root)
    service = _service(root)

    store = BackupStore(resolve_env(detect_install_context(base_dir=str(root))))
    set_record = {
        "id": "2026-01-01-000000-system",
        "created_at": "2026-01-01T00:00:00Z",
        "purpose": "manual",
        "label": "System backup",
        "archives": [
            {"type": "config", "name": os.path.basename(config_path),
             "optional": False},
            {"type": "databases", "name": os.path.basename(db_path),
             "optional": False},
            {"type": "influxdb", "name": os.path.basename(influx_path),
             "optional": True},
        ],
        "status": "complete",
        "warnings": [],
    }
    store.write_set(set_record)

    (root / "config" / "config.json").write_text('{"changed": true}')

    with pytest.raises(BackupRestoreError) as exc:
        service.create_restore_plan({"id": set_record["id"], "scope": "system"})
    assert "InfluxDB restore is not available" in str(exc.value)

    # The whole set is blocked; no config/database member is restored partially.
    assert service.plans._plans == {}
    assert (root / "config" / "config.json").read_text() == '{"changed": true}'


def test_influxdb_backup_can_still_be_listed_inspected_and_deleted(tmp_path):
    root = _build_install(tmp_path)
    influx_path = _make_influxdb_archive(root)
    service = _service(root)

    record = next(
        r for r in service.list_backups()["backups"] if r["backup_type"] == "influxdb"
    )

    inspected = service.inspect_backup(record["id"])
    assert inspected["ok"] is True
    assert inspected["manifest"]["backup_type"] == "influxdb"

    service.delete_backup(record["id"], confirm=True)
    assert service.list_backups()["summary"]["total"] == 0
    assert not os.path.exists(influx_path)

    # No path argument can be used to delete outside the backup directory.
    with pytest.raises(BackupRestoreError):
        service.delete_backup("../../etc/passwd", confirm=True)


# --- delete -----------------------------------------------------------------

def test_delete_backup_requires_confirm(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    service = _service(root)
    backup_id = service.list_backups()["backups"][0]["id"]

    with pytest.raises(BackupRestoreError):
        service.delete_backup(backup_id, confirm=False)
    assert service.list_backups()["summary"]["total"] == 1


def test_delete_backup_removes_only_selected_archive(tmp_path):
    root = _build_install(tmp_path)
    first = _make_config_archive(root)
    second = _make_config_archive(root, backup_purpose="rollback", rollback_for="x")
    service = _service(root)
    records = service.list_backups()["backups"]
    target = next(r for r in records if r["name"] == os.path.basename(first))

    service.delete_backup(target["id"], confirm=True)
    remaining = {r["name"] for r in service.list_backups()["backups"]}
    assert os.path.basename(first) not in remaining
    assert os.path.basename(second) in remaining


def test_delete_backup_rejects_unknown_or_unsafe_id(tmp_path):
    root = _build_install(tmp_path)
    _make_config_archive(root)
    service = _service(root)
    for bad in ("../../etc/passwd", "nope"):
        with pytest.raises(BackupRestoreError):
            service.delete_backup(bad, confirm=True)


def test_delete_backup_set_metadata_only_keeps_archives(tmp_path):
    root = _build_install(tmp_path)
    service = _service(root)
    created = service.create_backup({"scope": "system"})
    set_id = created["backup_set"]["id"]
    member_names = {a["name"] for a in created["archives"]}

    service.delete_backup(set_id, confirm=True, mode="metadata_only")
    assert service.list_backups()["sets"] == []
    remaining = {r["name"] for r in service.list_backups()["backups"]}
    assert member_names <= remaining


def test_delete_backup_set_with_archives_requires_explicit_mode(tmp_path):
    root = _build_install(tmp_path)
    service = _service(root)
    created = service.create_backup({"scope": "system"})
    set_id = created["backup_set"]["id"]
    member_names = {a["name"] for a in created["archives"]}

    with pytest.raises(BackupRestoreError):
        service.delete_backup(set_id, confirm=True, mode="archive")

    service.delete_backup(set_id, confirm=True, mode="metadata_and_archives")
    remaining = {r["name"] for r in service.list_backups()["backups"]}
    assert not (member_names & remaining)
    assert service.list_backups()["sets"] == []
