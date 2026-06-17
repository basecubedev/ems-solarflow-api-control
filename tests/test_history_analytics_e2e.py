# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end tests for the History & Analytics feature.

These drive the *real* stack rather than stubs:

- ``test_history_e2e_sqlite_full_stack`` records snapshots through the production
  :class:`~dashboard.sqlite_store.DashboardStore`, starts the real dashboard HTTP
  server, and asserts the columnar ``/api/history/series`` payload that the uPlot
  chart consumes. This is the default, zero-dependency analytics path and always
  runs.
- ``test_history_e2e_influxdb`` exercises the InfluxDB-backed path against a live
  InfluxDB 2.x: it reconciles the schema, writes telemetry line protocol, and
  reads it back through the same HTTP endpoint. It is opt-in and skips unless
  ``EMS_INFLUX_E2E_URL`` / ``EMS_INFLUX_E2E_TOKEN`` (and optionally
  ``EMS_INFLUX_E2E_ORG``) are set, e.g. when the bundled Docker InfluxDB from
  ``develop/influxdb/`` is running.
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from dashboard.server import start_dashboard_server
from dashboard.sqlite_store import DashboardStore


def _get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _serve(store, **kwargs):
    try:
        server = start_dashboard_server(store, host="127.0.0.1", port=0, **kwargs)
    except PermissionError as exc:  # sandboxes may forbid binding sockets
        pytest.skip(f"local socket creation is not permitted: {exc}")
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def _snapshot(ts, pv, output, battery, soc, ac_mode=2):
    """A telemetry snapshot shaped like dashboard/telemetry.py emits."""
    return {
        "timestamp": ts.isoformat(),
        "pv_total_w": pv,
        "inverter_output_w": output,
        "battery_power_w": battery,
        "average_soc": soc,
        "grid_power_w": 0,
        "home_load_w": output,
        "devices": {
            "WR1": {
                "pv_input_w": pv,
                "output_w": output,
                "battery_power_w": battery,
                "soc": soc,
                "target_w": output,
                "output_limit_w": output,
                "ac_mode": ac_mode,
            }
        },
    }


def test_history_e2e_sqlite_full_stack(tmp_path):
    # 1) Record snapshots through the real store (production write path).
    store = DashboardStore(str(tmp_path / "dashboard.sqlite"), retention_hours=48)
    now = datetime.now(timezone.utc)
    samples = [
        _snapshot(now - timedelta(minutes=2), 1000, 400, 200, 50),
        _snapshot(now - timedelta(minutes=1), 1100, 450, -150, 55),
        _snapshot(now, 1200, 500, 300, 60),
    ]
    for snapshot in samples:
        store.record(snapshot)

    # 2) Serve the real dashboard and 3) query the analytics endpoint over HTTP.
    server, base = _serve(store)
    try:
        status, payload = _get_json(
            f"{base}/api/history/series?range=1h&series=pv,output,battery,soc"
        )
        assert status == 200
        assert payload["source"] == "sqlite"
        assert payload["range"] == "1h"
        assert len(payload["time"]) == 3
        assert payload["series"]["pv"] == [1000, 1100, 1200]
        assert payload["series"]["output"] == [400, 450, 500]
        assert payload["series"]["battery"] == [200, -150, 300]
        assert payload["series"]["soc"] == [50, 55, 60]
        # time axis is ascending epoch seconds
        assert payload["time"] == sorted(payload["time"])

        # Device filter reads the per-device snapshot fields end to end.
        status, filtered = _get_json(
            f"{base}/api/history/series?range=1h&series=pv&devices=WR1"
        )
        assert status == 200
        assert filtered["devices"] == ["WR1"]
        assert filtered["series"]["pv"] == [1000, 1100, 1200]

        # Custom date range via explicit epoch bounds.
        start = int((now - timedelta(hours=1)).timestamp())
        end = int((now + timedelta(hours=1)).timestamp())
        status, custom = _get_json(
            f"{base}/api/history/series?start={start}&end={end}&series=pv"
        )
        assert status == 200
        assert custom["range"] == "custom"
        assert len(custom["time"]) == 3
    finally:
        server.shutdown()
        server.server_close()


def _influx_env():
    url = os.environ.get("EMS_INFLUX_E2E_URL")
    token = os.environ.get("EMS_INFLUX_E2E_TOKEN")
    if not url or not token:
        pytest.skip(
            "InfluxDB e2e disabled: set EMS_INFLUX_E2E_URL and "
            "EMS_INFLUX_E2E_TOKEN (and optionally EMS_INFLUX_E2E_ORG)"
        )
    return url, token, os.environ.get("EMS_INFLUX_E2E_ORG", "ems-e2e")


class _InfluxStoreStub:
    """Influx is the active provider here; the store is only a safe fallback."""

    path = None

    def latest(self):
        return {}

    def history(self, range_name):
        return []

    def energy_summary(self):
        return {}


def test_history_e2e_influxdb(tmp_path):
    url, token, org = _influx_env()

    from ems.config import normalize_influxdb_config
    from ems.history import schema
    from ems.history.influx_client import HistoryInfluxClient
    from ems.history.schema import bucket_name
    from scripts.influx_utils import build_line_protocol

    influx_config = normalize_influxdb_config(
        {
            "enabled": True,
            "url": url,
            "org": org,
            "token": token,
            # Test-scoped prefix so we never touch real ems_* buckets.
            "bucket_prefix": "emse2e",
        }
    )

    client = HistoryInfluxClient(url, org, token)

    # 1) Reconcile the schema (idempotent) — creates the emse2e_* buckets/tasks.
    try:
        schema.sync(client, influx_config)
    except Exception as exc:  # pragma: no cover - depends on live infra
        pytest.skip(f"InfluxDB not reachable / schema sync failed: {exc}")

    # 2) Write raw telemetry line protocol for one device.
    raw_bucket = bucket_name(influx_config["bucket_prefix"], "raw")
    now_ns = time.time_ns()
    lines = []
    expected_solar = []
    for i in range(6):
        ts_ns = now_ns - (5 - i) * 20_000_000_000  # 20s apart, last ~now
        solar = 1000 + i * 50
        expected_solar.append(solar)
        lines.append(
            build_line_protocol(
                "zendure_device",
                {"device": "WR1", "source": "zendure"},
                {
                    "solar": float(solar),
                    "output": float(400 + i * 10),
                    "soc": float(50 + i),
                    "pack_out": float(200),
                    "pack_in": float(0),
                },
                ts_ns,
            )
        )
        lines.append(
            build_line_protocol(
                "shelly_meter",
                {"source": "shelly"},
                {"grid_power": float(-50 + i * 10), "house_load": float(300 + i * 5)},
                ts_ns,
            )
        )
        lines.append(
            build_line_protocol(
                "ems_runtime",
                {"source": "ems"},
                {"target_output": float(420 + i * 10)},
                ts_ns,
            )
        )
    client.write_lines(raw_bucket, lines)
    time.sleep(1.0)  # let Influx make the points queryable

    # 3) Read back through the real HTTP endpoint with Influx as the provider.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"influxdb": influx_config}), encoding="utf-8")

    server, base = _serve(_InfluxStoreStub(), config_path=str(config_path))
    try:
        # Analytics status advertises the configured InfluxDB provider.
        status, advertised = _get_json(f"{base}/api/analytics/status")
        assert status == 200
        assert advertised["available"] is True

        status, payload = _get_json(
            f"{base}/api/analytics/series"
            "?range=1h&series=pv,output,soc,battery,grid,home,target&devices=WR1"
        )
        assert status == 200
        assert payload["source"] == "influxdb"
        assert payload["range"] == "1h"
        assert len(payload["time"]) > 0
        pv_values = [v for v in payload["series"]["pv"] if v is not None]
        assert pv_values, "expected at least one PV sample from InfluxDB"
        assert max(pv_values) <= max(expected_solar) + 1
        # battery = pack_out - pack_in = 200
        battery_values = [v for v in payload["series"]["battery"] if v is not None]
        assert battery_values and max(battery_values) == pytest.approx(200, abs=1)
        # grid (meter exchange) and target (EMS output target) are data-backed.
        grid_values = [v for v in payload["series"]["grid"] if v is not None]
        assert grid_values, "expected at least one grid sample from InfluxDB"
        target_values = [v for v in payload["series"]["target"] if v is not None]
        assert target_values, "expected at least one target sample from InfluxDB"
    finally:
        server.shutdown()
        server.server_close()
