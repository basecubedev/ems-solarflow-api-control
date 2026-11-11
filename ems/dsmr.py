# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DSMR P1 telegrams, read from a bridge that passes the P1 port on over TCP.

A Dutch or Belgian smart meter sends a telegram on its P1 port every second
(DSMR 5) or every ten seconds (older meters); a bridge such as a P1-to-WiFi
gateway or ser2net hands the raw bytes to whoever connects. The reader keeps
one connection, takes what arrived since its last call without waiting, and
only waits for the first telegram after connecting. Nothing is ever sent to the
bridge. An encrypted P1 port, as on Luxembourg's meters, is not read.
"""

import re
import socket
import time
from dataclasses import dataclass

MAX_BUFFER_BYTES = 64 * 1024

_TELEGRAM = re.compile(rb"/[^/!]*!([0-9A-Fa-f]{4})?\r?\n")
_OBJECT = re.compile(r"^(\d+-\d+:\d+\.\d+\.\d+)\(([^)]*)\)", re.M)


class DsmrError(ValueError):
    """A telegram the reader cannot use."""


@dataclass(frozen=True)
class DsmrReading:
    power_w: float
    timestamp: str | None


def crc16(data):
    """CRC16/ARC over a telegram from ``/`` to ``!``, as DSMR 4 and 5 sign it."""

    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def split_telegrams(buffer):
    """Whole telegrams in ``buffer``, and what is left of a telegram still arriving."""

    telegrams = []
    end = 0
    for match in _TELEGRAM.finditer(buffer):
        telegrams.append(match.group(0))
        end = match.end()
    rest = buffer[end:]
    start = rest.rfind(b"/")
    return telegrams, rest[start:] if start >= 0 else b""


def _power_kw(objects, code):
    value = objects.get(code)
    if value is None:
        return None
    number, _, unit = value.partition("*")
    try:
        power = float(number)
    except ValueError as exc:
        raise DsmrError(f"telegram value {code} is not a number: {value!r}") from exc
    if unit not in ("kW", "W") or power != power or power < 0:
        raise DsmrError(f"telegram value {code} is not a power in kW: {value!r}")
    return power if unit == "kW" else power / 1000.0


def parse_telegram(raw):
    """Grid power (import positive, export negative) and meter time of one telegram.

    A telegram that carries a checksum must match it; an older one without a
    checksum is taken as it is.
    """

    match = _TELEGRAM.fullmatch(raw)
    if match is None:
        raise DsmrError("not a whole DSMR telegram")
    if match.group(1) is not None:
        signed = raw[: raw.index(b"!") + 1]
        if crc16(signed) != int(match.group(1), 16):
            raise DsmrError("telegram checksum does not match")
    objects = dict(_OBJECT.findall(raw.decode("ascii", errors="replace")))
    drawn = _power_kw(objects, "1-0:1.7.0")
    if drawn is None:
        raise DsmrError("telegram carries no power drawn (1-0:1.7.0)")
    fed_in = _power_kw(objects, "1-0:2.7.0") or 0.0
    return DsmrReading(round((drawn - fed_in) * 1000.0, 1), objects.get("0-0:1.0.0"))


class DsmrTcpReader:
    """One connection to a P1 bridge; :meth:`poll` returns the telegrams it sent since."""

    def __init__(self, host, port, *, timeout=3.0, connect=socket.create_connection, clock=time.monotonic):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._connect = connect
        self._clock = clock
        self._socket = None
        self._buffer = b""

    @property
    def endpoint(self):
        return f"{self.host}:{self.port}"

    def poll(self):
        """Whole telegrams received since the last call, oldest first.

        Right after connecting it waits up to ``timeout`` for one; otherwise it
        takes only what is already there. A broken connection is closed and
        raises, and the next call connects again.
        """

        deadline = None
        if self._socket is None:
            self._socket = self._connect((self.host, self.port), self.timeout)
            self._buffer = b""
            deadline = self._clock() + self.timeout
        telegrams = []
        try:
            while True:
                waiting = deadline is not None and not telegrams
                self._socket.settimeout(max(0.0, deadline - self._clock()) if waiting else 0.0)
                try:
                    chunk = self._socket.recv(4096)
                except (BlockingIOError, TimeoutError):
                    break
                if not chunk:
                    raise ConnectionError(f"the P1 bridge at {self.endpoint} closed the connection")
                found, self._buffer = split_telegrams((self._buffer + chunk)[-MAX_BUFFER_BYTES:])
                telegrams.extend(found)
        except OSError:
            self.close()
            raise
        return telegrams

    def close(self):
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
