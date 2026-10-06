# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model-specific ZenSDK power-operation contracts.

The single authority for the property set a ``zensdk_properties_write`` power
command must carry. Source contract (Zendure-HA ``ZendureZenSdk``): every power
command writes ``smartMode``/``acMode``/``outputLimit``/``inputLimit``
atomically in ONE properties write — a bare ``outputLimit`` is ignored by a
device sitting in an inactive mode (``smartMode=0`` / ``acMode=1``), and
sending the mode fields separately would race the setpoint.

Operation contracts:

``discharge`` (target > 0)
    ``{"smartMode": 1, "acMode": 2, "outputLimit": target, "inputLimit": 0}``
    (Zendure-HA ``discharge``).

``idle`` (target == 0)
    ``{"smartMode": 1, "acMode": 2, "outputLimit": 0, "inputLimit": 0}``.
    Deliberate deviation from Zendure-HA ``power_off`` (which drops to
    ``smartMode: 0`` standby): the EMS five-second loop crosses 0 W routinely
    (deadbands, ramps, night transitions), and toggling ``smartMode`` on every
    crossing would churn a flash-persistent operating mode and delay the next
    output start. Staying in smart output regulation at 0 W is the safe steady
    contract; long standby phases are governed upstream (strict night idle
    stops commanding entirely).

``charge`` (target < 0)
    ``{"smartMode": 1, "acMode": 1, "outputLimit": 0, "inputLimit": |target|}``.
    Measured on a SolarFlow 800 Pro 2 on 2026-09-13: the device drew the
    commanded power within 4.5 s. Whether a given *model* has an AC charge path
    is a separate question, answered by its catalogue entry — this builder only
    owns the shape.

``expected_properties`` names the telemetry values that prove the command was
applied — confirmation must verify the fields that make the command effective,
not merely that some output sample changed.
"""

from dataclasses import dataclass

from ems.power_direction import (
    AC_MODE_INPUT,
    AC_MODE_OUTPUT,
    OPERATION_CHARGE,
    operation_for_target,
)


class ZenSdkOperationError(ValueError):
    """The requested ZenSDK power operation has no verified contract."""


@dataclass(frozen=True)
class ZenSdkPowerOperation:
    """One atomic ZenSDK power command: written and expected property sets."""

    operation: str
    properties: dict
    expected_properties: dict


def build_zensdk_power_operation(target_w) -> ZenSdkPowerOperation:
    """Build the atomic ZenSDK property set for a signed power target.

    The shape only. Whether a model may be sent this operation at all is the
    catalogue's decision, enforced before the command is built.
    """

    if isinstance(target_w, bool) or not isinstance(target_w, int):
        raise ZenSdkOperationError("target_w must be an integer")
    operation = operation_for_target(target_w)
    if operation == OPERATION_CHARGE:
        properties = {
            "smartMode": 1,
            "acMode": AC_MODE_INPUT,
            "outputLimit": 0,
            "inputLimit": abs(target_w),
        }
    else:
        properties = {
            "smartMode": 1,
            "acMode": AC_MODE_OUTPUT,
            "outputLimit": target_w,
            "inputLimit": 0,
        }
    return ZenSdkPowerOperation(
        operation=operation,
        properties=properties,
        expected_properties=dict(properties),
    )


__all__ = [
    "ZenSdkOperationError",
    "ZenSdkPowerOperation",
    "build_zensdk_power_operation",
]
