# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime device intent model for controller reservations."""

from dataclasses import dataclass
from enum import Enum


class DeviceRuntimeRole(str, Enum):
    AC_OUTPUT = "ac_output"
    AC_INPUT = "ac_input"


@dataclass(frozen=True)
class DeviceRuntimeIntent:
    device: str
    role: DeviceRuntimeRole
    reason: str
    desired_ac_mode: int | None
    output_control_allowed: bool
    priority: int = 0


def ac_output_intent(device_name: str, reason: str = "ac_output"):
    return DeviceRuntimeIntent(
        device=device_name,
        role=DeviceRuntimeRole.AC_OUTPUT,
        reason=reason,
        desired_ac_mode=2,
        output_control_allowed=True,
        priority=0,
    )


def ac_input_intent(device_name: str, reason: str):
    return DeviceRuntimeIntent(
        device=device_name,
        role=DeviceRuntimeRole.AC_INPUT,
        reason=reason,
        desired_ac_mode=1,
        output_control_allowed=False,
        priority=100,
    )


def runtime_intent_from_role(device_name: str, role: str, reason: str | None = None):
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
