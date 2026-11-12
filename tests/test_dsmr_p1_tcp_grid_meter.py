# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DSMR P1 telegrams read from a bridge that passes the P1 port on over TCP.

Telegrams follow the P1 companion standard (DSMR 5.0.2): ``/`` header, OBIS
objects, ``!`` and a CRC16/ARC over everything from ``/`` to ``!``; older
meters send no checksum. The checksum routine is held to the published check
value of CRC-16/ARC; no test reaches a real bridge.
"""

import json

import pytest

from ems import config as cfg
from ems import diagnostics
from ems import dsmr
from ems.clients import DSMR_MAX_AGE_S, DsmrTcpClient, create_grid_meter_client
from ems.dsmr import DsmrError, DsmrReading, DsmrTcpReader, crc16, parse_telegram, split_telegrams

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]

METER = {"type": "dsmr_p1_tcp", "ip": "192.0.2.80", "port": 23}


def telegram(drawn="01.193", fed_in="00.000", *, crc=True, stamp="261112050000W"):
    body = (
        "/ISk5\\2MT382-1000\r\n\r\n"
        "1-3:0.2.8(50)\r\n"
        f"0-0:1.0.0({stamp})\r\n"
        "1-0:1.8.1(001234.567*kWh)\r\n"
        "1-0:2.8.1(000123.456*kWh)\r\n"
    )
    if drawn is not None:
        body += f"1-0:1.7.0({drawn}*kW)\r\n"
    if fed_in is not None:
        body += f"1-0:2.7.0({fed_in}*kW)\r\n"
    body = (body + "!").encode("ascii")
    return body + (f"{crc16(body):04X}".encode() if crc else b"") + b"\r\n"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


# --- telegrams -----------------------------------------------------------------------


def test_the_checksum_is_crc16_arc():
    assert crc16(b"123456789") == 0xBB3D


@pytest.mark.parametrize(
    "raw, reading",
    [
        (telegram(), DsmrReading(1193.0, "261112050000W")),
        (telegram("00.000", "00.456"), DsmrReading(-456.0, "261112050000W")),
        (telegram("00.250", None), DsmrReading(250.0, "261112050000W")),
        (telegram("0000.19", "0000.00", crc=False), DsmrReading(190.0, "261112050000W")),
    ],
)
def test_a_telegram_reads_as_power_drawn_minus_power_fed_in(raw, reading):
    assert parse_telegram(raw) == reading


def test_a_telegram_whose_checksum_does_not_match_is_refused():
    raw = telegram().replace(b"01.193", b"01.194")

    with pytest.raises(DsmrError, match="checksum"):
        parse_telegram(raw)


@pytest.mark.parametrize(
    "raw, message",
    [
        (telegram(drawn=None), "1-0:1.7.0"),
        (telegram(drawn="1.2.3"), "not a number"),
        (telegram(drawn="-01.000"), "not a power"),
        (b"1-0:1.7.0(01.000*kW)\r\n", "whole DSMR telegram"),
    ],
)
def test_a_telegram_without_a_usable_power_is_refused(raw, message):
    with pytest.raises(DsmrError, match=message):
        parse_telegram(raw)


def test_telegrams_are_cut_from_the_stream_and_a_partial_one_waits():
    first, second = telegram(), telegram("00.100")
    partial = telegram("00.200")[:40]

    found, rest = split_telegrams(b"noise" + first + second + partial)

    assert found == [first, second]
    assert rest == partial
    assert split_telegrams(b"no telegram here") == ([], b"")


# --- the reader ----------------------------------------------------------------------


class FakeSocket:
    def __init__(self, *chunks):
        self.chunks = list(chunks)
        self.timeouts = []
        self.closed = False

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, _size):
        if not self.chunks:
            raise BlockingIOError() if self.timeouts[-1] == 0 else TimeoutError()
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def close(self):
        self.closed = True


class Connector:
    def __init__(self, *sockets):
        self.sockets = list(sockets)
        self.calls = []

    def __call__(self, address, timeout):
        self.calls.append((address, timeout))
        return self.sockets.pop(0)


def test_the_first_poll_waits_for_a_telegram_and_later_ones_take_what_is_there():
    one, two = telegram(), telegram("00.300")
    sock = FakeSocket(one[:30], one[30:], two)
    connect = Connector(sock)
    reader = DsmrTcpReader("192.0.2.80", 23, connect=connect, clock=Clock())

    assert reader.poll() == [one, two]
    assert connect.calls == [(("192.0.2.80", 23), 3.0)]
    assert sock.timeouts == [3.0, 3.0, 0.0, 0.0]
    assert reader.poll() == []
    assert sock.timeouts[-1] == 0.0


def test_a_closed_connection_is_dropped_and_the_next_poll_connects_again():
    first, second = FakeSocket(b""), FakeSocket(telegram())
    connect = Connector(first, second)
    reader = DsmrTcpReader("192.0.2.80", 23, connect=connect, clock=Clock())

    with pytest.raises(ConnectionError, match="closed the connection"):
        reader.poll()
    assert first.closed

    assert reader.poll() == [telegram()]
    assert len(connect.calls) == 2


# --- the meter -----------------------------------------------------------------------


class Reader:
    endpoint = "192.0.2.80:23"

    def __init__(self, *polls):
        self.polls = list(polls)
        self.closed = False

    def poll(self):
        answer = self.polls.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        self.closed = True


def test_a_new_telegram_counts_and_the_last_one_stands_until_it_is_too_old():
    clock = Clock()
    client = DsmrTcpClient(Reader([telegram("00.400")], [], [], [telegram("00.000", "00.120")]), clock=clock)

    assert client.get_power() == 400.0
    clock.now += DSMR_MAX_AGE_S
    assert client.get_power() == 400.0
    assert client.health.consecutive_failures == 0
    clock.now += 1
    assert client.get_power() == 400.0
    assert client.health.consecutive_failures == 1
    assert "no telegram" in client.health.last_error
    assert client.get_power() == -120.0
    assert client.health.consecutive_failures == 0


def test_a_bridge_that_falls_silent_is_connected_to_again():
    clock = Clock()
    reader = Reader([telegram("00.400")], *([[]] * 4))
    client = DsmrTcpClient(reader, clock=clock)

    client.get_power()
    for _ in range(3):
        clock.now += DSMR_MAX_AGE_S / 2
        client.get_power()
    assert reader.closed
    assert client.health.consecutive_failures == 1

    reader.closed = False
    clock.now += 1
    client.get_power()
    assert not reader.closed
    assert "no telegram from the P1 bridge for" in client.health.last_error


def test_a_bridge_back_from_a_restart_gets_a_full_wait_for_its_first_telegram():
    clock = Clock()
    reader = Reader([telegram("00.400")], [], ConnectionRefusedError("refused"), [], [], [telegram("00.500")])
    client = DsmrTcpClient(reader, clock=clock)

    client.get_power()
    clock.now += DSMR_MAX_AGE_S + 1
    client.get_power()
    assert reader.closed
    reader.closed = False
    clock.now += 40
    client.get_power()
    assert "refused" in client.health.last_error

    clock.now += 5
    client.get_power()
    clock.now += DSMR_MAX_AGE_S - 2
    client.get_power()
    assert not reader.closed
    assert client.get_power() == 500.0


def test_a_bridge_that_never_sends_is_connected_to_again():
    clock = Clock()
    reader = Reader(*([[]] * 3))
    client = DsmrTcpClient(reader, clock=clock)

    client.get_power()
    clock.now += DSMR_MAX_AGE_S
    client.get_power()
    assert not reader.closed
    clock.now += 1
    client.get_power()
    assert reader.closed
    assert "yet" in client.health.last_error


def test_the_newest_good_telegram_wins_and_a_bridge_without_any_fails():
    bad = telegram("00.900").replace(b"00.900", b"00.901")
    client = DsmrTcpClient(Reader([], [telegram("00.100"), bad], [bad], ConnectionRefusedError("refused")), clock=Clock())

    client.get_power()
    assert "yet" in client.health.last_error
    assert client.get_power() == 100.0
    assert client.get_power() == 100.0
    assert "checksum" in client.health.last_error
    client.get_power()
    assert "refused" in client.health.last_error


def test_the_factory_builds_a_reader_for_the_configured_bridge():
    client = create_grid_meter_client(METER, None)

    assert isinstance(client, DsmrTcpClient)
    assert (client.reader.host, client.reader.port, client.endpoint) == ("192.0.2.80", 23, "192.0.2.80:23")
    client.close()


@pytest.mark.parametrize("change, message", [({"ip": ""}, "grid_meter.ip"), ({"port": None}, "grid_meter.port"), ({"port": 70000}, "grid_meter.port")])
def test_settings_need_the_bridge_address_and_its_port(change, message):
    with pytest.raises(ValueError, match=message):
        cfg.dsmr_p1_tcp_grid_meter_settings({**METER, **change})


def test_the_meter_is_named_alike_everywhere():
    assert cfg.grid_meter_model_transport("dsmr_p1_tcp") == ("DSMR P1 meter via a TCP bridge", "TCP")
    assert diagnostics.GRID_METER_PROVIDERS["dsmr_p1_tcp"] == DsmrTcpClient.provider


# --- the CLI setup assistant ---------------------------------------------------------


def test_config_init_asks_address_and_port(monkeypatch):
    from ems import config_init

    menu = str([choice for choice, _ in config_init.GRID_METER_CHOICES].index("dsmr_p1_tcp") + 1)
    answers = iter([menu, "192.0.2.80", "23"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    result = config_init.ask_grid_meter({"type": "powerfox_poweropti", "ip": "192.0.2.9", "token": "abc"})

    assert result == METER
    assert config_init._meter_summary(result) == "DSMR P1 bridge at 192.0.2.80:23"
    with pytest.raises(config_init.ConfigInitError, match="port"):
        config_init.ask_grid_meter({**METER, "port": None}, noninteractive=True)


# --- diagnostics ---------------------------------------------------------------------


def test_diagnose_waits_long_enough_for_a_meter_that_sends_every_ten_seconds(monkeypatch):
    built = []

    class ProbeReader(Reader):
        def __init__(self, host, port):
            super().__init__([telegram("00.000", "00.678")])
            self.host, self.port, self.timeout = host, port, 3.0
            built.append(self)

    monkeypatch.setattr(dsmr, "DsmrTcpReader", ProbeReader)
    checks = []

    health = diagnostics.diagnose_hardware(checks, {"grid_meter": METER, "devices": []})

    codes = {check["code"]: check for check in checks}
    assert codes["grid_meter_read_ok"]["details"]["power_w"] == -678.0
    assert built[0].timeout > 10 and built[0].closed
    assert health["grid_meter"]["provider"] == "DSMR P1"
    assert "grid_meter_settings_complete" in json.dumps(_config_codes(METER))


def _config_codes(grid_meter):
    checks = []
    diagnostics.diagnose_grid_meter_config(checks, {"grid_meter": grid_meter})
    return {check["code"]: check["level"] for check in checks}
