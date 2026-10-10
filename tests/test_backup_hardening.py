# SPDX-License-Identifier: AGPL-3.0-or-later
"""Merge-readiness hardening for the backup tool (ems.backup)."""

import io
import json
import os
import tarfile
from datetime import datetime

import pytest

from ems import backup

pytestmark = [
    pytest.mark.backup_restore,
    pytest.mark.integration,
]


def write_project(tmp_path):
    base = tmp_path / "proj"
    (base / "data").mkdir(parents=True)
    config = {
        "system": {"runtime_state_path": "data/runtime-state.json"},
        "influxdb": {"enabled": False},
        "devices": [],
    }
    config_path = base / "config.json"
    config_path.write_text(json.dumps(config))
    (base / "data" / "runtime-state.json").write_text('{"runtime": true}\n')
    return str(base), config, str(config_path)


def create(base, config, config_path, **kwargs):
    return backup.create_config_backup(
        config,
        base_dir=base,
        config_path=config_path,
        backup_dir=os.path.join(base, "backup"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# No-clobber archive naming
# ---------------------------------------------------------------------------

def test_same_timestamp_does_not_overwrite(tmp_path):
    base, config, config_path = write_project(tmp_path)
    fixed = datetime(2026, 6, 20, 12, 0, 0)
    first = create(base, config, config_path, now=fixed)
    second = create(base, config, config_path, now=fixed)

    assert first != second
    assert os.path.isfile(first)
    assert os.path.isfile(second)
    assert second.endswith("-2.tar.gz")


def test_third_backup_gets_incrementing_suffix(tmp_path):
    base, config, config_path = write_project(tmp_path)
    fixed = datetime(2026, 6, 20, 12, 0, 0)
    create(base, config, config_path, now=fixed)
    create(base, config, config_path, now=fixed)
    third = create(base, config, config_path, now=fixed)
    assert third.endswith("-3.tar.gz")


def test_encrypted_same_timestamp_does_not_overwrite(tmp_path):
    base, config, config_path = write_project(tmp_path)
    fixed = datetime(2026, 6, 20, 12, 0, 0)
    first = create(base, config, config_path, now=fixed, password="pw-secret")
    second = create(base, config, config_path, now=fixed, password="pw-secret")
    assert first.endswith(".tar.gz.enc")
    assert second.endswith("-2.tar.gz.enc")
    assert os.path.isfile(first) and os.path.isfile(second)


def test_no_leftover_temp_files(tmp_path):
    base, config, config_path = write_project(tmp_path)
    create(base, config, config_path, password="pw-secret")
    leftovers = [
        name
        for name in os.listdir(os.path.join(base, "backup"))
        if name.endswith(".part") or name.endswith(".tar.gz")
        and "manual" not in name
    ]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Symlink behavior (reject during backup)
# ---------------------------------------------------------------------------

def test_symlink_source_rejected(tmp_path):
    base, config, config_path = write_project(tmp_path)
    real = os.path.join(base, "real-config.json")
    os.rename(config_path, real)
    os.symlink(real, config_path)

    with pytest.raises(backup.BackupError, match="symlink"):
        create(base, config, config_path)


def test_no_archive_remains_after_symlink_rejection(tmp_path):
    base, config, config_path = write_project(tmp_path)
    real = os.path.join(base, "real-config.json")
    os.rename(config_path, real)
    os.symlink(real, config_path)

    with pytest.raises(backup.BackupError):
        create(base, config, config_path)

    backup_dir = os.path.join(base, "backup")
    remaining = os.listdir(backup_dir) if os.path.isdir(backup_dir) else []
    assert remaining == []


def test_restore_rejects_symlink_tar_member(tmp_path):
    base = str(tmp_path / "restore-root")
    os.makedirs(base)
    archive = str(tmp_path / "crafted.tar.gz")
    payload = b"{}"
    import hashlib

    manifest = {
        "backup_format": 1,
        "files": [
            {
                "path": "config.json",
                "kind": "config",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    with tarfile.open(archive, "w:gz") as tar:
        mbytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo(backup.MANIFEST_NAME)
        info.size = len(mbytes)
        tar.addfile(info, io.BytesIO(mbytes))
        link = tarfile.TarInfo("config.json")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)

    with pytest.raises(backup.BackupError, match="not a regular file"):
        backup.restore_backup(archive, base_dir=base)


# ---------------------------------------------------------------------------
# Dry-run is conflict-safe
# ---------------------------------------------------------------------------

def test_dry_run_reports_conflict_without_aborting(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)
    with open(config_path, "w") as handle:
        handle.write('{"changed": true}')

    result = backup.restore_backup(path, base_dir=base, dry_run=True)
    actions = {a["path"]: a["action"] for a in result["actions"]}
    assert actions["config.json"] == "would_replace_conflict"
    # The conflicting file is untouched.
    assert json.loads(open(config_path).read()) == {"changed": True}


def test_dry_run_reports_identical(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)
    result = backup.restore_backup(path, base_dir=base, dry_run=True)
    assert all(
        a["action"] == "would_skip_identical" for a in result["actions"]
    )


def test_dry_run_does_not_create_rollback_or_write(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)
    os.remove(os.path.join(base, "data", "runtime-state.json"))
    with open(config_path, "w") as handle:
        handle.write('{"changed": true}')

    backup.restore_backup(path, base_dir=base, dry_run=True)

    # No new archives (no rollback) and no restored file.
    assert os.listdir(os.path.join(base, "backup")) == [os.path.basename(path)]
    assert not os.path.isfile(os.path.join(base, "data", "runtime-state.json"))
    assert json.loads(open(config_path).read()) == {"changed": True}


def test_dry_run_still_validates_checksum(tmp_path):
    base = str(tmp_path)
    archive = str(tmp_path / "cs.tar.gz")
    manifest = {
        "backup_format": 1,
        "files": [{"path": "config.json", "sha256": "deadbeef", "kind": "config"}],
    }
    with tarfile.open(archive, "w:gz") as tar:
        mbytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo(backup.MANIFEST_NAME)
        info.size = len(mbytes)
        tar.addfile(info, io.BytesIO(mbytes))
        data = b"hello"
        info = tarfile.TarInfo("config.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    with pytest.raises(backup.BackupError, match="checksum"):
        backup.restore_backup(archive, base_dir=base, dry_run=True)


def test_dry_run_still_rejects_path_traversal(tmp_path):
    archive = str(tmp_path / "bad.tar.gz")
    manifest = {
        "backup_format": 1,
        "files": [{"path": "../escape.txt", "sha256": "x"}],
    }
    with tarfile.open(archive, "w:gz") as tar:
        mbytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo(backup.MANIFEST_NAME)
        info.size = len(mbytes)
        tar.addfile(info, io.BytesIO(mbytes))

    with pytest.raises(backup.BackupError):
        backup.restore_backup(archive, base_dir=str(tmp_path), dry_run=True)


# ---------------------------------------------------------------------------
# Conflict resolver decisions
# ---------------------------------------------------------------------------

def test_invalid_conflict_resolver_decision_rejected_without_write(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)

    original = '{"changed": true}'
    with open(config_path, "w") as handle:
        handle.write(original)

    with pytest.raises(backup.BackupError, match="invalid conflict resolver decision"):
        backup.restore_backup(
            path,
            base_dir=base,
            conflict_resolver=lambda entry: "nonsense",
        )

    # The conflicting local file must be left exactly as it was.
    assert open(config_path).read() == original


def test_invalid_on_conflict_policy_rejected(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)
    with pytest.raises(backup.BackupError, match="invalid on_conflict policy"):
        backup.restore_backup(path, base_dir=base, on_conflict="nonsense")


# ---------------------------------------------------------------------------
# Option validation
# ---------------------------------------------------------------------------

def test_invalid_encryption_algorithm_rejected():
    with pytest.raises(backup.BackupError, match="algorithm"):
        backup.build_encryption_options(algorithm="rot13")


def test_invalid_chunk_size_rejected():
    with pytest.raises(backup.BackupError, match="chunk size"):
        backup.build_encryption_options(chunk_size=1)


def test_invalid_kdf_iterations_rejected():
    with pytest.raises(backup.BackupError, match="iterations"):
        backup.build_encryption_options(kdf_iterations=10)


def test_parse_chunk_size_units():
    assert backup.parse_chunk_size("4M") == 4 * 1024 * 1024
    assert backup.parse_chunk_size("512K") == 512 * 1024
    assert backup.parse_chunk_size("1048576") == 1048576
    assert backup.parse_chunk_size(None) is None
    with pytest.raises(backup.BackupError):
        backup.parse_chunk_size("nonsense")


def test_create_config_backup_rejects_invalid_encryption_options_without_archive(
    tmp_path,
):
    base, config, config_path = write_project(tmp_path)
    backup_dir = os.path.join(base, "backup")

    with pytest.raises(backup.BackupError, match="invalid encryption options"):
        create(
            base,
            config,
            config_path,
            password="pw",
            encryption_options={
                "algorithm": "chacha20-poly1305",
                "chunk_size": 1,
                "iterations": 1,
            },
        )

    # No final archive and no leftover temp (.part / plaintext .tar.gz) files.
    remaining = os.listdir(backup_dir) if os.path.isdir(backup_dir) else []
    assert remaining == []


def test_encryption_roundtrip_with_explicit_options(tmp_path):
    base, config, config_path = write_project(tmp_path)
    options = backup.build_encryption_options(
        algorithm="aes-256-gcm", chunk_size=4096, kdf_iterations=50_000
    )
    path = create(
        base, config, config_path, password="pw-secret", encryption_options=options
    )
    info = backup.inspect_backup(path, password="pw-secret")
    assert info["manifest"]["encryption"]["enabled"] is True
    assert "aes-256-gcm" in info["manifest"]["encryption"]["method"]


# ---------------------------------------------------------------------------
# Privacy wording
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

def _create_args(emsctl, **overrides):
    base = {
        "type": "config",
        "compression_level": backup.DEFAULT_COMPRESSION_LEVEL,
        "password": False,
        "encryption": backup.DEFAULT_ENCRYPTION_ALGORITHM,
        "chunk_size": None,
        "kdf_iterations": None,
        "config": None,
        "runtime_state": None,
        "dashboard_auth": None,
    }
    base.update(overrides)
    return emsctl.make_args(**base)


def test_cli_dry_run_conflict_is_safe(tmp_path, monkeypatch, capsys):
    import emsctl

    base, config, config_path = write_project(tmp_path)
    backup_dir = os.path.join(base, "backup")
    path = backup.create_config_backup(
        config, base_dir=base, config_path=config_path, backup_dir=backup_dir
    )
    with open(config_path, "w") as handle:
        handle.write('{"changed": true}')

    monkeypatch.setattr(emsctl, "BASE_DIR", base)
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: False)

    args = emsctl.make_args(
        config=config_path,
        runtime_state=os.path.join(base, "data", "runtime-state.json"),
        dashboard_auth=None,
        on_conflict=None,
        rollback=None,
        dry_run=True,
    )
    rc = emsctl.handle_backup_restore(args, config, path, interactive=False)
    out = capsys.readouterr().out

    assert rc == 0
    assert "would_replace_conflict" in out
    # No rollback archive created and the conflicting file is untouched.
    assert os.listdir(backup_dir) == [os.path.basename(path)]
    assert json.loads(open(config_path).read()) == {"changed": True}


def test_cli_invalid_compression_level_fails_cleanly(tmp_path, monkeypatch, capsys):
    import emsctl

    base, config, config_path = write_project(tmp_path)
    monkeypatch.setattr(emsctl, "BASE_DIR", base)
    monkeypatch.setattr(emsctl.sys.stdin, "isatty", lambda: False)

    args = _create_args(
        emsctl, compression_level=99, config=config_path,
        runtime_state=os.path.join(base, "data", "runtime-state.json"),
    )
    rc = emsctl.handle_backup_create(args, config)
    err = capsys.readouterr().err

    assert rc != 0
    assert "compression level" in err.lower()
    assert not os.path.isdir(os.path.join(base, "backup"))


def test_sqlite_marked_privacy_relevant_not_secret(tmp_path):
    import sqlite3

    base = tmp_path / "proj"
    (base / "data").mkdir(parents=True)
    db = base / "data" / "ems_dashboard.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    config = {
        "dashboard": {"database_path": "data/ems_dashboard.sqlite"},
        "influxdb": {"enabled": False},
    }
    path = backup.create_database_backup(
        config, base_dir=str(base), backup_dir=str(base / "backup")
    )
    manifest = backup.inspect_backup(path)["manifest"]
    entry = next(
        f for f in manifest["files"] if f["path"].endswith("ems_dashboard.sqlite")
    )
    assert entry["sensitive"] is False
    assert entry["privacy_relevant"] is True


# ---------------------------------------------------------------------------
# A write the filesystem refuses is a restore failure, not a crash
# ---------------------------------------------------------------------------

def test_a_write_the_filesystem_refuses_is_reported_as_a_backup_failure(tmp_path, monkeypatch):
    """`_atomic_write` raises OSError, and every caller catches BackupError.

    A full SD card -- the state this appliance reaches on its own, since nothing
    prunes the rollback archive each restore creates -- makes the second file of
    a restore fail with ENOSPC. The OSError then walks straight past
    `except BackupError` in the Admin restore, in its auto-rollback and in the
    dashboard's, so the half-applied restore is never rolled back, the rollback
    archive is never named to the operator, and the job ends as "failed
    unexpectedly" with no step detail.
    """

    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)
    with open(config_path, "w") as handle:
        handle.write('{"changed": true}')

    real_replace = os.replace

    def refuse(source, target, *args, **kwargs):
        if str(target).endswith("config.json"):
            raise OSError(28, "No space left on device")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(backup.os, "replace", refuse)

    with pytest.raises(backup.BackupError) as excinfo:
        backup.restore_backup(path, base_dir=base, on_conflict="replace")

    assert "config.json" in str(excinfo.value)
    assert "No space left on device" in str(excinfo.value)


def test_a_directory_that_cannot_be_created_is_reported_as_a_backup_failure(
    tmp_path, monkeypatch
):
    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)
    os.remove(os.path.join(base, "data", "runtime-state.json"))

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(backup.os, "makedirs", refuse)

    with pytest.raises(backup.BackupError):
        backup.restore_backup(path, base_dir=base, on_conflict="replace")


# ---------------------------------------------------------------------------
# A replaced file keeps the mode and owner it had
# ---------------------------------------------------------------------------

def test_a_restored_file_keeps_the_mode_of_the_file_it_replaces(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)
    with open(config_path, "w") as handle:
        handle.write('{"changed": true}')
    os.chmod(config_path, 0o640)

    backup.restore_backup(path, base_dir=base, on_conflict="replace")

    assert os.stat(config_path).st_mode & 0o777 == 0o640


def test_a_file_the_restore_creates_is_private(tmp_path):
    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)
    os.remove(config_path)

    backup.restore_backup(path, base_dir=base, on_conflict="replace")

    assert os.stat(config_path).st_mode & 0o777 == 0o600


def test_a_restore_as_root_gives_a_replaced_file_back_to_its_owner(tmp_path, monkeypatch):
    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)
    with open(config_path, "w") as handle:
        handle.write('{"changed": true}')
    owner = os.stat(config_path)
    calls = []
    monkeypatch.setattr(backup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(backup.os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))

    backup.restore_backup(path, base_dir=base, on_conflict="replace")

    assert calls == [(owner.st_uid, owner.st_gid)]


# ---------------------------------------------------------------------------
# The archived bytes are the bytes the manifest describes
# ---------------------------------------------------------------------------

def test_a_file_that_changes_between_manifest_and_archive_fails_the_backup(tmp_path, monkeypatch):
    base, config, config_path = write_project(tmp_path)
    real_build = backup.build_manifest

    def build_then_edit(*args, **kwargs):
        manifest = real_build(*args, **kwargs)
        with open(config_path, "w") as handle:
            handle.write('{"edited": "while the backup ran"}')
        return manifest

    monkeypatch.setattr(backup, "build_manifest", build_then_edit)

    with pytest.raises(backup.BackupError) as excinfo:
        create(base, config, config_path)

    assert "changed while it was being backed up" in str(excinfo.value)
    backup_dir = os.path.join(base, "backup")
    assert not os.path.isdir(backup_dir) or os.listdir(backup_dir) == []


def test_an_unchanged_backup_verifies_against_its_manifest(tmp_path):
    base, config, config_path = write_project(tmp_path)

    path = create(base, config, config_path)

    assert backup.verify_backup(path)["verified"] is True


# ---------------------------------------------------------------------------
# A file kept outside the project is never put back in the wrong place
# ---------------------------------------------------------------------------

def test_a_file_outside_the_project_is_archived_with_its_origin(tmp_path):
    base, config, config_path = write_project(tmp_path)
    outside = tmp_path / "elsewhere" / "dashboard-auth.json"
    outside.parent.mkdir()
    outside.write_text('{"hash": "x"}')

    path = backup.create_config_backup(
        config,
        base_dir=base,
        config_path=config_path,
        dashboard_auth_path=str(outside),
        backup_dir=os.path.join(base, "backup"),
    )
    entry = next(
        f for f in backup.inspect_backup(path)["manifest"]["files"]
        if f["path"].endswith("dashboard-auth.json")
    )

    assert entry["path"] == backup.archive_rel(str(outside), base)
    assert entry["path"].startswith(backup.OUTSIDE_PROJECT_PREFIX)
    assert entry["path"].endswith("/dashboard-auth.json")
    assert entry["source_path"] == str(outside)


def test_a_file_from_outside_the_project_is_named_but_not_restored(tmp_path):
    base, config, config_path = write_project(tmp_path)
    outside = tmp_path / "elsewhere" / "dashboard-auth.json"
    outside.parent.mkdir()
    outside.write_text('{"hash": "x"}')
    path = backup.create_config_backup(
        config,
        base_dir=base,
        config_path=config_path,
        dashboard_auth_path=str(outside),
        backup_dir=os.path.join(base, "backup"),
    )
    outside.write_text('{"hash": "changed"}')

    dry = backup.restore_backup(path, base_dir=base, dry_run=True)
    result = backup.restore_backup(path, base_dir=base, on_conflict="replace")

    planned = {a["path"]: a for a in dry["actions"]}
    done = {a["path"]: a for a in result["actions"]}
    key = backup.archive_rel(str(outside), base)
    assert planned[key]["action"] == "would_skip_outside_project"
    assert done[key]["action"] == "skipped_outside_project"
    assert done[key]["source_path"] == str(outside)
    assert outside.read_text() == '{"hash": "changed"}'
    assert not os.path.exists(os.path.join(base, "_outside_project"))
    assert not os.path.exists(os.path.join(base, "dashboard-auth.json"))


def test_the_admin_preview_skips_a_file_from_outside_the_project_under_every_policy():
    from admin.backup_restore import BackupRestoreService

    entry = {"status": "outside_project", "checksum_ok": True}

    for policy in ("abort", "keep", "replace"):
        assert BackupRestoreService._preview_action(entry, policy) == (
            "would_skip_outside_project",
            "skip",
        )


def test_emsctl_names_where_a_skipped_outside_file_lives(capsys):
    import emsctl

    emsctl.print_restore_actions([
        {"path": "_outside_project/a.json", "action": "skipped_outside_project",
         "source_path": "/etc/ems/a.json"},
    ])

    out = capsys.readouterr().out
    assert "skipped_outside_project: _outside_project/a.json" in out
    assert "/etc/ems/a.json" in out


def test_a_filesystem_that_refuses_the_mode_does_not_stop_the_restore(tmp_path, monkeypatch):
    base, config, config_path = write_project(tmp_path)
    path = create(base, config, config_path)
    with open(config_path, "w") as handle:
        handle.write('{"changed": true}')
    open_before = len(os.listdir("/proc/self/fd"))

    def refuse(fd, mode):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(backup.os, "fchmod", refuse)
    for attempt in range(3):
        with open(config_path, "w") as handle:
            handle.write(json.dumps({"attempt": attempt}))
        backup.restore_backup(path, base_dir=base, on_conflict="replace")

    assert json.loads(open(config_path).read()) == config
    assert len(os.listdir("/proc/self/fd")) <= open_before


def test_two_outside_files_with_one_name_are_archived_apart(tmp_path):
    base, config, config_path = write_project(tmp_path)
    cert = tmp_path / "cert" / "server.pem"
    key = tmp_path / "key" / "server.pem"
    for item, text in ((cert, "CERT"), (key, "KEY")):
        item.parent.mkdir()
        item.write_text(text)
    config = {**config, "dashboard": {"ssl_cert_file": str(cert), "ssl_key_file": str(key)}}

    path = create(base, config, config_path)
    files = {
        f["source_path"]: f["path"]
        for f in backup.inspect_backup(path)["manifest"]["files"]
        if f.get("source_path")
    }

    assert set(files) == {str(cert), str(key)}
    assert len(set(files.values())) == 2
    assert backup.verify_backup(path)["verified"] is True


def test_a_database_outside_the_project_records_where_it_lives(tmp_path):
    base = tmp_path / "proj"
    (base / "data").mkdir(parents=True)
    database = tmp_path / "var-lib-ems" / "dashboard.sqlite"
    database.parent.mkdir()
    import sqlite3

    con = sqlite3.connect(database)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()
    config = {"dashboard": {"database_path": str(database)}, "influxdb": {"enabled": False}}

    path = backup.create_database_backup(
        config, base_dir=str(base), backup_dir=str(base / "backup")
    )
    entry = next(
        f for f in backup.inspect_backup(path)["manifest"]["files"] if f.get("source_path")
    )

    assert entry["source_path"] == str(database)


def _crafted_archive(path, name, manifest_entry_extra):
    import hashlib as _hashlib
    import io as _io
    import tarfile as _tarfile

    data = b"payload"
    manifest = {
        "backup_format": backup.BACKUP_FORMAT_VERSION,
        "backup_type": "config",
        "backup_purpose": "manual",
        "created_at": "2026-01-01T00:00:00Z",
        "app": backup.APP_NAME,
        "files": [{
            "path": name, "kind": "config", "sensitive": True,
            "size_bytes": len(data), "sha256": _hashlib.sha256(data).hexdigest(),
            **manifest_entry_extra,
        }],
    }
    with _tarfile.open(path, "w:gz") as tar:
        raw = json.dumps(manifest).encode()
        info = _tarfile.TarInfo(backup.MANIFEST_NAME)
        info.size = len(raw)
        tar.addfile(info, _io.BytesIO(raw))
        info = _tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, _io.BytesIO(data))


@pytest.mark.parametrize(
    "name_kind,extra",
    [
        ("outside-without-origin", {}),
        ("inside-with-origin", {"source_path": "/etc/shadow"}),
        ("outside-with-other-origin", {"source_path": "/etc/shadow"}),
    ],
)
def test_an_entry_whose_name_and_origin_disagree_refuses_the_restore(tmp_path, name_kind, extra):
    base = tmp_path / "proj"
    base.mkdir()
    real = str(tmp_path / "letsencrypt" / "fullchain.pem")
    name = {
        "outside-without-origin": backup.outside_project_name(real),
        "inside-with-origin": "config.json",
        "outside-with-other-origin": backup.outside_project_name(real),
    }[name_kind]
    archive = tmp_path / "ems-config-manual-2026-01-01-000000.tar.gz"
    _crafted_archive(archive, name, extra)

    with pytest.raises(backup.BackupError, match="does not match the origin"):
        backup.restore_backup(str(archive), base_dir=str(base), on_conflict="replace")
    assert list(base.iterdir()) == []


def test_a_non_utf8_outside_path_still_gets_an_archive_name(tmp_path):
    name = backup.outside_project_name(str(tmp_path) + "/caf\udce9.pem")

    assert name.startswith(backup.OUTSIDE_PROJECT_PREFIX)
