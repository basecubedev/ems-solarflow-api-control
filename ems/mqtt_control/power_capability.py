# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single authority for Zendure MQTT power-write capability.

Capability is decided from independent axes:

``hardware profile``
    the pinned physical model, and whether it is writable at all;
``write profile``
    whether that model's command protocol has an implemented publish route;
``broker source``
    whether the broker the command travels through is a proven carrier for that
    route;
``write route``
    completeness of the address (product key / route device id) — a separate
    axis owned by ``zendure_mqtt_control_addressability``.

The observed telemetry family names how reports are parsed. On its own it
authorizes and blocks nothing: every Zendure power write — ZenSDK
``properties/write`` and legacy ``function/invoke`` automation alike — is
published to ``iot/<productKey>/<deviceId>/…`` regardless of which family the
device's telemetry was classified as (see ``ems/zendure_mqtt/write_protocols.py``
and ``ems/mqtt_control/zendure_commands.py``, neither of which reads the family).
It participates only as evidence for the broker-source axis, where the pair
"which broker" + "what that broker actually carries" is what is proven or not.

An unknown, ambiguous or deferred model, a model whose write profile has no
implemented publish route, and a broker source on which that route is
unverified all stay telemetry-only with a stable, machine-readable
``block_reason``.
"""

from dataclasses import dataclass

from ems.mqtt_control.topic_families import JSON_FAMILIES
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
BLOCK_TRANSPORT_WRITE_NOT_IMPLEMENTED = "transport_write_not_implemented"
BLOCK_OPERATION_UNSUPPORTED = "operation_unsupported"
BLOCK_CONTROL_NOT_ENABLED = "control_not_enabled"
BLOCK_IDENTIFIERS_MISSING = "identifiers_missing"
BLOCK_BROKER_SOURCE_UNKNOWN = "broker_source_unknown"
BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED = "broker_source_write_unverified"

# Retired: the telemetry family no longer blocks a write. Kept readable so a
# stored config, diagnostics record or migration report carrying the historic
# value still resolves to a known reason.
BLOCK_TRANSPORT_INCOMPATIBLE = "transport_incompatible"

# The broker a command travels through. These are the canonical connection
# sources already used by broker profiles and device entries; no alias is
# accepted, so an unrecognized value is an unknown source, never a permissive
# default.
BROKER_SOURCE_LOCAL_MQTT = "local_mqtt"
BROKER_SOURCE_ZENDURE_CLOUD_MQTT = "zendure_cloud_mqtt"
KNOWN_BROKER_SOURCES = frozenset(
    {BROKER_SOURCE_LOCAL_MQTT, BROKER_SOURCE_ZENDURE_CLOUD_MQTT}
)

# Telemetry families that prove a broker source carries the real Zendure device
# protocol. The Zendure cloud broker is a proven carrier on every family — it is
# the endpoint the device's own commands are addressed to, and the canonical
# route is hardware-confirmed there. A local broker is proven only where the
# device's JSON report families were observed on it: a scalar-only local feed is
# typically a bridge/integration republishing metrics, and no hardware evidence
# exists that it relays a command back to the device.
_BROKER_SOURCE_VERIFIED_FAMILIES = {
    BROKER_SOURCE_ZENDURE_CLOUD_MQTT: None,
    BROKER_SOURCE_LOCAL_MQTT: frozenset(JSON_FAMILIES),
}

# The publish routes an implemented write profile uses. Both are addressed on
# ``iot/<productKey>/<deviceId>/…``: they name *where a command goes*, which is
# independent of the family the device's telemetry arrives on.
WRITE_FAMILY_PROPERTIES_WRITE = "iot_properties_write"
WRITE_FAMILY_FUNCTION_INVOKE = "iot_function_invoke"

_PROFILE_WRITE_FAMILY = {
    WRITE_PROFILE_ZENSDK_PROPERTIES: WRITE_FAMILY_PROPERTIES_WRITE,
    WRITE_PROFILE_LEGACY_HUB: WRITE_FAMILY_FUNCTION_INVOKE,
    WRITE_PROFILE_LEGACY_OBJECT: WRITE_FAMILY_FUNCTION_INVOKE,
}


@dataclass(frozen=True)
class PowerWriteCapability:
    """Whether a hardware profile may publish a power write.

    ``model_supported``, ``transport_supported`` and ``broker_source_supported``
    are the independent axes behind ``supported``; ``telemetry_family`` is
    diagnostic context and evidence for the broker-source axis, never a verdict
    of its own.
    """

    supported: bool
    profile_id: str | None
    write_profile: str | None
    supported_operations: frozenset
    block_reason: str | None
    model_supported: bool = False
    transport_supported: bool = False
    telemetry_family: str | None = None
    write_family: str | None = None
    broker_source: str | None = None
    broker_source_supported: bool = False


@dataclass(frozen=True)
class BrokerSourceWriteSupport:
    """Whether the broker a command travels through carries the write route."""

    supported: bool
    broker_source: str | None
    block_reason: str | None


def write_family_for_profile(write_profile) -> str | None:
    """Publish route implemented for a write profile, or ``None``."""

    return _PROFILE_WRITE_FAMILY.get(write_profile)


def normalize_broker_source(broker_source) -> str | None:
    """Canonical broker-source id, or ``None`` when it is not a known source."""

    value = broker_source.strip().lower() if isinstance(broker_source, str) else ""
    return value if value in KNOWN_BROKER_SOURCES else None


def resolve_broker_source_write_support(
    broker_source, telemetry_family
) -> BrokerSourceWriteSupport:
    """Resolve the broker-source axis on its own.

    An unrecognized or missing source is never treated as permissive: it fails
    closed with ``broker_source_unknown`` so a caller that forgets to resolve the
    source cannot accidentally authorize a write.
    """

    source = normalize_broker_source(broker_source)
    if source is None:
        return BrokerSourceWriteSupport(False, None, BLOCK_BROKER_SOURCE_UNKNOWN)
    verified = _BROKER_SOURCE_VERIFIED_FAMILIES[source]
    if verified is None:
        return BrokerSourceWriteSupport(True, source, None)
    family = str(telemetry_family or "").strip()
    if family in verified:
        return BrokerSourceWriteSupport(True, source, None)
    return BrokerSourceWriteSupport(
        False, source, BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED
    )


def profile_write_route_implemented(hardware_profile) -> bool:
    """True when a pinned model is writable and has an implemented publish route.

    The model/write-profile axes only. Deliberately source-independent: it
    answers *which shape* a write route has (canonical ``iot/…`` topic vs the
    custom escape hatch), which the broker source must not change — otherwise a
    source-blocked device would be re-read as a custom-topic device and report a
    missing write topic instead of its real blocker.
    """

    name = hardware_profile.strip() if isinstance(hardware_profile, str) else ""
    profile = hardware_profile_by_name(name) if name else None
    if profile is None or not profile.writable:
        return False
    return write_family_for_profile(profile.power_write_profile) is not None


def resolve_power_write_capability(
    *, topic_family, hardware_profile, broker_source, operation=None
) -> PowerWriteCapability:
    """Resolve the power-write capability for a pinned hardware profile.

    ``hardware_profile`` is a config-pinned canonical model id (e.g.
    ``"hyper_2000"``), never a topic family. ``broker_source`` is the canonical
    source of the broker profile the device is bound to and is required: a
    caller that cannot resolve one passes ``None`` and the result fails closed.
    ``topic_family`` is diagnostic context and the evidence the broker-source
    axis is judged on. When ``operation`` is given the result additionally
    reflects whether that neutral operation (discharge/idle/charge) is supported
    by the model.

    Axes are reported in order, so the block reason always names the first
    missing precondition: model, write route, broker source, operation.
    """

    family = str(topic_family or "").strip() or None
    source = resolve_broker_source_write_support(broker_source, family)
    name = hardware_profile.strip() if isinstance(hardware_profile, str) else ""
    if not name:
        return PowerWriteCapability(
            False,
            None,
            None,
            frozenset(),
            BLOCK_HARDWARE_PROFILE_MISSING,
            telemetry_family=family,
            broker_source=source.broker_source,
            broker_source_supported=source.supported,
        )

    profile = hardware_profile_by_name(name)
    if profile is None:
        return PowerWriteCapability(
            False,
            None,
            None,
            frozenset(),
            BLOCK_HARDWARE_PROFILE_UNKNOWN,
            telemetry_family=family,
            broker_source=source.broker_source,
            broker_source_supported=source.supported,
        )

    if not profile.writable:
        reason = (
            BLOCK_HARDWARE_PROFILE_DEFERRED
            if profile.validation_status == VALIDATION_DEFERRED
            else BLOCK_HARDWARE_PROFILE_UNKNOWN
        )
        return PowerWriteCapability(
            False,
            profile.canonical_name,
            profile.power_write_profile,
            frozenset(),
            reason,
            telemetry_family=family,
            broker_source=source.broker_source,
            broker_source_supported=source.supported,
        )

    write_family = write_family_for_profile(profile.power_write_profile)
    if write_family is None:
        return PowerWriteCapability(
            False,
            profile.canonical_name,
            profile.power_write_profile,
            frozenset(),
            BLOCK_TRANSPORT_WRITE_NOT_IMPLEMENTED,
            model_supported=True,
            telemetry_family=family,
            broker_source=source.broker_source,
            broker_source_supported=source.supported,
        )

    ops = frozenset(profile.supported_operations)
    common = {
        "model_supported": True,
        "transport_supported": True,
        "telemetry_family": family,
        "write_family": write_family,
        "broker_source": source.broker_source,
        "broker_source_supported": source.supported,
    }
    if not source.supported:
        return PowerWriteCapability(
            False,
            profile.canonical_name,
            profile.power_write_profile,
            ops,
            source.block_reason,
            **common,
        )

    if operation is not None and operation not in ops:
        return PowerWriteCapability(
            False,
            profile.canonical_name,
            profile.power_write_profile,
            ops,
            BLOCK_OPERATION_UNSUPPORTED,
            **common,
        )

    return PowerWriteCapability(
        True,
        profile.canonical_name,
        profile.power_write_profile,
        ops,
        None,
        **common,
    )


__all__ = [
    "BLOCK_HARDWARE_PROFILE_MISSING",
    "BLOCK_HARDWARE_PROFILE_UNKNOWN",
    "BLOCK_HARDWARE_PROFILE_AMBIGUOUS",
    "BLOCK_HARDWARE_PROFILE_DEFERRED",
    "BLOCK_TRANSPORT_INCOMPATIBLE",
    "BLOCK_TRANSPORT_WRITE_NOT_IMPLEMENTED",
    "BLOCK_OPERATION_UNSUPPORTED",
    "BLOCK_CONTROL_NOT_ENABLED",
    "BLOCK_IDENTIFIERS_MISSING",
    "BLOCK_BROKER_SOURCE_UNKNOWN",
    "BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED",
    "BROKER_SOURCE_LOCAL_MQTT",
    "BROKER_SOURCE_ZENDURE_CLOUD_MQTT",
    "KNOWN_BROKER_SOURCES",
    "WRITE_FAMILY_PROPERTIES_WRITE",
    "WRITE_FAMILY_FUNCTION_INVOKE",
    "BrokerSourceWriteSupport",
    "PowerWriteCapability",
    "normalize_broker_source",
    "profile_write_route_implemented",
    "resolve_broker_source_write_support",
    "resolve_power_write_capability",
    "write_family_for_profile",
    "OPERATION_DISCHARGE",
    "OPERATION_IDLE",
    "OPERATION_CHARGE",
]
