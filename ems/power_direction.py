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


def in_ac_input_direction(state) -> bool:
    """Whether telemetry shows the device written to, or running in, AC input.

    Either value is enough. The written ``acMode`` decides how the next command
    is read -- a bare ``outputLimit`` is ignored in ``acMode = 1`` -- and the
    status covers the settling window in which the two still disagree.
    """

    def reported(name):
        value = getattr(state, name, None)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    return (
        reported("ac_mode") == AC_MODE_INPUT
        or reported("ac_status") == AC_STATUS_CHARGING
    )


def out_of_charge(state) -> bool:
    """Whether telemetry shows a device that is not in an AC charge.

    Out of the AC-input direction, or still in its mode with no setpoint and
    nothing flowing -- how a charge looks once it was ended by its setpoint
    alone, which keeps the device in the role a claim gave it.
    """

    if not in_ac_input_direction(state):
        return True

    def reported(name):
        value = getattr(state, name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return value

    return (
        reported("ac_status") != AC_STATUS_CHARGING
        and reported("input_limit_w") == 0
        and reported("grid_input") == 0
    )


def settling_out_of_charge(state) -> bool:
    """Whether a device shows an exit taken while its charge current still runs down.

    The output direction is written and no charge setpoint stands, but the
    status and the measured input lag by about two seconds. The current is the
    tail of a charge the device has already left, not one to be ended.
    """

    ac_mode = getattr(state, "ac_mode", None)
    setpoint = getattr(state, "input_limit_w", 0)
    if isinstance(ac_mode, bool) or ac_mode != AC_MODE_OUTPUT:
        return False
    return isinstance(setpoint, bool) or not isinstance(setpoint, (int, float)) or setpoint == 0


def found_in_ac_input(state) -> bool:
    """Whether telemetry shows a device held in AC input, charging or not.

    The AC-input direction, less the settling window of an exit the device has
    already taken: that current is the tail of a charge it left.
    """

    return in_ac_input_direction(state) and not settling_out_of_charge(state)


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
    "found_in_ac_input",
    "in_ac_input_direction",
    "operation_for_target",
    "out_of_charge",
    "settling_out_of_charge",
]
