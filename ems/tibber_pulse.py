# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grid power from a Tibber Pulse IR bridge, read over its local web server.

The bridge answers slowly and now and then serves a damaged telegram, so the
control loop never waits on it: one background worker reads the bridge and
keeps the last good reading, and ``get_power`` hands out that reading while it
is young enough. The worker takes the telegrams the bridge streams on ``/ws``
and asks for them one by one only while the stream does not work -- never
both at once, since a second reader makes the bridge hang.
Import-side-effect-free.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import threading
import time

import requests

from ems import sml
from ems.grid_reading import GridReading
from ems.health import CommHealth
from ems.logging_utils import log_event

BRIDGE_USER = "admin"
DATA_PATHS = ("/node_data.json", "/data.json")
POLL_INTERVAL_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_AGE_SECONDS = 10.0
MAX_PLAUSIBLE_POWER_W = 50_000
CRC_PIN_AFTER = 3
CRC_RELEASE_AFTER = 10
CLOSE_WAIT_SECONDS = 1.0
WARNING_REPEAT_SECONDS = 60.0
RETRY_CAP_SECONDS = 30.0
BACKOFF_DOUBLINGS = 5
LOGIN_FAILURES = ("unauthorized", "no_password", "bad_password")
STREAM_PATH = "/ws"
STREAM_CONNECT_TIMEOUT_SECONDS = 5.0
STREAM_RECEIVE_TIMEOUT_SECONDS = 0.5
STREAM_CHUNK_BYTES = 4096
STREAM_PATIENCE_SECONDS = 15.0
POLL_FALLBACK_SECONDS = 300.0
POLL_FALLBACK_CAP_SECONDS = 3600.0
MAX_STREAM_HEAD_BYTES = 8192


class BridgeError(Exception):
    """A bridge request that did not yield a telegram; ``reason`` is stable."""

    def __init__(self, reason, detail="", status=None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.status = status


ANY_METER = object()


def grid_power_from_telegram(telegram, crc_variants=sml.CRC_VARIANTS, *, meter=ANY_METER):
    """Signed grid power and the decoded telegram of one bridge answer.

    The one decode path of the runtime reader and the diagnose probe; a value
    beyond ``MAX_PLAUSIBLE_POWER_W`` is refused like a damaged telegram. With
    ``meter`` another meter's telegram is refused as ``foreign_meter`` before
    its power is read.
    """

    decoded = sml.parse(telegram, crc_variants=crc_variants)
    if meter is not ANY_METER and decoded.meter_id != meter:
        raise sml.SmlError("foreign_meter")
    power = sml.grid_power_w(decoded)
    if abs(power) > MAX_PLAUSIBLE_POWER_W:
        raise sml.SmlError("implausible_power", f"beyond {MAX_PLAUSIBLE_POWER_W} W")
    return power, decoded


def login_can_carry(password):
    """Whether HTTP Basic, as the bridge reads it, can send ``password``."""

    try:
        password.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


class TibberPulseBridge:
    """HTTP access to one reading head (node) of a Tibber Pulse bridge.

    Firmware 1794 (September 2026) moved ``/data.json`` to ``/node_data.json``
    and answers the old path with 404; the bridge updates itself over the air,
    so the path that worked last is tried first and the other on a 404.

    ``password_provider`` reads the password from the secret store. It is asked
    again while there is none and after the bridge refused one, so a password
    saved or corrected in the Admin works without restarting the EMS.
    """

    def __init__(self, host, password=None, *, port=80, node_id=1, session=None,
                 timeout=REQUEST_TIMEOUT_SECONDS, password_provider=None,
                 connect=socket.create_connection):
        self.host = str(host).strip()
        self.port = int(port)
        self.node_id = int(node_id)
        self.timeout = timeout
        self._password = password
        self._password_provider = password_provider
        self._password_error = None
        self._session = session or requests.Session()
        self._paths = list(DATA_PATHS)
        self._connect = connect

    @property
    def base_url(self):
        return f"http://{self.host}" if self.port == 80 else f"http://{self.host}:{self.port}"

    def _current_password(self):
        if self._password is None and self._password_provider is not None:
            try:
                self._password = self._password_provider()
                self._password_error = None
            except ValueError as exc:
                self._password_error = str(exc)
        if not self._password:
            raise BridgeError("no_password", self._password_error or "no bridge password is saved")
        return self._password

    def _login_password(self):
        password = self._current_password()
        if not login_can_carry(password):
            self._forget_password()
            raise BridgeError("bad_password", "the password has characters an HTTP login cannot carry")
        return password

    def _forget_password(self):
        if self._password_provider is not None:
            self._password = None

    def refused(self):
        """The bridge refused the password: it is read again next time."""

        self._forget_password()
        return BridgeError("unauthorized", "the bridge refused the password")

    def get(self, path, params=None):
        """One authenticated GET; a refused password is read again next time."""

        password = self._login_password()
        response = self._session.get(
            f"{self.base_url}{path}",
            params=params,
            auth=(BRIDGE_USER, password),
            timeout=self.timeout,
        )
        if response.status_code in (401, 403):
            raise self.refused()
        return response

    def open_stream(self):
        """A connection on which the bridge streams its telegrams as they come.

        Firmware 1794 and later accept the WebSocket handshake on ``/ws`` and
        then stream nothing; a plain GET with the login, without the upgrade,
        makes them stream the raw frames (issue #341, as ha-tibber-pulse-local
        reads them). The answer is read by ``StreamHead``.
        """

        password = self._login_password()
        token = base64.b64encode(f"{BRIDGE_USER}:{password}".encode("latin-1")).decode("ascii")
        host = self.base_url.split("://", 1)[1]
        request = (
            f"GET {STREAM_PATH} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Authorization: Basic {token}\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode("latin-1")
        connection = self._connect((self.host, self.port), timeout=STREAM_CONNECT_TIMEOUT_SECONDS)
        try:
            connection.settimeout(STREAM_RECEIVE_TIMEOUT_SECONDS)
            connection.sendall(request)
        except BaseException:
            connection.close()
            raise
        return connection

    def read_json(self, path):
        """A JSON endpoint; ``/test.json`` wraps its object in other text."""

        response = self.get(path)
        if response.status_code != 200:
            raise BridgeError("http_error", f"HTTP {response.status_code} from {path}", response.status_code)
        try:
            return response.json()
        except ValueError:
            text = response.text
            start, end = text.find("{"), text.rfind("}")
            if not 0 <= start < end:
                raise BridgeError("bad_json", path) from None
            try:
                return json.loads(text[start:end + 1])
            except ValueError as exc:
                raise BridgeError("bad_json", path) from exc

    def read_telegram(self) -> bytes:
        for path in list(self._paths):
            response = self.get(path, {"node_id": self.node_id})
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                raise BridgeError("http_error", f"HTTP {response.status_code} from {path}", response.status_code)
            if self._paths[0] != path:
                self._paths.remove(path)
                self._paths.insert(0, path)
            return response.content
        raise BridgeError("no_data_path", "the bridge serves neither data path")

    def close(self):
        self._session.close()


def _chunked(header_line):
    name, _colon, value = header_line.partition(b":")
    return name.strip().lower() == b"transfer-encoding" and b"chunked" in value.lower()


class StreamHead:
    """Reads past the HTTP answer that may open the stream.

    A bridge that answers with a status line has its refusal turned into a
    ``BridgeError``, and so has a protocol switch, a redirect or a chunked
    body, whose framing would land inside the SML frames; an interim ``1xx``
    answer is read past to the one behind it. One that streams the frames
    straight away is passed through as it is.
    """

    def __init__(self, bridge):
        self._bridge = bridge
        self._head = bytearray()
        self._done = False

    def payload(self, chunk):
        if self._done:
            return chunk
        self._head += chunk
        while True:
            if not self._head.startswith(b"HTTP/"[:len(self._head)]):
                self._done = True
                rest = bytes(self._head)
                self._head = bytearray()
                return rest
            end = self._head.find(b"\r\n\r\n")
            if end < 0:
                if len(self._head) > MAX_STREAM_HEAD_BYTES:
                    raise BridgeError("bad_stream", "the stream answer has no end")
                return b""
            lines = bytes(self._head[:end]).split(b"\r\n")
            del self._head[:end + 4]
            parts = lines[0].split()
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            if 100 <= status < 200 and status != 101:
                continue
            if status in (401, 403):
                raise self._bridge.refused()
            if not 200 <= status < 300:
                raise BridgeError("http_error", f"HTTP {status} from {STREAM_PATH}", status)
            if any(_chunked(line) for line in lines[1:]):
                raise BridgeError("unsupported_stream", "the bridge streams in chunks")
            self._done = True
            rest = bytes(self._head)
            self._head = bytearray()
            return rest


class TibberPulseGridMeterClient:
    """Cached grid meter reading fed by a background reader.

    A reading counts as new only when the meter's frame changed: its
    transaction number, the meter's seconds counter or a value moved; bytes the
    bridge serves around the frame do not count. A bridge whose head stopped
    (empty battery, turned head) keeps serving its last frame, and that must
    age out instead of looking fresh. The first frame is only the baseline, so
    a head that stopped before the start never yields a reading at all.

    The reader asks for one telegram first, which names the meter the
    configured reading head sees, and then reads the stream, taking only that
    meter's telegrams: the stream carries every head of the bridge, so a meter
    that names itself in neither way is only asked for. The stream is given
    ``STREAM_PATIENCE_SECONDS`` per new reading: the reading is stale after
    ``max_age_seconds`` whatever the stream does, a connection that has stuck
    is found a little later, and opening one anew still costs the bridge less
    than a poll every few seconds. A stream that ends after a new reading is
    opened again. One that brings none hands over to asking, for five minutes, twice
    as long after each further such hand-over, up to an hour; a refused login
    hands over for five minutes, and the polls tell whether the password is
    wrong. A stream that carried only other meters' telegrams first gets one
    poll, which may name a meter the head was moved to.
    """

    provider = "Tibber Pulse"
    transport = "http"

    def __init__(self, bridge, *, poll_interval=POLL_INTERVAL_SECONDS,
                 max_age_seconds=MAX_AGE_SECONDS, clock=time.monotonic, start=True):
        self.bridge = bridge
        self.poll_interval = float(poll_interval)
        self.max_age_seconds = float(max_age_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reading = None
        self._received_at = None
        self._sequence = 0
        self._last_marker = None
        self._last_error = None
        self._round_trip_ms = None
        self._crc_counts = {}
        self._crc_variants = sml.CRC_VARIANTS
        self._crc_misses = 0
        self._last_miss = None
        self._unanswered = 0
        self._ready = threading.Event()
        self._last_warning = (None, None)
        self._latency_reported_for = None
        self._meter = None
        self._poll_until = None
        self._stream_failures = 0
        self._foreign_meter_seen = False
        self._relearn_from = None
        self.last_value = 0
        self.health = CommHealth(self.provider, kind="read")
        self._thread = None
        if start:
            self._thread = threading.Thread(
                target=self._run, name="tibber-pulse-reader", daemon=True
            )
            self._thread.start()

    @property
    def endpoint(self):
        return f"{self.bridge.base_url} node {self.bridge.node_id}"

    def poll_once(self):
        """Fetch one telegram and cache it when it is a new, plausible reading."""

        start = self._clock()
        telegram = None
        try:
            telegram = self.bridge.read_telegram()
            power, decoded = grid_power_from_telegram(telegram, self._crc_variants)
        except (requests.RequestException, BridgeError, sml.SmlError) as exc:
            self._record_failure(exc, telegram, "tibber_pulse_poll_failed")
            return False
        if decoded.meter_id is not None:
            self._meter = decoded.meter_id
        return self._accept(power, decoded, start)

    def stream_once(self):
        """Read the bridge's stream until it ends; answers whether it brought a new reading.

        It ends when the bridge closes it, refuses the login, or brings no new
        reading of this meter for ``STREAM_PATIENCE_SECONDS`` after opening or
        after the last one. A damaged telegram, another head's or a repeated
        one is skipped and does not end it.
        """

        self._foreign_meter_seen = False
        try:
            connection = self.bridge.open_stream()
        except (OSError, BridgeError, ValueError) as exc:
            self._record_failure(exc, None, "tibber_pulse_stream_failed")
            return False
        head = StreamHead(self.bridge)
        splitter = sml.FrameSplitter()
        opened = self._clock()
        heard = None
        try:
            while not self._stop.is_set():
                try:
                    chunk = connection.recv(STREAM_CHUNK_BYTES)
                except TimeoutError:
                    chunk = None
                if chunk == b"":
                    raise BridgeError("stream_closed", "the bridge closed the stream")
                for frame in splitter.feed(head.payload(chunk)) if chunk else ():
                    if self._stream_telegram(frame):
                        heard = self._clock()
                if self._clock() - (opened if heard is None else heard) > STREAM_PATIENCE_SECONDS:
                    raise BridgeError("stream_silent", f"no new telegram for {STREAM_PATIENCE_SECONDS:.0f}s")
        except (OSError, BridgeError, ValueError) as exc:
            self._record_failure(exc, None, "tibber_pulse_stream_failed")
        finally:
            connection.close()
        return heard is not None

    def _stream_telegram(self, frame):
        """Take one streamed frame; answers whether it was a new reading of this meter.

        A damaged frame cannot be told apart from another head's, so it never
        counts towards releasing the pinned CRC variant; polling does that.
        """

        try:
            power, decoded = grid_power_from_telegram(frame, self._crc_variants, meter=self._meter)
        except sml.SmlError as exc:
            if exc.reason == "foreign_meter":
                self._foreign_meter_seen = True
            else:
                log_event(logging.DEBUG, "tibber_pulse_stream_telegram_skipped", endpoint=self.endpoint, reason=exc.reason)
            return False
        return self._accept(power, decoded, None)

    def _record_failure(self, exc, telegram, event):
        if getattr(exc, "reason", None) == "crc_mismatch":
            self._count_crc_miss(telegram)
        with self._lock:
            self._last_error = exc
            unanswered = isinstance(exc, OSError) or (getattr(exc, "status", None) or 0) >= 500
            self._unanswered = min(self._unanswered + 1, BACKOFF_DOUBLINGS) if unanswered else 0
        log_event(
            logging.DEBUG,
            event,
            endpoint=self.endpoint,
            reason=getattr(exc, "reason", type(exc).__name__),
        )

    def _accept(self, power, decoded, started_at):
        """Cache a decoded telegram when it is new; ``started_at`` dates a request."""

        self._crc_misses = 0
        self._last_miss = None
        with self._lock:
            self._last_error = None
            self._unanswered = 0
            if decoded.body == self._last_marker:
                return False
            baseline = self._last_marker is None
            self._last_marker = decoded.body
            if not baseline:
                self._reading = round(power, 1)
                self._received_at = self._clock()
                self._sequence += 1
                self._round_trip_ms = None if started_at is None else (self._received_at - started_at) * 1000.0
        self._learn_crc_variant(decoded.crc_variant)
        if baseline:
            return False
        self._ready.set()
        return True

    def _learn_crc_variant(self, variant):
        """Accept only the meter's own CRC variant once it has shown it.

        Four accepted variants let a damaged telegram through four times as
        often; three different intact telegrams agreeing on one variant pin
        it. A telegram the bridge repeats is one observation, not several.
        """
        if variant is None or len(self._crc_variants) == 1:
            return
        self._crc_counts[variant] = self._crc_counts.get(variant, 0) + 1
        if self._crc_counts[variant] >= CRC_PIN_AFTER:
            self._crc_variants = (variant,)

    def _count_crc_miss(self, telegram):
        """Release a pinned variant after a run of telegrams that all miss it.

        A head moved to another meter, or a variant pinned on bad luck, would
        otherwise fail every read until the EMS restarts. A damaged telegram the
        bridge repeats is one miss, not several.
        """
        if len(self._crc_variants) != 1 or telegram == self._last_miss:
            return
        self._last_miss = telegram
        self._crc_misses += 1
        if self._crc_misses >= CRC_RELEASE_AFTER:
            log_event(
                logging.WARNING,
                "tibber_pulse_crc_released",
                endpoint=self.endpoint,
                variant="-".join(self._crc_variants[0]),
            )
            self._crc_variants = sml.CRC_VARIANTS
            self._crc_counts = {}
            self._crc_misses = 0

    def wait_until_ready(self, timeout=None):
        """Wait for the first good reading; answers whether one came in time."""

        return self._ready.wait(timeout)

    def get_power(self):
        """Return the cached grid power, recording a failure once it is stale."""

        return self.get_reading().power_w

    def get_reading(self):
        """The cached grid power; each new frame is one reading."""

        with self._lock:
            reading = self._reading
            received_at = self._received_at
            sequence = self._sequence
            error = self._last_error
            round_trip_ms = self._round_trip_ms
        age = None if received_at is None else self._clock() - received_at
        if reading is not None:
            self.last_value = reading
        if reading is not None and age <= self.max_age_seconds:
            first_hand_out = received_at != self._latency_reported_for
            self._latency_reported_for = received_at
            self.health.record_success(round_trip_ms if first_hand_out else None, age_seconds=age)
            return GridReading(self.last_value, sequence)
        if error is None and reading is not None:
            error = f"no new telegram for {age:.1f}s"
        self.health.record_failure(error=error or "no reading yet", stale_used=True)
        reason = getattr(error, "reason", None) or (
            type(error).__name__ if isinstance(error, Exception)
            else "stale" if reading is not None else "no_reading"
        )
        if self._warning_due(reason):
            log_event(
                logging.WARNING,
                "tibber_pulse_read_error",
                endpoint=self.endpoint,
                reason=reason,
                age_seconds=None if age is None else round(age, 1),
                stale_value=self.last_value,
            )
        return GridReading(self.last_value, sequence)

    def _warning_due(self, reason):
        """Warn when the reason changes, then once a minute while it lasts."""

        now = self._clock()
        last_reason, last_at = self._last_warning
        if reason != last_reason or last_at is None or now - last_at >= WARNING_REPEAT_SECONDS:
            self._last_warning = (reason, now)
            return True
        return False

    def _stream_due(self):
        """The stream follows the first reading, so the start waits on polling only."""
        if not self._ready.is_set() or self._meter is None or self._relearn_from is not None:
            return False
        return self._poll_until is None or self._clock() >= self._poll_until

    def read_once(self):
        """One step of the reader: a whole stream while it is due, else one telegram."""

        if not self._stream_due():
            polled = self.poll_once()
            with self._lock:
                answered = self._last_error is None
            if self._relearn_from is not None and answered:
                moved = self._meter != self._relearn_from
                self._relearn_from = None
                if not moved:
                    self._hand_over("no_new_reading", login=False)
            return polled
        if self._poll_until is not None:
            self._poll_until = None
            log_event(logging.INFO, "tibber_pulse_stream_retry", endpoint=self.endpoint)
        delivered = self.stream_once()
        if delivered:
            self._stream_failures = 0
            return True
        if self._stop.is_set():
            return False
        if self._foreign_meter_seen:
            self._relearn_from = self._meter
            return False
        with self._lock:
            error = self._last_error
        reason = getattr(error, "reason", type(error).__name__ if error else "no_telegram")
        self._hand_over(reason, login=reason in LOGIN_FAILURES)
        return False

    def _hand_over(self, reason, *, login):
        """Ask for telegrams instead of streaming, for longer after each hand-over in a row."""

        if login:
            seconds = POLL_FALLBACK_SECONDS
        else:
            self._stream_failures += 1
            seconds = min(POLL_FALLBACK_CAP_SECONDS, POLL_FALLBACK_SECONDS * 2 ** (self._stream_failures - 1))
        self._poll_until = self._clock() + seconds
        log_event(
            logging.WARNING,
            "tibber_pulse_stream_fallback",
            endpoint=self.endpoint,
            reason=reason,
            poll_seconds=round(seconds),
        )

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self.read_once()
                except Exception as exc:
                    with self._lock:
                        self._last_error = exc
                    log_event(
                        logging.WARNING,
                        "tibber_pulse_poll_crashed",
                        endpoint=self.endpoint,
                        error=exc,
                    )
                self._stop.wait(self._pause())
        finally:
            self.bridge.close()

    def _pause(self):
        """The wait before the next poll or stream.

        A login that cannot work is retried every half minute, and a bridge
        that does not answer, or answers with a server error, waits twice as
        long each time up to the same cap, since it hangs under load and needs
        the quiet to recover. A stream is opened again no sooner than a poll
        would come.
        """

        with self._lock:
            error = self._last_error
            unanswered = self._unanswered
        cap = max(self.poll_interval, RETRY_CAP_SECONDS)
        if getattr(error, "reason", None) in LOGIN_FAILURES:
            return cap
        if unanswered:
            return min(cap, self.poll_interval * 2 ** unanswered)
        return self.poll_interval

    def close(self):
        """Stop the worker; a request still in flight closes the bridge itself."""

        self._stop.set()
        if self._thread is None:
            self.bridge.close()
        elif self._thread is not threading.current_thread():
            self._thread.join(timeout=CLOSE_WAIT_SECONDS)
