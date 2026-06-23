# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the native InfluxDB telemetry writer (ems.history.influx_writer).

Covers the task's requirements: analytics-compatible line protocol, the
background queue/worker, failure isolation + reconnect, non-blocking enqueue
(queue overflow drops instead of blocking), and the controller hook being a
no-op when the writer is disabled.
"""

import threading
import time
from types import SimpleNamespace


from ems.history.influx_writer import (
    InfluxTelemetryWriter,
    build_telemetry_lines,
)


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

    def __init__(self):
        self.writes = []
        self.fail = False
        self.lock = threading.Lock()

    def write_lines(self, bucket, lines):
        if self.fail:
            raise RuntimeError("influx unavailable")
        with self.lock:
            self.writes.append((bucket, list(lines)))


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
