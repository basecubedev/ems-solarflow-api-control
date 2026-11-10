# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decode SML 1.04 telegrams, the push format of German electricity meters.

Only what a grid meter reading needs: the transport frame with its CRC, the
GetListResponse entries, and the signed grid power. Import-side-effect-free.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import NamedTuple

ESCAPE = b"\x1b\x1b\x1b\x1b"
START = ESCAPE + b"\x01\x01\x01\x01"

GET_LIST_RESPONSE = 0x0701

POWER_TOTAL = bytes.fromhex("0100100700ff")
POWER_IMPORT = bytes.fromhex("0100010700ff")
POWER_EXPORT = bytes.fromhex("0100020700ff")
SERVER_ID = bytes.fromhex("0100600100ff")

UNIT_WATT = 27

_DZG_ID_PREFIX = b"\x0a\x01DZG"
_DZG_ID_LENGTH = 10
_DZG_DVS74_SIGN_BUG_SERIALS = ((42_000_000, 48_999_999), (55_000_000, 58_999_999))

_TYPE_OCTETS = 0
_TYPE_BOOL = 4
_TYPE_INT = 5
_TYPE_UINT = 6
_TYPE_LIST = 7


FRAME_DAMAGE = frozenset({"no_frame", "incomplete_frame", "bad_escape", "crc_mismatch", "bad_padding"})


class SmlError(ValueError):
    """A telegram that cannot be read; ``reason`` is a stable identifier."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class Number(NamedTuple):
    value: int
    signed: bool
    size: int


class _EndOfMessage:
    pass


END_OF_MESSAGE = _EndOfMessage()


@dataclass(frozen=True)
class Entry:
    obis: bytes
    unit: int | None
    scaler: int | None
    value: object


@dataclass(frozen=True)
class Telegram:
    entries: tuple[Entry, ...]
    server_id: bytes | None
    crc_variant: tuple[str, str] | None = None
    body: bytes | None = None

    def entry(self, obis: bytes) -> Entry | None:
        return next((e for e in self.entries if e.obis == obis), None)

    @property
    def has_dzg_sign_bug(self) -> bool:
        """A DZG DVS74 from the serial ranges whose firmware mis-signs power.

        The ranges are libsml's (``detect_buggy_dzg_dvs74``); DVS74 meters from
        serial 60000000 on, and every other DZG model, send the sign correctly.
        """
        ids = [self.server_id]
        serial = self.entry(SERVER_ID)
        if serial is not None:
            ids.append(serial.value)
        return any(_in_dzg_sign_bug_range(i) for i in ids)


def _in_dzg_sign_bug_range(meter_id: object) -> bool:
    if not isinstance(meter_id, bytes) or len(meter_id) != _DZG_ID_LENGTH:
        return False
    if not meter_id.startswith(_DZG_ID_PREFIX):
        return False
    serial = int.from_bytes(meter_id[len(_DZG_ID_PREFIX):], "big")
    return any(low <= serial <= high for low, high in _DZG_DVS74_SIGN_BUG_SERIALS)


def _crc16(data: bytes, init: int, xorout: int) -> int:
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ xorout


def crc16_x25(data: bytes) -> int:
    return _crc16(data, 0xFFFF, 0xFFFF)


def crc16_kermit(data: bytes) -> int:
    return _crc16(data, 0, 0)


CRC_VARIANTS = (
    ("x25", "big"),
    ("x25", "little"),
    ("kermit", "big"),
    ("kermit", "little"),
)
_CRC_FUNCTIONS = {"x25": crc16_x25, "kermit": crc16_kermit}


def _crc_variant(data: bytes, stored: bytes, variants) -> tuple[str, str] | None:
    """The CRC variant (algorithm, byte order) the stored checksum matches.

    Meters send X.25 or Kermit in either byte order, each meter always the
    same one; a caller that knows its meter's variant passes just that one.
    """
    computed = {}
    for name, order in variants:
        if name not in computed:
            computed[name] = _CRC_FUNCTIONS[name](data)
        if computed[name] == int.from_bytes(stored, order):
            return name, order
    return None


def unwrap_frame(data: bytes, *, crc_variants=CRC_VARIANTS) -> bytes:
    """Return the message bytes of the first complete, intact frame in ``data``."""
    return _unwrap(data, crc_variants)[0]


def _unwrap(data: bytes, crc_variants):
    """Try every start sequence in ``data``; the first intact frame wins.

    When none is intact, a CRC mismatch is reported before any other reason: a
    complete frame failing its checksum says more than a fragment beside it.
    """
    errors = []
    start = data.find(START)
    if start < 0:
        raise SmlError("no_frame", "start sequence missing")
    while start >= 0:
        try:
            return _unwrap_at(data, start, crc_variants)
        except SmlError as exc:
            errors.append(exc)
        start = data.find(START, start + 1)
    raise next((exc for exc in errors if exc.reason == "crc_mismatch"), errors[0])


def _unwrap_at(data: bytes, start: int, crc_variants):
    """Unwrap the frame that begins at ``start``.

    The transport is read in 4-byte blocks: a doubled escape is a literal
    escape, ``ESC 1a pp cc cc`` ends the frame with the padding count and the
    CRC over everything before the CRC.
    """
    body = bytearray()
    pos = start + len(START)
    while True:
        block = data[pos:pos + 4]
        if len(block) < 4:
            raise SmlError("incomplete_frame", "end sequence missing")
        if block != ESCAPE:
            body += block
            pos += 4
            continue
        following = data[pos + 4:pos + 8]
        if following == ESCAPE:
            body += ESCAPE
            pos += 8
            continue
        if len(following) < 4:
            raise SmlError("incomplete_frame", "end sequence truncated")
        if following[0] != 0x1A:
            raise SmlError("bad_escape", following.hex())
        padding = following[1]
        variant = _crc_variant(data[start:pos + 6], following[2:4], crc_variants)
        if variant is None:
            raise SmlError("crc_mismatch")
        if padding > len(body) or any(body[len(body) - padding:]):
            raise SmlError("bad_padding", str(padding))
        return bytes(body[:len(body) - padding]), variant


def _read_tl(buf: bytes, pos: int) -> tuple[int, int, int]:
    if pos >= len(buf):
        raise SmlError("truncated", f"type-length at {pos}")
    byte = buf[pos]
    kind = (byte >> 4) & 0x07
    length = byte & 0x0F
    tl_size = 1
    while byte & 0x80:
        if pos + tl_size >= len(buf):
            raise SmlError("truncated", f"type-length at {pos}")
        byte = buf[pos + tl_size]
        if (byte >> 4) & 0x07:
            raise SmlError("bad_type_length", f"at {pos + tl_size}")
        length = (length << 4) | (byte & 0x0F)
        tl_size += 1
    return kind, length, tl_size


MAX_DEPTH = 16


def read_element(buf: bytes, pos: int, depth: int = 0) -> tuple[object, int]:
    """Decode one element at ``pos``; return it and the position after it.

    Real telegrams nest five lists deep; a deeper one is refused rather than
    walked until the interpreter's recursion limit.
    """
    if depth > MAX_DEPTH:
        raise SmlError("too_deep", f"at {pos}")
    if pos < len(buf) and buf[pos] == 0x00:
        return END_OF_MESSAGE, pos + 1
    kind, length, tl_size = _read_tl(buf, pos)
    if kind == _TYPE_LIST:
        items = []
        pos += tl_size
        for _ in range(length):
            item, pos = read_element(buf, pos, depth + 1)
            items.append(item)
        return items, pos
    size = length - tl_size
    end = pos + length
    if size < 0 or end > len(buf):
        raise SmlError("truncated", f"element at {pos}")
    payload = buf[pos + tl_size:end]
    if kind == _TYPE_OCTETS:
        return (payload if payload else None), end
    if kind == _TYPE_BOOL:
        return bool(payload[0]) if payload else None, end
    if kind in (_TYPE_INT, _TYPE_UINT):
        if not payload:
            raise SmlError("bad_number", f"empty at {pos}")
        signed = kind == _TYPE_INT
        return Number(int.from_bytes(payload, "big", signed=signed), signed, size), end
    raise SmlError("bad_type", f"{kind} at {pos}")


def _number(item: object) -> int | None:
    return item.value if isinstance(item, Number) else None


def parse_messages(body: bytes) -> Telegram:
    """The GetListResponse entries of a frame body.

    A zero byte between messages is filler, as libsml reads it. An entry that
    is not a seven-element list is skipped and one whose scaler
    lies outside SML's Integer8 keeps ``scaler=None``, so a manufacturer's odd
    entry cannot hide the power values; reading such an entry fails.
    """
    entries: list[Entry] = []
    server_id = None
    pos = 0
    while pos < len(body):
        message, pos = read_element(body, pos)
        if message is END_OF_MESSAGE:
            continue
        if not isinstance(message, list) or len(message) != 6:
            raise SmlError("bad_message", f"before {pos}")
        message_body = message[3]
        if not isinstance(message_body, list) or len(message_body) != 2:
            raise SmlError("bad_message", f"body before {pos}")
        if _number(message_body[0]) != GET_LIST_RESPONSE:
            continue
        response = message_body[1]
        if not isinstance(response, list) or len(response) < 5 or not isinstance(response[4], list):
            raise SmlError("bad_list_response")
        server_id = response[1] if isinstance(response[1], bytes) else server_id
        for raw in response[4]:
            if not isinstance(raw, list) or len(raw) != 7 or not isinstance(raw[0], bytes):
                continue
            scaler = _number(raw[4])
            if scaler is None:
                scaler = 0
            elif not -128 <= scaler <= 127:
                scaler = None
            entries.append(Entry(raw[0], _number(raw[3]), scaler, raw[5]))
    if not entries:
        raise SmlError("no_values", "no GetListResponse entries")
    return Telegram(tuple(entries), server_id)


def parse(data: bytes, *, crc_variants=CRC_VARIANTS) -> Telegram:
    body, variant = _unwrap(data, crc_variants)
    return replace(parse_messages(body), crc_variant=variant, body=body)


def _dzg_corrected(value: Number) -> int:
    """Affected DZG DVS74 meters pack positive power short but tag it signed.

    They never shorten negative values, so a short negative one is a positive
    value read with the wrong sign. The same bound as libsml's workaround: a
    length field of at most 3, so a payload of at most two bytes.
    """
    if value.signed and value.size <= 2 and value.value < 0:
        return value.value + (1 << (8 * value.size))
    return value.value


def watts(entry: Entry, *, dzg: bool = False) -> float:
    if entry.unit != UNIT_WATT:
        raise SmlError("wrong_unit", f"{entry.obis.hex()} unit {entry.unit}")
    if not isinstance(entry.value, Number):
        raise SmlError("not_a_number", entry.obis.hex())
    if entry.scaler is None:
        raise SmlError("bad_scaler", entry.obis.hex())
    raw = _dzg_corrected(entry.value) if dzg and entry.obis == POWER_TOTAL else entry.value.value
    return raw * 10 ** entry.scaler


def grid_power_w(telegram: Telegram) -> float:
    """Signed grid power, import positive and export negative.

    A magnitude-only reading cannot tell import from export, so only the signed
    total or import minus export is accepted; the pair also stands in for a
    signed total whose unit or scaler is unusable.
    """
    total = telegram.entry(POWER_TOTAL)
    imported = telegram.entry(POWER_IMPORT)
    exported = telegram.entry(POWER_EXPORT)
    pair = imported is not None and exported is not None
    if total is not None and isinstance(total.value, Number) and total.value.signed:
        try:
            return watts(total, dzg=telegram.has_dzg_sign_bug)
        except SmlError:
            if not pair:
                raise
    if pair:
        return watts(imported) - watts(exported)
    raise SmlError("no_signed_power", "neither signed 16.7.0 nor 1.7.0 and 2.7.0")
