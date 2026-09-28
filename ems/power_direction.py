# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transport-neutral power direction vocabulary.

The controller commands one signed power target per device: positive discharges
into the house, negative charges from AC, zero idles. That sign convention
belongs to the control loop rather than to any one transport, so local HTTP and
both MQTT routes resolve an operation through the same names here.
"""

OPERATION_DISCHARGE = "discharge"
OPERATION_IDLE = "idle"
OPERATION_CHARGE = "charge"

# Where the neutral vocabulary meets the device. ``acMode`` is the value that was
# *written*; ``acStatus`` is what the device is *doing*, and a hardware probe on
# an 800 Pro 2 measured them ~2 s apart across a direction change. Only the
# status proves a direction was actually taken.
AC_MODE_INPUT = 1
AC_MODE_OUTPUT = 2
AC_STATUS_CHARGING = 2


def operation_for_target(target_w: int) -> str:
    """Map a signed controller target to a neutral operation.

    ``> 0`` discharge / AC output, ``== 0`` idle / stop, ``< 0`` AC charging.
    The EMS sign convention stays internal; the write adapter converts a charge
    operation to a positive charging watt value.
    """

    if target_w > 0:
        return OPERATION_DISCHARGE
    if target_w < 0:
        return OPERATION_CHARGE
    return OPERATION_IDLE


def derive_house_load_w(inverter_output_w, grid_power_w, inverter_charge_w=0):
    """Net both directions against the meter to get what the house draws.

    The meter reads one exchange power for everything behind it, so a charging
    device is indistinguishable from an appliance unless its draw is subtracted
    again. It reports ``output == 0`` while charging, which is why summing the
    outputs alone attributes the whole charge power to the household.
    """

    return max(
        0.0,
        float(inverter_output_w or 0)
        - float(inverter_charge_w or 0)
        + float(grid_power_w or 0),
    )


__all__ = [
    "AC_MODE_INPUT",
    "AC_MODE_OUTPUT",
    "AC_STATUS_CHARGING",
    "OPERATION_DISCHARGE",
    "OPERATION_IDLE",
    "OPERATION_CHARGE",
    "derive_house_load_w",
    "operation_for_target",
]
