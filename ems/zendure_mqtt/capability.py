# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared decision: does a Zendure MQTT device support output control?

Single source of truth for MQTT output-control capability, reused by Admin
proposal generation, Admin config drafts, config sanitization/validation,
runtime validation, diagnostics and frontend presentation data. Capability is
decided from the *pinned hardware profile* (via
:func:`ems.mqtt_control.power_capability.resolve_power_write_capability`), never
from the topic family. The topic family only names the telemetry transport; it
can never on its own authorize a write.

A device is:

``supported``
    a pinned hardware profile is known, writable and compatible with the topic
    family, or an explicit supported ``write_protocol`` (the operator-verified
    custom escape hatch) resolves.

``unsupported``
    a known device that cannot write here (transport-incompatible, deferred
    model, or a scalar family with no explicit write method).

``unknown``
    no hardware profile is pinned and no write method could be resolved.
"""

from collections.abc import Collection
from dataclasses import dataclass

from ems.mqtt_control.power_capability import (
    BLOCK_HARDWARE_PROFILE_DEFERRED,
    BLOCK_HARDWARE_PROFILE_MISSING,
    BLOCK_HARDWARE_PROFILE_UNKNOWN,
    BLOCK_TRANSPORT_INCOMPATIBLE,
    resolve_power_write_capability,
)
from ems.zendure_mqtt.topics import SCALAR_FAMILIES
from ems.zendure_mqtt.write_protocols import resolve_write_protocol

CAPABILITY_SUPPORTED = "supported"
CAPABILITY_UNSUPPORTED = "unsupported"
CAPABILITY_UNKNOWN = "unknown"

# Machine-readable reasons a device is (not) controllable.
REASON_SCALAR_WRITE_NOT_VERIFIED = "scalar_write_not_verified"
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

    @property
    def supported(self) -> bool:
        return self.status == CAPABILITY_SUPPORTED


def mqtt_output_control_capability(
    *,
    topic_family: str | None,
    hardware_profile: str | None = None,
    write_protocol: str | None = None,
    observed_capabilities: Collection[str] = (),
) -> MqttOutputControlCapability:
    """Resolve the output-control capability for a Zendure MQTT device.

    A pinned ``hardware_profile`` is the primary authority. Without one, only an
    explicit supported ``write_protocol`` (the custom escape hatch) authorizes a
    write; a bare topic family never does.
    """

    caps = {c for c in (observed_capabilities or ()) if isinstance(c, str)}
    observed = OUTPUT_CONTROL_CAPABILITY in caps
    family = str(topic_family or "").strip()

    if isinstance(hardware_profile, str) and hardware_profile.strip():
        cap = resolve_power_write_capability(
            topic_family=family, hardware_profile=hardware_profile
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
        )

    # No pinned profile: only an explicit, supported write protocol authorizes.
    protocol = resolve_write_protocol(family, write_protocol)
    if protocol is not None:
        return MqttOutputControlCapability(
            CAPABILITY_SUPPORTED,
            protocol,
            protocol,
            observed,
            block_reason=None,
        )

    if family in SCALAR_FAMILIES:
        return MqttOutputControlCapability(
            CAPABILITY_UNSUPPORTED,
            REASON_SCALAR_WRITE_NOT_VERIFIED,
            None,
            observed,
            block_reason=BLOCK_HARDWARE_PROFILE_MISSING,
        )
    return MqttOutputControlCapability(
        CAPABILITY_UNKNOWN,
        REASON_WRITE_METHOD_MISSING,
        None,
        observed,
        block_reason=BLOCK_HARDWARE_PROFILE_MISSING,
    )


def proposal_output_control(cap: MqttOutputControlCapability) -> tuple[bool, str]:
    """``(enabled, reason)`` for a discovery proposal.

    A discovered device is proposed as controllable as soon as its concrete
    hardware profile and telemetry transport resolve to a supported write
    method. Output-control telemetry is useful evidence, but it is not an
    additional operator gate: supported MQTT inverters join the same control
    loop by default as local-API devices.
    """

    if not cap.supported:
        return False, cap.reason
    return True, cap.reason


__all__ = [
    "CAPABILITY_SUPPORTED",
    "CAPABILITY_UNSUPPORTED",
    "CAPABILITY_UNKNOWN",
    "REASON_SCALAR_WRITE_NOT_VERIFIED",
    "REASON_WRITE_METHOD_MISSING",
    "REASON_OUTPUT_CONTROL_NOT_OBSERVED",
    "OUTPUT_CONTROL_CAPABILITY",
    "BLOCK_HARDWARE_PROFILE_DEFERRED",
    "BLOCK_TRANSPORT_INCOMPATIBLE",
    "MqttOutputControlCapability",
    "mqtt_output_control_capability",
    "proposal_output_control",
]
