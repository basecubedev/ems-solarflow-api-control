# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grid meters read over Modbus TCP from a device's published register map.

A profile names where one device keeps its grid power: the function code, the
registers (added up where a device gives the phases only), their type and word
order, a scale, and whether the device counts export as positive. A profile
with a status register is refused while the device reports its meter offline,
and one with a minimum interval is not asked again sooner than its device
allows, give or take the control loop's own jitter; a cycle in between gets
the last reading, as an MQTT meter's cycles do.
The register maps are those of the manufacturers' Modbus documents; every
request is a read, and the client underneath cannot send anything else.
"""

import time
from dataclasses import dataclass

from ems.e3dc_modbus import READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS, ReadOnlyModbusTcpClient

MODBUS_TCP_PORT = 502
MIN_INTERVAL_SLACK_S = 0.25


class ModbusMeterError(ValueError):
    """The device answered, but not with a usable grid power."""


@dataclass(frozen=True)
class MeterProfile:
    meter_type: str
    label: str
    unit_id: int
    function_code: int
    registers: tuple
    datatype: str
    low_word_first: bool = False
    scale: float = 1.0
    export_positive: bool = False
    status: tuple | None = None
    port: int = MODBUS_TCP_PORT
    min_interval_s: float = 0.0


PROFILES = (
    MeterProfile(
        "huawei_sun2000_modbus",
        "Huawei SUN2000 + DTSU666",
        unit_id=1,
        function_code=READ_HOLDING_REGISTERS,
        registers=(37113,),
        datatype="int32",
        export_positive=True,
        status=(37100, 1),
    ),
    MeterProfile(
        "sungrow_sh_modbus",
        "Sungrow SH series",
        unit_id=1,
        function_code=READ_INPUT_REGISTERS,
        registers=(5600,),
        datatype="int32",
        low_word_first=True,
        min_interval_s=1.0,
    ),
    MeterProfile(
        "carlo_gavazzi_em24_modbus",
        "Carlo Gavazzi EM24-E1",
        unit_id=1,
        function_code=READ_HOLDING_REGISTERS,
        registers=(0x0028,),
        datatype="int32",
        low_word_first=True,
        scale=0.1,
    ),
    MeterProfile(
        "victron_gx_modbus",
        "Victron GX",
        unit_id=100,
        function_code=READ_HOLDING_REGISTERS,
        registers=(820, 821, 822),
        datatype="int16",
    ),
    MeterProfile(
        "smart_me_modbus",
        "smart-me Telstar",
        unit_id=1,
        function_code=READ_HOLDING_REGISTERS,
        registers=(0x2003,),
        datatype="int32",
        scale=0.001,
        min_interval_s=2.0,
    ),
    MeterProfile(
        "iammeter_wem3080t_modbus",
        "IAMMETER WEM3080T",
        unit_id=1,
        function_code=READ_HOLDING_REGISTERS,
        registers=(32,),
        datatype="int32",
    ),
    MeterProfile(
        "wibeee_modbus",
        "Circutor Wibeee",
        unit_id=1,
        function_code=READ_HOLDING_REGISTERS,
        registers=(6,),
        datatype="int32",
        low_word_first=True,
    ),
)
PROFILES_BY_TYPE = {profile.meter_type: profile for profile in PROFILES}


def _signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _value(words, profile):
    if profile.datatype == "int16":
        return _signed(words[0], 16)
    high, low = (words[1], words[0]) if profile.low_word_first else (words[0], words[1])
    return _signed((high << 16) | low, 32)


def read_profile_power(client, profile):
    """Grid power in W, import positive, as the profile reads it in one request."""

    width = 2 if profile.datatype == "int32" else 1
    addresses = [*profile.registers, *((profile.status[0],) if profile.status else ())]
    first = min(addresses)
    count = max(address + (width if address in profile.registers else 1) for address in addresses) - first
    block = client.read_registers(first, count, function_code=profile.function_code)
    if profile.status and block[profile.status[0] - first] != profile.status[1]:
        raise ModbusMeterError(f"{profile.label} reports its meter offline (status {block[profile.status[0] - first]})")
    power = sum(_value(block[address - first:address - first + width], profile) for address in profile.registers)
    power *= profile.scale
    return -power if profile.export_positive else power


class ModbusTcpMeter:
    """One Modbus TCP connection to a meter; a failure drops it for the next read."""

    def __init__(self, host, port=MODBUS_TCP_PORT, unit_id=1, *, timeout=3.0, client_factory=ReadOnlyModbusTcpClient):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._client_factory = client_factory
        self._client = None

    @property
    def endpoint(self):
        return f"{self.host}:{self.port} unit {self.unit_id}"

    def read_power(self):
        try:
            if self._client is None:
                client = self._client_factory(self.host, port=self.port, unit_id=self.unit_id, timeout=self.timeout)
                client.connect()
                self._client = client
            return round(self._read(self._client), 1)
        except Exception:
            self.close()
            raise

    def close(self):
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def _read(self, client):
        raise NotImplementedError


class ProfileMeter(ModbusTcpMeter):
    """A meter read by its device's profile."""

    def __init__(self, host, profile, port=None, unit_id=None, *, clock=time.monotonic, **kwargs):
        super().__init__(
            host,
            profile.port if port is None else port,
            profile.unit_id if unit_id is None else unit_id,
            **kwargs,
        )
        self.profile = profile
        self._clock = clock
        self._last = None

    def read_power(self):
        now = self._clock()
        if self._last is not None and now - self._last[0] < self.profile.min_interval_s - MIN_INTERVAL_SLACK_S:
            return self._last[1]
        power = super().read_power()
        self._last = (now, power)
        return power

    def _read(self, client):
        return read_profile_power(client, self.profile)
