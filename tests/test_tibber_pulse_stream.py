# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Tibber Pulse bridge's stream: telegrams as they come, polling as the fallback.

A tester (issue #341) showed that firmware 1794 and later stream the raw SML
frames to a plain ``GET /ws`` with the login, about every three seconds, with
gaps of up to half a minute. The reader takes that stream, cuts the frames out
of it, keeps only the meter its configured reading head sees, and asks for
telegrams one by one only while the stream does not work -- never both at once.
"""

import base64
import logging

import pytest

from ems import sml
from ems.tibber_pulse import (
    POLL_FALLBACK_CAP_SECONDS,
    POLL_FALLBACK_SECONDS,
    POLL_INTERVAL_SECONDS,
    STREAM_PATIENCE_SECONDS,
    BridgeError,
    StreamHead,
    TibberPulseBridge,
    TibberPulseGridMeterClient,
)
from tests.test_sml import _entry, _frame, _get_list, _int

pytestmark = [pytest.mark.unit, pytest.mark.power_control]

METER = b"\x0a\x01EMH\x00\x00\x00\x00\x01"
OTHER_METER = b"\x0a\x01EMH\x00\x00\x00\x00\x02"


SILENT = [5.0] * 8


def _telegram(watts, meter=METER):
    return _frame(_get_list(_entry(sml.POWER_TOTAL, _int(watts, 4)), server_id=meter))


def test_frames_are_cut_out_of_a_stream_across_chunks_and_other_bytes():
    first, second = _telegram(100), _telegram(200)
    stream = b'\x82\x25<device:"x" topic:"sml">' + first + b"\x81\x05hello" + second
    splitter = sml.FrameSplitter()

    frames = []
    for index in range(0, len(stream), 7):
        frames += splitter.feed(stream[index:index + 7])

    assert frames == [first, second]
    assert [sml.grid_power_w(sml.parse(frame)) for frame in frames] == [100, 200]


def test_an_escaped_escape_inside_a_frame_does_not_end_it():
    messages = _get_list(_entry(sml.POWER_TOTAL, _int(500, 4)))
    body = messages + bytes(-len(messages) % 4) + sml.ESCAPE + bytes(4)
    frame = _frame(body)
    assert sml.ESCAPE + sml.ESCAPE in frame

    assert sml.FrameSplitter().feed(frame + _telegram(510)) == [frame, _telegram(510)]
    assert sml.unwrap_frame(frame) == body


def test_a_frame_broken_off_by_the_next_start_gives_way_to_it():
    complete = _telegram(300)
    frames = sml.FrameSplitter().feed(sml.START + bytes(64) + complete)

    assert frames[-1] == complete
    with pytest.raises(sml.SmlError):
        sml.parse(frames[0])


def test_an_escaped_escape_before_a_block_of_ones_is_no_new_start_across_chunks():
    messages = _get_list(_entry(sml.POWER_TOTAL, _int(500, 4)))
    body = messages + bytes(-len(messages) % 4) + sml.ESCAPE + b"\x01\x01\x01\x01" + bytes(4)
    frame = _frame(body)
    assert sml.ESCAPE + sml.START in frame
    splitter = sml.FrameSplitter()
    cut = frame.index(sml.ESCAPE + sml.START) + 12

    assert splitter.feed(frame[:cut]) == []
    assert splitter.feed(frame[cut:]) == [frame]


def test_a_frame_cut_off_before_a_start_off_its_grid_gives_way_to_it():
    complete = _telegram(300)

    assert sml.FrameSplitter().feed(sml.START + bytes(2) + complete + _telegram(310)) == [complete, _telegram(310)]


def test_a_frame_without_its_end_is_given_up_once_it_outgrows_the_limit():
    splitter = sml.FrameSplitter(limit=128)

    assert splitter.feed(sml.START + bytes(200)) == []
    assert len(splitter._buffer) < len(sml.START)
    assert splitter.feed(_telegram(300)) == [_telegram(300)]


def test_bytes_without_a_frame_never_pile_up():
    splitter = sml.FrameSplitter()

    for _ in range(100):
        assert splitter.feed(b"junk" * 1000) == []

    assert len(splitter._buffer) < len(sml.START)


def test_a_telegram_names_its_meter_by_server_id_or_its_serial_entry():
    assert sml.parse(_telegram(100)).meter_id == METER
    serial_only = sml.parse_messages(_get_list(_entry(sml.SERVER_ID, b"\x07" + b"SERIAL", unit=255), _entry(sml.POWER_TOTAL, _int(1, 4)), server_id=b""))
    assert serial_only.server_id is None
    assert serial_only.meter_id == b"SERIAL"


class _Connection:
    """A scripted socket: bytes are received, a number advances the clock and times out."""

    def __init__(self, clock, *script):
        self.clock = clock
        self.script = list(script)
        self.sent = b""
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        if not self.script:
            return b""
        item = self.script.pop(0)
        if isinstance(item, (int, float)):
            self.clock.now += item
            raise TimeoutError
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Bridge:
    base_url = "http://192.0.2.7"
    node_id = 1

    def __init__(self, telegrams=(), streams=()):
        self.telegrams = list(telegrams)
        self.streams = list(streams)
        self.polls = 0
        self.opened = 0

    def read_telegram(self):
        self.polls += 1
        item = self.telegrams.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def open_stream(self):
        self.opened += 1
        item = self.streams.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def refused(self):
        return BridgeError("unauthorized", "the bridge refused the password")

    def close(self):
        pass


def _streaming_client(clock, *streams, telegrams=None):
    """A client past its first polled reading, so its next step opens the stream."""

    bridge = _Bridge(telegrams if telegrams is not None else [_telegram(1), _telegram(2)], streams)
    client = TibberPulseGridMeterClient(bridge, clock=clock, start=False)
    assert client.read_once() is False
    assert client.read_once() is True
    assert bridge.polls == 2 and bridge.opened == 0
    return client, bridge


def test_after_one_telegram_names_the_meter_the_reader_takes_the_stream():
    clock = _Clock()
    connection = _Connection(clock, b"HTTP/1.1 200 OK\r\nContent-Type: x\r\n\r\n" + _telegram(250), 1.0, _telegram(260))
    client, bridge = _streaming_client(clock, connection)

    assert client.read_once() is True

    assert bridge.opened == 1 and bridge.polls == 2
    assert client.get_power() == 260
    assert connection.closed


def test_another_heads_telegrams_and_damaged_ones_are_skipped_without_ending_the_stream(caplog):
    clock = _Clock()
    damaged = bytearray(_telegram(999))
    damaged[40] ^= 0x01
    connection = _Connection(clock, _telegram(-400, OTHER_METER), bytes(damaged), _telegram(310), _telegram(320))
    client, _bridge = _streaming_client(clock, connection)

    with caplog.at_level(logging.DEBUG):
        assert client.read_once() is True

    assert client.get_power() == 320
    assert "tibber_pulse_stream_telegram_skipped" in caplog.text


def test_a_stream_that_falls_silent_ends_and_says_why():
    clock = _Clock()
    connection = _Connection(clock, _telegram(100), _telegram(110), *SILENT)
    client, _bridge = _streaming_client(clock, connection)

    assert client.read_once() is True

    assert client._last_error.reason == "stream_silent"
    assert STREAM_PATIENCE_SECONDS < clock.now - 1000.0 <= STREAM_PATIENCE_SECONDS + 5.0


def test_a_stream_that_delivers_nothing_ends_after_its_first_wait():
    clock = _Clock()
    connection = _Connection(clock, *SILENT)
    client, _bridge = _streaming_client(clock, connection)

    assert client.read_once() is False

    assert client._last_error.reason == "stream_silent"
    assert STREAM_PATIENCE_SECONDS < clock.now - 1000.0 <= STREAM_PATIENCE_SECONDS + 5.0


def test_a_stream_that_delivers_nothing_hands_over_to_polling_for_longer_each_time(caplog):
    clock = _Clock()
    client, bridge = _streaming_client(
        clock, *[_Connection(clock, *SILENT) for _ in range(6)],
        telegrams=[_telegram(n) for n in range(1, 20)],
    )

    periods = []
    for _ in range(6):
        with caplog.at_level(logging.WARNING):
            assert client.read_once() is False
        periods.append(client._poll_until - clock.now)
        assert client.read_once() is True
        clock.now = client._poll_until

    assert periods == [300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0]
    assert POLL_FALLBACK_SECONDS == 300.0 and POLL_FALLBACK_CAP_SECONDS == 3600.0
    assert "tibber_pulse_stream_fallback" in caplog.text and "poll_seconds=300" in caplog.text
    assert bridge.opened == 6


def test_a_stream_that_ends_after_a_reading_is_opened_again_and_an_empty_one_hands_over():
    clock = _Clock()
    client, bridge = _streaming_client(
        clock,
        _Connection(clock, _telegram(5), _telegram(6)),
        _Connection(clock, _telegram(7), ConnectionResetError("reset")),
        _Connection(clock, *SILENT),
        _Connection(clock, _telegram(8)),
        OSError("refused"),
    )
    bridge.telegrams += [_telegram(20)]

    assert client.read_once() is True
    assert client._pause() == POLL_INTERVAL_SECONDS
    assert client.read_once() is True
    assert client._poll_until is None
    assert client.read_once() is False
    assert client._poll_until == pytest.approx(clock.now + POLL_FALLBACK_SECONDS)

    assert client.read_once() is True
    clock.now = client._poll_until
    assert client.read_once() is True
    assert client.read_once() is False
    assert client._poll_until == pytest.approx(clock.now + POLL_FALLBACK_SECONDS)
    assert bridge.opened == 5


def test_a_gap_shorter_than_the_patience_keeps_the_stream():
    clock = _Clock()
    client, bridge = _streaming_client(clock, _Connection(clock, _telegram(5), *[5.0] * 2, _telegram(6), *SILENT))

    assert client.read_once() is True

    assert client.get_power() == 6
    assert bridge.opened == 1


def test_after_a_stream_of_other_meters_only_one_poll_names_the_meter_afresh():
    clock = _Clock()
    client, bridge = _streaming_client(
        clock,
        _Connection(clock, _telegram(5), _telegram(6)),
        _Connection(clock, _telegram(90, OTHER_METER), _telegram(91, OTHER_METER), *SILENT),
        _Connection(clock, _telegram(93, OTHER_METER), _telegram(94, OTHER_METER)),
    )
    bridge.telegrams.append(_telegram(92, OTHER_METER))

    assert client.read_once() is True
    assert client.read_once() is False
    assert client._poll_until is None

    client.read_once()
    assert bridge.polls == 3 and client._meter == OTHER_METER
    assert client._poll_until is None
    assert client.read_once() is True
    assert client.get_power() == 94


def test_a_stream_of_other_meters_whose_head_did_not_move_hands_over_after_one_poll(caplog):
    clock = _Clock()
    client, bridge = _streaming_client(clock, _Connection(clock, _telegram(90, OTHER_METER), *SILENT))
    bridge.telegrams.append(_telegram(3))

    assert client.read_once() is False
    assert client._poll_until is None
    with caplog.at_level(logging.WARNING):
        client.read_once()

    assert client._meter == METER
    assert client._poll_until == pytest.approx(clock.now + POLL_FALLBACK_SECONDS)
    assert "reason=no_new_reading" in caplog.text


def test_repeats_of_the_last_telegram_are_no_new_reading():
    clock = _Clock()
    last = _telegram(2)
    client, _bridge = _streaming_client(clock, _Connection(clock, last, *[5.0] * 3, last, *[5.0] * 4, last, *SILENT))

    assert client.read_once() is False

    assert client._poll_until == pytest.approx(clock.now + POLL_FALLBACK_SECONDS)


def test_a_refused_login_on_the_stream_hands_over_without_doubling():
    clock = _Clock()
    refusals = [_Connection(clock, b"HTTP/1.1 401 Unauthorized\r\n\r\n") for _ in range(3)]
    client, _bridge = _streaming_client(clock, *refusals)

    for _ in range(3):
        assert client.read_once() is False
        assert client._poll_until == pytest.approx(clock.now + POLL_FALLBACK_SECONDS)
        clock.now = client._poll_until

    assert client._stream_failures == 0
    assert client._pause() == 30.0


def test_a_meter_that_names_itself_in_no_way_is_only_asked_for():
    clock = _Clock()

    def anonymous(watts):
        return _frame(_get_list(_entry(sml.POWER_TOTAL, _int(watts, 4)), server_id=b""))

    bridge = _Bridge([anonymous(70), anonymous(71), anonymous(72)], [_Connection(clock, anonymous(73))])
    client = TibberPulseGridMeterClient(bridge, clock=clock, start=False)

    for _ in range(3):
        client.read_once()

    assert (bridge.polls, bridge.opened) == (3, 0)


def test_a_poll_that_names_no_meter_keeps_the_one_known():
    clock = _Clock()
    anonymous = _frame(_get_list(_entry(sml.POWER_TOTAL, _int(70, 4)), server_id=b""))
    client, bridge = _streaming_client(clock, _Connection(clock, *SILENT), _Connection(clock, _telegram(8), _telegram(9)))
    bridge.telegrams.append(anonymous)

    client.read_once()
    client.read_once()
    clock.now = client._poll_until

    assert client._meter == METER
    assert client.read_once() is True


def test_a_head_moved_to_another_meter_is_followed_by_the_next_poll():
    clock = _Clock()
    client, bridge = _streaming_client(clock, _Connection(clock, *SILENT), _Connection(clock, _telegram(80, OTHER_METER), _telegram(81, OTHER_METER)))
    bridge.telegrams.append(_telegram(79, OTHER_METER))
    assert client._meter == METER

    assert client.read_once() is False
    client.read_once()
    clock.now = client._poll_until

    assert client.read_once() is True
    assert client.get_power() == 81


def test_stopping_during_a_stream_counts_no_failure(caplog):
    clock = _Clock()
    client, _bridge = _streaming_client(clock, _Connection(clock, 1.0, 1.0))
    client._stop.set()

    with caplog.at_level(logging.WARNING):
        assert client.read_once() is False

    assert client._poll_until is None
    assert "tibber_pulse_stream_fallback" not in caplog.text


def test_the_reader_never_streams_and_polls_at_once():
    clock = _Clock()
    client, bridge = _streaming_client(clock, _Connection(clock, *SILENT), telegrams=[_telegram(1), _telegram(2), _telegram(3)])

    client.read_once()
    assert (bridge.polls, bridge.opened) == (2, 1)

    client.read_once()
    assert (bridge.polls, bridge.opened) == (3, 1)


def test_the_start_waits_only_on_polling():
    clock = _Clock()
    bridge = _Bridge([_telegram(1), _telegram(2)], [_Connection(clock, *SILENT)])
    client = TibberPulseGridMeterClient(bridge, clock=clock, start=False)

    client.read_once()
    assert not client.wait_until_ready(0)
    client.read_once()

    assert client.wait_until_ready(0)
    assert bridge.opened == 0


def test_a_damaged_streamed_frame_never_counts_towards_releasing_the_crc_variant():
    clock = _Clock()
    damaged = bytearray(_telegram(999))
    damaged[40] ^= 0x01
    client, _bridge = _streaming_client(clock, _Connection(clock, *[bytes(damaged)] * 12, _telegram(5), _telegram(6)))
    client._crc_variants = (("x25", "big"),)

    assert client.read_once() is True

    assert client._crc_misses == 0
    assert client._crc_variants == (("x25", "big"),)


def test_a_refused_login_on_the_stream_waits_as_a_refused_poll_does():
    clock = _Clock()
    connection = _Connection(clock, b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic\r\n\r\nNo permission")
    client, _bridge = _streaming_client(clock, connection)

    assert client.read_once() is False

    assert client._last_error.reason == "unauthorized"
    assert client._pause() == 30.0


def _bridge(password="ABCD-1234", **kwargs):
    connects = []

    def connect(address, timeout):
        connects.append((address, timeout))
        return _Connection(_Clock())

    return TibberPulseBridge("192.0.2.7", password, connect=connect, **kwargs), connects


def test_the_stream_is_asked_for_with_a_plain_get_and_the_login():
    bridge, connects = _bridge(port=8080)

    connection = bridge.open_stream()

    token = base64.b64encode(b"admin:ABCD-1234").decode()
    assert connection.sent == (
        f"GET /ws HTTP/1.1\r\nHost: 192.0.2.7:8080\r\nAuthorization: Basic {token}\r\n"
        "Connection: keep-alive\r\n\r\n"
    ).encode()
    assert connects == [(("192.0.2.7", 8080), 5.0)]
    assert connection.timeout == 0.5


def test_a_password_no_login_can_carry_opens_no_connection():
    bridge, connects = _bridge(password="Pässwört€")

    with pytest.raises(BridgeError, match="bad_password"):
        bridge.open_stream()

    assert connects == []


def test_the_answer_to_the_stream_request_is_read_past_or_refused():
    passes = StreamHead(_Bridge())
    assert passes.payload(b"HTTP/1.1 2") == b""
    assert passes.payload(b"00 OK\r\nX: y\r\n\r\n\x1b\x1b") == b"\x1b\x1b"
    assert passes.payload(b"more") == b"more"

    raw = StreamHead(_Bridge())
    assert raw.payload(b"\x1b\x1b\x1b\x1b") == b"\x1b\x1b\x1b\x1b"

    with pytest.raises(BridgeError, match="http_error"):
        StreamHead(_Bridge()).payload(b"HTTP/1.1 500 Internal\r\n\r\n")
    with pytest.raises(BridgeError, match="http_error"):
        StreamHead(_Bridge()).payload(b"HTTP/1.1 302 Found\r\nLocation: /\r\n\r\n")
    with pytest.raises(BridgeError, match="http_error"):
        StreamHead(_Bridge()).payload(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
    for header in (b"Transfer-Encoding: chunked", b"transfer-encoding:chunked", b"Transfer-Encoding: gzip, chunked"):
        with pytest.raises(BridgeError, match="unsupported_stream"):
            StreamHead(_Bridge()).payload(b"HTTP/1.1 200 OK\r\n" + header + b"\r\n\r\n1a\r\n")

    interim = StreamHead(_Bridge())
    assert interim.payload(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\n\r\nSML") == b"SML"
    raw_after_interim = StreamHead(_Bridge())
    assert raw_after_interim.payload(b"HTTP/1.1 100 Continue\r\n\r\n\x1b\x1b\x1b\x1b") == b"\x1b\x1b\x1b\x1b"
    with pytest.raises(BridgeError, match="unauthorized"):
        StreamHead(_Bridge()).payload(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 401 Unauthorized\r\n\r\n")

    asked = []
    bridge = TibberPulseBridge("192.0.2.7", password_provider=lambda: asked.append(1) or "ABCD-1234")
    bridge._current_password()
    with pytest.raises(BridgeError, match="unauthorized"):
        StreamHead(bridge).payload(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
    bridge._current_password()
    assert len(asked) == 2
