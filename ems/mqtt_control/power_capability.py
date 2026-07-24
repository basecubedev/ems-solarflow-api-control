# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single authority for Zendure MQTT power-write capability.

Capability is decided from the *pinned hardware profile*, never from the topic
family. The topic family only names the observed telemetry transport; a profile
is writable only when it is known, supported, and compatible with that transport.
An unknown, ambiguous, deferred or transport-incompatible device stays
telemetry-only with a stable, machine-readable ``block_reason``.

This replaces the old ``topic_family -> legacy_properties_write`` inference: a
transport alone can never authorize a hardware write.
"""

from dataclasses import dataclass

from ems.mqtt_control.topic_families import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
)
from ems.mqtt_control.zendure_profiles import (
    OPERATION_CHARGE,
    OPERATION_DISCHARGE,
    OPERATION_IDLE,
    VALIDATION_DEFERRED,
    WRITE_PROFILE_LEGACY_HUB,
    WRITE_PROFILE_LEGACY_OBJECT,
    WRITE_PROFILE_ZENSDK_PROPERTIES,
    hardware_profile_by_name,
)

# Stable, machine-readable reasons a device may not publish a power write.
BLOCK_HARDWARE_PROFILE_MISSING = "hardware_profile_missing"
BLOCK_HARDWARE_PROFILE_UNKNOWN = "hardware_profile_unknown"
BLOCK_HARDWARE_PROFILE_AMBIGUOUS = "hardware_profile_ambiguous"
BLOCK_HARDWARE_PROFILE_DEFERRED = "hardware_profile_deferred"
BLOCK_TRANSPORT_INCOMPATIBLE = "transport_incompatible"
BLOCK_OPERATION_UNSUPPORTED = "operation_unsupported"
BLOCK_CONTROL_NOT_ENABLED = "control_not_enabled"
BLOCK_IDENTIFIERS_MISSING = "identifiers_missing"

# Every MQTT power write (function/invoke automation *and* ZenSDK properties/
# write) is addressed over a JSON-report transport (``iot/<pk>/<dev>/...``). A
# scalar HA/cloud transport carries telemetry only — those devices are controlled
# over the local HTTP API, never via an MQTT power write — so a writable profile
# on a scalar family is transport-incompatible.
_WRITABLE_TRANSPORT_FAMILIES = frozenset(
    {FAMILY_LEGACY_JSON, FAMILY_LEGACY_JSON_ALT}
)
_PROFILE_COMPATIBLE_FAMILIES = {
    WRITE_PROFILE_LEGACY_OBJECT: _WRITABLE_TRANSPORT_FAMILIES,
    WRITE_PROFILE_LEGACY_HUB: _WRITABLE_TRANSPORT_FAMILIES,
    WRITE_PROFILE_ZENSDK_PROPERTIES: _WRITABLE_TRANSPORT_FAMILIES,
}


@dataclass(frozen=True)
class PowerWriteCapability:
    """Whether a hardware profile may publish a power write on a transport."""

    supported: bool
    profile_id: str | None
    write_profile: str | None
    supported_operations: frozenset
    block_reason: str | None


def _transport_compatible(write_profile: str, topic_family: str) -> bool:
    allowed = _PROFILE_COMPATIBLE_FAMILIES.get(write_profile)
    return bool(allowed) and topic_family in allowed


def resolve_power_write_capability(
    *, topic_family, hardware_profile, operation=None
) -> PowerWriteCapability:
    """Resolve the power-write capability for a pinned hardware profile.

    ``hardware_profile`` is a config-pinned canonical model id (e.g.
    ``"hyper_2000"``), never a topic family. When ``operation`` is given the
    result additionally reflects whether that neutral operation
    (discharge/idle/charge) is supported by the model.
    """

    name = hardware_profile.strip() if isinstance(hardware_profile, str) else ""
    if not name:
        return PowerWriteCapability(
            False, None, None, frozenset(), BLOCK_HARDWARE_PROFILE_MISSING
        )

    profile = hardware_profile_by_name(name)
    if profile is None:
        return PowerWriteCapability(
            False, None, None, frozenset(), BLOCK_HARDWARE_PROFILE_UNKNOWN
        )

    if not profile.writable:
        reason = (
            BLOCK_HARDWARE_PROFILE_DEFERRED
            if profile.validation_status == VALIDATION_DEFERRED
            else BLOCK_HARDWARE_PROFILE_UNKNOWN
        )
        return PowerWriteCapability(
            False, profile.canonical_name, profile.power_write_profile, frozenset(), reason
        )

    family = str(topic_family or "").strip()
    if not _transport_compatible(profile.power_write_profile, family):
        return PowerWriteCapability(
            False,
            profile.canonical_name,
            profile.power_write_profile,
            frozenset(),
            BLOCK_TRANSPORT_INCOMPATIBLE,
        )

    ops = frozenset(profile.supported_operations)
    if operation is not None and operation not in ops:
        return PowerWriteCapability(
            False,
            profile.canonical_name,
            profile.power_write_profile,
            ops,
            BLOCK_OPERATION_UNSUPPORTED,
        )

    return PowerWriteCapability(
        True, profile.canonical_name, profile.power_write_profile, ops, None
    )


__all__ = [
    "BLOCK_HARDWARE_PROFILE_MISSING",
    "BLOCK_HARDWARE_PROFILE_UNKNOWN",
    "BLOCK_HARDWARE_PROFILE_AMBIGUOUS",
    "BLOCK_HARDWARE_PROFILE_DEFERRED",
    "BLOCK_TRANSPORT_INCOMPATIBLE",
    "BLOCK_OPERATION_UNSUPPORTED",
    "BLOCK_CONTROL_NOT_ENABLED",
    "BLOCK_IDENTIFIERS_MISSING",
    "PowerWriteCapability",
    "resolve_power_write_capability",
    "OPERATION_DISCHARGE",
    "OPERATION_IDLE",
    "OPERATION_CHARGE",
]
