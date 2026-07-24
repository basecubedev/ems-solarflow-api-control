# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable Guided Upgrade execution context (survives an Admin process restart)."""

import json

import pytest

from admin.guided_upgrade import guided_upgrade_request_fingerprint
from admin.guided_upgrade_context import GuidedUpgradeContextStore

pytestmark = pytest.mark.simulation

TAG = "v0.8.0"
OPERATION_ID = "op-guided-1"
OPTIONS = {
    "backup": True,
    "config_check": True,
    "config_add_keys": True,
    "config_comments": False,
    "pull_image": True,
    "recreate": True,
    "diagnostics": True,
}


def _fingerprint(options=None):
    return guided_upgrade_request_fingerprint(TAG, options or OPTIONS)


def _save(store, **overrides):
    kwargs = {
        "operation_id": OPERATION_ID,
        "target_system_tag": TAG,
        "options": OPTIONS,
        "request_fingerprint": _fingerprint(),
        "backup_completed": True,
        "backup_reference": "backup-2026-07-15",
        "backup_verified": True,
    }
    kwargs.update(overrides)
    return store.save(**kwargs)


def test_options_survive_admin_process_restart(tmp_path):
    _save(GuidedUpgradeContextStore(tmp_path / "state"))

    # A brand new store object models a freshly started Admin process.
    reloaded = GuidedUpgradeContextStore(tmp_path / "state").load(
        operation_id=OPERATION_ID, target_system_tag=TAG
    )

    assert reloaded is not None
    assert reloaded.options == OPTIONS
    assert reloaded.target_system_tag == TAG


def test_fingerprint_matches_the_stored_options(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    _save(store)

    reloaded = store.load(operation_id=OPERATION_ID, target_system_tag=TAG)

    assert reloaded.request_fingerprint == guided_upgrade_request_fingerprint(
        TAG, reloaded.options
    )


def test_backup_status_is_persisted(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    _save(store, backup_completed=True, backup_reference="bk-1", backup_verified=True)

    reloaded = GuidedUpgradeContextStore(tmp_path / "state").load(
        operation_id=OPERATION_ID, target_system_tag=TAG
    )

    assert reloaded.backup_completed is True
    assert reloaded.backup_reference == "bk-1"
    assert reloaded.backup_verified is True


def test_wrong_operation_or_tag_is_rejected(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    _save(store)

    assert store.load(operation_id="other-op", target_system_tag=TAG) is None
    assert store.load(operation_id=OPERATION_ID, target_system_tag="v9.9.9") is None


def test_tampered_options_are_rejected_fail_closed(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    _save(store)
    # An attacker flips a comfort option but cannot recompute the bound
    # fingerprint (config_check is not a fail-safe-forced deploy step).
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["options"]["config_check"] = False
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    assert store.load(operation_id=OPERATION_ID, target_system_tag=TAG) is None


def test_mandatory_deploy_options_are_forced_on(tmp_path):
    # Pulling the target image and recreating the EMS container are binding: a
    # request that omits them is still stored (and run) with them enabled.
    store = GuidedUpgradeContextStore(tmp_path / "state")
    weakened = {**OPTIONS, "pull_image": False, "recreate": False}
    _save(
        store,
        options=weakened,
        request_fingerprint=guided_upgrade_request_fingerprint(TAG, weakened),
    )

    context = store.load(operation_id=OPERATION_ID, target_system_tag=TAG)

    assert context.options["pull_image"] is True
    assert context.options["recreate"] is True


def test_unknown_options_are_rejected(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    _save(store)
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["options"]["evil_option"] = True
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    assert store.load(operation_id=OPERATION_ID, target_system_tag=TAG) is None


def test_save_rejects_a_fingerprint_that_does_not_match_options(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    with pytest.raises(ValueError):
        _save(store, request_fingerprint=_fingerprint({**OPTIONS, "backup": False}))


def test_pre_alignment_incomplete_is_rejected(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    with pytest.raises(ValueError):
        _save(store, pre_alignment_completed=False)


def test_pre_alignment_incomplete_on_disk_blocks_resume(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    _save(store)
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["pre_alignment_completed"] = False
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    assert store.load(operation_id=OPERATION_ID, target_system_tag=TAG) is None


def test_backup_enabled_but_unverified_is_rejected(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    with pytest.raises(ValueError):
        _save(store, backup_completed=True, backup_verified=False,
              backup_reference="bk-1")


def test_backup_enabled_but_missing_reference_is_rejected(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    with pytest.raises(ValueError):
        _save(store, backup_completed=True, backup_verified=True,
              backup_reference=None)


def test_backup_disabled_must_not_invent_verification(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    no_backup = {**OPTIONS, "backup": False}
    with pytest.raises(ValueError):
        store.save(
            operation_id=OPERATION_ID,
            target_system_tag=TAG,
            options=no_backup,
            request_fingerprint=guided_upgrade_request_fingerprint(TAG, no_backup),
            backup_completed=False,
            backup_reference=None,
            backup_verified=True,
        )


def test_backup_disabled_context_loads(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    no_backup = {**OPTIONS, "backup": False}
    store.save(
        operation_id=OPERATION_ID,
        target_system_tag=TAG,
        options=no_backup,
        request_fingerprint=guided_upgrade_request_fingerprint(TAG, no_backup),
        backup_completed=False,
        backup_reference=None,
        backup_verified=False,
    )

    context = store.load(operation_id=OPERATION_ID, target_system_tag=TAG)

    assert context is not None
    assert context.backup_verified is False


def test_integrity_covers_backup_verified_field(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    _save(store)
    # Flip a backup completion flag on disk: the request fingerprint (tag+options)
    # is untouched, but the integrity fingerprint over the backup fields no longer
    # reproduces, so the context is refused fail-closed.
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["backup_verified"] = False
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    assert store.load(operation_id=OPERATION_ID, target_system_tag=TAG) is None


def test_exact_backup_archive_is_persisted(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    _save(store, backup_reference="data/backups/ems-config-manual-2026-07-15.tar.gz")

    context = GuidedUpgradeContextStore(tmp_path / "state").load(
        operation_id=OPERATION_ID, target_system_tag=TAG
    )

    assert context.backup_reference == (
        "data/backups/ems-config-manual-2026-07-15.tar.gz"
    )
    assert context.backup_verified is True


def test_no_secrets_or_config_content_are_stored(tmp_path):
    store = GuidedUpgradeContextStore(tmp_path / "state")
    _save(store)
    raw = json.loads(store.path.read_text(encoding="utf-8"))

    assert set(raw) == {
        "format_version",
        "operation_id",
        "target_system_tag",
        "options",
        "request_fingerprint",
        "pre_alignment_completed",
        "backup_completed",
        "backup_reference",
        "backup_verified",
        "mqtt_migration_required",
        "mqtt_migration_completed",
        "mqtt_migration_revision",
        "integrity",
    }
    # Options are booleans only — never config values or credentials.
    assert all(isinstance(value, bool) for value in raw["options"].values())
