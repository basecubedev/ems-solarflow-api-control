# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the native InfluxDB telemetry writer (ems.history.influx_writer).

Covers the task's requirements: analytics-compatible line protocol, the
background queue/worker, failure isolation + reconnect, non-blocking enqueue
(queue overflow drops instead of blocking), and the controller hook being a
no-op when the writer is disabled.
"""

import contextlib
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


import pytest

from ems.history.influx_writer import (
    SCHEMA_CHECK_FAILURES,
    SCHEMA_CHECK_REPORTS,
    InfluxTelemetryWriter,
    build_telemetry_lines,
)
from ems.history.schema import planned_bucket_names

pytestmark = [
    pytest.mark.integration,
]


CONFIG = {
    "enabled": True,
    "url": "http://127.0.0.1:8086",
    "org": "ems",
    "token": "test-token",
    "token_env": "INFLUXDB_TOKEN",
    "bucket_prefix": "ems",
}


class _Dev:
    def __init__(self, name):
        self.name = name


def _state(**kw):
    base = {"solar": 1000, "output": 400, "soc": 55, "pack_in": 0, "pack_out": 200}
    base.update(kw)
    return SimpleNamespace(**base)


class FakeClient:
    """Records writes; can be flipped to fail to exercise failure isolation."""

    def __init__(
        self,
        present=("ems_raw", "ems_1m", "ems_5m", "ems_1h"),
        tasks=("ems-downsample-1m", "ems-downsample-5m", "ems-downsample-1h"),
    ):
        self.writes = []
        self.fail = False
        self.lock = threading.Lock()
        self.present = set(present)
        self.tasks = list(tasks)
        self.looked_up = []
        self.task_timeouts = []

    def write_lines(self, bucket, lines):
        if self.fail:
            raise RuntimeError("influx unavailable")
        with self.lock:
            self.writes.append((bucket, list(lines)))

    def find_bucket(self, name, timeout=None):
        with self.lock:
            self.looked_up.append(name)
        return {"id": "1"} if name in self.present else None

    def list_tasks(self, limit=500, timeout=None):
        with self.lock:
            self.task_timeouts.append(timeout)
        return [{"name": name, "status": "active"} for name in self.tasks]


def _wait_until(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- line protocol / analytics compatibility -------------------------------


def test_build_lines_cover_all_analytics_series():
    lines = build_telemetry_lines(
        [_Dev("WR1"), _Dev("WR2")],
        [_state(), _state(solar=600, output=480, soc=57, pack_in=30, pack_out=200)],
        {"WR1": True, "WR2": True},
        748.0,
        target=900.0,
        timestamp_ns=1_000_000_000,
    )
    text = "\n".join(lines)
    # zendure_device with the five fields every Analytics series reads.
    assert text.count("zendure_device") == 2
    for field in ("solar=", "output=", "soc=", "pack_in=", "pack_out="):
        assert field in text
    # grid + home series come from shelly_meter: grid is meter exchange power,
    # house_load = max(0, inverter_total + grid_power) = (400 + 480) + 748.
    assert "shelly_meter" in text
    assert "grid_power=748" in text
    assert "house_load=1628" in text
    # target series comes from the ems_runtime measurement.
    assert "ems_runtime" in text
    assert "target_output=900" in text
    assert "device=WR1" in text and "device=WR2" in text


def test_build_lines_grid_power_export_clamps_house_load_to_zero():
    # Strong export (negative grid) below total inverter output -> house load 0.
    lines = build_telemetry_lines(
        [_Dev("WR1")],
        [_state(output=400)],
        {"WR1": True},
        -900.0,
        timestamp_ns=1_000_000_000,
    )
    text = "\n".join(lines)
    assert "grid_power=-900" in text
    assert "house_load=0" in text
    # No target supplied -> no ems_runtime point.
    assert "ems_runtime" not in text


def test_build_lines_marks_offline_devices_unavailable():
    lines = build_telemetry_lines(
        [_Dev("WR1")],
        [None],
        {"WR1": False},
        None,
        timestamp_ns=1_000_000_000,
    )
    text = "\n".join(lines)
    assert "zendure_device" in text
    assert "available=" in text
    # An offline device must not emit telemetry fields.
    assert "solar=" not in text
    # No meter reading -> no shelly_meter line.
    assert "shelly_meter" not in text


# -- background worker -----------------------------------------------------


def test_writer_flushes_batches_to_client():
    client = FakeClient()
    writer = InfluxTelemetryWriter(CONFIG, client_factory=lambda: client)
    writer.start()
    try:
        writer.enqueue(["zendure_device,device=WR1 solar=1000 1"])
        assert _wait_until(lambda: client.writes)
        bucket, lines = client.writes[0]
        assert bucket == "ems_raw"
        assert lines == ["zendure_device,device=WR1 solar=1000 1"]
    finally:
        writer.stop()


def test_writer_survives_influx_failure_and_reconnects():
    failing = FakeClient()
    failing.fail = True
    healthy = FakeClient()
    clients = [failing, healthy]

    def factory():
        return clients.pop(0) if clients else healthy

    writer = InfluxTelemetryWriter(
        CONFIG, client_factory=factory, max_backoff_s=0.05, error_log_interval_s=0
    )
    writer.start()
    try:
        # The first attempt hits the failing client (error contained, client
        # dropped); subsequent batches reconnect to the healthy client. Keep
        # feeding batches until the healthy client receives one (or timeout).
        def fed():
            writer.enqueue(["line a=1 1"])
            return bool(healthy.writes)

        assert _wait_until(fed, timeout=4.0)
    finally:
        writer.stop()
    # The writer never raised and recovered onto the healthy client.
    assert healthy.writes


def test_enqueue_is_non_blocking_and_drops_on_overflow():
    # No worker started: the queue fills and further enqueues must drop, never
    # block or raise, so the control loop is never stalled by a slow InfluxDB.
    writer = InfluxTelemetryWriter(CONFIG, client_factory=FakeClient, max_queue=2)
    start = time.time()
    for i in range(50):
        writer.enqueue([f"line v={i} {i}"])
    elapsed = time.time() - start
    assert elapsed < 1.0  # clearly non-blocking
    assert writer._dropped > 0


def test_writer_unconfigured_without_token_does_not_crash():
    cfg = dict(CONFIG)
    cfg["token"] = ""
    cfg["token_env"] = "DEFINITELY_UNSET_INFLUX_TOKEN_ENV"
    writer = InfluxTelemetryWriter(
        cfg, max_backoff_s=0.05, error_log_interval_s=0
    )
    writer.start()
    try:
        writer.enqueue(["line a=1 1"])
        # Should keep running (worker alive) despite no usable client.
        time.sleep(0.2)
        assert writer._thread is not None and writer._thread.is_alive()
    finally:
        writer.stop()


# -- the schema gap is named once ------------------------------------------

# A schema sync that ran while InfluxDB was still starting leaves the raw bucket
# (the first start creates it) and nothing else. Telemetry then keeps arriving
# while every Analytics range that reads a downsampled bucket is empty, which is
# how a live appliance stayed like that for a day without a single log line.
@contextlib.contextmanager
def caplog_at_warning():
    """Collect formatted warning records, independent of pytest's caplog.

    The clock is patched inside these tests, and caplog's own handling stays
    clear of that; a plain handler keeps the two from interacting.
    """

    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Handler(level=logging.WARNING)
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


DOWNSAMPLED_CONFIG = dict(
    CONFIG,
    downsampling=[
        {"source": "raw", "target": "1m", "window": "1m"},
        {"source": "1m", "target": "5m", "window": "5m"},
        {"source": "5m", "target": "1h", "window": "1h"},
    ],
)


class _Refusing(FakeClient):
    """Refuses bucket lookups until ``refuse`` is cleared."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.refuse = True
        self.attempts = 0

    def find_bucket(self, name, timeout=None):
        if self.refuse:
            if name == planned_bucket_names(DOWNSAMPLED_CONFIG)[0]:
                self.attempts += 1
            raise OSError("connection reset")
        return super().find_bucket(name, timeout=timeout)


def test_a_schema_that_was_never_synced_is_named(caplog):
    client = FakeClient(present=("ems_raw",), tasks=())
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with caplog.at_level(logging.WARNING):
        writer._report_schema_gap_once(client)

    assert "event=influx_schema_incomplete" in caplog.text
    assert "missing_buckets=ems_1m,ems_5m,ems_1h" in caplog.text
    assert "influx sync" in caplog.text


def test_buckets_without_their_tasks_are_named_too(caplog):
    """`sync` creates buckets before tasks, so it can stop between the two.

    Every bucket is then in place and nothing fills them: the query succeeds,
    the chart is empty, and `influx status` reports no missing buckets. Only
    the tasks give it away.
    """

    client = FakeClient(tasks=("ems-downsample-1m",))
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with caplog.at_level(logging.WARNING):
        writer._report_schema_gap_once(client)

    assert "event=influx_schema_incomplete" in caplog.text
    assert "missing_tasks=ems-downsample-5m,ems-downsample-1h" in caplog.text
    assert "missing_buckets" not in caplog.text


def test_a_bucket_only_a_query_profile_names_is_reported_separately(caplog):
    """Same symptom, opposite fix, so it cannot share the sentence.

    Nothing cross-checks `query_profiles` against `downsampling`, and a profile
    naming a bucket no downsampling entry produces names one that no sync will
    ever create. Sending the operator to `influx sync` there is a loop, and
    scoring it as complete -- which a check built only from the downsampling
    plan does -- means the EMS never mentions it at all.
    """

    config = dict(
        DOWNSAMPLED_CONFIG,
        query_profiles=[
            {"max_range": "24h", "bucket": "1m", "window": "1m"},
            {"max_range": "30d", "bucket": "10m", "window": "10m"},
        ],
    )
    client = FakeClient()
    writer = InfluxTelemetryWriter(config, client_factory=lambda: client)

    with caplog.at_level(logging.WARNING):
        writer._report_schema_gap_once(client)

    assert "event=influx_schema_bucket_not_planned" in caplog.text
    assert "buckets=ems_10m" in caplog.text
    assert "query_profiles" in caplog.text
    assert "event=influx_schema_incomplete" not in caplog.text
    assert writer._schema_checked is False


def test_the_budget_covers_the_profile_buckets_it_also_checks():
    """A bucket added to the check has to be given time to be checked."""

    import ems.history.influx_writer as writer_module

    config = dict(
        DOWNSAMPLED_CONFIG,
        query_profiles=[{"max_range": "30d", "bucket": "10m", "window": "10m"}],
    )

    assert writer_module.schema_check_budget_seconds(
        config
    ) > writer_module.schema_check_budget_seconds(DOWNSAMPLED_CONFIG)


def test_the_gap_is_not_named_on_every_batch(caplog):
    """It is written from the worker's write path, which runs every cycle."""

    client = FakeClient(present=("ems_raw",), tasks=())
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with caplog.at_level(logging.WARNING):
        writer._report_schema_gap_once(client)
        caplog.clear()
        writer._report_schema_gap_once(client)

    assert caplog.text == ""
    assert client.looked_up == ["ems_raw", "ems_1m", "ems_5m", "ems_1h"]


def test_a_gap_seen_mid_sync_is_not_latched(caplog):
    """`sync` creates the buckets one at a time, and a write can land between.

    The reading is true at that instant and wrong a second later. Latching it
    would leave a false warning standing for the life of the process, so only a
    complete schema ends the checking.
    """

    import ems.history.influx_writer as writer_module

    clock = {"now": 0.0}
    client = FakeClient(present=("ems_raw", "ems_1m"), tasks=())
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with patch.object(writer_module, "_now", lambda: clock["now"]):
        with caplog.at_level(logging.WARNING):
            writer._report_schema_gap_once(client)
        assert "event=influx_schema_incomplete" in caplog.text

        client.present = {"ems_raw", "ems_1m", "ems_5m", "ems_1h"}
        client.tasks = list(FakeClient().tasks)
        clock["now"] += writer_module.SCHEMA_CHECK_RETRY_SECONDS
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            writer._report_schema_gap_once(client)
            writer._report_schema_gap_once(client)

    assert caplog.text == ""
    assert writer._schema_checked is True


def test_a_task_that_exists_but_is_switched_off_fills_nothing(caplog):
    """An inactive task is the "buckets exist, nothing fills them" case.

    `sync` deactivates tasks it no longer wants, so a name alone does not say
    the pipeline is running; counting it as present leaves no signal anywhere.
    """

    class Inactive(FakeClient):
        def list_tasks(self, limit=500, timeout=None):
            return [
                {"name": "ems-downsample-1m", "status": "active"},
                {"name": "ems-downsample-5m", "status": "inactive"},
                {"name": "ems-downsample-1h", "status": "active"},
            ]

    client = Inactive()
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with caplog.at_level(logging.WARNING):
        writer._report_schema_gap_once(client)

    assert "missing_tasks=ems-downsample-5m" in caplog.text


def test_a_complete_schema_says_nothing(caplog):
    client = FakeClient()
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with caplog.at_level(logging.WARNING):
        writer._report_schema_gap_once(client)

    assert "influx_schema_incomplete" not in caplog.text


def test_an_unexpected_failure_in_the_check_does_not_stop_the_writer():
    """The check runs on the thread that writes telemetry.

    Its own lookups are guarded, but the rest of it is not, and a diagnostic
    that can kill the writer thread costs the installation its history -- a far
    worse outcome than the empty Analytics tab it was added to explain.
    """

    import ems.history.influx_writer as writer_module

    client = FakeClient()
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)
    writer.start()
    try:
        with patch.object(
            writer_module,
            "planned_bucket_names",
            side_effect=RuntimeError("config went away"),
        ):
            writer.enqueue(["line a=1 1"])
            assert _wait_until(lambda: client.writes)

        writer.enqueue(["line a=1 2"])
        assert _wait_until(lambda: len(client.writes) > 1)
        assert writer._thread is not None and writer._thread.is_alive()
    finally:
        writer.stop()


def test_a_token_that_may_not_read_the_schema_is_told_once(caplog):
    """The check reads metadata the writer itself never needed.

    An install whose token is scoped to writes is healthy; the check simply
    cannot run there. Retrying a refusal ten times prints ten warnings about a
    fault that is not one and will never answer differently.
    """

    class Forbidden(FakeClient):
        def find_bucket(self, name, timeout=None):
            error = RuntimeError("403 Client Error: Forbidden")
            error.response = SimpleNamespace(status_code=403)
            raise error

    client = Forbidden()
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with caplog.at_level(logging.WARNING):
        writer._report_schema_gap_once(client)
        first = caplog.text
        caplog.clear()
        writer._report_schema_gap_once(client)

    assert "event=influx_schema_check_not_permitted" in first
    assert "Writing telemetry is unaffected" in first
    assert caplog.text == ""
    assert writer._schema_failures == 0


def test_a_check_that_fails_never_reaches_the_control_loop(caplog):
    client = _Refusing()
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with caplog.at_level(logging.WARNING):
        writer._report_schema_gap_once(client)

    assert "event=influx_schema_check_failed" in caplog.text


def test_a_lookup_that_fails_while_influx_starts_does_not_silence_the_warning(caplog):
    """The writes land before InfluxDB has finished coming up on a Pi.

    A bucket lookup can still fail there, and treating that one refusal as the
    answer would hide an incomplete schema for the life of the process -- the
    exact silence this event exists to break.
    """

    import ems.history.influx_writer as writer_module

    clock = {"now": 0.0}
    client = _Refusing()
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with patch.object(writer_module, "_now", lambda: clock["now"]):
        writer._report_schema_gap_once(client)

        client.refuse = False
        client.present = {"ems_raw"}
        client.tasks = []
        clock["now"] += writer_module.SCHEMA_CHECK_RETRY_SECONDS

        with caplog.at_level(logging.WARNING):
            writer._report_schema_gap_once(client)

    assert "event=influx_schema_incomplete" in caplog.text


def test_a_check_that_keeps_failing_gives_up_instead_of_probing_every_cycle():
    """It runs on the writer's success path, which is every control cycle."""

    import ems.history.influx_writer as writer_module

    clock = {"now": 0.0}
    client = _Refusing()
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with patch.object(writer_module, "_now", lambda: clock["now"]):
        for _ in range(500):
            writer._report_schema_gap_once(client)
            clock["now"] += 5.0

    assert client.attempts == SCHEMA_CHECK_FAILURES


def test_a_check_that_cannot_run_does_not_spend_the_reporting_allowance():
    """Two different limits, and conflating them is how the signal goes silent.

    A lookup that fails while InfluxDB is still settling says nothing about the
    schema. Counting it against the times an incomplete schema may be named
    would let a fault that outlasts a couple of minutes end the reporting
    before a single reading was ever taken.
    """

    import ems.history.influx_writer as writer_module

    clock = {"now": 0.0}
    client = _Refusing()
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with patch.object(writer_module, "_now", lambda: clock["now"]):
        for _ in range(SCHEMA_CHECK_REPORTS + 1):
            writer._report_schema_gap_once(client)
            clock["now"] += writer_module.SCHEMA_CHECK_RETRY_SECONDS

        client.refuse = False
        client.present = {"ems_raw"}
        client.tasks = []

        with caplog_at_warning() as records:
            writer._report_schema_gap_once(client)

    assert any("event=influx_schema_incomplete" in line for line in records)


def test_a_check_that_gave_up_halfway_does_not_spend_a_report():
    """A partial reading has already cost a failure slot.

    Charging it a report slot as well lets three flaky checks end the warnings
    without ever having named the downsampling tasks -- which the docs call the
    only signal for a sync that stopped between the buckets and the tasks.
    """

    class HalfWay(FakeClient):
        def list_tasks(self, limit=500, timeout=None):
            raise OSError("connection reset")

    client = HalfWay(present=("ems_raw",), tasks=())
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with caplog_at_warning() as records:
        writer._report_schema_gap_once(client)

    assert any("missing_buckets=" in line for line in records)
    assert writer._schema_failures == 1
    assert writer._schema_reports == 0


def test_a_writer_asked_to_stop_does_not_finish_its_diagnostic():
    """The check makes several requests; the shutdown join is two seconds.

    A diagnostic does not get to outlive the writer it runs on, nor to keep
    talking to InfluxDB through shutdown while the queue is dropped.
    """

    client = FakeClient(present=("ems_raw",), tasks=())
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)
    writer._stop.set()

    with caplog_at_warning() as records:
        writer._report_schema_gap_once(client)

    assert client.looked_up == []
    # A shutdown is not a fault: nothing to report, and nothing held against the
    # check's allowance either.
    assert records == []
    assert writer._schema_failures == 0


def test_the_gap_between_checks_is_measured_from_the_end(caplog):
    """A check can spend the better part of a minute on a slow InfluxDB.

    Measured from its start, the next one follows almost immediately and the
    writer thread spends most of every minute in a diagnostic, with telemetry
    queued behind it.
    """

    import ems.history.influx_writer as writer_module

    clock = {"now": 0.0}
    budget = writer_module.schema_check_budget_seconds(DOWNSAMPLED_CONFIG)

    class Slow(FakeClient):
        def find_bucket(self, name, timeout=None):
            clock["now"] += budget / 8
            return super().find_bucket(name, timeout=timeout)

    client = Slow(present=("ems_raw",), tasks=())
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with patch.object(writer_module, "_now", lambda: clock["now"]):
        writer._report_schema_gap_once(client)
        spent = clock["now"]

        assert writer._schema_check_retry_at >= (
            spent + writer_module.SCHEMA_CHECK_RETRY_SECONDS
        )


def test_one_check_costs_one_report_however_much_it_found(caplog):
    """The allowance counts how often the operator is told, not what was wrong.

    A config that is both unsynced and self-contradictory produces two lines,
    and spending two of three reports on one reading would stop the warnings a
    round early and print each of them twice on the way.
    """

    config = dict(
        DOWNSAMPLED_CONFIG,
        query_profiles=[{"max_range": "30d", "bucket": "10m", "window": "10m"}],
    )
    client = FakeClient(present=("ems_raw",), tasks=())
    writer = InfluxTelemetryWriter(config, client_factory=lambda: client)

    with caplog.at_level(logging.WARNING):
        writer._report_schema_gap_once(client)

    assert "event=influx_schema_incomplete" in caplog.text
    assert "event=influx_schema_bucket_not_planned" in caplog.text
    assert writer._schema_reports == 1


def test_the_retries_are_spread_over_minutes_not_over_three_batches():
    """Three probes on three successive batches are gone in fifteen seconds.

    A lookup fault that outlasts that -- InfluxDB still settling after the first
    writes land -- would then silence the warning for the life of the process,
    which is the failure mode this event was added to end.
    """

    import ems.history.influx_writer as writer_module

    clock = {"now": 0.0}
    client = _Refusing()
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with patch.object(writer_module, "_now", lambda: clock["now"]):
        # Three batches at the default write interval: fifteen seconds.
        for _ in range(3):
            writer._report_schema_gap_once(client)
            clock["now"] += 5.0

        assert client.attempts == 1

    covered = writer_module.SCHEMA_CHECK_RETRY_SECONDS * SCHEMA_CHECK_FAILURES

    assert covered >= 600


def test_every_request_in_the_check_is_held_to_the_same_budget():
    """One unbudgeted call makes the budget on the others decorative.

    The task listing runs on the writer thread beside the bucket lookups; left
    on the client default it can stall for three times their budget. Each gets
    an equal share of the check's budget, split across connect and read because
    a scalar is spent twice.
    """

    import ems.history.influx_writer as writer_module

    client = FakeClient(present=("ems_raw",), tasks=())
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    writer._report_schema_gap_once(client)

    phase = writer_module.SCHEMA_CHECK_REQUEST_TIMEOUT_SECONDS

    assert client.task_timeouts == [(phase, phase)]


def test_a_slow_influx_still_gets_as_far_as_the_task_list():
    """The buckets must not be able to eat the whole budget.

    A budget that does not allow every planned request is spent on the lookups
    and never asks about the tasks -- and a schema whose buckets all exist while
    its tasks were never created has no other signal anywhere.
    """

    import ems.history.influx_writer as writer_module

    clock = {"now": 0.0}
    per_request = 2 * writer_module.SCHEMA_CHECK_REQUEST_TIMEOUT_SECONDS

    class Slow(FakeClient):
        """Every request takes the whole tolerance it is allowed."""

        def find_bucket(self, name, timeout=None):
            clock["now"] += per_request
            return super().find_bucket(name, timeout=timeout)

        def list_tasks(self, limit=500, timeout=None):
            clock["now"] += per_request
            return super().list_tasks(limit=limit, timeout=timeout)

    client = Slow(tasks=())
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with patch.object(writer_module, "_now", lambda: clock["now"]):
        with caplog_at_warning() as records:
            writer._report_schema_gap_once(client)

    assert client.task_timeouts, "the task list was never reached"
    assert any("missing_tasks=" in line for line in records)


def test_the_writer_is_as_tolerant_as_the_check_that_asks_the_same_questions():
    """The same lookups, the same InfluxDB, the same board.

    The Admin status check gives them a per-phase budget; deriving a stricter
    one here by dividing a flat total made ordinary lookups on a Pi time out,
    and a lookup that times out reports nothing at all.
    """

    from ems.history.influx_writer import SCHEMA_CHECK_REQUEST_TIMEOUT_SECONDS
    from ems.influx_setup import INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS

    assert SCHEMA_CHECK_REQUEST_TIMEOUT_SECONDS == INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS


def test_the_budget_allows_every_request_the_check_makes():
    """Derived from the config, so a longer chain gets a longer check.

    A fixed total gives each request less as the downsampling chain grows --
    exactly backwards, since the chain is what adds the requests.
    """

    import ems.history.influx_writer as writer_module

    short = dict(CONFIG, downsampling=[{"source": "raw", "target": "1m", "window": "1m"}])
    per_request = 2 * writer_module.SCHEMA_CHECK_REQUEST_TIMEOUT_SECONDS

    for config, requests in ((short, 3), (DOWNSAMPLED_CONFIG, 5)):
        assert writer_module.schema_check_budget_seconds(config) == (
            requests * per_request
        )


def test_the_whole_check_is_one_budget_not_one_per_request():
    """Five requests at five seconds each is not a ten-second check.

    It runs on the writer thread with telemetry queued behind it, so the
    sequence needs the single deadline the provider's twin path already has.
    """

    import ems.history.influx_writer as writer_module

    clock = {"now": 0.0}
    budget = writer_module.schema_check_budget_seconds(DOWNSAMPLED_CONFIG)

    class Crawling(FakeClient):
        def find_bucket(self, name, timeout=None):
            clock["now"] += budget / 2
            return super().find_bucket(name, timeout=timeout)

    client = Crawling(present=("ems_raw",), tasks=())
    writer = InfluxTelemetryWriter(DOWNSAMPLED_CONFIG, client_factory=lambda: client)

    with patch.object(writer_module, "_now", lambda: clock["now"]):
        writer._report_schema_gap_once(client)

    # Two requests spend the budget; the remaining buckets and the task list
    # are not attempted, and the attempt is retried later instead.
    assert client.looked_up == ["ems_raw", "ems_1m"]
    assert client.task_timeouts == []
    assert writer._schema_checked is False


def test_the_schema_is_only_checked_once_a_write_has_succeeded():
    """A check that runs before InfluxDB answers is the fault it looks for.

    An unreachable InfluxDB and a missing bucket are indistinguishable from the
    outside, so the check is worth nothing until a write has proven the
    connection and the token. It therefore hangs off the writer's success path
    rather than off startup.
    """

    wrote = threading.Event()

    class Gated(FakeClient):
        def write_lines(self, bucket, lines):
            super().write_lines(bucket, lines)
            wrote.set()

    failing = Gated(present=("ems_raw",), tasks=())
    failing.fail = True
    healthy = Gated(present=("ems_raw",), tasks=())
    clients = [failing, healthy]

    writer = InfluxTelemetryWriter(
        DOWNSAMPLED_CONFIG,
        client_factory=lambda: clients.pop(0) if clients else healthy,
        max_backoff_s=0.05,
        error_log_interval_s=0,
    )
    writer.start()
    try:
        def fed():
            writer.enqueue(["line a=1 1"])
            return wrote.is_set()

        assert _wait_until(fed, timeout=4.0)
    finally:
        writer.stop()

    assert failing.looked_up == []
    assert healthy.looked_up[0] == "ems_raw"


# -- controller hook -------------------------------------------------------


def test_controller_publish_to_influx_noop_when_disabled():
    from ems.controller import EMSController

    fake = SimpleNamespace(influx_writer=None, devices=[_Dev("WR1")], device_online={})
    # Must not raise and must not require a writer.
    EMSController.publish_to_influx(fake, 200.0, [_state()])


def test_controller_publish_to_influx_enqueues_built_lines():
    from ems.controller import EMSController

    captured = []

    class Sink:
        def enqueue(self, lines):
            captured.append(list(lines))

    fake = SimpleNamespace(
        influx_writer=Sink(),
        devices=[_Dev("WR1")],
        device_online={"WR1": True},
    )
    EMSController.publish_to_influx(fake, 748.0, [_state()], target=900.0)
    assert captured and any("zendure_device" in line for line in captured[0])
    assert any("grid_power=748" in line for line in captured[0])
    assert any("house_load=" in line for line in captured[0])
    assert any("target_output=900" in line for line in captured[0])


# -- write cadence: Influx every loop, SQLite at write_interval ------------


class _CountingSink:
    def __init__(self):
        self.batches = []

    def enqueue(self, lines):
        self.batches.append(list(lines))


class _CountingStore:
    def __init__(self):
        self.records = []

    def record(self, snapshot):
        self.records.append(snapshot)


def _dashboard_fake(store, writer):
    import types

    from ems.controller import EMSController

    fake = SimpleNamespace(
        dashboard_store=store,
        influx_writer=writer,
        devices=[_Dev("WR1")],
        device_online={"WR1": True},
        _last_dashboard_publish=0,
        _last_influx_publish=0,
    )
    # Bind the real publish_to_influx so publish_to_dashboard can call it.
    fake.publish_to_influx = types.MethodType(
        EMSController.publish_to_influx, fake
    )
    return fake


def _publish_dashboard(fake):
    from ems.controller import EMSController

    EMSController.publish_to_dashboard(
        fake,
        200.0,  # load
        [_state()],  # states
        [400],  # targets
        [400],  # effective_targets
        400,  # allocated_total
        400,  # effective_total
        True,  # enabled
        1800,  # max_power
        50,  # min_output_limit
    )


def test_influx_writes_every_loop_while_sqlite_throttled(monkeypatch):
    """Influx is enqueued every loop even when the SQLite write is throttled."""
    import dashboard.telemetry as telemetry
    from ems import config as cfg

    monkeypatch.setattr(
        telemetry, "build_dashboard_snapshot", lambda *a, **k: {"snap": True}
    )
    # Long SQLite interval, default (every-loop) Influx cadence.
    monkeypatch.setattr(cfg, "DASHBOARD_CONFIG", {"write_interval_seconds": 100})
    monkeypatch.setattr(cfg, "INFLUXDB_CONFIG", {"raw_write_interval_seconds": 0})

    store = _CountingStore()
    writer = _CountingSink()
    fake = _dashboard_fake(store, writer)

    _publish_dashboard(fake)
    _publish_dashboard(fake)
    _publish_dashboard(fake)

    # Influx enqueued on every loop; SQLite only once (within the 100s window).
    assert len(writer.batches) == 3
    assert len(store.records) == 1


def test_raw_write_interval_throttles_influx(monkeypatch):
    """A positive raw_write_interval_seconds throttles raw writes."""
    import dashboard.telemetry as telemetry
    from ems import config as cfg

    monkeypatch.setattr(
        telemetry, "build_dashboard_snapshot", lambda *a, **k: {"snap": True}
    )
    monkeypatch.setattr(cfg, "DASHBOARD_CONFIG", {"write_interval_seconds": 0})
    monkeypatch.setattr(
        cfg, "INFLUXDB_CONFIG", {"raw_write_interval_seconds": 100}
    )

    store = _CountingStore()
    writer = _CountingSink()
    fake = _dashboard_fake(store, writer)

    _publish_dashboard(fake)
    _publish_dashboard(fake)
    _publish_dashboard(fake)

    # SQLite writes every loop (interval 0) but Influx is throttled to once.
    assert len(writer.batches) == 1
    assert len(store.records) == 3


def test_influx_failure_does_not_stop_publish(monkeypatch):
    """An Influx enqueue error is contained and never reaches the loop."""
    import dashboard.telemetry as telemetry
    from ems import config as cfg

    monkeypatch.setattr(
        telemetry, "build_dashboard_snapshot", lambda *a, **k: {"snap": True}
    )
    monkeypatch.setattr(cfg, "DASHBOARD_CONFIG", {"write_interval_seconds": 0})
    monkeypatch.setattr(cfg, "INFLUXDB_CONFIG", {"raw_write_interval_seconds": 0})

    class _BoomSink:
        def enqueue(self, lines):
            raise RuntimeError("influx boom")

    store = _CountingStore()
    fake = _dashboard_fake(store, _BoomSink())

    # Must not raise; SQLite still records.
    _publish_dashboard(fake)
    assert len(store.records) == 1


# -- runtime credential wiring: bundled native host ------------------------


def _bundled_writer_config():
    from ems.config import normalize_influxdb_config

    return normalize_influxdb_config(
        {
            "enabled": True,
            "mode": "bundled",
            "bucket_prefix": "ems",
            "url": "http://influxdb:8086",
            "host_url": "http://127.0.0.1:8086",
        }
    )


def test_build_client_bundled_native_uses_host_url_and_secret_token(
    tmp_path, monkeypatch
):
    from ems import influx_setup
    import ems.history.influx_client as influx_client_mod

    cfg = _bundled_writer_config()
    monkeypatch.setattr(influx_setup, "BASE_DIR", str(tmp_path))
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    monkeypatch.delenv("INFLUXDB_TOKEN", raising=False)
    influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    secret = influx_setup.read_secret_file_token(cfg, base_dir=str(tmp_path))

    captured = {}

    class _RecordingClient:
        def __init__(self, url, org, token):
            captured.update(url=url, org=org, token=token)

    monkeypatch.setattr(
        influx_client_mod, "HistoryInfluxClient", _RecordingClient
    )

    writer = InfluxTelemetryWriter(cfg)
    client = writer._build_client()
    assert isinstance(client, _RecordingClient)
    assert captured["url"] == "http://127.0.0.1:8086"
    assert captured["org"] == cfg["org"]
    assert captured["token"] == secret
    assert secret


def test_build_client_returns_none_without_usable_token(tmp_path, monkeypatch):
    from ems import influx_setup

    cfg = _bundled_writer_config()
    monkeypatch.setattr(influx_setup, "BASE_DIR", str(tmp_path))
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    monkeypatch.delenv("INFLUXDB_TOKEN", raising=False)
    # No secret file -> bundled fallback yields no token.

    writer = InfluxTelemetryWriter(cfg)
    assert writer._build_client() is None


def test_build_client_factory_override_is_unchanged():
    sentinel = object()
    writer = InfluxTelemetryWriter(CONFIG, client_factory=lambda: sentinel)
    assert writer._build_client() is sentinel
