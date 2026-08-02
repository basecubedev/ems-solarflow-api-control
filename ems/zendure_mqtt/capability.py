# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared decision: does a Zendure MQTT device support output control?

Single source of truth for MQTT output-control capability, reused by Admin
proposal generation, Admin config drafts, config sanitization/validation,
runtime validation, diagnostics and frontend presentation data. Capability is
decided from the *pinned hardware profile* and the *broker source* the command
would travel through (via
:func:`ems.mqtt_control.power_capability.resolve_power_write_capability`), never
from the topic family on its own. The topic family names the telemetry
transport; it neither authorizes nor blocks a write by itself, because every
power write is published to ``iot/<productKey>/<deviceId>/…`` on any family. It
participates only as the evidence the broker-source axis is judged on.

A device is:

``supported``
    a pinned hardware profile is known, writable, has an implemented write route
    and its broker source is a proven carrier for that route; or an explicit
    supported ``write_protocol`` (the operator-verified custom escape hatch)
    resolves.

``unsupported``
    a known device that cannot write (deferred/telemetry-only model, a write
    profile with no implemented publish route, or a broker source on which that
    route is unverified).

``unknown``
    no hardware profile is pinned and no write method could be resolved.

``broker_source`` is a required argument everywhere in this module: a caller
that cannot resolve one passes ``None`` and the verdict fails closed, so no
consumer can silently default to a permissive transport.

:func:`resolve_output_control_capability` composes that verdict with write-route
completeness (``zendure_mqtt_control_addressability``) into the single result
Admin projects for both the Setup and the Maintenance manual editors.
"""

from collections.abc import Collection
from dataclasses import dataclass

from ems.mqtt_control.power_capability import (
    BLOCK_BROKER_SOURCE_UNKNOWN,
    BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED,
    BLOCK_HARDWARE_PROFILE_DEFERRED,
    BLOCK_HARDWARE_PROFILE_MISSING,
    BLOCK_HARDWARE_PROFILE_UNKNOWN,
    BLOCK_TRANSPORT_WRITE_NOT_IMPLEMENTED,
    normalize_broker_source,
    profile_write_route_implemented,
    resolve_power_write_capability,
)
from ems.zendure_mqtt.write_protocols import resolve_write_protocol

CAPABILITY_SUPPORTED = "supported"
CAPABILITY_UNSUPPORTED = "unsupported"
CAPABILITY_UNKNOWN = "unknown"

# Machine-readable reasons a device is (not) controllable.
REASON_WRITE_METHOD_MISSING = "write_method_missing"
REASON_OUTPUT_CONTROL_NOT_OBSERVED = "output_control_not_observed"

# The observed telemetry capability that marks a device as an output-controllable
# inverter (outputLimit / inverseMaxPower seen).
OUTPUT_CONTROL_CAPABILITY = "output_control"

# Block reasons that mean "no model pinned" rather than "known but not writable".
_UNKNOWN_BLOCK_REASONS = frozenset(
    {BLOCK_HARDWARE_PROFILE_MISSING, BLOCK_HARDWARE_PROFILE_UNKNOWN}
)


@dataclass(frozen=True)
class MqttOutputControlCapability:
    """Whether a Zendure MQTT device can accept output-control writes."""

    status: str
    reason: str
    write_protocol: str | None
    output_control_observed: bool
    hardware_profile: str | None = None
    power_write_profile: str | None = None
    block_reason: str | None = None
    model_supported: bool = False
    transport_supported: bool = False
    telemetry_family: str | None = None
    write_family: str | None = None
    broker_source: str | None = None
    broker_source_supported: bool = False

    @property
    def supported(self) -> bool:
        return self.status == CAPABILITY_SUPPORTED


@dataclass(frozen=True)
class OutputControlCapability:
    """Model, transport, broker-source and write-route verdict for one device.

    ``supported`` requires all four; ``reason`` names the first missing one so a
    UI never has to reconstruct the rule.
    """

    supported: bool
    reason: str | None
    model_supported: bool
    transport_supported: bool
    write_route_ready: bool
    telemetry_family: str | None
    write_family: str | None
    broker_source: str | None = None
    broker_source_supported: bool = False

    @property
    def block_reason(self) -> str | None:
        """Alias of ``reason``; the machine-readable cause when not supported."""

        return self.reason


def mqtt_output_control_capability(
    *,
    topic_family: str | None,
    broker_source: str | None,
    hardware_profile: str | None = None,
    write_protocol: str | None = None,
    observed_capabilities: Collection[str] = (),
) -> MqttOutputControlCapability:
    """Resolve the output-control capability for a Zendure MQTT device.

    A pinned ``hardware_profile`` is the primary authority and its write route
    additionally needs a broker source that is a proven carrier for it. Without a
    pinned profile, only an explicit supported ``write_protocol`` (the custom
    escape hatch) authorizes a write; a bare topic family never does.
    """

    caps = {c for c in (observed_capabilities or ()) if isinstance(c, str)}
    observed = OUTPUT_CONTROL_CAPABILITY in caps
    family = str(topic_family or "").strip()

    if isinstance(hardware_profile, str) and hardware_profile.strip():
        cap = resolve_power_write_capability(
            topic_family=family,
            hardware_profile=hardware_profile,
            broker_source=broker_source,
        )
        if cap.supported:
            return MqttOutputControlCapability(
                CAPABILITY_SUPPORTED,
                cap.write_profile,
                None,
                observed,
                hardware_profile=cap.profile_id,
                power_write_profile=cap.write_profile,
                block_reason=None,
                model_supported=True,
                transport_supported=True,
                telemetry_family=cap.telemetry_family,
                write_family=cap.write_family,
                broker_source=cap.broker_source,
                broker_source_supported=True,
            )
        status = (
            CAPABILITY_UNKNOWN
            if cap.block_reason in _UNKNOWN_BLOCK_REASONS
            else CAPABILITY_UNSUPPORTED
        )
        return MqttOutputControlCapability(
            status,
            cap.block_reason,
            None,
            observed,
            hardware_profile=cap.profile_id,
            power_write_profile=cap.write_profile,
            block_reason=cap.block_reason,
            model_supported=cap.model_supported,
            transport_supported=cap.transport_supported,
            telemetry_family=cap.telemetry_family,
            write_family=cap.write_family,
            broker_source=cap.broker_source,
            broker_source_supported=cap.broker_source_supported,
        )

    # No pinned profile: only an explicit, supported write protocol authorizes.
    # The custom escape hatch carries an operator-supplied publish topic instead
    # of a source-derived canonical route, so the broker-source axis does not
    # apply to it — the operator has verified that exact address themselves.
    protocol = resolve_write_protocol(family, write_protocol)
    if protocol is not None:
        return MqttOutputControlCapability(
            CAPABILITY_SUPPORTED,
            protocol,
            protocol,
            observed,
            block_reason=None,
            transport_supported=True,
            telemetry_family=family or None,
            broker_source=normalize_broker_source(broker_source),
            broker_source_supported=True,
        )

    return MqttOutputControlCapability(
        CAPABILITY_UNKNOWN,
        REASON_WRITE_METHOD_MISSING,
        None,
        observed,
        block_reason=BLOCK_HARDWARE_PROFILE_MISSING,
        telemetry_family=family or None,
        broker_source=normalize_broker_source(broker_source),
    )


def resolve_output_control_capability(
    *,
    topic_family: str | None,
    hardware_profile: str | None,
    broker_source: str | None,
    product_key: str | None = None,
    device_id: str | None = None,
    write_protocol: str | None = None,
    write_topic: str | None = None,
) -> OutputControlCapability:
    """Compose model, broker-source and write-route completeness.

    The shared projection Admin renders in both manual editors: it answers
    "controllable?" and, when not, names the first missing precondition with the
    same reason codes config validation and migration already use.
    """

    from ems.zendure_mqtt.config_entries import zendure_mqtt_control_addressability

    capability = mqtt_output_control_capability(
        topic_family=topic_family,
        hardware_profile=hardware_profile,
        broker_source=broker_source,
        write_protocol=write_protocol,
    )
    # Every axis is resolved and reported, even when an earlier one already
    # blocks: a UI that shows "unavailable" must be able to say which single
    # precondition is missing without implying the others also failed.
    addressability = zendure_mqtt_control_addressability(
        {
            "mqtt": {
                "product_key": product_key or "",
                "device_id": device_id or "",
                "write_topic": write_topic or "",
            }
        },
        profile_backed=profile_write_route_implemented(hardware_profile),
    )
    reason = capability.block_reason or capability.reason
    if capability.supported:
        reason = None if addressability.ready else addressability.reason
    return OutputControlCapability(
        capability.supported and addressability.ready,
        reason,
        capability.model_supported,
        capability.transport_supported,
        addressability.ready,
        capability.telemetry_family,
        capability.write_family,
        broker_source=capability.broker_source,
        broker_source_supported=capability.broker_source_supported,
    )


def proposal_output_control(cap: MqttOutputControlCapability) -> tuple[bool, str]:
    """``(enabled, reason)`` for a discovery proposal.

    A discovered device is proposed as controllable as soon as its concrete
    hardware profile resolves to a supported write method. Output-control
    telemetry is useful evidence, but it is not an additional operator gate:
    supported MQTT inverters join the same control loop by default as local-API
    devices.
    """

    if not cap.supported:
        return False, cap.reason
    return True, cap.reason


__all__ = [
    "CAPABILITY_SUPPORTED",
    "CAPABILITY_UNSUPPORTED",
    "CAPABILITY_UNKNOWN",
    "REASON_WRITE_METHOD_MISSING",
    "REASON_OUTPUT_CONTROL_NOT_OBSERVED",
    "OUTPUT_CONTROL_CAPABILITY",
    "BLOCK_BROKER_SOURCE_UNKNOWN",
    "BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED",
    "BLOCK_HARDWARE_PROFILE_DEFERRED",
    "BLOCK_TRANSPORT_WRITE_NOT_IMPLEMENTED",
    "MqttOutputControlCapability",
    "OutputControlCapability",
    "mqtt_output_control_capability",
    "resolve_output_control_capability",
    "proposal_output_control",
]
