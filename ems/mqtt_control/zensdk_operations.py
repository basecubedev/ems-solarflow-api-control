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
    NOT built here. The ZenSDK AC-charge shape
    (``smartMode 1 / acMode 1 / inputLimit <w>``) is known from the reference
    implementation but no ZenSDK profile enables charge until it is validated
    on hardware, so this builder fails closed rather than emitting an
    unverified charge command.

``expected_properties`` names the telemetry values that prove the command was
applied — confirmation must verify the fields that make the command effective,
not merely that some output sample changed.
"""

from dataclasses import dataclass

from ems.mqtt_control.zendure_profiles import (
    OPERATION_DISCHARGE,
    OPERATION_IDLE,
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

    Raises :class:`ZenSdkOperationError` for any operation without a verified
    contract (currently: charge). Callers gate operations through the model
    capability first; this raise is fail-closed defense in depth.
    """

    if isinstance(target_w, bool) or not isinstance(target_w, int):
        raise ZenSdkOperationError("target_w must be an integer")
    operation = operation_for_target(target_w)
    if operation not in (OPERATION_DISCHARGE, OPERATION_IDLE):
        raise ZenSdkOperationError(
            f"zensdk operation {operation} has no verified command contract"
        )
    properties = {
        "smartMode": 1,
        "acMode": 2,
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
