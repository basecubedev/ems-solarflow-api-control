# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
from dataclasses import dataclass, field

# Battery presence is three-valued because absence cannot be inferred from
# silence: `packNum` missing means nobody said, not that there is no pack.
BATTERY_PRESENT = "yes"
BATTERY_ABSENT = "no"
BATTERY_UNKNOWN = "unknown"


def parse_pack_count(value):
    """Return an observed pack count, or None when the value is not one.

    Shared so the state store never records a count the controller refuses to
    believe: a stored 0 means a confirmed zero, and a support bundle prints it
    as one. JSON has no integers, so 2.0 is a count and 2.9 is not.
    """

    if value is None or isinstance(value, bool):
        return None

    try:
        packs = float(str(value).strip())
    except (TypeError, ValueError):
        return None

    if not packs.is_integer() or packs < 0:
        return None

    return int(packs)


def battery_presence(state):
    """Classify battery presence from telemetry.

    Only an observed ``packNum`` decides. A field the device never reported
    stays ``unknown`` and must keep behaving exactly as an unclassified device
    always has, because no caller may turn silence into a licence.
    """

    packs = parse_pack_count(getattr(state, "pack_num", None))

    if packs is None:
        return BATTERY_UNKNOWN

    return BATTERY_PRESENT if packs > 0 else BATTERY_ABSENT


DEVICE_STATE_PROPERTIES = frozenset(
    {
        "electricLevel", "minSoc", "socSet", "solarInputPower", "outputHomePower",
        "packInputPower", "outputPackPower", "hyperTmp", "BatVolt", "rssi",
        "remainOutTime", "solarPower1", "solarPower2", "solarPower3", "solarPower4",
        "outputLimit", "socLimit", "packState", "faultLevel", "smartMode",
        "gridOffMode", "acMode", "acStatus", "dcStatus", "gridState", "inputLimit",
        "packNum", "socStatus", "batCalTime", "gridInputPower", "gridReverse",
        "pass", "reverseState",
        "chargeLimit", "chargeMaxLimit",
        "lampSwitch", "Fanmode", "Fanspeed", "buzzerSwitch", "autoHeat",
    }
)


MAX_PACK_NUMBER = 16


@dataclass(frozen=True)
class PackCells:
    """One battery pack's readings, each None when not reported.

    Its lowest and highest cell voltage, the power and state (0 idle,
    1 charging, 2 discharging) the pack reports of itself, which the
    device-level pack values do not show while the device stands idle, and
    its temperature in °C.
    """

    pack: int
    cell_min_v: float | None
    cell_max_v: float | None
    power_w: float | None = None
    state: int | None = None
    temp_c: float | None = None


@dataclass
class DeviceState:
    """One device report as the controller reads it.

    Each ``*_reported`` flag is False when the report carried no usable value
    for a field a reconciler writes (``acMode``, ``minSoc``, ``socSet``,
    ``smartMode``, ``gridOffMode``, ``inputLimit``); the value then reads 0,
    which is not a state the device is in (K10, K20). ``setting_properties``
    holds the ``ems.device_settings`` properties the report carries.
    ``pass_state`` and ``reverse_state`` are the report's ``pass`` and
    ``reverseState``, None when it carries none: recorded, never acted on.
    """

    soc: float

    min_soc: float
    max_soc: float

    solar: float
    output: float

    pack_in: float
    pack_out: float

    temp: float | None
    voltage: float
    rssi: int

    remain_minutes: float

    solar1: float
    solar2: float
    solar3: float
    solar4: float

    output_limit: float
    soc_limit: int
    pack_state: int

    fault_level: int
    
    smart_mode: int
    grid_off_mode: int
    ac_mode: int
    ac_status: int
    dc_status: int
    grid_state: int
    input_limit_w: int = 0
    # The device's own AC charge ceiling. It is not the output limit: drawing in
    # and feeding out are different paths with different ratings. None when the
    # device reports none, which is not the same as reporting 0.
    charge_max_limit_w: int | None = None
    # Measured AC input power, the mirror of `output`. `input_limit_w` is what
    # was asked for; this is what flows.
    grid_input: int = 0
    # Whether the device exports surplus on its own rather than curtailing at
    # max SoC. Only ems.surplus_export sets it: 1 while another device can take
    # that surplus in, 2 on every other device it manages.
    grid_reverse: int = 0
    grid_reverse_reported: bool = False
    pass_state: int | None = None
    reverse_state: int | None = None
    # None when the device never reported the field; 0 is an observed "no pack".
    pack_num: int | None = None
    soc_status: int = 0
    battery_calibration_time: int | None = None
    battery_packs: tuple[PackCells, ...] = ()
    # Which of solar1..solar4 the report carried; None when nobody said,
    # which reads as all four.
    pv_inputs_reported: tuple[bool, ...] | None = None
    ac_mode_reported: bool = True
    min_soc_reported: bool = True
    max_soc_reported: bool = True
    smart_mode_reported: bool = True
    grid_off_mode_reported: bool = True
    input_limit_reported: bool = True
    setting_properties: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeviceCapabilities:
    can_charge: bool
    can_discharge: bool
    can_export: bool
    can_ac_charge: bool
    reason: str
    export_held: bool = False

# =====================
# DEVICE PARSING
# =====================
