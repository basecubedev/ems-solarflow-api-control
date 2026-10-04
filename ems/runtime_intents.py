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

from ems.config import safe_int
from ems.power_direction import (
    AC_MODE_INPUT,
    AC_MODE_OUTPUT,
    AC_STATUS_CHARGING,
    found_in_ac_input,
)
from ems.target_control import derive_soc_runtime_state

PRIORITY_DEFAULT = 0
PRIORITY_FIRMWARE_OBSERVED = 50
PRIORITY_REGULATOR = 100
PRIORITY_OPERATOR_PARK = 150
PRIORITY_MAINTENANCE = 200

FIRMWARE_CHARGE_REASON = "firmware_owned_charge"
UNPROVEN_CHARGE_REASON = "unproven_charge"
REGULATOR_CHARGE_REASON = "ac_charge_regulator"


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
        desired_ac_mode=AC_MODE_OUTPUT,
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
        desired_ac_mode=AC_MODE_INPUT,
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


def is_floor_charge(state):
    """Whether telemetry shows an AC charge at the battery's floor.

    Observations, no prediction. The firmware decides when to recover an empty
    battery from AC; reimplementing that trigger here would be a second
    authority for someone else's threshold and would drift the first time a
    firmware or a model moves it.

    The written mode must be the charge mode, and the charge must be visible:
    either the status says charging, or a charge setpoint stands while current
    has yet to follow -- the ~2 s settling window. A mode with nothing set up to
    charge is not a charge. Judging the settling window here rather than
    leaving it to the state reconciler is what keeps that reconciler from
    writing ``acMode = 2`` into a charge the firmware is starting. The battery
    must be at its floor, which is where the firmware acts.
    """

    charging = safe_int(getattr(state, "ac_status", 0)) == AC_STATUS_CHARGING
    settling = safe_int(getattr(state, "input_limit_w", 0), 0) > 0
    return (
        (charging or settling)
        and safe_int(getattr(state, "ac_mode", 0)) == AC_MODE_INPUT
        and derive_soc_runtime_state(state) == "soc_empty"
    )


def firmware_charge_intent(
    device_name, state, *, ems_commanded_charge=False, watched=True
):
    """Claim an AC input the EMS did not command, or None.

    The EMS must not have asked for this charge itself — without that, the
    regulator's own charge would be read back as firmware-owned, the device
    would be marked uncommandable, and the regulator would shut itself down two
    cycles after starting.

    ``watched`` says whether the EMS can tell who put the device there: it saw
    the device out of AC input, or in its own charge, since this process
    started, or held there by a claim that outranks this one, or the device
    took the exit, or was asked for it often enough. Unwatched, a device found
    in AC input -- above the floor as at it, charging or not -- may hold the
    charge of the process before this one, which a stop by signal leaves
    running. The claim then stands the state reconciler down and leaves the
    device to the power command, which writes the exit until the device takes
    it, three attempts at most (owner decisions 2026-10-04).

    Watched, a charge at the floor is the firmware's protection charge, and it
    is respected -- nothing is written until it ends. Above the floor, AC input
    the EMS can attribute to someone else is no claim of its own: the device
    gets what it got before charging existed.
    """

    if ems_commanded_charge:
        return None

    if not watched:
        if not found_in_ac_input(state):
            return None
        return DeviceRuntimeIntent(
            device=device_name,
            role=DeviceRuntimeRole.AC_INPUT,
            reason=UNPROVEN_CHARGE_REASON,
            desired_ac_mode=None,
            output_control_allowed=True,
            priority=PRIORITY_FIRMWARE_OBSERVED,
        )

    if not is_floor_charge(state):
        return None

    return DeviceRuntimeIntent(
        device=device_name,
        role=DeviceRuntimeRole.AC_INPUT,
        reason=FIRMWARE_CHARGE_REASON,
        desired_ac_mode=None,
        output_control_allowed=False,
        priority=PRIORITY_FIRMWARE_OBSERVED,
    )


def regulator_charge_intent(device_name, *, ems_commanded_charge):
    """Claim a device the surplus regulator is currently charging, or None.

    This exists to *stop* a write, not to make one. The per-cycle default claim
    is ``ac_output``, whose desired mode is ``AC_MODE_OUTPUT``, so while the
    regulator holds a device in ``acMode = 1`` the state reconciler sees a
    mismatch and writes ``acMode = 2`` — every cycle, against the power command
    writing ``acMode = 1``. Two writers of one property, a relay commanded back
    and forth once per loop: exactly the wear the hysteresis exists to prevent,
    and invisible in any test whose state-reconciliation gate happens to be shut.

    ``desired_ac_mode=None`` is the claim "the power command owns this device's
    direction", which is what the reconciler already honours for a
    firmware-owned charge. ``output_control_allowed`` stays **True**: the
    regulator must keep the right to command the device it is charging, or it
    reads its own claim back as someone else's and shuts itself down.

    Why not simply point the reconciler at ``AC_MODE_INPUT`` instead, so it
    keeps enforcing the *right* mode? Because it writes a bare ``{"acMode": n}``,
    and the hardware probe established that **a direction change needs the
    atomic set** while a power change within a direction does not. A bare
    ``acMode: 1`` would put the device into charge mode carrying whatever
    ``inputLimit`` it still held — 0 after an exit — so it would sit in the
    charge direction drawing nothing, and ``ac_charge_not_delivered`` would then
    warn about it. Standing down is not "nobody watches": a device that drifts
    out of charge mode reports no AC input, the write deadband measures the
    target against exactly that, and the power command puts it back in the next
    cycle with a complete command.

    Priority sits above the firmware observation and below an operator park, so
    parking a device or a maintenance claim still takes it away mid-charge.
    """

    if not ems_commanded_charge:
        return None

    return DeviceRuntimeIntent(
        device=device_name,
        role=DeviceRuntimeRole.AC_INPUT,
        reason=REGULATOR_CHARGE_REASON,
        desired_ac_mode=None,
        output_control_allowed=True,
        priority=PRIORITY_REGULATOR,
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
    "PRIORITY_REGULATOR",
    "REGULATOR_CHARGE_REASON",
    "regulator_charge_intent",
    "FIRMWARE_CHARGE_REASON",
    "UNPROVEN_CHARGE_REASON",
    "is_floor_charge",
    "DeviceRuntimeRole",
    "DeviceRuntimeIntent",
    "ac_output_intent",
    "ac_input_intent",
    "runtime_intent_from_role",
    "firmware_charge_intent",
    "resolve_device_intent",
]
