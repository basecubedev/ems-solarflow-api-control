# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inverters an OpenDTU reads, taken from its local web API and never controlled.

``GET /api/livedata/status?inv=<serial>`` answers one inverter in one request:
whether the OpenDTU reaches it, how old its last answer is (``data_age``,
seconds), its limit and the power of each AC and DC channel. Each inverter is
read every ``POLL_SECONDS`` on a thread of its own, and one that does not
answer backs off; the control loop only takes the last result. Only ``GET`` is
ever sent: setting the limit (``POST /api/limit/config``) belongs to a write
path of its own.
"""

import logging
import math
import threading
import time
from dataclasses import dataclass

import requests

from ems.logging_utils import log_event
from ems.models import DeviceState

LIVEDATA_PATH = "/api/livedata/status"
CONNECT_TIMEOUT_SECONDS = 1.0
READ_TIMEOUT_SECONDS = 2.0
MAXIMUM_RETRY_SECONDS = 30.0
POLL_SECONDS = 5.0
RESULT_STALE_SECONDS = 30.0
DUSK_OUTPUT_W = 25.0
CLOSE_SECONDS = CONNECT_TIMEOUT_SECONDS + READ_TIMEOUT_SECONDS + 1.0
PLAUSIBLE_POWER_W = (-100.0, 100000.0)
PV_INPUT_SLOTS = 4


class OpenDtuAnswerError(ValueError):
    """The OpenDTU answered, but not with a reading of the configured inverter."""


@dataclass(frozen=True)
class OpenDtuReading:
    """One inverter as the OpenDTU last heard it."""

    serial: str
    reachable: bool
    data_age_seconds: float | None
    output_w: float
    pv_inputs_w: tuple
    limit_w: float | None

    @property
    def solar_w(self):
        return float(sum(value for value in self.pv_inputs_w if value is not None))


def _power(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    low, high = PLAUSIBLE_POWER_W
    return number if math.isfinite(number) and low <= number <= high else None


def _channel_power(channels, key):
    channel = channels.get(key) if isinstance(channels, dict) else None
    power = channel.get("Power") if isinstance(channel, dict) else None
    return _power(power.get("v")) if isinstance(power, dict) else None


def _channel_keys(channels):
    if not isinstance(channels, dict):
        return []
    return sorted((key for key in channels if str(key).isdigit()), key=int)


def select_inverter(payload, serial):
    """The inverter ``serial`` names in a livedata answer, or the only one when no serial is set."""

    inverters = payload.get("inverters") if isinstance(payload, dict) else None
    if not isinstance(inverters, list):
        raise OpenDtuAnswerError("the answer lists no inverters")
    listed = [item for item in inverters if isinstance(item, dict)]
    if serial is None:
        if len(listed) != 1:
            raise OpenDtuAnswerError(f"the OpenDTU lists {len(listed)} inverters; set serial to choose one")
        return listed[0]
    wanted = str(serial).strip().lower()
    for item in listed:
        if str(item.get("serial") or "").strip().lower() == wanted:
            return item
    raise OpenDtuAnswerError(f"the OpenDTU lists no inverter {serial}")


def listed_inverters(payload):
    """``(serial, name)`` of each inverter a livedata answer lists, or ``None`` for an answer no OpenDTU gives.

    The answer without ``inv`` lists every inverter beside the OpenDTU's own
    ``total``; the two together tell an OpenDTU's answer apart. An entry
    without a serial names no inverter and is left out.
    """

    if not isinstance(payload, dict):
        return None
    total = payload.get("total")
    inverters = payload.get("inverters")
    if not isinstance(total, dict) or "Power" not in total or not isinstance(inverters, list):
        return None
    listed = []
    for item in inverters:
        serial = item.get("serial") if isinstance(item, dict) else None
        if isinstance(serial, bool) or not isinstance(serial, (str, int)) or not str(serial).strip():
            continue
        name = item.get("name")
        listed.append((str(serial).strip(), name.strip() if isinstance(name, str) else ""))
    return tuple(listed)


def parse_inverter(item):
    """Map one inverter of a livedata answer onto a reading.

    A summary without channels, as the OpenDTU lists inverters when no
    ``inv`` is asked for, is no reading: its power is unknown, not 0.
    """

    output_w = _channel_power(item.get("AC"), "0")
    if output_w is None:
        raise OpenDtuAnswerError("the answer carries no AC power for the inverter")
    dc = item.get("DC")
    pv_inputs = [_channel_power(dc, key) for key in _channel_keys(dc)]
    age = item.get("data_age")
    limit = _power(item.get("limit_absolute"))
    return OpenDtuReading(
        serial=str(item.get("serial") or ""),
        reachable=item.get("reachable") is True,
        data_age_seconds=float(age) if _power(age) is not None and age >= 0 else None,
        output_w=output_w,
        pv_inputs_w=tuple(pv_inputs),
        limit_w=limit if limit is not None and limit >= 0 else None,
    )


def opendtu_device_state(reading):
    """The DeviceState a dashboard tile and the history read; all zero without a current reading."""

    reading_inputs = [value or 0.0 for value in reading.pv_inputs_w] if reading else []
    slots = (reading_inputs + [0.0] * PV_INPUT_SLOTS)[:PV_INPUT_SLOTS]
    return DeviceState(
        soc=0,
        min_soc=0,
        max_soc=0,
        solar=reading.solar_w if reading else 0,
        output=reading.output_w if reading else 0,
        pack_in=0,
        pack_out=0,
        temp=None,
        voltage=None,
        rssi=0,
        remain_minutes=0,
        solar1=slots[0],
        solar2=slots[1],
        solar3=slots[2],
        solar4=slots[3],
        output_limit=(reading.limit_w or 0) if reading else 0,
        soc_limit=0,
        pack_state=0,
        fault_level=0,
        smart_mode=0,
        grid_off_mode=0,
        ac_mode=0,
        ac_status=0,
        dc_status=0,
        grid_state=0,
        pack_num=0,
        pv_inputs_reported=tuple(
            index < len(reading_inputs) and reading.pv_inputs_w[index] is not None
            for index in range(PV_INPUT_SLOTS)
        ),
    )


def history_fields(reading):
    """The fields the history records for a current reading: what the inverter reports, nothing in its place."""

    fields = ["output", "solar"]
    fields += [
        f"solar{index}"
        for index, value in enumerate(reading.pv_inputs_w[:PV_INPUT_SLOTS], start=1)
        if value is not None
    ]
    if reading.limit_w is not None:
        fields.append("output_limit")
    return tuple(fields)


@dataclass(frozen=True)
class OpenDtuStatus:
    """What the last read of one inverter found.

    ``measured_zero`` is an answer that the inverter is off: the OpenDTU does
    not reach it after it last produced at most ``DUSK_OUTPUT_W``, as every
    night. Its 0 W is a measurement, so the device is offline without making
    the cycle unmeasured. One the OpenDTU loses while it produced more, a radio
    link gone in daylight, and a failed or out-of-date read are unmeasured.
    """

    reading: OpenDtuReading | None
    online: bool
    measured_zero: bool
    error: str | None
    taken: float | None


class OpenDtuDevice:
    """One configured inverter, read through its OpenDTU's web API on a thread of its own.

    The control loop only takes the last result (:meth:`status`), so a slow or
    unreachable OpenDTU, or a host name that takes long to resolve, never holds
    a cycle up. A result older than ``RESULT_STALE_SECONDS`` counts as no read.
    """

    def __init__(self, settings, *, session=None, clock=time.monotonic):
        self.name = settings["name"]
        self.host = settings["host"]
        self.port = settings["port"]
        self.configured_serial = settings["serial"]
        self.max_age_seconds = settings["max_age_seconds"]
        self._serial = self.configured_serial
        self._session = session or requests.Session()
        self._clock = clock
        self._lock = threading.Lock()
        self._status = OpenDtuStatus(None, False, False, "not read yet", None)
        self.consecutive_failures = 0
        self._last_output_w = None
        self._stop = threading.Event()
        self._thread = None

    @property
    def url(self):
        return f"http://{self.host}:{self.port}{LIVEDATA_PATH}"

    def status(self):
        with self._lock:
            status = self._status
        if status.taken is None or self._clock() - status.taken > RESULT_STALE_SECONDS:
            return OpenDtuStatus(None, False, False, status.error or "no answer for a while", status.taken)
        return status

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"opendtu-http-{self.name}", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, deadline):
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._session.close()

    def close(self):
        self.stop()
        self.join(time.monotonic() + CLOSE_SECONDS)

    def _run(self):
        while not self._stop.is_set():
            try:
                delay = self.poll_once()
            except Exception as exc:
                log_event(logging.ERROR, "opendtu_http_reader_failed", device=self.name, error_type=type(exc).__name__, error=exc)
                delay = MAXIMUM_RETRY_SECONDS
            self._stop.wait(delay)

    def poll_once(self):
        """Read the inverter once and keep what was found; the seconds until the next read."""

        try:
            reading = self._read()
        except Exception as exc:
            return self._failed(exc)
        return self._succeeded(reading)

    def _get(self, params):
        response = self._session.get(
            self.url, params=params, timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
        )
        if response.status_code == 401:
            raise OpenDtuAnswerError("the OpenDTU refuses to be read; allow read-only access in its security settings")
        response.raise_for_status()
        return response.json()

    def _read(self):
        """One inverter's channels; without a configured serial, the only inverter's.

        The list without ``inv`` carries no channels on current firmware, so the
        serial it names is asked for once and kept until a read fails.
        """

        if self._serial is None:
            listed = select_inverter(self._get(None), None)
            if isinstance(listed.get("AC"), dict):
                return parse_inverter(listed)
            serial = str(listed.get("serial") or "").strip()
            if not serial:
                raise OpenDtuAnswerError("the OpenDTU names no serial for its inverter")
            self._serial = serial
        return parse_inverter(select_inverter(self._get({"inv": self._serial}), self._serial))

    def _keep(self, status):
        with self._lock:
            self._status = status

    def _succeeded(self, reading):
        if self.consecutive_failures:
            log_event(logging.INFO, "opendtu_http_recovered", device=self.name, failed_reads=self.consecutive_failures)
        self.consecutive_failures = 0
        age = reading.data_age_seconds
        current = reading.reachable and age is not None and age <= self.max_age_seconds
        measured_zero = not reading.reachable and (self._last_output_w or 0.0) <= DUSK_OUTPUT_W
        if current:
            error = None
            self._last_output_w = reading.output_w
        elif not reading.reachable:
            error = "the OpenDTU does not reach the inverter"
        else:
            error = f"the OpenDTU's last answer from the inverter is {age} s old"
        self._keep(OpenDtuStatus(reading if current else None, current, measured_zero, error, self._clock()))
        return POLL_SECONDS

    def _failed(self, exc):
        self.consecutive_failures += 1
        if self.configured_serial is None:
            self._serial = None
        self._keep(OpenDtuStatus(None, False, False, str(exc), self._clock()))
        if self.consecutive_failures == 1:
            log_event(
                logging.WARNING,
                "opendtu_http_read_error",
                device=self.name,
                host=self.host,
                port=self.port,
                error_type=type(exc).__name__,
                error=exc,
            )
        return min(2 ** min(self.consecutive_failures - 1, 16), MAXIMUM_RETRY_SECONDS)


class OpenDtuDeviceRuntime:
    """The ``opendtu_http`` entries of ``devices[]``, each read on a thread of its own.

    They are never controller devices: nothing here allocates, limits,
    reconciles or writes. The dashboard and the history read :meth:`tiles`
    beside the controlled devices, under ``source=read_only`` like a device the
    MQTT mapping reads, so a series written that way goes on under the same name.
    """

    def __init__(self, devices):
        self._devices = list(devices)

    @property
    def names(self):
        return [device.name for device in self._devices]

    def refresh(self):
        for device in self._devices:
            device.start()

    def close(self):
        """Stop every reader, then wait for them against one deadline."""

        for device in self._devices:
            device.stop()
        deadline = time.monotonic() + CLOSE_SECONDS
        for device in self._devices:
            device.join(deadline)

    def tiles(self):
        tiles = []
        for device in self._devices:
            status = device.status()
            reading = status.reading
            fields = {
                "output_limit_applicable": reading is not None and reading.limit_w is not None,
                "firmware_status_applicable": False,
            }
            if reading is not None:
                fields["pv_inputs_w"] = [None if value is None else round(value, 1) for value in reading.pv_inputs_w]
            tile = {
                "name": device.name,
                "source": "read_only",
                "online": status.online,
                "measured_offline": status.measured_zero,
                "state": opendtu_device_state(reading),
                "has_reading": reading is not None,
                "soc_reported": False,
                "hardware": {"pv": True, "battery": False},
                "fields": fields,
            }
            if reading is not None:
                tile["history_fields"] = history_fields(reading)
            tiles.append(tile)
        return tiles


def build_opendtu_device_runtime(device_configs, *, session_factory=requests.Session):
    """Build the runtime for the enabled OpenDTU entries, or ``None`` if there are none."""

    from ems.config import opendtu_http_device_settings
    from ems.read_only_devices import is_opendtu_http_device_config
    from ems.zendure_mqtt.config_entries import config_entry_enabled

    devices = []
    for item in device_configs or ():
        if not is_opendtu_http_device_config(item) or not config_entry_enabled(item):
            continue
        devices.append(OpenDtuDevice(opendtu_http_device_settings(item), session=session_factory()))
    return OpenDtuDeviceRuntime(devices) if devices else None
