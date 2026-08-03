# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 5: Admin migration review + confirmed apply service layer.

Admin orchestrates the EMS-owned Zendure MQTT control migration: a read-only
review (with a config fingerprint) and a confirmed apply that requires the
fingerprint to still match, backs up by default, runs EMS-owned migration,
validates the result and writes atomically. Applying a stale preview is rejected;
re-running is a no-op; a failed migration leaves the original config active. No
broker secret is ever exposed.
"""

import hashlib
import json

import pytest

from admin.config_apply import ConfigApplyService
from admin.install_context import detect_install_context
from admin.zendure_mqtt_migration_review import (
    load_migration_review,
    prepare_migration_apply,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.integration,
    pytest.mark.simulation,
]


def _write_config(base, config):
    cfg_dir = base / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "config.json"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def _revision(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_control_config():
    return {
        "config_schema_version": 3,
        "zendure_mqtt": {
            "brokers": {
                "local_a": {
                    "host": "10.0.0.9",
                    "port": 1883,
                    "username": "mqtt",
                    "password": "s3cr3t-broker-pass",
                }
            }
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "Legacy",
                "product": "Hyper 2000",
                "mqtt": {
                    "broker_ref": "local_a",
                    "source": "local_mqtt",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEV",
                    "product_key": "PK",
                },
                "capabilities": {"write_output_limit": True},
            }
        ],
    }


def _safe_config():
    config = _legacy_control_config()
    config["devices"][0]["hardware_profile"] = "hub_2000"
    del config["devices"][0]["product"]
    return config


def _apply_service(base):
    return ConfigApplyService(
        config_export=None,
        admin_data_dir=str(base / "admin-data"),
        install_context_provider=lambda: detect_install_context(base_dir=str(base)),
    )


# --- review ------------------------------------------------------------------


def test_review_missing_config(tmp_path):
    review = load_migration_review(base_dir=str(tmp_path))
    assert review["status"] == "missing"


def test_review_flags_needed_migration(tmp_path):
    path = _write_config(tmp_path, _legacy_control_config())
    review = load_migration_review(base_dir=str(tmp_path))
    assert review["status"] == "ok"
    assert review["revision"] == _revision(path)
    assert review["review"]["needs_migration"] is True
    assert review["confirmation_required"] is True
    assert review["review"]["changes"]


def test_review_no_migration_for_safe_config(tmp_path):
    _write_config(tmp_path, _safe_config())
    review = load_migration_review(base_dir=str(tmp_path))
    assert review["status"] == "ok"
    assert review["review"]["needs_migration"] is False
    assert review["confirmation_required"] is False


def test_review_never_exposes_broker_secret(tmp_path):
    _write_config(tmp_path, _legacy_control_config())
    review = load_migration_review(base_dir=str(tmp_path))
    assert "s3cr3t-broker-pass" not in json.dumps(review)


# --- prepare apply -----------------------------------------------------------


def test_prepare_apply_conflict_on_stale_revision(tmp_path):
    _write_config(tmp_path, _legacy_control_config())
    prepared = prepare_migration_apply("deadbeef-not-the-revision", base_dir=str(tmp_path))
    assert prepared["status"] == "conflict"


def test_prepare_apply_produces_migrated_payload(tmp_path):
    path = _write_config(tmp_path, _legacy_control_config())
    prepared = prepare_migration_apply(_revision(path), base_dir=str(tmp_path))
    assert prepared["status"] == "ok"
    assert prepared["changed"] is True
    migrated = json.loads(prepared["payload"].decode("utf-8"))
    assert migrated["devices"][0]["hardware_profile"] == "hyper_2000"
    assert any(
        w["code"] == "zendure_mqtt_control_model_pinned" for w in prepared["warnings"]
    )


def test_prepare_apply_idempotent_noop_on_safe_config(tmp_path):
    path = _write_config(tmp_path, _safe_config())
    prepared = prepare_migration_apply(_revision(path), base_dir=str(tmp_path))
    assert prepared["status"] == "ok"
    assert prepared["changed"] is False


# --- full apply through the shared transaction -------------------------------


def test_full_apply_pins_profile_backs_up_and_is_idempotent(tmp_path):
    path = _write_config(tmp_path, _legacy_control_config())
    revision = _revision(path)
    prepared = prepare_migration_apply(revision, base_dir=str(tmp_path))
    service = _apply_service(tmp_path)
    result = service.apply_maintenance(prepared["payload"], revision, create_backup=True)
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["backup_path"]

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["devices"][0]["hardware_profile"] == "hyper_2000"

    # Re-running the review is now a no-op.
    review = load_migration_review(base_dir=str(tmp_path))
    assert review["review"]["needs_migration"] is False
    prepared2 = prepare_migration_apply(
        _revision(path), base_dir=str(tmp_path)
    )
    assert prepared2["changed"] is False


def test_stale_preview_apply_leaves_config_unchanged(tmp_path):
    path = _write_config(tmp_path, _legacy_control_config())
    stale_revision = _revision(path)
    prepared = prepare_migration_apply(stale_revision, base_dir=str(tmp_path))
    # The config changes after the preview was taken.
    _write_config(tmp_path, _safe_config())
    before = path.read_bytes()
    service = _apply_service(tmp_path)
    from admin.config_apply import ConfigChangedError

    with pytest.raises(ConfigChangedError):
        service.apply_maintenance(prepared["payload"], stale_revision, create_backup=True)
    assert path.read_bytes() == before
