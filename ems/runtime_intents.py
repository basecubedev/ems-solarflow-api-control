# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime device intent model and the arbiter that resolves competing claims.

Several sources want to own a device's AC mode in the same cycle: the operator's
parked role and a maintenance routine today, the surplus charge regulator later.
They are candidates, not writers — ``resolve_device_intent`` picks one, so "who
owns acMode and inputLimit right now" has a single answer that can be logged.

Priority ladder. Only the levels with a producer today carry a name; later
phases slot into the gaps without renumbering: 100 is reserved for the charge
regulator and 300 for safety preemption.

A firmware-observed charge sits deliberately *low*, just above the default. It
exists to stop the loop's reflexive ac_output claim from overwriting a charge the
device started by itself — not to stop the EMS from steering a device on purpose.
Every deliberate claim, operator or maintenance, outranks it.
"""

from dataclasses import dataclass
from enum import Enum

from ems.power_direction import AC_MODE_INPUT, AC_STATUS_CHARGING

PRIORITY_DEFAULT = 0
PRIORITY_FIRMWARE_OBSERVED = 50
PRIORITY_OPERATOR_PARK = 150
PRIORITY_MAINTENANCE = 200

FIRMWARE_CHARGE_REASON = "firmware_owned_charge"


class DeviceRuntimeRole(str, Enum):
    AC_OUTPUT = "ac_output"
    AC_INPUT = "ac_input"


@dataclass(frozen=True)
class DeviceRuntimeIntent:
    """One source's claim on a device for this cycle.

    ``setpoint_w`` is the AC charge power the claim commands. ``None`` is not
    the absence of a claim — it means the winner owns the device but commands no
    value, which is what keeps a firmware-owned charge distinguishable from an
    EMS-commanded one.
    """

    device: str
    role: DeviceRuntimeRole
    reason: str
    desired_ac_mode: int | None
    output_control_allowed: bool
    priority: int = PRIORITY_DEFAULT
    setpoint_w: int | None = None


def ac_output_intent(device_name, reason: str = "ac_output", *, priority=PRIORITY_DEFAULT):
    return DeviceRuntimeIntent(
        device=device_name,
        role=DeviceRuntimeRole.AC_OUTPUT,
        reason=reason,
        desired_ac_mode=2,
        output_control_allowed=True,
        priority=priority,
    )


def ac_input_intent(
    device_name, reason: str, *, setpoint_w=None, priority=PRIORITY_OPERATOR_PARK
):
    return DeviceRuntimeIntent(
        device=device_name,
        role=DeviceRuntimeRole.AC_INPUT,
        reason=reason,
        desired_ac_mode=1,
        output_control_allowed=False,
        priority=priority,
        setpoint_w=setpoint_w,
    )


def runtime_intent_from_role(device_name, role, reason: str | None = None):
    normalized = str(role or DeviceRuntimeRole.AC_OUTPUT.value).strip().lower()
    if normalized in ("normal_output", DeviceRuntimeRole.AC_OUTPUT.value):
        return ac_output_intent(device_name, reason or "ac_output")
    if normalized in (
        "ac_input_charge",
        "reserved",
        DeviceRuntimeRole.AC_INPUT.value,
    ):
        return ac_input_intent(device_name, reason or "runtime_state")
    return None


def _telemetry_int(value):
    """Coerce a telemetry field to int; anything unreadable counts as absent."""

    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def firmware_charge_intent(device_name, state):
    """Claim a device the firmware put into AC charge by itself, or None.

    Observed, never predicted. The firmware decides when to recover an empty
    battery from AC; reimplementing that trigger here would be a second
    authority for someone else's threshold and would drift the first time a
    firmware or a model moves it. The device's own status is read instead.

    Both the written mode and the observed status must agree, which keeps the
    ~2 s settling window after an EMS command from being mistaken for the
    firmware acting on its own. Whether the EMS meant to put the device there is
    not this producer's question: the priority ladder answers it, because every
    deliberate claim outranks this one.
    """

    if _telemetry_int(getattr(state, "ac_status", 0)) != AC_STATUS_CHARGING:
        return None

    if _telemetry_int(getattr(state, "ac_mode", 0)) != AC_MODE_INPUT:
        return None

    return DeviceRuntimeIntent(
        device=device_name,
        role=DeviceRuntimeRole.AC_INPUT,
        reason=FIRMWARE_CHARGE_REASON,
        desired_ac_mode=None,
        output_control_allowed=False,
        priority=PRIORITY_FIRMWARE_OBSERVED,
    )


def resolve_device_intent(candidates):
    """Return the highest-priority candidate, or ``None`` when there is none.

    A tie keeps the earlier candidate, so caller order is the documented
    tie-break rather than an accident of sorting.
    """

    winner = None
    for candidate in candidates:
        if candidate is None:
            continue
        if winner is None or candidate.priority > winner.priority:
            winner = candidate
    return winner


__all__ = [
    "PRIORITY_DEFAULT",
    "PRIORITY_OPERATOR_PARK",
    "PRIORITY_MAINTENANCE",
    "PRIORITY_FIRMWARE_OBSERVED",
    "FIRMWARE_CHARGE_REASON",
    "DeviceRuntimeRole",
    "DeviceRuntimeIntent",
    "ac_output_intent",
    "ac_input_intent",
    "runtime_intent_from_role",
    "firmware_charge_intent",
    "resolve_device_intent",
]
