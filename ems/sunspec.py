# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grid power from a SunSpec meter model, read over Modbus TCP.

A SunSpec device marks its register map with ``SunS`` at one of three base
addresses and chains its models after it, each as id, length and points. The
meter models 201 to 204 carry the total real power ``W`` as an int16 with a
scale factor, the models 211 to 214 as a float32; the offsets are those of the
SunSpec model definitions. The meter is looked up once per connection, and the
client underneath can only read.
"""

import math
import struct
from dataclasses import dataclass

from ems.e3dc_modbus import ModbusExceptionResponse, decode_string
from ems.modbus_meters import ModbusTcpMeter

SUNSPEC_BASES = (40000, 0, 50000)
SUNSPEC_MARKER = (0x5375, 0x6E53)
END_OF_MODELS = 0xFFFF
INT_METER_MODELS = frozenset({201, 202, 203, 204})
FLOAT_METER_MODELS = frozenset({211, 212, 213, 214})
INT_W_OFFSET = 18
INT_W_SF_OFFSET = 22
FLOAT_W_OFFSET = 28
NOT_IMPLEMENTED_INT16 = 0x8000
MAX_MODELS = 64
COMMON_MODEL_ID = 1
COMMON_MODEL_REGISTERS = 68


class SunSpecError(ValueError):
    """The device does not serve a usable SunSpec meter."""


@dataclass(frozen=True)
class MeterModel:
    model_id: int
    address: int


def _signed(register):
    return register - 0x10000 if register & 0x8000 else register


def _marker_base(client):
    for base in SUNSPEC_BASES:
        try:
            marker = tuple(client.read_registers(base, 2))
        except ModbusExceptionResponse:
            continue
        if marker == SUNSPEC_MARKER:
            return base
    raise SunSpecError("no SunSpec device answers at this unit ID: no 'SunS' at register 40000, 0 or 50000")


def locate_meter(client):
    """The first meter model in the device's SunSpec chain."""

    address = _marker_base(client) + 2
    for _ in range(MAX_MODELS):
        model_id, length = client.read_registers(address, 2)
        if model_id == END_OF_MODELS:
            break
        if model_id in INT_METER_MODELS or model_id in FLOAT_METER_MODELS:
            return MeterModel(model_id, address)
        address += 2 + length
        if address > 0xFFFF - 1:
            break
    raise SunSpecError("this SunSpec device carries no meter model (201 to 204, 211 to 214) at this unit ID")


def read_common(client):
    """Manufacturer, model and serial number the device's SunSpec common model names."""

    registers = client.read_registers(_marker_base(client) + 2, COMMON_MODEL_REGISTERS)
    if registers[0] != COMMON_MODEL_ID:
        raise SunSpecError("the SunSpec chain does not start with the common model")
    return decode_string(registers[2:18]), decode_string(registers[18:34]), decode_string(registers[50:66])


def read_meter_power(client, model):
    """Total real power in W, as the meter model reports it."""

    if model.model_id in INT_METER_MODELS:
        registers = client.read_registers(model.address + INT_W_OFFSET, INT_W_SF_OFFSET - INT_W_OFFSET + 1)
        power, scale = registers[0], registers[-1]
        if NOT_IMPLEMENTED_INT16 in (power, scale):
            raise SunSpecError(f"meter model {model.model_id} does not report its total real power")
        scale = _signed(scale)
        if not -10 <= scale <= 10:
            raise SunSpecError(f"meter model {model.model_id} reports an implausible scale factor {scale}")
        return _signed(power) * 10.0 ** scale
    high, low = client.read_registers(model.address + FLOAT_W_OFFSET, 2)
    power = struct.unpack(">f", struct.pack(">HH", high, low))[0]
    if not math.isfinite(power):
        raise SunSpecError(f"meter model {model.model_id} does not report its total real power")
    return power


class SunSpecMeter(ModbusTcpMeter):
    """A SunSpec meter; its meter model is looked up again on each new connection."""

    def __init__(self, host, port=502, unit_id=1, **kwargs):
        super().__init__(host, port, unit_id, **kwargs)
        self.model = None

    def _read(self, client):
        if self.model is None:
            self.model = locate_meter(client)
        return read_meter_power(client, self.model)

    def close(self):
        self.model = None
        super().close()
