# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model-aware Zendure ``function/invoke`` power command builder.

The single place that constructs a ``deviceAutomation`` invoke payload. It
validates the resolved hardware profile, the requested operation and the write
identifiers before building; an unsupported operation (Hub/AIO AC charging) or
an unknown/deferred/ZenSDK profile is rejected with :class:`PowerCommandError`
and never converted into a fallback command. ZenSDK devices keep their separate
``properties/write`` path (see :mod:`ems.zendure_mqtt.write_protocols`) and are
never routed through this builder.
"""

from dataclasses import dataclass

from ems.mqtt_control.zendure_profiles import (
    OPERATION_CHARGE,
    OPERATION_DISCHARGE,
    OPERATION_IDLE,
    WRITE_PROFILE_LEGACY_HUB,
    WRITE_PROFILE_LEGACY_OBJECT,
    hardware_profile_by_name,
    operation_for_target,
)
from ems.zendure_mqtt.write_protocols import next_message_id as next_power_message_id

INVOKE_FUNCTION = "deviceAutomation"
_INVOKE_TOPIC = "iot/{product_key}/{device_id}/function/invoke"

# The autoModel program/mode selectors are protocol constants observed on real
# devices; discharge and charge share autoModel 8, idle uses 0.
_AUTO_MODEL_PROGRAM = {
    OPERATION_DISCHARGE: 2,
    OPERATION_IDLE: 0,
    OPERATION_CHARGE: 1,
}
_AUTO_MODEL = {
    OPERATION_DISCHARGE: 8,
    OPERATION_IDLE: 0,
    OPERATION_CHARGE: 8,
}

# Flat AC-charge price schedule; the EMS does not run a tariff, so every hour
# carries the same neutral price.
_FLAT_PRICES = [1] * 24
_CHARGE_PRICE = 2


class PowerCommandError(ValueError):
    """A power command could not be built and must not be published."""


@dataclass(frozen=True)
class ZendurePowerCommand:
    topic: str
    payload: dict[str, object]
    operation: str
    target_w: int


def _object_value(operation: str, watts: int) -> dict[str, object]:
    if operation == OPERATION_CHARGE:
        return {
            "chargingType": 1,
            "price": _CHARGE_PRICE,
            "chargingPower": watts,
            "prices": list(_FLAT_PRICES),
            "outPower": 0,
            "freq": 0,
        }
    return {"chargingType": 0, "chargingPower": 0, "freq": 0, "outPower": watts}


def _valid_positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def build_power_command(
    *,
    hardware_profile,
    target_w,
    product_key,
    device_id,
    message_id,
    timestamp,
) -> ZendurePowerCommand:
    """Build a validated ``function/invoke`` power command, or raise.

    ``target_w`` follows the EMS sign convention: ``> 0`` discharge, ``0`` idle,
    ``< 0`` AC charging (published as a positive charging-watt value).
    """

    profile = (
        hardware_profile_by_name(hardware_profile)
        if isinstance(hardware_profile, str)
        else None
    )
    if profile is None:
        raise PowerCommandError(f"unknown hardware profile: {hardware_profile!r}")

    write_profile = profile.power_write_profile
    if write_profile not in (WRITE_PROFILE_LEGACY_HUB, WRITE_PROFILE_LEGACY_OBJECT):
        raise PowerCommandError(
            f"profile {profile.canonical_name} is not a function/invoke automation profile"
        )

    if isinstance(target_w, bool) or not isinstance(target_w, int):
        raise PowerCommandError("target_w must be an integer")
    operation = operation_for_target(target_w)
    if not profile.supports_operation(operation):
        raise PowerCommandError(
            f"{profile.canonical_name} does not support operation {operation}"
        )

    if not isinstance(product_key, str) or not product_key:
        raise PowerCommandError("product_key is required")
    if not isinstance(device_id, str) or not device_id:
        raise PowerCommandError("device_id is required")
    if not _valid_positive_int(message_id):
        raise PowerCommandError("message_id must be a positive integer")
    if not _valid_positive_int(timestamp):
        raise PowerCommandError("timestamp must be a positive integer")

    watts = abs(target_w)
    if write_profile == WRITE_PROFILE_LEGACY_HUB:
        auto_model_value: object = watts
    else:
        auto_model_value = _object_value(operation, watts)

    argument = {
        "autoModelProgram": _AUTO_MODEL_PROGRAM[operation],
        "autoModelValue": auto_model_value,
        "msgType": 1,
        "autoModel": _AUTO_MODEL[operation],
    }
    payload = {
        "function": INVOKE_FUNCTION,
        "arguments": [argument],
        "messageId": message_id,
        "deviceKey": device_id,
        "deviceId": device_id,
        "timestamp": timestamp,
    }
    topic = _INVOKE_TOPIC.format(product_key=product_key, device_id=device_id)
    return ZendurePowerCommand(
        topic=topic, payload=payload, operation=operation, target_w=target_w
    )


__all__ = [
    "PowerCommandError",
    "ZendurePowerCommand",
    "build_power_command",
    "next_power_message_id",
    "INVOKE_FUNCTION",
]
