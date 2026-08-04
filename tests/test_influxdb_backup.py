# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for bundled InfluxDB data backup/restore.

Docker and InfluxDB are never executed here: the backup/restore *runners* (which
own the Docker orchestration) are replaced with fakes that write/read a local
directory, so these stay deterministic and offline.
"""

import os
import tarfile

import pytest

from ems import backup, backup_crypto, influx_setup

pytestmark = [
    pytest.mark.backup_restore,
    pytest.mark.unit,
]


def bundled_config(**overrides):
    influx = {"enabled": True, "mode": "bundled", "org": "ems",
              "bucket_prefix": "ems"}
    influx.update(overrides)
    return {"influxdb": influx, "devices": []}


def fake_backup_runner(*, files=("20260618T000000Z.bolt.gz", "data.tar.gz")):
    """Return a runner that drops a couple of fake influx-backup files."""

    def runner(influx_out_dir):
        for name in files:
            with open(os.path.join(influx_out_dir, name), "wb") as handle:
                handle.write(b"INFLUX-BACKUP-" + name.encode())

    return runner


# ---------------------------------------------------------------------------
# evaluate / skip / reject
# ---------------------------------------------------------------------------

def test_influxdb_backup_skips_when_disabled(tmp_path):
    config = {"influxdb": {"enabled": False}}
    evaluation = backup.evaluate_influxdb_backup(config)
    assert evaluation["supported"] is False
    assert evaluation["reason"] == "disabled"
    assert "disabled" in evaluation["message"].lower()
    with pytest.raises(backup.BackupError):
        backup.create_influxdb_backup(
            config, base_dir=str(tmp_path),
            backup_dir=str(tmp_path / "backup"),
            backup_runner=fake_backup_runner(),
        )


def test_influxdb_backup_rejects_external_mode(tmp_path):
    config = {"influxdb": {"enabled": True, "mode": "external"}}
    evaluation = backup.evaluate_influxdb_backup(config)
    assert evaluation["supported"] is False
    assert evaluation["reason"] == "external"
    assert "external" in evaluation["message"].lower()
    with pytest.raises(backup.BackupError):
        backup.create_influxdb_backup(
            config, base_dir=str(tmp_path),
            backup_dir=str(tmp_path / "backup"),
            backup_runner=fake_backup_runner(),
        )


# ---------------------------------------------------------------------------
# command builders (official influx CLI)
# ---------------------------------------------------------------------------

def test_influxdb_backup_uses_official_influx_backup_command():
    files = influx_setup.compose_files(
        {"enabled": True, "mode": "bundled"}
    )
    cmd = influx_setup.build_influx_backup_command(files, "/tmp/ems-influx-backup-x")
    joined = " ".join(cmd)
    assert "influx backup" in joined
    assert "exec" in cmd and "-T" in cmd
    assert influx_setup.INFLUX_SERVICE in cmd
    # The secret is referenced by env var name, not embedded literally.
    assert '"$INFLUXDB_TOKEN"' in joined


def test_influxdb_restore_uses_official_full_restore_command():
    files = ["a.yml"]
    cmd = influx_setup.build_influx_restore_command(files, "/tmp/ems-influx-restore-x")
    joined = " ".join(cmd)
    assert "influx restore --full" in joined
    assert "/tmp/ems-influx-restore-x" in joined


# ---------------------------------------------------------------------------
# packaging
# ---------------------------------------------------------------------------

def test_influxdb_backup_packages_manifest_and_backup_output(tmp_path):
    config = bundled_config()
    path = backup.create_influxdb_backup(
        config, base_dir=str(tmp_path),
        backup_dir=str(tmp_path / "backup"),
        backup_runner=fake_backup_runner(),
    )
    assert os.path.basename(path).startswith("ems-influxdb-manual-")
    assert path.endswith(".tar.gz")

    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
    assert backup.MANIFEST_NAME in names
    assert any(name.startswith("influxdb/") for name in names)

    manifest = backup.inspect_backup(path)["manifest"]
    assert manifest["backup_type"] == "influxdb"
    assert manifest["backup_purpose"] == "manual"
    block = manifest["influxdb"]
    assert block["included"] is True
    assert block["mode"] == "bundled"
    assert block["service"] == "influxdb"
    assert block["container_name"] == "ems-influxdb"
    assert block["backup_method"] == "influx backup"
    assert block["org"] == "ems"
    assert block["bucket_prefix"] == "ems"
    assert block["data_dir"] == "data/influxdb"
    # All archived files live under influxdb/ with checksums.
    assert all(f["path"].startswith("influxdb/") for f in manifest["files"])


def test_influxdb_backup_no_output_fails(tmp_path):
    config = bundled_config()
    with pytest.raises(backup.BackupError):
        backup.create_influxdb_backup(
            config, base_dir=str(tmp_path),
            backup_dir=str(tmp_path / "backup"),
            backup_runner=lambda out_dir: None,  # produces nothing
        )


# ---------------------------------------------------------------------------
# encryption
# ---------------------------------------------------------------------------

def test_influxdb_backup_encrypted_roundtrip(tmp_path):
    config = bundled_config()
    path = backup.create_influxdb_backup(
        config, base_dir=str(tmp_path),
        backup_dir=str(tmp_path / "backup"),
        password="influxpw1",
        backup_runner=fake_backup_runner(),
    )
    assert path.endswith(".tar.gz.enc")
    assert backup.is_encrypted(path)

    info = backup.inspect_backup(path, password="influxpw1")
    assert info["encrypted"] is True
    assert info["manifest"]["backup_type"] == "influxdb"
    assert info["manifest"]["encryption"]["enabled"] is True

    # Restore extracts the influx payload and hands it to the restore runner.
    received = {}

    def restore_runner(influx_dir):
        received["files"] = sorted(os.listdir(influx_dir))

    result = backup.restore_influxdb_backup(
        path, config, base_dir=str(tmp_path), password="influxpw1",
        restore_runner=restore_runner,
    )
    assert result["strategy"] == "replace"
    assert received["files"]


def test_influxdb_backup_wrong_password_fails(tmp_path):
    config = bundled_config()
    path = backup.create_influxdb_backup(
        config, base_dir=str(tmp_path),
        backup_dir=str(tmp_path / "backup"),
        password="rightpw",
        backup_runner=fake_backup_runner(),
    )
    with pytest.raises(backup_crypto.BackupPasswordError):
        backup.restore_influxdb_backup(
            path, config, base_dir=str(tmp_path), password="wrongpw",
            restore_runner=lambda influx_dir: None,
        )


def test_influxdb_backup_inspect_encrypted_without_password(tmp_path):
    config = bundled_config()
    path = backup.create_influxdb_backup(
        config, base_dir=str(tmp_path),
        backup_dir=str(tmp_path / "backup"),
        password="secretpw",
        backup_runner=fake_backup_runner(),
    )
    info = backup.inspect_backup(path)
    assert info["encrypted"] is True
    assert info["manifest"] is None


# ---------------------------------------------------------------------------
# restore core
# ---------------------------------------------------------------------------

def test_influxdb_restore_rejects_external_mode(tmp_path):
    config = bundled_config()
    path = backup.create_influxdb_backup(
        config, base_dir=str(tmp_path),
        backup_dir=str(tmp_path / "backup"),
        backup_runner=fake_backup_runner(),
    )
    external = {"influxdb": {"enabled": True, "mode": "external"}}
    with pytest.raises(backup.BackupError):
        backup.restore_influxdb_backup(
            path, external, base_dir=str(tmp_path),
            restore_runner=lambda influx_dir: None,
        )


def test_influxdb_restore_rejects_non_influxdb_archive(tmp_path):
    base = tmp_path / "proj"
    (base / "data").mkdir(parents=True)
    config_path = base / "config.json"
    config_path.write_text("{}")
    cfg = {"system": {}, "dashboard": {}, "influxdb": {"enabled": False}}
    config_archive = backup.create_config_backup(
        cfg, base_dir=str(base), config_path=str(config_path),
        backup_dir=str(base / "backup"),
    )
    with pytest.raises(backup.BackupError):
        backup.restore_influxdb_backup(
            config_archive, bundled_config(), base_dir=str(base),
            restore_runner=lambda influx_dir: None,
        )


def test_influxdb_rollback_backup_metadata(tmp_path):
    config = bundled_config()
    path = backup.create_influxdb_rollback_backup(
        config,
        "ems-influxdb-manual-2026-06-18-221500.tar.gz",
        base_dir=str(tmp_path),
        backup_runner=fake_backup_runner(),
    )
    assert path.startswith(str(tmp_path / "data" / "backups"))
    assert "ems-influxdb-rollback-" in os.path.basename(path)
    manifest = backup.inspect_backup(path)["manifest"]
    assert manifest["backup_purpose"] == "rollback"
    assert manifest["rollback_for"] == (
        "ems-influxdb-manual-2026-06-18-221500.tar.gz"
    )
