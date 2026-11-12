# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grid power from a Tibber Pulse IR bridge, read over its local web server.

The bridge answers slowly and now and then serves a damaged telegram, so the
control loop never waits on it: one background worker polls the bridge and
keeps the last good reading, and ``get_power`` hands out that reading while it
is young enough. Import-side-effect-free.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import requests

from ems import sml
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


class BridgeError(Exception):
    """A bridge request that did not yield a telegram; ``reason`` is stable."""

    def __init__(self, reason, detail="", status=None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.status = status


def grid_power_from_telegram(telegram, crc_variants=sml.CRC_VARIANTS):
    """Signed grid power and the decoded telegram of one bridge answer.

    The one decode path of the runtime reader and the diagnose probe; a value
    beyond ``MAX_PLAUSIBLE_POWER_W`` is refused like a damaged telegram.
    """

    decoded = sml.parse(telegram, crc_variants=crc_variants)
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
                 timeout=REQUEST_TIMEOUT_SECONDS, password_provider=None):
        self.host = str(host).strip()
        self.port = int(port)
        self.node_id = int(node_id)
        self.timeout = timeout
        self._password = password
        self._password_provider = password_provider
        self._password_error = None
        self._session = session or requests.Session()
        self._paths = list(DATA_PATHS)

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

    def get(self, path, params=None):
        """One authenticated GET; a refused password is read again next time."""

        password = self._current_password()
        if not login_can_carry(password):
            if self._password_provider is not None:
                self._password = None
            raise BridgeError("bad_password", "the password has characters an HTTP login cannot carry")
        response = self._session.get(
            f"{self.base_url}{path}",
            params=params,
            auth=(BRIDGE_USER, password),
            timeout=self.timeout,
        )
        if response.status_code in (401, 403):
            if self._password_provider is not None:
                self._password = None
            raise BridgeError("unauthorized", "the bridge refused the password")
        return response

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


class TibberPulseGridMeterClient:
    """Cached grid meter reading fed by a background poller.

    A reading counts as new only when the meter's frame changed: its
    transaction number, the meter's seconds counter or a value moved; bytes the
    bridge serves around the frame do not count. A bridge whose head stopped
    (empty battery, turned head) keeps serving its last frame, and that must
    age out instead of looking fresh. The first frame is only the baseline, so
    a head that stopped before the start never yields a reading at all.
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
            if getattr(exc, "reason", None) == "crc_mismatch":
                self._count_crc_miss(telegram)
            with self._lock:
                self._last_error = exc
                unanswered = isinstance(exc, requests.RequestException) or (getattr(exc, "status", None) or 0) >= 500
                self._unanswered = min(self._unanswered + 1, BACKOFF_DOUBLINGS) if unanswered else 0
            log_event(
                logging.DEBUG,
                "tibber_pulse_poll_failed",
                endpoint=self.endpoint,
                reason=getattr(exc, "reason", type(exc).__name__),
            )
            return False
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
                self._round_trip_ms = (self._received_at - start) * 1000.0
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

        with self._lock:
            reading = self._reading
            received_at = self._received_at
            error = self._last_error
            round_trip_ms = self._round_trip_ms
        age = None if received_at is None else self._clock() - received_at
        if reading is not None:
            self.last_value = reading
        if reading is not None and age <= self.max_age_seconds:
            first_hand_out = received_at != self._latency_reported_for
            self._latency_reported_for = received_at
            self.health.record_success(round_trip_ms if first_hand_out else None, age_seconds=age)
            return self.last_value
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
        return self.last_value

    def _warning_due(self, reason):
        """Warn when the reason changes, then once a minute while it lasts."""

        now = self._clock()
        last_reason, last_at = self._last_warning
        if reason != last_reason or last_at is None or now - last_at >= WARNING_REPEAT_SECONDS:
            self._last_warning = (reason, now)
            return True
        return False

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self.poll_once()
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
        """The wait before the next poll.

        A login that cannot work is retried every half minute, and a bridge
        that does not answer, or answers with a server error, waits twice as
        long each time up to the same cap, since it hangs under load and needs
        the quiet to recover.
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
