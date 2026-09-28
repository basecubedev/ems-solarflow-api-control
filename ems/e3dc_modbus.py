# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only Modbus/TCP access to an E3/DC (HagerEnergy) energy storage system.

The register knowledge here is the single owner for everything in this project
that reads an E3/DC: the grid-meter client in ``ems.clients`` and the probe in
``scripts/e3dc_modbus_probe.py``. It follows "Modbus/TCP-Schnittstelle der
HagerEnergy GmbH", V1.90 (28.01.2022), section 3.1 "E3/DC Simple Mode",
cross-checked against three independent open-source implementations that agree
on every address and on the word order:

* mccrossen/iobroker-modbus-e3dc  (register table, manual V1.70, ``int32sw``)
* stonehage/E3DC-Modbus-ESP32     (``REG_OFFSET -1``, magic probe, int32 math)
* nischram/EMD_1                  (same offsets plus the DC string registers)

Two traps in the manual are resolved deliberately:

* Section 4.2's feed-in example lists its two register values the other way
  round from what its own arithmetic then substitutes. The arithmetic, the
  section's first example and both ESP32 implementations agree that the *lower*
  address holds the *low* word, so that is what is implemented here; the
  feed-in example's two table lines are transposed and must not be copied.
* Section 3.1.4 labels inverter offset 4 "L2" (a duplicate of offset 2) and
  spells reactive power "Blinkleistung". Read as L3 and "Blindleistung".

Safety: only Modbus function codes 3 and 4, both reads, can ever be sent. The
request builder refuses anything outside ``READ_ONLY_FUNCTION_CODES``, so no
caller can turn this into a writer. The module is stdlib-only and imports
nothing else from ``ems``, so the standalone probe can use it too.
"""

import socket
import struct
import time
from dataclasses import dataclass

READ_HOLDING_REGISTERS = 3
READ_INPUT_REGISTERS = 4
READ_ONLY_FUNCTION_CODES = frozenset({READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS})

MAX_REGISTERS_PER_READ = 125
MODBUS_TCP_PORT = 502

MAGIC_ADDRESS = 40001
MAGIC_VALUE = 0xE3DC
SUNSPEC_FIRST_WORD = 0x5375

ADDRESS_OFFSET_CANDIDATES = (-1, 0, -2, -3, 1)
UNIT_ID_CANDIDATES = (1, 0, 255)

MODBUS_EXCEPTION_TEXT = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "server device failure",
    5: "acknowledge",
    6: "server device busy",
    8: "memory parity error",
    10: "gateway path unavailable",
    11: "gateway target device failed to respond",
}


class ModbusError(Exception):
    """Any failure that keeps a value from being read."""


class ModbusExceptionResponse(ModbusError):
    """The device answered with a Modbus exception rather than data."""

    def __init__(self, function_code, exception_code):
        self.function_code = function_code
        self.exception_code = exception_code
        text = MODBUS_EXCEPTION_TEXT.get(exception_code, "unknown")
        super().__init__(f"modbus exception {exception_code} ({text})")


def decode_uint16(registers):
    return registers[0]


def decode_int16(registers):
    raw = registers[0]
    return raw - 0x10000 if raw & 0x8000 else raw


def decode_int32_word_swapped(registers):
    """Low word at the lower address, each word big-endian (manual 4.2)."""

    raw = (registers[1] << 16) | registers[0]
    return raw - 0x100000000 if raw & 0x80000000 else raw


def decode_string(registers):
    raw = b"".join(struct.pack(">H", value) for value in registers)
    return raw.split(b"\x00", 1)[0].decode("latin-1").strip()


def decode_version(registers):
    return f"{registers[0] >> 8}.{registers[0] & 0xFF}"


def decode_byte_pair(registers):
    """Two 8-bit percentages packed into one register (manual 4.3)."""

    return [registers[0] >> 8, registers[0] & 0xFF]


DECODERS = {
    "uint16": decode_uint16,
    "int16": decode_int16,
    "int32sw": decode_int32_word_swapped,
    "string": decode_string,
    "version": decode_version,
    "u8u8": decode_byte_pair,
}

SCALABLE_DATATYPES = frozenset({"uint16", "int16", "int32sw"})


@dataclass(frozen=True)
class Field:
    """One readable quantity, addressed the way the E3/DC manual numbers it."""

    key: str
    address: int
    length: int
    datatype: str
    label: str
    unit: str = ""
    scale: float = 1.0
    note: str = ""

    def decode(self, registers):
        value = DECODERS[self.datatype](registers)
        if self.scale != 1.0 and self.datatype in SCALABLE_DATATYPES:
            return round(value * self.scale, 2)
        return value


IDENTIFICATION_FIRST = 40001
IDENTIFICATION_COUNT = 67
IDENTIFICATION_FIELDS = (
    Field("magic", 40001, 1, "uint16", "Magic word"),
    Field("modbus_firmware", 40002, 1, "version", "Modbus firmware"),
    Field("supported_registers", 40003, 1, "uint16", "Supported registers"),
    Field("manufacturer", 40004, 16, "string", "Manufacturer"),
    Field("model", 40020, 16, "string", "Model"),
    Field("serial_number", 40036, 16, "string", "Serial number"),
    Field("firmware_release", 40052, 16, "string", "Firmware release"),
)

POWER_FIRST = 40068
POWER_COUNT = 18
POWER_FIELDS = (
    Field("pv_power", 40068, 2, "int32sw", "PV power", "W"),
    Field(
        "battery_power", 40070, 2, "int32sw", "Battery power", "W",
        note="negative = discharge",
    ),
    Field("home_power", 40072, 2, "int32sw", "Home consumption", "W"),
    Field(
        "grid_power", 40074, 2, "int32sw", "Grid power at transfer point", "W",
        note="negative = feed-in",
    ),
    Field("additional_power", 40076, 2, "int32sw", "Additional producers", "W"),
    Field("wallbox_power", 40078, 2, "int32sw", "Wallbox power", "W"),
    Field("wallbox_solar_power", 40080, 2, "int32sw", "Wallbox solar share", "W"),
    Field(
        "autarky_self_consumption", 40082, 1, "u8u8", "Autarky / self-consumption", "%",
        note="[autarky, self-consumption]",
    ),
    Field("battery_soc", 40083, 1, "uint16", "Battery SOC", "%"),
    Field("emergency_power_status", 40084, 1, "uint16", "Emergency power status"),
    Field("ems_status", 40085, 1, "uint16", "EMS status", note="bit field, manual 3.1.5"),
)

GRID_POWER_FIELD = next(item for item in POWER_FIELDS if item.key == "grid_power")

DC_STRING_FIRST = 40096
DC_STRING_COUNT = 9
DC_STRING_FIELDS = (
    Field("dc_string_1_voltage", 40096, 1, "uint16", "DC string 1 voltage", "V"),
    Field("dc_string_2_voltage", 40097, 1, "uint16", "DC string 2 voltage", "V"),
    Field("dc_string_1_current", 40099, 1, "uint16", "DC string 1 current", "A", scale=0.01),
    Field("dc_string_2_current", 40100, 1, "uint16", "DC string 2 current", "A", scale=0.01),
    Field("dc_string_1_power", 40102, 1, "uint16", "DC string 1 power", "W"),
    Field("dc_string_2_power", 40103, 1, "uint16", "DC string 2 power", "W"),
)

POWER_METER_FIRST = 40105
POWER_METER_COUNT = 28
POWER_METER_STRIDE = 4
POWER_METER_INDICES = range(7)

POWER_METER_TYPES = {
    1: "root power meter (control point, usually the grid connection)",
    2: "external production",
    3: "two-way meter",
    4: "external consumption",
    5: "farm",
    6: "unused",
    7: "wallbox",
    8: "external farm power meter",
    9: "data display only (not part of the control loop)",
    10: "control bypass",
}

INVERTER_FIRST = 41000
INVERTER_STRIDE = 34
INVERTER_COUNT = 34
INVERTER_FIELDS = (
    Field("apparent_power_l1", 0, 2, "int32sw", "Apparent power L1", "VA"),
    Field("apparent_power_l2", 2, 2, "int32sw", "Apparent power L2", "VA"),
    Field("apparent_power_l3", 4, 2, "int32sw", "Apparent power L3", "VA"),
    Field("active_power_l1", 6, 2, "int32sw", "Active power L1", "W"),
    Field("active_power_l2", 8, 2, "int32sw", "Active power L2", "W"),
    Field("active_power_l3", 10, 2, "int32sw", "Active power L3", "W"),
    Field("reactive_power_l1", 12, 2, "int32sw", "Reactive power L1", "var"),
    Field("reactive_power_l2", 14, 2, "int32sw", "Reactive power L2", "var"),
    Field("reactive_power_l3", 16, 2, "int32sw", "Reactive power L3", "var"),
    Field("ac_voltage_l1", 18, 1, "int16", "AC voltage L1", "V", scale=0.1),
    Field("ac_voltage_l2", 19, 1, "int16", "AC voltage L2", "V", scale=0.1),
    Field("ac_voltage_l3", 20, 1, "int16", "AC voltage L3", "V", scale=0.1),
    Field("ac_current_l1", 21, 1, "int16", "AC current L1", "A", scale=0.01),
    Field("ac_current_l2", 22, 1, "int16", "AC current L2", "A", scale=0.01),
    Field("ac_current_l3", 23, 1, "int16", "AC current L3", "A", scale=0.01),
    Field("frequency", 24, 1, "int16", "Phase frequency L1", "Hz", scale=0.01),
    Field("dc_power_l1", 25, 1, "int16", "DC power L1", "W"),
    Field("dc_power_l2", 26, 1, "int16", "DC power L2", "W"),
    Field("dc_voltage_l1", 28, 1, "int16", "DC voltage L1", "V", scale=0.1),
    Field("dc_voltage_l2", 29, 1, "int16", "DC voltage L2", "V", scale=0.1),
    Field("dc_current_l1", 31, 1, "int16", "DC current L1", "A", scale=0.01),
    Field("dc_current_l2", 32, 1, "int16", "DC current L2", "A", scale=0.01),
)


def power_meter_fields(index):
    """Type plus the three phase powers of one of the seven power meters."""

    base = POWER_METER_FIRST + index * POWER_METER_STRIDE
    return (
        Field(f"lm{index}_type", base, 1, "uint16", f"Power meter {index} type"),
        Field(f"lm{index}_l1", base + 1, 1, "int16", f"Power meter {index} L1", "W"),
        Field(f"lm{index}_l2", base + 2, 1, "int16", f"Power meter {index} L2", "W"),
        Field(f"lm{index}_l3", base + 3, 1, "int16", f"Power meter {index} L3", "W"),
    )


class ReadOnlyModbusTcpClient:
    """Modbus/TCP client that can physically only read.

    The request builder rejects every function code outside
    ``READ_ONLY_FUNCTION_CODES``, so no caller and no argument combination can
    turn this into a writer.
    """

    def __init__(self, host, port=MODBUS_TCP_PORT, unit_id=1, timeout=3.0):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._socket = None
        self._transaction_id = 0
        self.connect_seconds = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    def connect(self):
        self.close()
        started = time.monotonic()
        try:
            self._socket = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as exc:
            raise ModbusError(f"cannot connect to {self.host}:{self.port}: {exc}")
        self._socket.settimeout(self.timeout)
        self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connect_seconds = time.monotonic() - started

    def close(self):
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _receive_exactly(self, count):
        chunks = []
        remaining = count
        while remaining > 0:
            try:
                chunk = self._socket.recv(remaining)
            except TimeoutError:
                raise ModbusError("timeout waiting for the Modbus response")
            except OSError as exc:
                raise ModbusError(f"socket error while reading: {exc}")
            if not chunk:
                raise ModbusError("device closed the connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_registers(self, address, count, function_code=READ_HOLDING_REGISTERS, unit_id=None):
        if function_code not in READ_ONLY_FUNCTION_CODES:
            raise ModbusError(f"function code {function_code} is not a read; refused")
        if not 1 <= count <= MAX_REGISTERS_PER_READ:
            raise ModbusError(f"register count {count} outside 1..{MAX_REGISTERS_PER_READ}")
        if not 0 <= address <= 0xFFFF or address + count - 1 > 0xFFFF:
            raise ModbusError(f"address range {address}..{address + count - 1} outside 0..65535")
        if self._socket is None:
            raise ModbusError("not connected")

        effective_unit = self.unit_id if unit_id is None else unit_id
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        request = struct.pack(
            ">HHHBBHH", self._transaction_id, 0, 6, effective_unit, function_code, address, count
        )
        try:
            self._socket.sendall(request)
        except OSError as exc:
            raise ModbusError(f"socket error while sending: {exc}")

        transaction_id, protocol_id, length = struct.unpack(">HHH", self._receive_exactly(6))
        if protocol_id != 0:
            raise ModbusError(f"unexpected Modbus protocol id {protocol_id}")
        if not 2 <= length <= 253:
            raise ModbusError(f"implausible Modbus frame length {length}")
        body = self._receive_exactly(length)
        if transaction_id != self._transaction_id:
            raise ModbusError(
                f"transaction id mismatch (sent {self._transaction_id}, got {transaction_id})"
            )
        answered_function = body[1]
        if answered_function == (function_code | 0x80):
            if len(body) < 3:
                raise ModbusError("truncated Modbus exception response")
            raise ModbusExceptionResponse(function_code, body[2])
        if answered_function != function_code:
            raise ModbusError(f"device answered function code {answered_function}")
        if len(body) < 3:
            raise ModbusError("truncated Modbus response")
        byte_count = body[2]
        if byte_count != count * 2 or len(body) < 3 + byte_count:
            raise ModbusError(f"device returned {byte_count} bytes for {count} registers")
        return struct.unpack(f">{count}H", body[3:3 + byte_count])


@dataclass
class Mapping:
    """The addressing that actually answered on this installation."""

    unit_id: int
    function_code: int
    address_offset: int
    magic_value: int

    def wire_address(self, manual_address):
        return manual_address + self.address_offset


@dataclass
class Attempt:
    unit_id: int
    function_code: int
    address_offset: int
    outcome: str


def detect_mapping(client, unit_ids, function_codes, offsets):
    """Probe for the magic word the way the manual's section 4.1 prescribes.

    The manual states the address offset is not uniform across Modbus software
    and must be established with the magic word before any other register is
    trusted, so every candidate offset is tried rather than assumed.
    """

    attempts = []
    for unit_id in unit_ids:
        for function_code in function_codes:
            for offset in offsets:
                address = MAGIC_ADDRESS + offset
                try:
                    registers = client.read_registers(address, 1, function_code, unit_id)
                except ModbusExceptionResponse as exc:
                    attempts.append(Attempt(unit_id, function_code, offset, str(exc)))
                    continue
                except ModbusError as exc:
                    attempts.append(Attempt(unit_id, function_code, offset, str(exc)))
                    try:
                        client.connect()
                    except ModbusError:
                        return None, attempts
                    continue
                value = registers[0]
                attempts.append(Attempt(unit_id, function_code, offset, f"0x{value:04X}"))
                if value == MAGIC_VALUE:
                    return Mapping(unit_id, function_code, offset, value), attempts
    return None, attempts


def looks_like_sunspec(attempts):
    """A SunSpec base address answers 'SunS'; that means the wrong mapping is on."""

    return any(attempt.outcome == f"0x{SUNSPEC_FIRST_WORD:04X}" for attempt in attempts)


@dataclass
class Reading:
    field: Field
    available: bool
    value: object = None
    error: str = ""
    base: int = 0

    @property
    def register(self):
        """The manual register this value came from, absolute for a based block."""

        return self.base + self.field.address


def read_fields(client, mapping, fields, block_first, block_count, base=0):
    """Read a whole block in one request, falling back to single reads.

    One request keeps the values of a snapshot consistent with each other. When
    the block is refused, each field is retried alone so that one unsupported
    register does not hide the ones that do work.
    """

    try:
        registers = client.read_registers(
            mapping.wire_address(base + block_first), block_count,
            mapping.function_code, mapping.unit_id,
        )
    except ModbusError as block_error:
        return _read_fields_individually(client, mapping, fields, base, str(block_error))

    readings = []
    for item in fields:
        start = item.address - block_first
        chunk = registers[start:start + item.length]
        if len(chunk) != item.length:
            readings.append(
                Reading(item, False, error="outside the returned block", base=base)
            )
            continue
        readings.append(Reading(item, True, item.decode(chunk), base=base))
    return readings


def _read_fields_individually(client, mapping, fields, base, block_error):
    readings = []
    for item in fields:
        try:
            registers = client.read_registers(
                mapping.wire_address(base + item.address), item.length,
                mapping.function_code, mapping.unit_id,
            )
        except ModbusError as exc:
            readings.append(
                Reading(item, False, error=f"{exc} (block read: {block_error})", base=base)
            )
            try:
                client.connect()
            except ModbusError:
                pass
            continue
        readings.append(Reading(item, True, item.decode(registers), base=base))
    return readings
