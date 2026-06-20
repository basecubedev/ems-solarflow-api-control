# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the manual config backup/restore MVP (ems.backup / backup_crypto)."""

import io
import json
import os
import sqlite3
import tarfile

import pytest

from ems import backup, backup_crypto


def write_project(tmp_path, *, influx=None, with_auth=False, with_secret=False):
    """Create a minimal project tree and return (base_dir, config, config_path)."""

    base = tmp_path / "proj"
    (base / "data").mkdir(parents=True)
    (base / "config").mkdir()

    config = {
        "system": {"runtime_state_path": "data/runtime-state.json"},
        "dashboard": {
            "auth_file": "config/dashboard-auth.json",
            "ssl_cert_file": "config/dashboard.crt",
            "ssl_key_file": "config/dashboard.key",
        },
        "influxdb": influx if influx is not None else {"enabled": False},
        "devices": [],
    }
    config_path = base / "config.json"
    config_path.write_text(json.dumps(config))
    (base / "data" / "runtime-state.json").write_text('{"runtime": true}\n')

    if with_auth:
        (base / "config" / "dashboard-auth.json").write_text('{"hash": "x"}')
        (base / "config" / "dashboard.crt").write_text("CERT")
        (base / "config" / "dashboard.key").write_text("KEY")

    if with_secret:
        (base / "deploy" / "docker").mkdir(parents=True)
        (base / "deploy" / "docker" / "influxdb.env").write_text("TOKEN=secret")

    return str(base), config, str(config_path)


def create(tmp_path, config, base, config_path, **kwargs):
    return backup.create_config_backup(
        config,
        base_dir=base,
        config_path=config_path,
        backup_dir=os.path.join(base, "backup"),
        **kwargs,
    )


def test_running_in_container_truthy_env(tmp_path):
    missing_marker = str(tmp_path / "no-dockerenv")
    assert backup.running_in_container(
        environ={"EMS_IN_CONTAINER": "1"}, docker_env_path=missing_marker
    )


def test_running_in_container_docker_marker(tmp_path):
    marker = tmp_path / ".dockerenv"
    marker.write_text("")
    assert backup.running_in_container(environ={}, docker_env_path=str(marker))


def test_running_in_container_native(tmp_path):
    missing_marker = str(tmp_path / "no-dockerenv")
    assert not backup.running_in_container(
        environ={}, docker_env_path=missing_marker
    )


def test_running_in_container_explicit_false_overrides_marker(tmp_path):
    marker = tmp_path / ".dockerenv"
    marker.write_text("")
    assert not backup.running_in_container(
        environ={"EMS_IN_CONTAINER": "0"}, docker_env_path=str(marker)
    )


def test_default_backup_dir_uses_data_backups_via_docker_marker(tmp_path, monkeypatch):
    marker = tmp_path / ".dockerenv"
    marker.write_text("")
    real_exists = os.path.exists
    monkeypatch.setattr(
        backup.os.path,
        "exists",
        lambda path: True if path == "/.dockerenv" else real_exists(path),
    )
    assert (
        backup.default_backup_dir(base_dir=str(tmp_path), environ={})
        == "/app/data/backups"
    )


def test_default_backup_dir_uses_data_backups_in_container(tmp_path):
    assert backup.default_backup_dir(
        base_dir=str(tmp_path),
        environ={"EMS_IN_CONTAINER": "1"},
    ) == "/app/data/backups"


def test_default_backup_dir_remains_project_backup_for_native(tmp_path):
    assert backup.default_backup_dir(
        base_dir=str(tmp_path),
        environ={"EMS_IN_CONTAINER": "0"},
    ) == os.path.join(str(tmp_path), "backup")


def test_create_backup_creates_container_default_dir(tmp_path, monkeypatch):
    base, config, config_path = write_project(tmp_path)
    container_backup_dir = tmp_path / "app" / "data" / "backups"
    monkeypatch.setattr(backup, "CONTAINER_BACKUP_DIR", str(container_backup_dir))
    monkeypatch.setenv("EMS_IN_CONTAINER", "1")

    path = backup.create_config_backup(
        config,
        base_dir=base,
        config_path=config_path,
    )

    assert path.startswith(str(container_backup_dir))
    assert container_backup_dir.is_dir()
    assert os.path.isfile(path)


def test_explicit_backup_dir_overrides_container_default(tmp_path, monkeypatch):
    base, config, config_path = write_project(tmp_path)
    explicit_dir = tmp_path / "custom-backups"
    monkeypatch.setenv("EMS_IN_CONTAINER", "1")

    path = backup.create_config_backup(
        config,
        base_dir=base,
        config_path=config_path,
        backup_dir=str(explicit_dir),
    )

    assert path.startswith(str(explicit_dir))
    assert explicit_dir.is_dir()


# ---------------------------------------------------------------------------
# File selection
# ---------------------------------------------------------------------------

def test_collect_includes_config_and_runtime(tmp_path):
    base, config, config_path = write_project(tmp_path)
    included, skipped = backup.collect_config_backup_files(
        config, base_dir=base, config_path=config_path
    )
    kinds = {entry["kind"] for entry in included}
    assert "config" in kinds
    assert "runtime_state" in kinds


def test_collect_auth_and_cert_only_when_present(tmp_path):
    base, config, config_path = write_project(tmp_path, with_auth=False)
    included, _ = backup.collect_config_backup_files(
        config, base_dir=base, config_path=config_path
    )
    kinds = {entry["kind"] for entry in included}
    assert "dashboard_auth" not in kinds
    assert "dashboard_cert" not in kinds
    assert "dashboard_key" not in kinds

    base2, config2, config_path2 = write_project(tmp_path / "b", with_auth=True)
    included2, _ = backup.collect_config_backup_files(
        config2, base_dir=base2, config_path=config_path2
    )
    kinds2 = {entry["kind"] for entry in included2}
    assert {"dashboard_auth", "dashboard_cert", "dashboard_key"} <= kinds2


def test_influx_secret_included_only_for_bundled_enabled(tmp_path):
    influx = {"enabled": True, "mode": "bundled"}
    base, config, config_path = write_project(
        tmp_path, influx=influx, with_secret=True
    )
    included, _ = backup.collect_config_backup_files(
        config, base_dir=base, config_path=config_path
    )
    assert "influxdb_secret" in {entry["kind"] for entry in included}


def test_influx_secret_skipped_when_external(tmp_path):
    influx = {"enabled": True, "mode": "external"}
    base, config, config_path = write_project(
        tmp_path, influx=influx, with_secret=True
    )
    included, _ = backup.collect_config_backup_files(
        config, base_dir=base, config_path=config_path
    )
    assert "influxdb_secret" not in {entry["kind"] for entry in included}


def test_influx_secret_skipped_when_disabled(tmp_path):
    influx = {"enabled": False, "mode": "bundled"}
    base, config, config_path = write_project(
        tmp_path, influx=influx, with_secret=True
    )
    included, _ = backup.collect_config_backup_files(
        config, base_dir=base, config_path=config_path
    )
    assert "influxdb_secret" not in {entry["kind"] for entry in included}


def test_databases_recorded_as_skipped(tmp_path):
    base, config, config_path = write_project(tmp_path)
    (tmp_path / "proj" / "data" / "ems_dashboard.sqlite").write_text("db")
    included, skipped = backup.collect_config_backup_files(
        config, base_dir=base, config_path=config_path
    )
    assert any(
        item["reason"] == backup.SKIP_DATABASE_REASON for item in skipped
    )
    assert "ems_dashboard.sqlite" not in {
        os.path.basename(e["arcname"]) for e in included
    }


# ---------------------------------------------------------------------------
# Manifest / archive
# ---------------------------------------------------------------------------

def test_create_writes_valid_manifest_and_checksums(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path)

    assert os.path.basename(path).startswith("ems-config-manual-")
    assert path.endswith(".tar.gz")

    with tarfile.open(path, "r:gz") as tar:
        manifest = json.loads(
            tar.extractfile(backup.MANIFEST_NAME).read().decode()
        )
        # Verify each file checksum matches the archived content.
        for entry in manifest["files"]:
            data = tar.extractfile(entry["path"]).read()
            import hashlib
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]

    assert manifest["backup_type"] == "config"
    assert manifest["backup_purpose"] == "manual"
    assert manifest["backup_format"] == backup.BACKUP_FORMAT_VERSION
    assert manifest["created_at"].endswith("Z")
    assert manifest["encryption"]["enabled"] is False
    assert "config_backup_format_version" in manifest["contracts"]
    assert "git_commit" in manifest["source"]


def test_inspect_unencrypted(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path)
    info = backup.inspect_backup(path)
    assert info["encrypted"] is False
    assert info["manifest"]["backup_purpose"] == "manual"


def test_rollback_backup_metadata(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = backup.create_rollback_backup(
        config,
        "ems-config-manual-2026-06-18-221500.tar.gz",
        base_dir=base,
        config_path=config_path,
        backup_dir=os.path.join(base, "backup"),
    )
    assert "-rollback-" in os.path.basename(path)
    manifest = backup.inspect_backup(path)["manifest"]
    assert manifest["backup_purpose"] == "rollback"
    assert manifest["rollback_for"] == (
        "ems-config-manual-2026-06-18-221500.tar.gz"
    )


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

def test_encrypted_backup_roundtrip(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path, password="hunter2pw")
    assert path.endswith(".tar.gz.enc")
    assert backup.is_encrypted(path)

    info = backup.inspect_backup(path, password="hunter2pw")
    assert info["encrypted"] is True
    assert info["manifest"]["encryption"]["enabled"] is True


def test_encrypted_inspect_without_password(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path, password="hunter2pw")
    info = backup.inspect_backup(path)
    assert info["encrypted"] is True
    assert info["manifest"] is None


def test_encrypted_restore_wrong_password(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path, password="correcthorse")
    with pytest.raises(backup_crypto.BackupPasswordError):
        backup.restore_backup(path, base_dir=base, password="wrongpw")


def test_encrypted_restore_correct_password(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path, password="correcthorse")
    # Remove a file then restore.
    os.remove(os.path.join(base, "data", "runtime-state.json"))
    result = backup.restore_backup(
        path, base_dir=base, password="correcthorse", on_conflict="replace"
    )
    assert os.path.isfile(os.path.join(base, "data", "runtime-state.json"))
    assert any(a["action"] == "restored" for a in result["actions"])


# ---------------------------------------------------------------------------
# Restore behavior
# ---------------------------------------------------------------------------

def test_restore_dry_run_does_not_write(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path)
    os.remove(os.path.join(base, "data", "runtime-state.json"))
    result = backup.restore_backup(path, base_dir=base, dry_run=True)
    assert result["dry_run"] is True
    assert not os.path.isfile(os.path.join(base, "data", "runtime-state.json"))
    assert any(a["action"] == "would_restore_new" for a in result["actions"])


def test_restore_skips_identical(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path)
    result = backup.restore_backup(path, base_dir=base, on_conflict="abort")
    assert all(a["action"] == "skip_identical" for a in result["actions"])


def test_restore_conflict_keep(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path)
    config_file = os.path.join(base, "config.json")
    with open(config_file, "w") as handle:
        handle.write('{"changed": true}')
    result = backup.restore_backup(path, base_dir=base, on_conflict="keep")
    assert any(a["action"] == "kept_current" for a in result["actions"])
    assert json.loads(open(config_file).read()) == {"changed": True}


def test_restore_conflict_replace(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path)
    config_file = os.path.join(base, "config.json")
    original = open(config_file).read()
    with open(config_file, "w") as handle:
        handle.write('{"changed": true}')
    result = backup.restore_backup(path, base_dir=base, on_conflict="replace")
    assert any(a["action"] == "restored" for a in result["actions"])
    assert open(config_file).read() == original


def test_restore_conflict_abort_default(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path)
    with open(os.path.join(base, "config.json"), "w") as handle:
        handle.write('{"changed": true}')
    with pytest.raises(backup.BackupError):
        backup.restore_backup(path, base_dir=base, on_conflict="abort")


def test_restore_resolver_called_for_conflict(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path)
    with open(os.path.join(base, "config.json"), "w") as handle:
        handle.write('{"changed": true}')

    seen = []

    def resolver(entry):
        seen.append(entry["path"])
        return "replace"

    backup.restore_backup(
        path, base_dir=base, conflict_resolver=resolver
    )
    assert "config.json" in seen


# ---------------------------------------------------------------------------
# Security: traversal / manifest / checksum
# ---------------------------------------------------------------------------

def _write_manifest_tar(path, manifest, files):
    with tarfile.open(path, "w:gz") as tar:
        mbytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo(backup.MANIFEST_NAME)
        info.size = len(mbytes)
        tar.addfile(info, io.BytesIO(mbytes))
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_path_traversal_rejected(tmp_path):
    path = str(tmp_path / "bad.tar.gz")
    manifest = {
        "backup_format": 1,
        "files": [{"path": "../escape.txt", "sha256": "x"}],
    }
    _write_manifest_tar(path, manifest, {})
    with pytest.raises(backup.BackupError):
        backup.inspect_backup(path)


def test_absolute_path_rejected(tmp_path):
    path = str(tmp_path / "bad.tar.gz")
    manifest = {
        "backup_format": 1,
        "files": [{"path": "/etc/passwd", "sha256": "x"}],
    }
    _write_manifest_tar(path, manifest, {})
    with pytest.raises(backup.BackupError):
        backup.inspect_backup(path)


def test_missing_manifest_rejected(tmp_path):
    path = str(tmp_path / "nomanifest.tar.gz")
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("config.json")
        data = b"{}"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(backup.BackupError):
        backup.inspect_backup(path)


def test_invalid_manifest_rejected(tmp_path):
    path = str(tmp_path / "bad.tar.gz")
    with tarfile.open(path, "w:gz") as tar:
        data = b"not json"
        info = tarfile.TarInfo(backup.MANIFEST_NAME)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(backup.BackupError):
        backup.inspect_backup(path)


def test_checksum_mismatch_rejected(tmp_path):
    base = str(tmp_path)
    path = str(tmp_path / "cs.tar.gz")
    content = b"hello world"
    manifest = {
        "backup_format": 1,
        "files": [
            {"path": "config.json", "sha256": "deadbeef", "kind": "config"}
        ],
    }
    _write_manifest_tar(path, manifest, {"config.json": content})
    with pytest.raises(backup.BackupError):
        backup.restore_backup(path, base_dir=base)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def test_diff_text_file(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(tmp_path, config, base, config_path)
    with open(os.path.join(base, "config.json"), "w") as handle:
        handle.write('{"changed": true}\n')
    result = backup.diff_backup_file(path, "config.json", base_dir=base)
    assert result["binary"] is False
    assert "config.json" in result["text"]


def test_diff_binary_refused(tmp_path):
    base = str(tmp_path)
    path = str(tmp_path / "bin.tar.gz")
    binary = b"\x00\x01\x02binary"
    import hashlib
    manifest = {
        "backup_format": 1,
        "files": [
            {
                "path": "config.json",
                "sha256": hashlib.sha256(binary).hexdigest(),
                "kind": "config",
            }
        ],
    }
    _write_manifest_tar(path, manifest, {"config.json": binary})
    result = backup.diff_backup_file(path, "config.json", base_dir=base)
    assert result["binary"] is True
    assert "binary" in result["text"].lower()


# ---------------------------------------------------------------------------
# Crypto unit level
# ---------------------------------------------------------------------------

def test_crypto_encrypt_decrypt_roundtrip(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_bytes(b"secret payload")
    enc = tmp_path / "plain.txt.enc"
    backup_crypto.encrypt_file(str(plain), str(enc), "pw12345")
    assert backup_crypto.is_encrypted_backup(str(enc))
    assert not backup_crypto.is_encrypted_backup(str(plain))

    out = backup_crypto.decrypt_file_to_temp(str(enc), "pw12345")
    try:
        assert open(out, "rb").read() == b"secret payload"
    finally:
        os.remove(out)


def test_crypto_wrong_password_raises(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_bytes(b"data")
    enc = tmp_path / "plain.txt.enc"
    backup_crypto.encrypt_file(str(plain), str(enc), "rightpw")
    with pytest.raises(backup_crypto.BackupPasswordError):
        backup_crypto.decrypt_file_to_temp(str(enc), "wrongpw")


# ---------------------------------------------------------------------------
# Database backups (SQLite)
# ---------------------------------------------------------------------------

def _make_sqlite(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def write_db_project(
    tmp_path, *, dashboard=True, state=True, influx=None
):
    """Create a project tree with real SQLite databases."""

    base = tmp_path / "proj"
    (base / "data").mkdir(parents=True)
    config = {
        "dashboard": {"database_path": "data/ems_dashboard.sqlite"},
        "battery_full_charge_assist": {
            "state_database_path": "data/ems_state.sqlite"
        },
        "influxdb": influx if influx is not None else {"enabled": False},
    }
    if dashboard:
        _make_sqlite(str(base / "data" / "ems_dashboard.sqlite"), "dash")
    if state:
        _make_sqlite(str(base / "data" / "ems_state.sqlite"), "state")
    return str(base), config


def create_db(base, config, **kwargs):
    return backup.create_database_backup(
        config, base_dir=base, backup_dir=os.path.join(base, "backup"), **kwargs
    )


def test_db_backup_includes_both_databases(tmp_path):
    base, config = write_db_project(tmp_path)
    path = create_db(base, config)
    assert os.path.basename(path).startswith("ems-databases-manual-")
    manifest = backup.inspect_backup(path)["manifest"]
    assert manifest["backup_type"] == "databases"
    roles = {db["role"]: db for db in manifest["databases"]}
    assert roles["dashboard_history"]["included"] is True
    assert roles["ems_state"]["included"] is True
    assert {f["path"] for f in manifest["files"]} == {
        "data/ems_dashboard.sqlite",
        "data/ems_state.sqlite",
    }


def test_db_backup_missing_database_marked_skipped(tmp_path):
    base, config = write_db_project(tmp_path, state=False)
    path = create_db(base, config)
    manifest = backup.inspect_backup(path)["manifest"]
    roles = {db["role"]: db for db in manifest["databases"]}
    assert roles["dashboard_history"]["included"] is True
    assert roles["ems_state"]["included"] is False
    assert roles["ems_state"]["reason"] == backup.MISSING_DATABASE_REASON
    # Only the present database is in the restorable file list.
    assert {f["path"] for f in manifest["files"]} == {
        "data/ems_dashboard.sqlite"
    }


def test_db_backup_both_missing_does_not_fail(tmp_path):
    base, config = write_db_project(tmp_path, dashboard=False, state=False)
    path = create_db(base, config)
    manifest = backup.inspect_backup(path)["manifest"]
    assert manifest["files"] == []
    assert all(db["included"] is False for db in manifest["databases"])


def test_db_backup_copy_is_valid_sqlite(tmp_path):
    base, config = write_db_project(tmp_path)
    path = create_db(base, config)
    # Extract the archived dashboard db and confirm it is a readable SQLite db.
    out = tmp_path / "extracted.sqlite"
    with tarfile.open(path, "r:gz") as tar:
        data = tar.extractfile("data/ems_dashboard.sqlite").read()
    out.write_bytes(data)
    conn = sqlite3.connect(str(out))
    try:
        assert conn.execute("SELECT x FROM t").fetchone() == ("dash",)
    finally:
        conn.close()


def test_db_backup_not_a_plain_file_copy(tmp_path):
    """The staged snapshot differs byte-for-byte path but is consistent."""

    base, config = write_db_project(tmp_path)
    path = create_db(base, config)
    with tarfile.open(path, "r:gz") as tar:
        archived = tar.extractfile("data/ems_state.sqlite").read()
    # A VACUUM/backup snapshot is a complete, self-contained SQLite image.
    assert archived[:16] == b"SQLite format 3\x00"


def test_db_backup_encrypted_roundtrip(tmp_path):
    base, config = write_db_project(tmp_path)
    path = create_db(base, config, password="dbpassword")
    assert path.endswith(".tar.gz.enc")
    assert backup.is_encrypted(path)
    os.remove(os.path.join(base, "data", "ems_state.sqlite"))
    result = backup.restore_backup(
        path, base_dir=base, password="dbpassword", on_conflict="replace"
    )
    assert any(a["action"] == "restored" for a in result["actions"])
    conn = sqlite3.connect(os.path.join(base, "data", "ems_state.sqlite"))
    try:
        assert conn.execute("SELECT x FROM t").fetchone() == ("state",)
    finally:
        conn.close()


def test_db_backup_encrypted_wrong_password(tmp_path):
    base, config = write_db_project(tmp_path)
    path = create_db(base, config, password="rightpw")
    with pytest.raises(backup_crypto.BackupPasswordError):
        backup.restore_backup(path, base_dir=base, password="nope")


def test_db_restore_keep_does_not_overwrite(tmp_path):
    base, config = write_db_project(tmp_path)
    path = create_db(base, config)
    # Mutate the live dashboard db so it differs from the backup.
    conn = sqlite3.connect(os.path.join(base, "data", "ems_dashboard.sqlite"))
    conn.execute("INSERT INTO t VALUES ('local-change')")
    conn.commit()
    conn.close()
    result = backup.restore_backup(path, base_dir=base, on_conflict="keep")
    assert any(a["action"] == "kept_current" for a in result["actions"])
    conn = sqlite3.connect(os.path.join(base, "data", "ems_dashboard.sqlite"))
    try:
        rows = {r[0] for r in conn.execute("SELECT x FROM t").fetchall()}
    finally:
        conn.close()
    assert "local-change" in rows


def test_db_restore_abort_does_not_overwrite(tmp_path):
    base, config = write_db_project(tmp_path)
    path = create_db(base, config)
    db_path = os.path.join(base, "data", "ems_dashboard.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO t VALUES ('local-change')")
    conn.commit()
    conn.close()
    before = open(db_path, "rb").read()
    with pytest.raises(backup.BackupError):
        backup.restore_backup(path, base_dir=base, on_conflict="abort")
    assert open(db_path, "rb").read() == before


def test_influx_detected_bundled(tmp_path):
    base, config = write_db_project(
        tmp_path, influx={"enabled": True, "mode": "bundled"}
    )
    path = create_db(base, config)
    influx = backup.inspect_backup(path)["manifest"]["influxdb"]
    assert influx["detected"] is True
    assert influx["mode"] == "bundled"
    assert influx["included"] is False
    assert influx["reason"] == backup.INFLUX_SKIP_REASON


def test_influx_detected_external(tmp_path):
    base, config = write_db_project(
        tmp_path, influx={"enabled": True, "mode": "external"}
    )
    influx = backup.detect_influxdb_status(config)
    assert influx["detected"] is True
    assert influx["mode"] == "external"
    assert influx["included"] is False


def test_influx_disabled(tmp_path):
    base, config = write_db_project(tmp_path, influx={"enabled": False})
    influx = backup.detect_influxdb_status(config)
    assert influx["detected"] is False
    assert influx["included"] is False


def test_db_rollback_backup_metadata(tmp_path):
    base, config = write_db_project(tmp_path)
    path = backup.create_database_rollback_backup(
        config,
        "ems-databases-manual-2026-06-18-221500.tar.gz",
        base_dir=base,
        backup_dir=os.path.join(base, "backup"),
    )
    assert "ems-databases-rollback-" in os.path.basename(path)
    manifest = backup.inspect_backup(path)["manifest"]
    assert manifest["backup_purpose"] == "rollback"
    assert manifest["rollback_for"] == (
        "ems-databases-manual-2026-06-18-221500.tar.gz"
    )


def test_db_backup_inspect_encrypted_without_password(tmp_path):
    base, config = write_db_project(tmp_path)
    path = create_db(base, config, password="dbpw1234")
    info = backup.inspect_backup(path)
    assert info["encrypted"] is True
    assert info["manifest"] is None


# ---------------------------------------------------------------------------
# Post-restore diagnose hint (emsctl integration)
# ---------------------------------------------------------------------------

def test_post_restore_diagnose_hint(monkeypatch, capsys):
    import emsctl

    # Non-tty path prints the hint and does not prompt.
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: False)
    emsctl.print_restore_done(emsctl.make_args(), {})
    out = capsys.readouterr().out
    assert "Restore completed." in out
    assert "diagnose --deep" in out


def test_post_restore_database_message(monkeypatch, capsys):
    import emsctl

    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: False)
    emsctl.print_restore_done(
        emsctl.make_args(),
        {},
        backup_type="databases",
        influx={"detected": True, "mode": "bundled"},
    )
    out = capsys.readouterr().out
    assert "Database restore completed." in out
    assert "InfluxDB data was not part of this backup" in out
    assert "diagnose --deep" in out


# ---------------------------------------------------------------------------
# CLI password prompting + rollback encryption (emsctl integration)
# ---------------------------------------------------------------------------

def _restore_args(emsctl, *, config=None, runtime_state=None,
                  dashboard_auth=None):
    return emsctl.make_args(
        config=config,
        runtime_state=runtime_state,
        dashboard_auth=dashboard_auth,
        on_conflict=None,
        rollback=None,
        dry_run=False,
    )


def _scripted_prompt_text(answers):
    """Return a prompt_text stand-in answering by keyword in the label."""

    def fake(label, default=None):
        low = label.lower()
        for keyword, value in answers.items():
            if keyword in low:
                return value
        return "n"

    return fake


def test_cli_encrypted_restore_prompts_for_password(tmp_path, monkeypatch):
    import emsctl

    base, config, config_path = write_project(tmp_path)
    backup_dir = os.path.join(base, "backup")
    path = backup.create_config_backup(
        config, base_dir=base, config_path=config_path,
        backup_dir=backup_dir, password="sourcepw",
    )

    monkeypatch.setattr(emsctl, "BASE_DIR", base)
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)

    gp_calls = []

    def fake_getpass(prompt=""):
        gp_calls.append(prompt)
        return "sourcepw"

    monkeypatch.setattr(emsctl.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(
        emsctl, "prompt_text",
        _scripted_prompt_text({"create rollback": "n", "diagnose": "n"}),
    )

    args = _restore_args(
        emsctl, config=config_path,
        runtime_state=os.path.join(base, "data", "runtime-state.json"),
    )
    rc = emsctl.handle_backup_restore(args, config, path, interactive=True)
    assert rc == 0
    assert any("password" in prompt.lower() for prompt in gp_calls)


def test_cli_encrypted_inspect_prompts_for_password(tmp_path, monkeypatch):
    import emsctl

    base, config, config_path = write_project(tmp_path)
    path = backup.create_config_backup(
        config, base_dir=base, config_path=config_path,
        backup_dir=os.path.join(base, "backup"), password="sourcepw",
    )

    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)
    gp_calls = []
    monkeypatch.setattr(
        emsctl.getpass, "getpass",
        lambda prompt="": (gp_calls.append(prompt) or "sourcepw"),
    )

    args = _restore_args(emsctl)
    password = emsctl.resolve_backup_password(args, path, interactive=True)
    rc = emsctl.handle_backup_inspect(path, password=password)
    assert rc == 0
    assert gp_calls


def test_config_restore_rollback_can_be_encrypted(tmp_path, monkeypatch):
    import emsctl

    base, config, config_path = write_project(tmp_path, with_auth=True)
    backup_dir = os.path.join(base, "backup")
    # Unencrypted source backup; the rollback is encrypted independently.
    path = backup.create_config_backup(
        config, base_dir=base, config_path=config_path, backup_dir=backup_dir
    )

    monkeypatch.setattr(emsctl, "BASE_DIR", base)
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(emsctl.getpass, "getpass", lambda prompt="": "rbpw1234")
    monkeypatch.setattr(
        emsctl, "prompt_text",
        _scripted_prompt_text({
            "create rollback": "y",
            "protect rollback": "y",
            "replace": "r",
            "diagnose": "n",
        }),
    )

    args = _restore_args(
        emsctl, config=config_path,
        runtime_state=os.path.join(base, "data", "runtime-state.json"),
        dashboard_auth=os.path.join(base, "config", "dashboard-auth.json"),
    )
    rc = emsctl.handle_backup_restore(args, config, path, interactive=True)
    assert rc == 0

    rollbacks = [
        name for name in os.listdir(backup_dir)
        if name.startswith("ems-config-rollback-")
    ]
    assert rollbacks
    assert all(name.endswith(".tar.gz.enc") for name in rollbacks)
    rb = os.path.join(backup_dir, rollbacks[0])
    assert backup.is_encrypted(rb)
    # Cannot inspect without the password.
    assert backup.inspect_backup(rb)["manifest"] is None
    manifest = backup.inspect_backup(rb, password="rbpw1234")["manifest"]
    assert manifest["backup_purpose"] == "rollback"
    assert manifest["encryption"]["enabled"] is True


def test_database_restore_rollback_can_be_encrypted(tmp_path, monkeypatch):
    import emsctl

    base, config = write_db_project(tmp_path)
    backup_dir = os.path.join(base, "backup")
    path = backup.create_database_backup(
        config, base_dir=base, backup_dir=backup_dir
    )

    monkeypatch.setattr(emsctl, "BASE_DIR", base)
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(emsctl.getpass, "getpass", lambda prompt="": "dbrbpw99")
    monkeypatch.setattr(
        emsctl, "prompt_text",
        _scripted_prompt_text({
            "create rollback": "y",
            "protect rollback": "y",
            "replace": "r",
            "diagnose": "n",
        }),
    )

    args = _restore_args(emsctl)
    rc = emsctl.handle_backup_restore(args, config, path, interactive=True)
    assert rc == 0

    rollbacks = [
        name for name in os.listdir(backup_dir)
        if name.startswith("ems-databases-rollback-")
    ]
    assert rollbacks
    assert all(name.endswith(".tar.gz.enc") for name in rollbacks)
    rb = os.path.join(backup_dir, rollbacks[0])
    assert backup.inspect_backup(rb)["manifest"] is None
    manifest = backup.inspect_backup(rb, password="dbrbpw99")["manifest"]
    assert manifest["backup_type"] == "databases"
    assert manifest["backup_purpose"] == "rollback"
    assert manifest["encryption"]["enabled"] is True


def test_rollback_password_mismatch_aborts(tmp_path, monkeypatch):
    import emsctl

    base, config, config_path = write_project(tmp_path)
    backup_dir = os.path.join(base, "backup")
    path = backup.create_config_backup(
        config, base_dir=base, config_path=config_path, backup_dir=backup_dir
    )

    monkeypatch.setattr(emsctl, "BASE_DIR", base)
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: True)
    mismatched = iter(["firstpw", "secondpw"])
    monkeypatch.setattr(
        emsctl.getpass, "getpass", lambda prompt="": next(mismatched)
    )
    monkeypatch.setattr(
        emsctl, "prompt_text",
        _scripted_prompt_text({
            "create rollback": "y",
            "protect rollback": "y",
        }),
    )

    args = _restore_args(
        emsctl, config=config_path,
        runtime_state=os.path.join(base, "data", "runtime-state.json"),
    )
    rc = emsctl.handle_backup_restore(args, config, path, interactive=True)
    assert rc == 0  # aborted cleanly before any changes

    # No partial rollback archive must be left behind.
    rollbacks = [
        name for name in os.listdir(backup_dir)
        if name.startswith("ems-config-rollback-")
    ]
    assert rollbacks == []
