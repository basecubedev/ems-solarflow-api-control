# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin orchestration of the EMS-owned Zendure MQTT control migration.

EMS/Core owns migration semantics; Admin only *orchestrates* the preview and
apply — it never implements a second migration algorithm. This module renders the
EMS-owned dry-run plan as a Maintenance review DTO (exact before/after changes,
control-disable warnings, the final-validity of the result) and applies it
through the same EMS-owned entry point. The Guided Upgrade step reuses the same
functions before a new EMS container starts.
"""

import copy
import hashlib
import json

from admin.install_context import detect_install_context
from ems.zendure_mqtt.migration import (
    ACTION_DISABLE_CONTROL,
    ZendureMqttMigrationError,
    migrate_zendure_mqtt_control_configs,
    plan_zendure_mqtt_migration,
    validate_migrated_zendure_mqtt_config,
    zendure_mqtt_control_configs_need_migration,
)

_CONFIG_ENCODING = "utf-8"


def _serialize_config(config) -> bytes:
    """Serialize a migrated config exactly as the Admin config writer does."""

    return (
        json.dumps(config, indent=2, ensure_ascii=False, allow_nan=False).encode(
            _CONFIG_ENCODING
        )
        + b"\n"
    )


def _load_config(base_dir):
    """Read the resolved EMS config; return ``(status, context, raw, config)``.

    A missing or unreadable config degrades to a status string the route can
    render, never an exception.
    """

    context = detect_install_context(base_dir=base_dir)
    if not context.config_exists:
        return "missing", context, None, None
    try:
        raw = context.config_path.read_bytes()
        config = json.loads(raw.decode(_CONFIG_ENCODING))
    except (OSError, UnicodeError, ValueError):
        return "invalid", context, None, None
    if not isinstance(config, dict):
        return "invalid", context, None, None
    return "ok", context, raw, config


def _change_to_dict(change) -> dict:
    disables = change.action == ACTION_DISABLE_CONTROL
    return {
        "device": change.device,
        "index": change.index,
        "device_id": change.device_id,
        "action": change.action,
        "hardware_profile": change.hardware_profile,
        "power_write_profile": change.power_write_profile,
        "code": change.code,
        "severity": change.severity,
        "message": change.message,
        "disables_control": disables,
        "changes": [dict(entry) for entry in change.changes],
    }


def zendure_mqtt_migration_review(config) -> dict:
    """Read-only Maintenance migration review for ``config`` (never mutates).

    Returns the EMS-owned dry-run plan as review data plus the validity of the
    result the migration would produce, so Admin can require explicit confirmation
    and offer a backup before applying through EMS/Core.
    """

    changes = plan_zendure_mqtt_migration(config)
    change_dicts = [_change_to_dict(change) for change in changes]
    validation_errors = validate_migrated_zendure_mqtt_config(config)
    return {
        "needs_migration": zendure_mqtt_control_configs_need_migration(config),
        "changes": change_dicts,
        "warnings_disabling_control": [
            c for c in change_dicts if c["disables_control"]
        ],
        "final_valid": not validation_errors,
        "validation_errors": validation_errors,
    }


def apply_zendure_mqtt_migration(config):
    """Apply the migration through the EMS-owned entry point; return ``(config, warnings)``.

    Raises :class:`ems.zendure_mqtt.migration.ZendureMqttMigrationError` (from
    EMS/Core) if the migrated result would be invalid — Admin surfaces that as an
    actionable error and writes nothing.
    """

    return migrate_zendure_mqtt_control_configs(config)


def load_migration_review(base_dir=None) -> dict:
    """Read the live EMS config and return a secret-safe migration review + fingerprint.

    The ``revision`` is the fingerprint the confirmed apply must present so a
    stale preview can never be applied. ``confirmation_required`` is true exactly
    when the migration would change the config.
    """

    status, context, raw, config = _load_config(base_dir)
    config_path = str(context.config_path)
    if status != "ok":
        message = (
            "No config.json was found at the resolved install path."
            if status == "missing"
            else "The config file could not be read as a JSON object."
        )
        return {
            "status": status,
            "config_path": config_path,
            "source": context.config_source,
            "message": message,
        }
    review = zendure_mqtt_migration_review(config)
    return {
        "status": "ok",
        "config_path": config_path,
        "source": context.config_source,
        "revision": hashlib.sha256(raw).hexdigest(),
        "review": review,
        "confirmation_required": bool(review["needs_migration"]),
    }


def prepare_migration_apply(expected_revision, base_dir=None) -> dict:
    """Run the EMS-owned migration and return the payload to write, or a status.

    Fails closed at every step: a config that changed since the review
    (``conflict``), an unreadable config (``missing``/``invalid``) or a migration
    whose result would be invalid (``invalid``, from EMS/Core) all return without
    a payload, so the caller writes nothing and the original config stays active.
    ``changed`` is false when the config is already migrated (idempotent no-op).
    """

    status, context, raw, config = _load_config(base_dir)
    config_path = str(context.config_path)
    if status != "ok":
        return {"status": status, "config_path": config_path}
    revision = hashlib.sha256(raw).hexdigest()
    if expected_revision != revision:
        return {
            "status": "conflict",
            "message": "The config changed since the review; re-run the preview.",
            "revision": revision,
        }
    working = copy.deepcopy(config)
    try:
        migrated, warnings = migrate_zendure_mqtt_control_configs(working)
    except ZendureMqttMigrationError as exc:
        return {
            "status": "invalid",
            "message": str(exc),
            "errors": list(exc.errors),
        }
    return {
        "status": "ok",
        "changed": migrated != config,
        "payload": _serialize_config(migrated),
        "expected_revision": revision,
        "warnings": warnings,
    }


__all__ = [
    "zendure_mqtt_migration_review",
    "apply_zendure_mqtt_migration",
    "load_migration_review",
    "prepare_migration_apply",
]
