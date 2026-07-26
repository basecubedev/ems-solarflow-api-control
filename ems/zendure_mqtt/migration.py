# SPDX-License-Identifier: AGPL-3.0-or-later
"""Safe migration of legacy Zendure MQTT control configs.

Older configs enabled output control by relying on topic-family write inference.
That inference is gone, so such a config no longer authorizes a write. Migration
is deterministic and fails closed, never silently preserving the unsafe behavior:

* control device with exact model evidence -> pin the resolved ``hardware_profile``
  (the operator still reviews/applies the diff),
* control device without exact model evidence -> disable output control
  (telemetry-only) and attach an actionable warning.

Only a device that already pins a known, writable, transport-compatible
``hardware_profile`` is safe. A bare ``mqtt.write_protocol`` (the removed legacy
properties/write escape hatch) is never safe on its own.

:func:`plan_zendure_mqtt_migration` is a read-only dry-run (returns the exact
diff, mutates nothing); :func:`migrate_zendure_mqtt_control_configs` applies it in
place. Callers render the diff and apply only after an explicit confirmation.
"""

import copy
from dataclasses import dataclass, field

from ems.mqtt_control.power_capability import resolve_power_write_capability
from ems.mqtt_control.zendure_profiles import (
    EVIDENCE_CLOUD_DEVICE_LIST,
    EVIDENCE_FULL_REPORT,
    EVIDENCE_RETAINED_METADATA,
    hardware_profile_by_name,
    make_hardware_profile_evidence,
    resolve_hardware_profile_evidence,
)
from ems.zendure_mqtt.config_entries import (
    config_entry_enabled,
    is_control_zendure_mqtt_device_config,
    validate_zendure_mqtt_control_device_config,
    zendure_mqtt_device_identifier,
    zendure_mqtt_hardware_profile,
    zendure_mqtt_product_key,
    zendure_mqtt_topic_family,
    zendure_mqtt_write_topic,
)

MIGRATION_REQUIRED_CODE = "zendure_mqtt_control_migration_required"

MIGRATION_DISABLED_WARNING = (
    "MQTT power control was disabled because the exact Zendure hardware model is "
    "not configured. Select the model in Maintenance before enabling control again."
)

MIGRATION_UNADDRESSABLE_WARNING = (
    "MQTT power control was disabled because the Zendure device is not "
    "write-addressable (no product_key or write_topic). Add the write address in "
    "Maintenance before enabling control again."
)

ACTION_PIN_PROFILE = "pin_profile"
ACTION_DISABLE_CONTROL = "disable_control"
# A pure cleanup: a profile-backed device carries an obsolete mqtt.write_topic
# that the runtime already ignores (canonical topic wins). Removing it is safe
# and never blocks startup — the device is already writable.
ACTION_NORMALIZE_WRITE_TOPIC = "normalize_write_topic"

MIGRATION_WRITE_TOPIC_NORMALIZED_WARNING = (
    "An obsolete mqtt.write_topic was removed: a pinned Zendure hardware model "
    "always publishes to its canonical iot/<productKey>/<deviceId>/properties/"
    "write topic, so the stored override was ignored and is now cleaned up."
)

# Actions that make a config safe to run but do not, on their own, force a
# migration before startup (the runtime already behaves correctly).
_NON_BLOCKING_ACTIONS = frozenset({ACTION_NORMALIZE_WRITE_TOPIC})

# Validation error codes migration is responsible for (write addressing,
# capability, metadata consistency) — broker/structural errors are unrelated
# pre-existing config concerns and never make a migration "fail".
_MIGRATION_VALIDATION_CODES = frozenset(
    {
        "control_not_requested",
        "write_target_missing",
        "hardware_profile_missing",
        "hardware_profile_unknown",
        "hardware_profile_deferred",
        "transport_incompatible",
        "operation_unsupported",
        "write_protocol_unsupported",
        "write_topic_required",
        "write_topic_invalid",
        "power_write_profile_mismatch",
    }
)


class ZendureMqttMigrationError(RuntimeError):
    """The migrated config failed validation and must not be written."""

    def __init__(self, message, errors):
        super().__init__(message)
        self.errors = list(errors)


@dataclass(frozen=True)
class ZendureMqttMigrationChange:
    """One planned migration change for a control device (dry-run diff entry).

    Carries a stable device identity (``index``, ``device_id``) so duplicate
    device names are never ambiguous, and ``changes`` — the exact
    ``{path, before, after}`` edits — so the operator sees precisely what will
    change before applying.
    """

    device: str
    action: str
    hardware_profile: str | None
    power_write_profile: str | None
    code: str
    severity: str
    message: str
    index: int = -1
    device_id: str | None = None
    changes: tuple = field(default_factory=tuple)


def _warning(change: ZendureMqttMigrationChange):
    return {
        "code": change.code,
        "severity": change.severity,
        "device": change.device,
        "index": change.index,
        "device_id": change.device_id,
        "message": change.message,
    }


def _diff(path, before, after):
    return {"path": path, "before": before, "after": after}


# Every legacy model hint is a corroborating (non-decisive) discovery-tier
# signal: none is an operator-reviewed selection, so two exact-but-different
# hints must surface as a conflict rather than letting whichever field is read
# first silently pin a writable model. The source labels are for provenance only.
_MIGRATION_EVIDENCE_FIELDS = (
    ("product", EVIDENCE_FULL_REPORT),
    ("model", EVIDENCE_RETAINED_METADATA),
    ("mqtt.product", EVIDENCE_CLOUD_DEVICE_LIST),
)


def _field_value(device, path):
    if path == "mqtt.product":
        mqtt = device.get("mqtt")
        return mqtt.get("product") if isinstance(mqtt, dict) else None
    return device.get(path)


def _model_resolution(device):
    """Resolve the device model from every available signal, detecting conflicts.

    Builds one evidence record per non-empty model hint and resolves them
    together, so two exact-but-different hints become a conflict (never a pin)
    instead of the first-read value winning.
    """

    evidences = []
    for path, source in _MIGRATION_EVIDENCE_FIELDS:
        value = _field_value(device, path)
        if isinstance(value, str) and value.strip():
            evidences.append(make_hardware_profile_evidence(source, value))
    return resolve_hardware_profile_evidence(evidences)


def _addressing_complete(device):
    """A device can carry a power write only with a product_key or a write_topic."""

    return bool(zendure_mqtt_product_key(device)) or bool(
        zendure_mqtt_write_topic(device)
    )


def _intended_model(device):
    """The concrete model this device should resolve to: ``(profile_id, source)``.

    An existing valid pinned ``hardware_profile`` is authoritative; otherwise the
    model is resolved from every discovery signal together (conflict-aware).
    """

    pinned = zendure_mqtt_hardware_profile(device)
    if pinned and hardware_profile_by_name(pinned) is not None:
        return pinned, pinned
    resolution = _model_resolution(device)
    return resolution.profile_id, resolution.source_value


def _already_safe(device):
    """A control device is safe only when its concrete model is fully consistent.

    Requires a known, writable, transport-compatible pinned ``hardware_profile``,
    complete write addressing, a ``power_write_profile`` that matches the registry
    (or is absent), and no leftover ``mqtt.write_protocol``. A bare
    ``mqtt.write_protocol`` (the removed legacy escape hatch) is never authority.
    """

    hardware_profile = zendure_mqtt_hardware_profile(device)
    profile = hardware_profile_by_name(hardware_profile) if hardware_profile else None
    if profile is None:
        return False
    cap = resolve_power_write_capability(
        topic_family=zendure_mqtt_topic_family(device),
        hardware_profile=hardware_profile,
    )
    if not cap.supported or not _addressing_complete(device):
        return False
    stored = device.get("power_write_profile")
    if isinstance(stored, str) and stored.strip() and stored.strip() != profile.power_write_profile:
        return False
    mqtt = device.get("mqtt")
    if isinstance(mqtt, dict) and mqtt.get("write_protocol"):
        return False
    return True


def _pin_change(device, index, name, device_id, profile_id, write_profile, source):
    prefix = f"devices[{index}]"
    changes = []
    if device.get("hardware_profile") != profile_id:
        changes.append(
            _diff(f"{prefix}.hardware_profile", device.get("hardware_profile"), profile_id)
        )
    if device.get("power_write_profile") != write_profile:
        changes.append(
            _diff(
                f"{prefix}.power_write_profile",
                device.get("power_write_profile"),
                write_profile,
            )
        )
    mqtt = device.get("mqtt")
    if isinstance(mqtt, dict) and mqtt.get("write_protocol") is not None:
        changes.append(
            _diff(f"{prefix}.mqtt.write_protocol", mqtt.get("write_protocol"), None)
        )
    if _obsolete_write_topic(device) is not None:
        changes.append(
            _diff(f"{prefix}.mqtt.write_topic", mqtt.get("write_topic"), None)
        )
    return ZendureMqttMigrationChange(
        device=name,
        action=ACTION_PIN_PROFILE,
        hardware_profile=profile_id,
        power_write_profile=write_profile,
        code="zendure_mqtt_control_model_pinned",
        severity="info",
        message=(
            f"{name}: pinned Zendure hardware model '{profile_id}' "
            f"(from '{source}')."
        ),
        index=index,
        device_id=device_id,
        changes=tuple(changes),
    )


def _obsolete_write_topic(device):
    """The obsolete ``mqtt.write_topic`` on a profile-backed device, else ``None``.

    A pinned model publishes to its canonical topic, so a stored ``write_topic``
    is dead config. It is only removed when the device is addressable by
    ``product_key`` (the canonical topic is buildable) — a device addressed solely
    by ``write_topic`` keeps it so migration never strips its only address.
    """

    if not zendure_mqtt_product_key(device):
        return None
    return zendure_mqtt_write_topic(device)


def _normalize_write_topic_change(device, index, name, device_id):
    prefix = f"devices[{index}]"
    mqtt = device.get("mqtt")
    before = mqtt.get("write_topic") if isinstance(mqtt, dict) else None
    return ZendureMqttMigrationChange(
        device=name,
        action=ACTION_NORMALIZE_WRITE_TOPIC,
        hardware_profile=zendure_mqtt_hardware_profile(device),
        power_write_profile=None,
        code="zendure_mqtt_control_write_topic_normalized",
        severity="info",
        message=f"{name}: {MIGRATION_WRITE_TOPIC_NORMALIZED_WARNING}",
        index=index,
        device_id=device_id,
        changes=(_diff(f"{prefix}.mqtt.write_topic", before, None),),
    )


def _disable_change(device, index, name, device_id, *, code, message):
    prefix = f"devices[{index}]"
    changes = []
    caps = device.get("capabilities")
    before_cap = caps.get("write_output_limit") if isinstance(caps, dict) else None
    if before_cap is not False:
        changes.append(
            _diff(f"{prefix}.capabilities.write_output_limit", before_cap, False)
        )
    if device.get("hardware_profile") is not None:
        changes.append(_diff(f"{prefix}.hardware_profile", device.get("hardware_profile"), None))
    mqtt = device.get("mqtt")
    if isinstance(mqtt, dict) and mqtt.get("write_protocol") is not None:
        changes.append(_diff(f"{prefix}.mqtt.write_protocol", mqtt.get("write_protocol"), None))
    return ZendureMqttMigrationChange(
        device=name,
        action=ACTION_DISABLE_CONTROL,
        hardware_profile=None,
        power_write_profile=None,
        code=code,
        severity="warning",
        message=message,
        index=index,
        device_id=device_id,
        changes=tuple(changes),
    )


def _plan_device(device, index) -> ZendureMqttMigrationChange | None:
    """The change needed to make one device safe, or ``None`` if already safe.

    Pure: it never mutates ``device`` so the same helper backs both the read-only
    dry-run plan and the mutating apply. A pin is proposed only when the resolved
    model is writable, transport-compatible AND write-addressable; exact evidence
    with incomplete addressing disables control (telemetry preserved) rather than
    leaving an unaddressable ``write_target_missing`` config.
    """

    if not isinstance(device, dict) or not is_control_zendure_mqtt_device_config(device):
        return None
    name = device.get("name") if isinstance(device.get("name"), str) else "device"
    device_id = zendure_mqtt_device_identifier(device)
    if _already_safe(device):
        if _obsolete_write_topic(device) is not None:
            return _normalize_write_topic_change(device, index, name, device_id)
        return None

    profile_id, source = _intended_model(device)
    if profile_id is not None:
        cap = resolve_power_write_capability(
            topic_family=zendure_mqtt_topic_family(device),
            hardware_profile=profile_id,
        )
        if cap.supported:
            if _addressing_complete(device):
                return _pin_change(
                    device, index, name, device_id, profile_id, cap.write_profile, source
                )
            return _disable_change(
                device,
                index,
                name,
                device_id,
                code="zendure_mqtt_control_disabled_unaddressable",
                message=f"{name}: {MIGRATION_UNADDRESSABLE_WARNING}",
            )
    return _disable_change(
        device,
        index,
        name,
        device_id,
        code="zendure_mqtt_control_disabled_unknown_model",
        message=f"{name}: {MIGRATION_DISABLED_WARNING}",
    )


def _iter_devices(config):
    if not isinstance(config, dict):
        return []
    devices = config.get("devices")
    return devices if isinstance(devices, list) else []


def plan_zendure_mqtt_migration(config) -> list[ZendureMqttMigrationChange]:
    """Read-only migration plan (dry-run): the exact changes, no mutation.

    Never writes files, never mutates ``config``. Callers render the diff and
    apply it only after an explicit operator confirmation.
    """

    changes = []
    for index, device in enumerate(_iter_devices(config)):
        change = _plan_device(device, index)
        if change is not None:
            changes.append(change)
    return changes


def zendure_mqtt_control_configs_need_migration(config) -> bool:
    """True when any control device needs a safe migration before it can run."""

    return bool(plan_zendure_mqtt_migration(config))


def validate_zendure_mqtt_control_configs(config) -> list:
    """Migration-relevant control-config errors for the config as-is (no plan).

    Runs the normal control-device validation over every enabled control device
    and keeps only the errors migration is responsible for (write addressing,
    capability, metadata consistency). Broker/structural errors are pre-existing
    concerns and are not reported here.
    """

    errors = []
    for index, device in enumerate(_iter_devices(config)):
        if not isinstance(device, dict):
            continue
        if not is_control_zendure_mqtt_device_config(device):
            continue
        if not config_entry_enabled(device):
            continue
        for issue in validate_zendure_mqtt_control_device_config(device):
            if issue.get("severity") == "error" and issue.get("code") in _MIGRATION_VALIDATION_CODES:
                errors.append({**issue, "index": index})
    return errors


def validate_migrated_zendure_mqtt_config(config) -> list:
    """Build the migrated result in memory and validate it (no mutation).

    Returns the migration-relevant errors the *result* would still carry — an
    empty list means the migration produces a valid control config.
    """

    migrated = copy.deepcopy(config) if isinstance(config, dict) else config
    for index, device in enumerate(_iter_devices(migrated)):
        change = _plan_device(device, index)
        if change is not None:
            _apply_change(device, change)
    return validate_zendure_mqtt_control_configs(migrated)


def zendure_mqtt_control_migration_startup_error(config) -> dict | None:
    """Sanitized startup-abort fields when an enabled control device is unsafe.

    Returns ``{"code", "count", "devices"}`` when at least one *enabled* control
    device needs migration (an unsafe, unmigrated control config), else ``None``.
    Normal startup must not silently rewrite the config: it rejects the control
    runtime with an actionable migration-required error instead. Telemetry-only
    devices, disabled entries and already-pinned control devices never block.
    """

    # Match blocking changes by stable list index, so duplicate device names can
    # never merge or hide a distinct unsafe control device.
    enabled_control_indexes = {
        index
        for index, device in enumerate(_iter_devices(config))
        if isinstance(device, dict)
        and is_control_zendure_mqtt_device_config(device)
        and config_entry_enabled(device)
    }
    blocking = [
        change
        for change in plan_zendure_mqtt_migration(config)
        if change.index in enabled_control_indexes
        and change.action not in _NON_BLOCKING_ACTIONS
    ]
    if not blocking:
        return None
    return {
        "code": MIGRATION_REQUIRED_CODE,
        "count": len(blocking),
        "devices": sorted({str(change.device) for change in blocking}),
    }


def _apply_change(device, change: ZendureMqttMigrationChange) -> None:
    """Apply one planned change in place, stripping obsolete write metadata."""

    mqtt = device.get("mqtt")
    if change.action == ACTION_NORMALIZE_WRITE_TOPIC:
        if isinstance(mqtt, dict):
            mqtt.pop("write_topic", None)
        return
    if change.action == ACTION_PIN_PROFILE:
        device["hardware_profile"] = change.hardware_profile
        # Canonical registry metadata, and no stale legacy escape hatch.
        device["power_write_profile"] = change.power_write_profile
        if isinstance(mqtt, dict):
            mqtt.pop("write_protocol", None)
            if _obsolete_write_topic(device) is not None:
                mqtt.pop("write_topic", None)
        return
    caps = device.get("capabilities")
    if isinstance(caps, dict):
        caps["write_output_limit"] = False
    # A disabled control device drops any writable identity so it can't be
    # re-enabled with a stale/unaddressable model; the telemetry entry stays.
    device.pop("hardware_profile", None)
    device.pop("power_write_profile", None)
    if isinstance(mqtt, dict):
        mqtt.pop("write_protocol", None)


def migrate_zendure_mqtt_control_configs(config):
    """Apply the migration plan in place; return ``(config, warnings)``.

    The complete migrated result is built and validated in memory first: if it
    would still carry a migration-relevant error, the write is refused
    (:class:`ZendureMqttMigrationError`) rather than committing an invalid config.
    ``config`` is only mutated once the result is known valid. ``warnings`` is a
    list of ``{code, severity, device, index, device_id, message}`` describing
    every auto-pin and control-disable. Idempotent: re-running yields no changes.
    """

    errors = validate_migrated_zendure_mqtt_config(config)
    if errors:
        raise ZendureMqttMigrationError(
            "the migrated Zendure MQTT control config would be invalid", errors
        )
    warnings = []
    for index, device in enumerate(_iter_devices(config)):
        change = _plan_device(device, index)
        if change is None:
            continue
        _apply_change(device, change)
        warnings.append(_warning(change))
    return config, warnings


__all__ = [
    "MIGRATION_DISABLED_WARNING",
    "MIGRATION_UNADDRESSABLE_WARNING",
    "MIGRATION_REQUIRED_CODE",
    "ACTION_PIN_PROFILE",
    "ACTION_DISABLE_CONTROL",
    "ACTION_NORMALIZE_WRITE_TOPIC",
    "MIGRATION_WRITE_TOPIC_NORMALIZED_WARNING",
    "ZendureMqttMigrationChange",
    "ZendureMqttMigrationError",
    "plan_zendure_mqtt_migration",
    "zendure_mqtt_control_configs_need_migration",
    "zendure_mqtt_control_migration_startup_error",
    "validate_zendure_mqtt_control_configs",
    "validate_migrated_zendure_mqtt_config",
    "migrate_zendure_mqtt_control_configs",
]
