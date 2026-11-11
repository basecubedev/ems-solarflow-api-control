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

from ems.e3dc_modbus import ModbusExceptionResponse, ReadOnlyModbusTcpClient

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


class SunSpecError(ValueError):
    """The device does not serve a usable SunSpec meter."""


@dataclass(frozen=True)
class MeterModel:
    model_id: int
    address: int


def _signed(register):
    return register - 0x10000 if register & 0x8000 else register


def locate_meter(client):
    """The first meter model in the device's SunSpec chain."""

    for base in SUNSPEC_BASES:
        try:
            marker = tuple(client.read_registers(base, 2))
        except ModbusExceptionResponse:
            continue
        if marker != SUNSPEC_MARKER:
            continue
        address = base + 2
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
    raise SunSpecError("no SunSpec device answers at this unit ID: no 'SunS' at register 40000, 0 or 50000")


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


class SunSpecMeter:
    """One Modbus TCP connection to a SunSpec meter; a failure drops it for the next read."""

    def __init__(self, host, port=502, unit_id=1, *, timeout=3.0, client_factory=ReadOnlyModbusTcpClient):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._client_factory = client_factory
        self._client = None
        self.model = None

    @property
    def endpoint(self):
        return f"{self.host}:{self.port} unit {self.unit_id}"

    def read_power(self):
        try:
            if self._client is None:
                client = self._client_factory(self.host, port=self.port, unit_id=self.unit_id, timeout=self.timeout)
                client.connect()
                self._client = client
            if self.model is None:
                self.model = locate_meter(self._client)
            return round(read_meter_power(self._client, self.model), 1)
        except Exception:
            self.close()
            raise

    def close(self):
        client, self._client, self.model = self._client, None, None
        if client is not None:
            client.close()
