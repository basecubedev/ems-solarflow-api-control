# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keeps the analytics schema in place from inside the running EMS.

The setup paths reconcile the InfluxDB schema once, and a fresh appliance
install does not reach that step at all: InfluxDB and the EMS start together,
InfluxDB's own setup creates only the raw bucket, and the downsampled buckets
and their tasks are never made. A sync that runs too early, or is killed, leaves
the same gap, and nothing ever asked again.

The keeper runs the safe ``schema.sync``: shortly after the EMS starts, again on
a widening ladder until InfluxDB accepts it, and then at a long interval, so a
bucket or task that goes missing later comes back. Safe means it creates what is
missing and deletes nothing -- an appliance whose raw bucket has kept months of
telemetry forever keeps it until an operator syncs on purpose. A schema already
in place costs a handful of lookups and no writes.

Every schema write on the host takes the schema lock (``schema_sync_lock``), so
the keeper and an ``emsctl influx sync`` -- the operator's, or the Admin's
one-off -- never create the same task side by side. The keeper does not wait for
it: a held lock means a sync is already running, and it asks again shortly.

It is off when ``influxdb.auto_sync`` is false, works on its own thread, and
never raises into the control loop.
"""

import logging
import threading

from ems.file_io import FileLockTimeout
from ems.history import schema
from ems.logging_utils import log_event

FIRST_ATTEMPT_SECONDS = 20.0
RECHECK_SECONDS = 30 * 60.0
RETRY_SECONDS = (30.0, 60.0, 120.0, 300.0, 600.0)
# A board InfluxDB takes about half a minute to answer after a cold start, so
# the first refusals are expected and said at info level.
QUIET_FAILURES = 3
# Every failure up to here is logged; after it only every LOG_EVERY-th, which at
# the top of the ladder is one line an hour.
LOGGED_FAILURES = 5
LOG_EVERY = 6

SCHEMA_LOCK_FILENAME = "influx-schema.lock"

UNAUTHORIZED_HINT = (
    "InfluxDB does not accept the token, so the EMS cannot complete the "
    "analytics schema. It changes when InfluxDB's data is reset or restored; "
    "check influxdb.token or the secret file against the running InfluxDB."
)
FORBIDDEN_HINT = (
    "The InfluxDB token may not create buckets or tasks, so the EMS cannot "
    "complete the analytics schema. Use a token with write access to the "
    "organisation, or set influxdb.auto_sync to false and manage the schema "
    "by hand."
)
TASK_KEPT_HINT = (
    "A downsampling task is switched off or differs from the config. The EMS "
    "leaves a task as it is once it exists; to reconcile it, run: "
    "python3 emsctl.py influx sync"
)
RETENTION_KEPT_HINT = (
    "The bucket keeps data longer than influxdb.retention says. Shortening it "
    "would delete the older data, so the EMS leaves it as it is. To apply the "
    "configured retention, run: python3 emsctl.py influx sync"
)


def keeper_wanted(influx_config):
    """Whether this config lets the EMS reconcile the schema on its own."""

    if not isinstance(influx_config, dict) or not influx_config.get("enabled"):
        return False
    return influx_config.get("auto_sync") is not False


def schema_lock_path():
    from ems.paths import resolve_data_dir

    return resolve_data_dir() / SCHEMA_LOCK_FILENAME


def schema_sync_lock(timeout):
    """The host-wide lock every schema write takes; FileLockTimeout once ``timeout`` passes."""

    from ems.file_io import exclusive_file_lock

    return exclusive_file_lock(str(schema_lock_path()), timeout=timeout)


def _refusal_status(exc):
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if status in (401, 403) else None


def _changes(report):
    """The actions of a sync report that changed something, as log fields."""

    fields = {}
    for section, action, key in (
        ("buckets", "created", "created_buckets"),
        ("buckets", "updated", "updated_buckets"),
        ("tasks", "created", "created_tasks"),
    ):
        names = [entry["name"] for entry in report.get(section, []) if entry.get("action") == action]
        if names:
            fields[key] = ",".join(names)
    return fields


class InfluxSchemaKeeper:
    """Reconciles the analytics schema now, until it works, and then now and then."""

    def __init__(
        self,
        influx_config,
        *,
        client_factory=None,
        lock=schema_sync_lock,
        first_attempt_s=FIRST_ATTEMPT_SECONDS,
    ):
        self.config = influx_config
        self._client_factory = client_factory
        self._lock = lock
        self._first_attempt_s = first_attempt_s
        self._stop = threading.Event()
        self._thread = None
        self._failures = 0
        self._refused = None
        self._kept_buckets = set()
        self._kept_tasks = set()
        self._unchecked_buckets = set()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="influx-schema-keeper", daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if not thread.is_alive():
                self._thread = None

    def _run(self):
        delay = self._first_attempt_s
        while not self._stop.wait(delay):
            delay = self.reconcile()

    def _build_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from ems.history.influx_client import HistoryInfluxClient
        from ems.influx_setup import (
            INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS,
            runtime_influx_token,
            runtime_influx_url,
        )

        url = runtime_influx_url(self.config)
        token = runtime_influx_token(self.config)
        if not url or not token:
            return None
        return HistoryInfluxClient(
            url, self.config["org"], token, timeout=INFLUX_SYNC_REQUEST_TIMEOUT_SECONDS
        )

    def reconcile(self):
        """One round. Returns the seconds until the next one; never raises."""

        try:
            client = self._build_client()
            if client is None:
                return self._failed("no_token", "no InfluxDB URL or token yet")
            with self._lock(0):
                report = schema.sync(
                    client, self.config, safe=True, known_kept=frozenset(self._kept_buckets)
                )
        except FileLockTimeout:
            return RETRY_SECONDS[0]
        except Exception as exc:
            status = _refusal_status(exc)
            if status is not None:
                return self._not_permitted(status)
            return self._failed(type(exc).__name__, str(exc))

        changes = _changes(report)
        if changes:
            log_event(logging.INFO, "influx_schema_synced", **changes)
        self._report_kept(report)
        self._failures = 0
        self._refused = None
        return RECHECK_SECONDS

    def _report_kept(self, report):
        """Say once per bucket and task what the safe sync leaves as it is."""

        for bucket in report.get("buckets", []):
            name = bucket.get("name")
            if bucket.get("action") != "kept" or name in self._kept_buckets:
                continue
            if bucket.get("reason") != "older_data":
                if name not in self._unchecked_buckets:
                    self._unchecked_buckets.add(name)
                    log_event(
                        logging.INFO,
                        "influx_schema_retention_unchecked",
                        bucket=name,
                        hint="Could not check the bucket for older data; asked again next round.",
                    )
                continue
            self._kept_buckets.add(name)
            log_event(
                logging.WARNING,
                "influx_schema_retention_kept",
                bucket=name,
                retention_seconds=bucket.get("live_retention_seconds"),
                configured_seconds=bucket.get("retention_seconds"),
                hint=RETENTION_KEPT_HINT,
            )
        tasks = [
            task.get("name")
            for task in report.get("tasks", [])
            if task.get("action") == "kept" and task.get("name") not in self._kept_tasks
        ]
        if tasks:
            self._kept_tasks.update(tasks)
            log_event(logging.WARNING, "influx_schema_task_kept", tasks=",".join(tasks), hint=TASK_KEPT_HINT)

    def _retry_delay(self):
        return RETRY_SECONDS[min(self._failures, len(RETRY_SECONDS)) - 1]

    def _failed(self, error, detail):
        self._failures += 1
        delay = self._retry_delay()
        if self._failures <= LOGGED_FAILURES or self._failures % LOG_EVERY == 0:
            log_event(
                logging.INFO if self._failures <= QUIET_FAILURES else logging.WARNING,
                "influx_schema_sync_failed",
                error=error,
                detail=detail,
                attempt=self._failures,
                retry_in_s=int(delay),
            )
        return delay

    def _not_permitted(self, status):
        if self._refused != status:
            self._refused = status
            log_event(
                logging.WARNING,
                "influx_schema_sync_not_permitted",
                status=status,
                hint=UNAUTHORIZED_HINT if status == 401 else FORBIDDEN_HINT,
            )
        self._failures = len(RETRY_SECONDS)
        return RETRY_SECONDS[-1]


def start_schema_keeper(influx_config):
    """Start a keeper when the config allows one. Returns it, or None; never raises."""

    if not keeper_wanted(influx_config):
        return None
    try:
        keeper = InfluxSchemaKeeper(influx_config)
        keeper.start()
    except Exception as exc:
        log_event(logging.WARNING, "influx_schema_keeper_start_failed", error=exc)
        return None
    return keeper
