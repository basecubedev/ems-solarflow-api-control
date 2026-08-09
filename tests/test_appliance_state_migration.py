# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migrating an existing installation into the web/agent state split.

An appliance that is already running holds the operator's password, the
known-good history and the operation records. The migration therefore copies
and verifies before it removes anything, refuses a symlinked source and keeps
both copies when the two layouts disagree.
"""

import json

import pytest

from appliance.migration import (
    RESULT_ALREADY_DONE,
    RESULT_CONFLICT,
    RESULT_MIGRATED,
    RESULT_REFUSED,
    RESULT_SKIPPED,
    migrate_state,
    write_report,
)
from tests.helpers.appliance import appliance_paths

pytestmark = [pytest.mark.unit, pytest.mark.simulation]


def legacy_installation(tmp_path):
    """An appliance as the previous release left it: one shared state tree."""

    paths = appliance_paths(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)

    paths.legacy_auth_file.write_text('{"algorithm": "pbkdf2-sha256", "generation": "abc"}')
    paths.legacy_state_file.write_text('{"seen": true}')

    paths.legacy_operations_dir.mkdir(parents=True, exist_ok=True)
    (paths.legacy_operations_dir / ("a" * 32 + ".json")).write_text('{"type": "admin.install"}')

    paths.legacy_known_good_dir.mkdir(parents=True, exist_ok=True)
    (paths.legacy_known_good_dir / "history.json").write_text('[{"admin_version": "v1.0.0"}]')

    paths.legacy_compose_backup_dir.mkdir(parents=True, exist_ok=True)
    (paths.legacy_compose_backup_dir / "docker-compose.admin.yml").write_text("services: {}\n")

    paths.legacy_appliance_log.write_text("web log\n")
    paths.legacy_operations_log.write_text("operation log\n")
    paths.legacy_audit_log.write_text('{"action": "login.success"}\n')
    return paths


def results(report):
    return {entry.source.rsplit("/", 1)[-1]: entry.result for entry in report.entries}


# --- fresh installation ----------------------------------------------------


def test_a_fresh_installation_has_nothing_to_migrate(tmp_path):
    paths = appliance_paths(tmp_path)
    report = migrate_state(paths)

    assert report.ok
    assert report.migrated == []
    assert set(results(report).values()) == {RESULT_SKIPPED}


def test_migration_creates_the_split_layout(tmp_path):
    paths = appliance_paths(tmp_path)
    migrate_state(paths)

    for directory in (paths.web_auth_dir, paths.web_sessions_dir, paths.web_preferences_dir):
        assert directory.is_dir(), directory
    for directory in (paths.operations_dir, paths.known_good_dir, paths.compose_backup_dir):
        assert directory.is_dir(), directory
    assert paths.audit_log_dir.is_dir()


# --- migration from the shared layout --------------------------------------


def test_state_moves_to_its_owner(tmp_path):
    paths = legacy_installation(tmp_path)
    report = migrate_state(paths)

    assert report.ok, report.to_dict()
    assert paths.auth_file.is_file()
    assert (paths.operations_dir / ("a" * 32 + ".json")).is_file()
    assert (paths.known_good_dir / "history.json").is_file()
    assert (paths.compose_backup_dir / "docker-compose.admin.yml").is_file()
    assert paths.audit_log.is_file()


def test_authentication_is_preserved_byte_for_byte(tmp_path):
    paths = legacy_installation(tmp_path)
    before = paths.legacy_auth_file.read_text()
    migrate_state(paths)
    assert paths.auth_file.read_text() == before


def test_known_good_metadata_is_preserved(tmp_path):
    paths = legacy_installation(tmp_path)
    migrate_state(paths)
    history = json.loads((paths.known_good_dir / "history.json").read_text())
    assert history[0]["admin_version"] == "v1.0.0"


def test_operation_records_are_preserved(tmp_path):
    paths = legacy_installation(tmp_path)
    migrate_state(paths)
    records = list(paths.operations_dir.glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["type"] == "admin.install"


def test_the_old_location_is_removed_only_after_the_copy_exists(tmp_path):
    paths = legacy_installation(tmp_path)
    migrate_state(paths)
    assert not paths.legacy_auth_file.exists()
    assert not paths.legacy_known_good_dir.exists()
    assert paths.auth_file.exists()
    assert (paths.known_good_dir / "history.json").exists()


# --- idempotence -----------------------------------------------------------


def test_running_the_migration_twice_changes_nothing(tmp_path):
    paths = legacy_installation(tmp_path)
    first = migrate_state(paths)
    content = paths.auth_file.read_text()

    second = migrate_state(paths)

    assert first.ok and second.ok
    assert second.migrated == []
    assert paths.auth_file.read_text() == content


def test_a_partially_completed_migration_finishes(tmp_path):
    paths = legacy_installation(tmp_path)
    # Only the auth file made it across before the previous run was interrupted.
    paths.web_auth_dir.mkdir(parents=True, exist_ok=True)
    paths.auth_file.write_text(paths.legacy_auth_file.read_text())
    paths.legacy_auth_file.unlink()

    report = migrate_state(paths)

    assert report.ok, report.to_dict()
    assert (paths.known_good_dir / "history.json").is_file()
    assert paths.auth_file.is_file()


# --- refusals and conflicts ------------------------------------------------


def test_a_symlinked_source_is_refused_not_followed(tmp_path):
    paths = appliance_paths(tmp_path)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-secret.json"
    outside.write_text("do not copy me")
    paths.legacy_auth_file.symlink_to(outside)

    report = migrate_state(paths)

    refused = [entry for entry in report.entries if entry.result == RESULT_REFUSED]
    assert refused, report.to_dict()
    assert not paths.auth_file.exists()
    assert outside.read_text() == "do not copy me"
    assert not report.ok


def test_a_destination_conflict_preserves_both_copies(tmp_path):
    paths = legacy_installation(tmp_path)
    paths.web_auth_dir.mkdir(parents=True, exist_ok=True)
    paths.auth_file.write_text('{"algorithm": "pbkdf2-sha256", "generation": "newer"}')

    report = migrate_state(paths)

    conflicts = [entry for entry in report.entries if entry.result == RESULT_CONFLICT]
    assert conflicts, report.to_dict()
    assert not report.ok
    assert "newer" in paths.auth_file.read_text()
    preserved = paths.web_auth_dir / "auth.json.migrated-conflict"
    assert preserved.is_file()
    assert "abc" in preserved.read_text()


def test_identical_content_on_both_sides_is_not_a_conflict(tmp_path):
    paths = legacy_installation(tmp_path)
    paths.web_auth_dir.mkdir(parents=True, exist_ok=True)
    paths.auth_file.write_text(paths.legacy_auth_file.read_text())

    report = migrate_state(paths)

    auth_entries = [entry for entry in report.entries if entry.destination.endswith("auth.json")]
    assert auth_entries[0].result in (RESULT_ALREADY_DONE, RESULT_MIGRATED)
    assert report.ok


def test_a_migration_finding_is_reported_and_recorded(tmp_path):
    paths = legacy_installation(tmp_path)
    paths.web_auth_dir.mkdir(parents=True, exist_ok=True)
    paths.auth_file.write_text('{"generation": "newer"}')

    report = migrate_state(paths)
    target = write_report(paths, report)

    assert target is not None and target.is_file()
    recorded = json.loads(target.read_text())
    assert recorded["ok"] is False
    assert recorded["findings"]


# --- ownership -------------------------------------------------------------


def test_directory_modes_after_migration(tmp_path):
    import stat

    paths = legacy_installation(tmp_path)
    migrate_state(paths)

    assert stat.S_IMODE(paths.web_auth_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.operations_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.audit_log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.agent_state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.agent_log_dir.stat().st_mode) == 0o700


def test_migrated_agent_files_are_root_only(tmp_path):
    import stat

    paths = legacy_installation(tmp_path)
    migrate_state(paths)

    for path in (paths.audit_log, paths.operations_log):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    for record in paths.known_good_dir.rglob("*"):
        if record.is_file():
            assert stat.S_IMODE(record.stat().st_mode) == 0o600, record
