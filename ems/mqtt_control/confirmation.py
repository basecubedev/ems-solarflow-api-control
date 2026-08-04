# SPDX-License-Identifier: AGPL-3.0-or-later
"""Telemetry-confirmation policy per Zendure write profile.

Whether an acknowledged command can be *confirmed* from telemetry — and on which
metric, within what tolerance, before what deadline — is a property of the
resolved power-write profile, not a guess. A profile that reports a directly
commanded ``outputLimit`` in telemetry supports confirmation; a profile whose
physical effect is not observable in telemetry does not, and its acknowledged
commands complete as ``completed_unconfirmed`` rather than falsely claiming a
confirmation that is impossible.
"""

from dataclasses import dataclass

from ems.mqtt_control.zendure_profiles import (
    WRITE_PROFILE_LEGACY_HUB,
    WRITE_PROFILE_LEGACY_OBJECT,
    WRITE_PROFILE_ZENSDK_PROPERTIES,
)

DEFAULT_CONFIRMATION_TIMEOUT_SECONDS = 30.0
DEFAULT_CONFIRMATION_TOLERANCE_W = 25
CONFIRMATION_METRIC_OUTPUT_LIMIT = "outputLimit"

# Write profiles whose commanded output is observable in telemetry as the
# ``outputLimit`` metric. Legacy automation confirmation stays fixture-verified
# (see docs) until physical-hardware validation.
_CONFIRMABLE_WRITE_PROFILES = frozenset(
    {
        WRITE_PROFILE_ZENSDK_PROPERTIES,
        WRITE_PROFILE_LEGACY_HUB,
        WRITE_PROFILE_LEGACY_OBJECT,
    }
)


@dataclass(frozen=True)
class TelemetryConfirmationPolicy:
    telemetry_confirmation_supported: bool
    confirmation_metric: str | None
    confirmation_tolerance_w: int
    confirmation_timeout_seconds: float


def resolve_confirmation_policy(
    write_profile,
    *,
    timeout_seconds=None,
    tolerance_w=None,
    supported_override=None,
) -> TelemetryConfirmationPolicy:
    """Resolve the confirmation policy for a write profile.

    ``supported_override`` lets a device declare confirmation unavailable even for
    a normally confirmable profile (e.g. a custom write topic whose telemetry
    mapping is unknown), so it completes ``completed_unconfirmed`` after ack.
    """

    if supported_override is not None:
        supported = bool(supported_override)
    else:
        supported = write_profile in _CONFIRMABLE_WRITE_PROFILES
    metric = CONFIRMATION_METRIC_OUTPUT_LIMIT if supported else None
    timeout = (
        float(timeout_seconds)
        if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0
        else DEFAULT_CONFIRMATION_TIMEOUT_SECONDS
    )
    tolerance = (
        int(tolerance_w)
        if isinstance(tolerance_w, (int, float)) and tolerance_w > 0
        else DEFAULT_CONFIRMATION_TOLERANCE_W
    )
    return TelemetryConfirmationPolicy(supported, metric, tolerance, timeout)


__all__ = [
    "DEFAULT_CONFIRMATION_TIMEOUT_SECONDS",
    "DEFAULT_CONFIRMATION_TOLERANCE_W",
    "CONFIRMATION_METRIC_OUTPUT_LIMIT",
    "TelemetryConfirmationPolicy",
    "resolve_confirmation_policy",
]
