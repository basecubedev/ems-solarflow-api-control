# SPDX-License-Identifier: AGPL-3.0-or-later
"""One read-only E3/DC connection per installation, read once per control cycle.

The control loop drives the reads. The grid meter and the device tile both ask
the same session for this cycle's reading, and whichever asks first performs
the Modbus request, so the poll period is the loop interval, the two roles
never describe different moments, and one E3/DC answers one request per cycle
however many roles it plays. Every request goes through
``ems.e3dc_modbus.ReadOnlyModbusTcpClient``, which cannot send a write.
"""

import logging
import threading
import time
from dataclasses import dataclass, replace

from ems import e3dc_modbus as modbus
from ems.logging_utils import log_event
from ems.models import DeviceState


@dataclass(frozen=True)
class E3dcReading:
    """One consistent snapshot of the power block (and the inverter, if read).

    ``inverter_w`` is ``None`` when the inverter block was not read or did not
    answer; the grid, PV and battery values stand on their own.
    """

    pv_w: float
    battery_w: float
    grid_w: float
    soc: float
    inverter_w: float | None
    taken_monotonic: float
    round_trip_ms: float


def _field_value(fields, first, registers, key):
    item = next(field for field in fields if field.key == key)
    offset = item.address - first
    return item.decode(registers[offset:offset + item.length])


class E3dcModbusSession:
    """The single owner of one E3/DC's Modbus connection."""

    CYCLE_REUSE_SECONDS = 0.5
    MAXIMUM_RETRY_SECONDS = 30.0
    MAXIMUM_RETRY_DOUBLINGS = 16

    def __init__(
        self,
        host,
        *,
        port,
        unit_id,
        timeout_seconds=2.0,
        modbus_client_factory=None,
        clock=time.monotonic,
    ):
        self.host = str(host).strip()
        self.port = int(port)
        self.unit_id = int(unit_id)
        self.read_inverter = False
        self._clock = clock
        factory = modbus_client_factory or modbus.ReadOnlyModbusTcpClient
        self._client = factory(
            self.host, self.port, unit_id=self.unit_id, timeout=float(timeout_seconds)
        )
        self._lock = threading.Lock()
        self.mapping = None
        self.device_info = {}
        self.last_reading = None
        self.last_error = None
        self.consecutive_failures = 0
        self._retry_at = None
        self._consumed_by = set()
        self._inverter_failures = 0
        self._inverter_retry_at = None

    @property
    def endpoint(self):
        return f"{self.host}:{self.port} unit {self.unit_id}"

    def reading_for_cycle(self, role="grid_meter"):
        """Return ``(reading, None)`` for this cycle, or ``(None, reason)``.

        A reading another role took moments ago is this cycle's reading, but no
        role is ever handed the same reading twice: the control loop adds each
        grid reading to what it already commands, so a repeat would be counted
        again. While a failed device is backing off, no request is sent and the
        cycle is told why it has no reading instead of waiting on the network.
        """

        with self._lock:
            now = self._clock()
            last = self.last_reading
            if (
                not self.consecutive_failures
                and last is not None
                and role not in self._consumed_by
                and now - last.taken_monotonic < self.CYCLE_REUSE_SECONDS
            ):
                self._consumed_by.add(role)
                return last, None
            if self._retry_at is not None and now < self._retry_at:
                return None, self.last_error
            try:
                reading = self._poll()
            except Exception as exc:
                # Anything a poll raises is a failed read: a malformed frame must
                # not take the grid meter or the device tile down with it.
                self._poll_failed(exc)
                return None, self.last_error
            self._poll_succeeded(reading)
            self._consumed_by = {role}
            return reading, None

    def close(self):
        with self._lock:
            self._client.close()
            self.mapping = None

    def _poll(self):
        if self.mapping is None:
            self._connect_and_detect()
        started = time.monotonic()
        power = self._client.read_registers(
            self.mapping.wire_address(modbus.POWER_FIRST),
            modbus.POWER_COUNT,
            self.mapping.function_code,
            self.mapping.unit_id,
        )
        round_trip_ms = (time.monotonic() - started) * 1000.0

        def value(key):
            return _field_value(modbus.POWER_FIELDS, modbus.POWER_FIRST, power, key)

        pv_w = float(value("pv_power"))
        battery_w = float(value("battery_power"))
        grid_w = float(value("grid_power"))
        soc = float(value("battery_soc"))
        inverter_w = self._read_inverter() if self._inverter_due() else None
        return E3dcReading(
            pv_w=pv_w,
            battery_w=battery_w,
            grid_w=grid_w,
            soc=soc,
            inverter_w=inverter_w,
            taken_monotonic=self._clock(),
            round_trip_ms=round_trip_ms,
        )

    def _inverter_due(self):
        if not self.read_inverter:
            return False
        return self._inverter_retry_at is None or self._clock() >= self._inverter_retry_at

    def _read_inverter(self):
        """The inverter's AC power, or ``None``; it never fails the grid reading.

        The inverter block is for the device tile only, and it backs off on its
        own: a block the E3/DC refuses or lets time out is not asked for again
        every cycle. A refusal leaves the connection usable; a transport error
        ends it so no late answer can be read as the next one, and the next
        cycle reconnects for the grid value alone.
        """

        try:
            inverter = self._client.read_registers(
                self.mapping.wire_address(modbus.INVERTER_FIRST),
                modbus.INVERTER_COUNT,
                self.mapping.function_code,
                self.mapping.unit_id,
            )
            value = float(sum(
                _field_value(modbus.INVERTER_FIELDS, 0, inverter, key)
                for key in ("active_power_l1", "active_power_l2", "active_power_l3")
            ))
        except modbus.ModbusExceptionResponse as exc:
            self._inverter_failed(exc)
            return None
        except Exception as exc:
            self._inverter_failed(exc)
            self._client.close()
            self.mapping = None
            return None
        if self._inverter_failures:
            log_event(
                logging.INFO,
                "e3dc_modbus_inverter_recovered",
                host=self.host,
                port=self.port,
                failed_reads=self._inverter_failures,
            )
        self._inverter_failures = 0
        self._inverter_retry_at = None
        return value

    def _inverter_failed(self, exc):
        self._inverter_failures += 1
        doublings = min(self._inverter_failures - 1, self.MAXIMUM_RETRY_DOUBLINGS)
        self._inverter_retry_at = self._clock() + min(2 ** doublings, self.MAXIMUM_RETRY_SECONDS)
        if self._inverter_failures == 1:
            log_event(
                logging.WARNING,
                "e3dc_modbus_inverter_unavailable",
                host=self.host,
                port=self.port,
                error=exc,
            )

    def _connect_and_detect(self):
        self._client.connect()
        self.mapping = self._detect_mapping()
        self.device_info = self._read_device_info()
        log_event(
            logging.INFO,
            "e3dc_modbus_connected",
            host=self.host,
            port=self.port,
            unit_id=self.mapping.unit_id,
            address_offset=self.mapping.address_offset,
            **self.device_info,
        )

    def _detect_mapping(self):
        """Find the address offset from the magic word, one short attempt each.

        Unlike the probe's detection, a transport error ends the search at once:
        this runs inside the control cycle, and a device that does not answer
        one read will not answer five.
        """

        answers = []
        for offset in modbus.ADDRESS_OFFSET_CANDIDATES:
            try:
                registers = self._client.read_registers(
                    modbus.MAGIC_ADDRESS + offset,
                    1,
                    modbus.READ_HOLDING_REGISTERS,
                    self.unit_id,
                )
            except modbus.ModbusExceptionResponse as exc:
                answers.append(modbus.Attempt(self.unit_id, 3, offset, str(exc)))
                continue
            except modbus.ModbusError as exc:
                if answers:
                    raise modbus.ModbusError(
                        f"connection to {self.host}:{self.port} lost after it answered "
                        f"(last answer: {answers[-1].outcome}): {exc}"
                    ) from exc
                raise modbus.ModbusError(
                    f"no Modbus answer from {self.host}:{self.port}: {exc}"
                ) from exc
            outcome = f"0x{registers[0]:04X}"
            answers.append(modbus.Attempt(self.unit_id, 3, offset, outcome))
            if registers[0] == modbus.MAGIC_VALUE:
                return modbus.Mapping(
                    self.unit_id, modbus.READ_HOLDING_REGISTERS, offset, registers[0]
                )
        if modbus.looks_like_sunspec(answers):
            raise modbus.ModbusError(
                "device answers in SunSpec mode; set the Modbus TCP protocol to E3DC"
            )
        last = answers[-1].outcome if answers else "no answer"
        raise modbus.ModbusError(
            f"magic word 0x{modbus.MAGIC_VALUE:04X} not found at unit id "
            f"{self.unit_id} (last answer: {last})"
        )

    def _read_device_info(self):
        """Manufacturer, model and firmware, from one block read.

        Unlike the probe, there is no field-by-field fallback with reconnects:
        this runs inside the control cycle. A refusal only leaves the info
        empty; a transport error is a failed poll.
        """

        try:
            registers = self._client.read_registers(
                self.mapping.wire_address(modbus.IDENTIFICATION_FIRST),
                modbus.IDENTIFICATION_COUNT,
                self.mapping.function_code,
                self.mapping.unit_id,
            )
        except modbus.ModbusExceptionResponse:
            return {}
        wanted = ("manufacturer", "model", "firmware_release", "modbus_firmware")
        return {
            key: _field_value(
                modbus.IDENTIFICATION_FIELDS, modbus.IDENTIFICATION_FIRST, registers, key
            )
            for key in wanted
        }

    def _poll_succeeded(self, reading):
        if self.consecutive_failures:
            log_event(
                logging.INFO,
                "e3dc_modbus_recovered",
                host=self.host,
                port=self.port,
                failed_polls=self.consecutive_failures,
            )
        self.consecutive_failures = 0
        self._retry_at = None
        self.last_error = None
        self.last_reading = reading

    def _poll_failed(self, exc):
        self.consecutive_failures += 1
        self.mapping = None
        self._client.close()
        self.last_error = str(exc)
        doublings = min(self.consecutive_failures - 1, self.MAXIMUM_RETRY_DOUBLINGS)
        self._retry_at = self._clock() + min(2 ** doublings, self.MAXIMUM_RETRY_SECONDS)
        if self.consecutive_failures == 1:
            log_event(
                logging.WARNING,
                "e3dc_modbus_read_error",
                host=self.host,
                port=self.port,
                unit_id=self.unit_id,
                error_type=type(exc).__name__,
                error=exc,
            )


class E3dcSessions:
    """One session per E3/DC endpoint, shared by every role that reads it."""

    def __init__(self, session_factory=E3dcModbusSession):
        self._factory = session_factory
        self._sessions = {}

    def session(self, host, port, unit_id):
        key = (str(host).strip().lower(), int(port), int(unit_id))
        if key not in self._sessions:
            self._sessions[key] = self._factory(host, port=port, unit_id=unit_id)
        return self._sessions[key]

    def close(self):
        for session in self._sessions.values():
            try:
                session.close()
            except Exception as exc:
                log_event(logging.WARNING, "e3dc_modbus_close_failed", error=exc)


def e3dc_device_state(reading):
    """Map one reading onto the DeviceState a dashboard tile reads.

    ``battery_w`` is positive while charging, the dashboard's own convention,
    so it splits into ``pack_out`` (into the pack) and ``pack_in`` (out of it).
    """

    battery_w = reading.battery_w if reading else 0.0
    return DeviceState(
        soc=reading.soc if reading else 0,
        min_soc=0,
        max_soc=0,
        solar=reading.pv_w if reading else 0,
        output=(reading.inverter_w or 0) if reading else 0,
        pack_in=max(0.0, -battery_w),
        pack_out=max(0.0, battery_w),
        temp=0,
        voltage=0,
        rssi=0,
        remain_minutes=0,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
        output_limit=0,
        soc_limit=0,
        pack_state=0,
        fault_level=0,
        smart_mode=0,
        grid_off_mode=0,
        ac_mode=0,
        ac_status=0,
        dc_status=0,
        grid_state=0,
    )


@dataclass
class _E3dcDevice:
    name: str
    session: E3dcModbusSession
    reading: E3dcReading | None = None
    online: bool = False
    error: str | None = None


class E3dcDeviceRuntime:
    """The read-only E3/DC entries of ``devices[]``, refreshed once per cycle.

    They are never controller devices: nothing here allocates, limits,
    reconciles or writes. The dashboard reads :meth:`tiles` beside the
    controlled devices, the way it reads telemetry-only MQTT devices.
    """

    def __init__(self, devices):
        self._devices = list(devices)

    @property
    def names(self):
        return [device.name for device in self._devices]

    def refresh(self):
        for device in self._devices:
            reading, error = device.session.reading_for_cycle(role=f"device:{device.name}")
            # Without the inverter the tile's output is unknown, and a tile that
            # reports 0 W output would understate the home load it feeds.
            device.online = reading is not None and reading.inverter_w is not None
            device.error = error
            if reading is None:
                continue
            if reading.inverter_w is None and device.reading is not None:
                reading = replace(reading, inverter_w=device.reading.inverter_w)
            device.reading = reading

    def tiles(self):
        tiles = []
        for device in self._devices:
            reading = device.reading
            tiles.append(
                {
                    "name": device.name,
                    "source": "e3dc",
                    "online": device.online,
                    "state": e3dc_device_state(reading),
                    "has_reading": reading is not None,
                    "fields": {
                        "grid_power_w": round(reading.grid_w, 1) if reading else None,
                        "output_limit_applicable": False,
                        "firmware_status_applicable": False,
                    },
                }
            )
        return tiles


def build_e3dc_device_runtime(device_configs, sessions):
    """Build the runtime for the enabled E3/DC entries, or ``None`` if there are none."""

    from ems.config import e3dc_modbus_device_settings
    from ems.read_only_devices import is_e3dc_modbus_device_config
    from ems.zendure_mqtt.config_entries import config_entry_enabled

    devices = []
    for item in device_configs or ():
        if not is_e3dc_modbus_device_config(item) or not config_entry_enabled(item):
            continue
        settings = e3dc_modbus_device_settings(item)
        session = sessions.session(settings["host"], settings["port"], settings["unit_id"])
        session.read_inverter = True
        devices.append(_E3dcDevice(name=settings["name"], session=session))
    return E3dcDeviceRuntime(devices) if devices else None
