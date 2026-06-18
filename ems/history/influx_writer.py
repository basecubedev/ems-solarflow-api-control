# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native InfluxDB telemetry writer for the EMS control loop.

Writes the telemetry the EMS already collects each cycle directly into the
InfluxDB raw bucket, so the InfluxDB Analytics works out of the box without the
standalone collector (``scripts/capture_runtime_to_influx.py``). The collector
remains available for development / diagnostics / backfill, but is no longer the
primary ingestion path.

Design constraints (see the task spec):

- **Optional**: only used when ``influxdb.enabled`` is true.
- **Single polling source**: it reuses the device states already fetched by the
  control loop; it never polls the hardware itself.
- **Non-blocking**: the control loop only enqueues line protocol onto a bounded
  queue; a daemon worker thread performs the HTTP writes. On overflow it drops
  the oldest-style (refuses new) batch instead of blocking the control path.
- **Failure-isolated**: any InfluxDB error is logged (rate-limited) as a warning
  and never propagates to the control loop; the worker reconnects automatically.
- **No downsampling**: it writes only to ``{prefix}_raw``; the Flux tasks
  reconciled by ``emsctl influx sync`` handle raw -> 1m -> 5m -> 1h.

The measurement/field schema matches what
:mod:`ems.history.influx_provider` reads (``zendure_device`` /
``shelly_meter``), so the existing query profiles, buckets and Analytics UI keep
working unchanged.

Import-side-effect-free.
"""

import logging
import queue
import threading
import time

from ems.history.influx_client import build_line_protocol
from ems.history.schema import bucket_name
from ems.logging_utils import log_event

# Numeric/boolean device telemetry fields written per cycle. The first five
# (solar/output/soc/pack_in/pack_out) back every Analytics series; the rest are
# written for parity with the standalone collector and future series.
_DEVICE_FIELDS = (
    "soc",
    "min_soc",
    "max_soc",
    "solar",
    "solar1",
    "solar2",
    "solar3",
    "solar4",
    "output",
    "output_limit",
    "pack_in",
    "pack_out",
    "soc_limit",
    "pack_state",
    "fault_level",
    "smart_mode",
    "grid_off_mode",
    "ac_mode",
    "ac_status",
    "dc_status",
    "grid_state",
    "rssi",
    "voltage",
    "temp",
    "remain_minutes",
)


def _device_field_values(state):
    """Pull the telemetry field set from a device state, skipping missing ones."""
    fields = {}
    for name in _DEVICE_FIELDS:
        value = getattr(state, name, None)
        if isinstance(value, bool):
            fields[name] = value
        elif isinstance(value, (int, float)):
            fields[name] = float(value)
    return fields


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_telemetry_lines(
    devices, states, online_map, grid_power, target=None, timestamp_ns=None
):
    """Build InfluxDB line protocol from one control-loop telemetry snapshot.

    Produces one ``zendure_device`` point per device, one ``shelly_meter`` point
    carrying the meter exchange power (``grid_power``, positive import / negative
    export) and the calculated household load (``house_load``), and, when a
    controller target is supplied, one ``ems_runtime`` point with the selected
    output target (``target_output``). Offline devices are recorded with
    ``available=False`` so gaps are explicit. Returns a list of line strings.

    ``house_load`` mirrors the dashboard telemetry semantics:
    ``max(0, inverter_output_total + grid_power)``.
    """
    if timestamp_ns is None:
        timestamp_ns = time.time_ns()

    online_map = online_map or {}
    lines = []
    inverter_total = 0.0
    for device, state in zip(devices, states):
        tags = {"device": device.name, "source": "zendure"}
        online = bool(online_map.get(device.name, True))
        if state is None or not online:
            line = build_line_protocol(
                "zendure_device", tags, {"available": False}, timestamp_ns
            )
            if line:
                lines.append(line)
            continue
        fields = {"available": True}
        fields.update(_device_field_values(state))
        if _is_number(fields.get("output")):
            inverter_total += fields["output"]
        line = build_line_protocol("zendure_device", tags, fields, timestamp_ns)
        if line:
            lines.append(line)

    if _is_number(grid_power):
        grid_power = float(grid_power)
        house_load = max(0.0, inverter_total + grid_power)
        line = build_line_protocol(
            "shelly_meter",
            {"source": "shelly"},
            {"grid_power": grid_power, "house_load": house_load},
            timestamp_ns,
        )
        if line:
            lines.append(line)

    if _is_number(target):
        line = build_line_protocol(
            "ems_runtime",
            {"source": "ems"},
            {"target_output": float(target)},
            timestamp_ns,
        )
        if line:
            lines.append(line)

    return lines


class InfluxTelemetryWriter:
    """Background, failure-isolated writer of EMS telemetry to InfluxDB raw."""

    def __init__(
        self,
        influx_config,
        *,
        client_factory=None,
        max_queue=600,
        batch_max=240,
        error_log_interval_s=60.0,
        max_backoff_s=30.0,
    ):
        self.config = influx_config
        self.bucket = bucket_name(influx_config["bucket_prefix"], "raw")
        self._client_factory = client_factory
        self._queue = queue.Queue(maxsize=max_queue)
        self._batch_max = batch_max
        self._error_log_interval = error_log_interval_s
        self._max_backoff = max_backoff_s
        self._thread = None
        self._stop = threading.Event()
        self._client = None
        self._dropped = 0
        self._last_error_log = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="influx-writer", daemon=True
        )
        self._thread.start()
        log_event(logging.INFO, "influx_writer_started", bucket=self.bucket)

    def stop(self, timeout=2.0):
        self._stop.set()
        try:
            self._queue.put_nowait(None)  # wake the worker
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None

    # -- producer side (control loop) --------------------------------------
    def enqueue(self, lines):
        """Non-blocking enqueue of a batch of line-protocol strings.

        Drops the batch (rate-limited warning) if the queue is full so the
        control loop is never blocked by a slow or unavailable InfluxDB.
        """
        if not lines:
            return
        try:
            self._queue.put_nowait(list(lines))
        except queue.Full:
            self._dropped += len(lines)
            self._maybe_log_error(
                "influx_writer_queue_full", dropped_total=self._dropped
            )

    # -- consumer side (worker thread) -------------------------------------
    def _build_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from ems.history.influx_client import HistoryInfluxClient
        from ems.influx_setup import runtime_influx_token, runtime_influx_url

        url = runtime_influx_url(self.config)
        token = runtime_influx_token(self.config)
        if not token or not url:
            return None
        return HistoryInfluxClient(url, self.config["org"], token)

    def _client_or_none(self):
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _drain_batch(self, first):
        batch = list(first)
        while len(batch) < self._batch_max:
            try:
                more = self._queue.get_nowait()
            except queue.Empty:
                break
            if more is None:
                self._stop.set()
                break
            batch.extend(more)
        return batch

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            batch = self._drain_batch(item)
            if not batch:
                continue

            client = self._client_or_none()
            if client is None:
                self._maybe_log_error(
                    "influx_writer_unconfigured", hint=self._setup_hint()
                )
                self._sleep_backoff(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue

            try:
                client.write_lines(self.bucket, batch)
                backoff = 1.0
            except Exception as exc:
                # Drop the cached client so the next attempt reconnects, and
                # never let the failure escape to the control loop.
                self._client = None
                self._maybe_log_error(
                    "influx_writer_write_error",
                    error=exc,
                    line_count=len(batch),
                    hint=self._setup_hint(),
                )
                self._sleep_backoff(backoff)
                backoff = min(backoff * 2, self._max_backoff)

    def _sleep_backoff(self, seconds):
        # Wait, but wake immediately on stop().
        self._stop.wait(timeout=seconds)

    def _setup_hint(self):
        """Actionable one-liner for "enabled but not reachable" log lines.

        The EMS controller never manages Docker, so when bundled InfluxDB is
        unreachable the fix is a host-side setup command, not anything the
        control loop can do. External InfluxDB is user-managed.
        """
        if self.config.get("mode") == "bundled":
            return (
                "InfluxDB is enabled but not reachable. For bundled mode run: "
                "python3 emsctl.py influx init or start the full stack with: "
                "python3 emsctl.py stack up"
            )
        return (
            "InfluxDB is enabled but not reachable. Check influxdb.url/token "
            "(external InfluxDB is user-managed) and run: "
            "python3 emsctl.py influx status"
        )

    def _maybe_log_error(self, event, **fields):
        now = time.time()
        if now - self._last_error_log >= self._error_log_interval:
            self._last_error_log = now
            log_event(logging.WARNING, event, bucket=self.bucket, **fields)
