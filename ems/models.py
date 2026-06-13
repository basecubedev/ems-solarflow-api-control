# SPDX-License-Identifier: AGPL-3.0-or-later
from dataclasses import dataclass


@dataclass
class DeviceState:
    soc: float

    min_soc: float
    max_soc: float

    solar: float
    output: float

    pack_in: float
    pack_out: float

    temp: float
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


@dataclass
class DeviceCapabilities:
    can_charge: bool
    can_discharge: bool
    can_export: bool
    can_ac_charge: bool
    reason: str

# =====================
# DEVICE PARSING
# =====================
