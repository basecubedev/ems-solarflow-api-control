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
from ems.history.schema import (
    bucket_name,
    planned_bucket_names,
    planned_task_names,
    query_profile_bucket_names,
)
from ems.influx_setup import INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS
from ems.power_direction import derive_house_load_w
from ems.logging_utils import log_event

# Shown once when telemetry is being written but the downsampling half of the
# schema was never created. The dashboard says the same thing for a range it
# cannot serve; a test holds the two remediation commands together.
SCHEMA_SYNC_HINT = (
    "Analytics ranges that read a downsampled bucket stay empty until the "
    "schema is created. Run: python3 emsctl.py influx sync"
)
# Same symptom, opposite fix, so it is said separately: no sync creates this
# bucket, because the two halves of the influxdb config disagree about which
# buckets exist. The dashboard says the same thing for a range it cannot serve.
# The check reads bucket and task metadata, which the writer itself never
# needed. A token scoped to writes answers every lookup the same way forever, so
# it is said once and the check stops rather than repeating on the ladder.
SCHEMA_READ_HINT = (
    "The InfluxDB token cannot read bucket and task metadata, so the analytics "
    "schema cannot be checked. Writing telemetry is unaffected."
)
UNPLANNED_BUCKET_HINT = (
    "A query profile reads a bucket that no downsampling entry creates, so no "
    "schema sync will make it exist. Check influxdb.query_profiles against "
    "influxdb.downsampling in config.json."
)

# How often an incomplete schema is named before the writer stops saying it.
# Nobody is going to act on the eleventh line, and a sync taking a while is a
# state the next check settles anyway.
SCHEMA_CHECK_REPORTS = 3
# How often a check that could not run at all is retried. A separate, longer
# allowance on purpose: a lookup can fail for a reason that passes -- InfluxDB
# still settling while the first writes already land -- and spending the
# reporting allowance on those failures is how the signal goes silent for the
# life of the process, which is the thing the event exists to prevent.
SCHEMA_CHECK_FAILURES = 10
# Spaced, not consecutive: three probes on three successive batches are gone in
# fifteen seconds.
SCHEMA_CHECK_RETRY_SECONDS = 60.0
# What one phase of one request may take. The same tolerance the Admin status
# check gives these very lookups, because they run against the same InfluxDB on
# the same board: a budget derived by dividing a flat total made this path the
# stricter of the two, and a lookup timing out here reports nothing at all.
SCHEMA_CHECK_REQUEST_TIMEOUT_SECONDS = INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS


def _now():
    return time.monotonic()


class _WriterStopping(Exception):
    """Raised inside the schema check when the writer has been asked to stop."""


def _is_permission_error(exc):
    """Whether InfluxDB refused the request rather than failed to answer it.

    A refusal does not become an acceptance by being retried, so it ends the
    check instead of spending its allowance on the same answer ten times.
    """

    response = getattr(exc, "response", None)

    return getattr(response, "status_code", None) in (401, 403)


def schema_check_budget_seconds(influx_config):
    """How long the whole schema check may take, for this config.

    One request per planned bucket plus the task list, each able to spend its
    budget on connect and again on read. Derived rather than fixed so a longer
    downsampling chain gets a longer check instead of a stricter one.
    """

    requests = len(checked_bucket_names(influx_config)) + 1

    return requests * 2 * SCHEMA_CHECK_REQUEST_TIMEOUT_SECONDS


def checked_bucket_names(influx_config):
    """Every bucket the check looks up: the planned ones, then any extra that a
    query profile reads. The second set is what a sync can never create."""

    planned = planned_bucket_names(influx_config)
    extra = [
        name
        for name in query_profile_bucket_names(influx_config)
        if name not in planned
    ]

    return planned + extra

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
    "grid_input",
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

    ``house_load`` mirrors the dashboard telemetry semantics and is derived by
    the shared :func:`ems.power_direction.derive_house_load_w`, so a charging
    device's own draw is not billed to the household.
    """
    if timestamp_ns is None:
        timestamp_ns = time.time_ns()

    online_map = online_map or {}
    lines = []
    inverter_total = 0.0
    charge_total = 0.0
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
        if _is_number(fields.get("grid_input")):
            charge_total += fields["grid_input"]
        line = build_line_protocol("zendure_device", tags, fields, timestamp_ns)
        if line:
            lines.append(line)

    if _is_number(grid_power):
        grid_power = float(grid_power)
        house_load = derive_house_load_w(inverter_total, grid_power, charge_total)
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
        self._schema_checked = False
        self._schema_reports = 0
        self._schema_failures = 0
        self._schema_check_retry_at = 0.0
        self._schema_check_deadline = 0.0

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
            else:
                backoff = 1.0
                try:
                    self._report_schema_gap_once(client)
                except Exception as exc:
                    # Outside the write's own handler, so nothing here may reach
                    # the loop: this is a diagnostic, and it does not get to
                    # stop telemetry. An unexpected failure ends the diagnostic,
                    # not the writer.
                    self._schema_failures = SCHEMA_CHECK_FAILURES
                    log_event(
                        logging.WARNING,
                        "influx_schema_check_failed",
                        error=type(exc).__name__,
                        attempts_left=0,
                    )

    def _report_schema_gap_once(self, client):
        """Say once that the analytics half of the schema was never created.

        A write proves only that the raw bucket exists, so a schema sync that
        never completed stays invisible: telemetry keeps arriving while every
        Analytics range that reads a downsampled bucket is empty. Reported from
        here rather than at startup because a check that runs before InfluxDB
        is listening cannot tell the two apart -- which is the fault itself.

        Tasks are checked beside the buckets because ``sync`` creates buckets
        first: a run that stops in between leaves the buckets in place, and a
        query against an empty bucket succeeds. Nothing else would ever notice.

        A bucket that only a query profile names is checked too and reported
        separately, because running a sync is the answer for the planned ones
        and no answer at all for that one.
        """

        if self._schema_checked:
            return
        if self._schema_reports >= SCHEMA_CHECK_REPORTS:
            return
        if self._schema_failures >= SCHEMA_CHECK_FAILURES:
            return
        now = _now()
        if now < self._schema_check_retry_at:
            return

        planned = planned_bucket_names(self.config)
        # The deadline follows from the requests rather than capping them: it
        # has to let every checked bucket and the task list be asked, because
        # the task list is the only signal for a schema whose buckets all exist
        # and whose tasks were never created.
        self._schema_check_deadline = now + schema_check_budget_seconds(self.config)
        buckets = []
        unplanned = []
        tasks = []
        whole = False
        try:
            for name in checked_bucket_names(self.config):
                if client.find_bucket(name, timeout=self._probe_timeout()) is None:
                    (buckets if name in planned else unplanned).append(name)
            live = {
                task.get("name")
                for task in client.list_tasks(timeout=self._probe_timeout())
                if task.get("status") == "active"
            }
            tasks = [
                name for name in planned_task_names(self.config) if name not in live
            ]
            whole = True
        except _WriterStopping:
            # An ordinary shutdown, not a fault: nothing to report and nothing
            # to hold against the check's allowance.
            return
        except Exception as exc:
            if _is_permission_error(exc):
                self._schema_checked = True
                log_event(
                    logging.WARNING,
                    "influx_schema_check_not_permitted",
                    hint=SCHEMA_READ_HINT,
                )
                return

            # What the lookups already settled is kept. A budget spent before
            # the task list still names buckets the operator has to create, and
            # the alternative is the silence this event exists to break.
            self._schema_failures += 1
            log_event(
                logging.WARNING,
                "influx_schema_check_failed",
                error=type(exc).__name__,
                attempts_left=SCHEMA_CHECK_FAILURES - self._schema_failures,
            )

        missing = {}
        if buckets:
            missing["missing_buckets"] = ",".join(buckets)
        if tasks:
            missing["missing_tasks"] = ",".join(tasks)

        # Measured from the end, so a check that spent its whole budget is not
        # retried ten seconds later: the writer thread has telemetry queued
        # behind it, and the gap is what keeps the diagnostic out of its way.
        self._schema_check_retry_at = _now() + SCHEMA_CHECK_RETRY_SECONDS

        if unplanned:
            log_event(
                logging.WARNING,
                "influx_schema_bucket_not_planned",
                buckets=",".join(unplanned),
                hint=UNPLANNED_BUCKET_HINT,
            )

        if missing:
            # Deliberately not latched. A read taken while `influx sync` is
            # midway through is a true snapshot of a state that is about to be
            # fine, and latching it would leave a false warning standing for the
            # life of the process. The ladder settles it either way.
            log_event(
                logging.WARNING,
                "influx_schema_incomplete",
                hint=SCHEMA_SYNC_HINT,
                **missing,
            )

        # One report per completed check, whatever it found: the allowance
        # counts how often the operator is told the whole story. A check that
        # gave up mid-scan already cost a failure slot, and spending a report
        # slot on its partial reading is how three flaky checks end the warnings
        # without ever having named the tasks.
        if not whole:
            return
        if missing or unplanned:
            self._schema_reports += 1
        else:
            self._schema_checked = True

    def _probe_timeout(self):
        """What one phase of the next request may take.

        Bounded by what is left of the check, so a request that overran cannot
        push the sequence past the deadline. Raising once the budget is gone
        ends the check as a failed attempt, to be retried later rather than run
        to completion at any cost.
        """

        if self._stop.is_set():
            # The check makes several requests and the shutdown join is two
            # seconds; a diagnostic does not get to outlive the writer it runs
            # on, nor to keep talking to InfluxDB through shutdown.
            raise _WriterStopping()
        remaining = self._schema_check_deadline - _now()
        if remaining <= 0:
            raise TimeoutError("schema check budget exhausted")
        phase = max(
            0.1, min(SCHEMA_CHECK_REQUEST_TIMEOUT_SECONDS, remaining / 2)
        )

        return (phase, phase)

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
