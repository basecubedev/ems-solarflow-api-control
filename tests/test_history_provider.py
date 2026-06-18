# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from ems.config import normalize_influxdb_config
from ems.history import (
    HistoryResult,
    SqliteHistoryProvider,
    create_history_provider,
    decimate_history_result,
)
from ems.history.influx_provider import (
    InfluxHistoryProvider,
    build_device_filter,
    build_field_flux,
    parse_series_csv,
    resolve_query_bucket,
    select_query_profile,
)


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class FactoryTest(unittest.TestCase):
    def test_defaults_to_sqlite(self):
        cfg = normalize_influxdb_config({"enabled": False})
        provider = create_history_provider(cfg, "data/x.sqlite")
        self.assertIsInstance(provider, SqliteHistoryProvider)

    def test_influx_when_enabled(self):
        cfg = normalize_influxdb_config({"enabled": True, "token": "t"})
        provider = create_history_provider(
            cfg, "data/x.sqlite", influx_client=object()
        )
        self.assertIsInstance(provider, InfluxHistoryProvider)


class SqliteProviderTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        con = sqlite3.connect(self.db_path)
        con.execute(
            "CREATE TABLE snapshots (timestamp TEXT PRIMARY KEY, payload TEXT)"
        )
        base = _utc(2024, 1, 1, 12, 0)
        for i in range(3):
            ts = (base + timedelta(minutes=i)).isoformat()
            payload = {
                "timestamp": ts,
                "pv_total_w": 100 + i,
                "inverter_output_w": 50 + i,
                "battery_power_w": -20 + i,
                "average_soc": 80 + i,
                "controller": {"commanded_total_w": 200 + i},
            }
            con.execute(
                "INSERT INTO snapshots VALUES (?, ?)", (ts, json.dumps(payload))
            )
        con.commit()
        con.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_available(self):
        self.assertTrue(SqliteHistoryProvider(self.db_path).available())
        self.assertFalse(SqliteHistoryProvider("/nope/x.sqlite").available())

    def test_query_default_series(self):
        provider = SqliteHistoryProvider(self.db_path)
        result = provider.query(_utc(2024, 1, 1, 11), _utc(2024, 1, 1, 13))
        self.assertEqual(result.source, "sqlite")
        self.assertEqual(len(result.time), 3)
        self.assertEqual(set(result.series), {"pv", "output", "battery"})
        self.assertEqual(result.series["pv"], [100.0, 101.0, 102.0])

    def test_query_dotted_field_and_custom_series(self):
        provider = SqliteHistoryProvider(self.db_path)
        result = provider.query(
            _utc(2024, 1, 1, 11),
            _utc(2024, 1, 1, 13),
            series=["target", "soc"],
        )
        self.assertEqual(result.series["target"], [200.0, 201.0, 202.0])
        self.assertEqual(result.series["soc"], [80.0, 81.0, 82.0])

    def test_query_time_window_excludes_out_of_range(self):
        provider = SqliteHistoryProvider(self.db_path)
        result = provider.query(
            _utc(2024, 1, 1, 12, 1), _utc(2024, 1, 1, 13)
        )
        self.assertEqual(len(result.time), 2)

    def test_unavailable_provider_returns_empty_result(self):
        provider = SqliteHistoryProvider("/nope/x.sqlite")
        result = provider.query(_utc(2024, 1, 1), _utc(2024, 1, 2))
        self.assertTrue(result.meta.get("unavailable"))
        self.assertEqual(result.time, [])


class DecimateTest(unittest.TestCase):
    def _result(self, count):
        return HistoryResult(
            source="sqlite",
            start=_utc(2026, 1, 1),
            end=_utc(2026, 1, 2),
            window="raw",
            time=list(range(count)),
            series={"pv": list(range(count)), "output": list(range(count))},
        )

    def test_no_op_when_under_limit(self):
        result = decimate_history_result(self._result(100), max_points=2000)
        self.assertEqual(len(result.time), 100)
        self.assertNotIn("decimated", result.meta)

    def test_caps_points_and_keeps_last(self):
        result = decimate_history_result(self._result(10000), max_points=2000)
        self.assertLessEqual(len(result.time), 2000)
        self.assertEqual(len(result.series["pv"]), len(result.time))
        self.assertEqual(len(result.series["output"]), len(result.time))
        self.assertEqual(result.time[0], 0)
        self.assertEqual(result.time[-1], 9999)  # last sample preserved
        self.assertTrue(result.meta["decimated"])
        self.assertEqual(result.meta["point_count"], len(result.time))


class QueryProfileTest(unittest.TestCase):
    def setUp(self):
        self.cfg = normalize_influxdb_config({"enabled": True})

    def test_select_profile_picks_smallest_covering(self):
        profiles = self.cfg["query_profiles"]
        profile_1h = select_query_profile(profiles, 3600)
        self.assertEqual(profile_1h["bucket"], "raw")  # 1h -> 1h profile
        self.assertEqual(profile_1h["window"], "1s")
        self.assertEqual(
            select_query_profile(profiles, 4 * 3600)["bucket"], "raw"
        )  # 4h -> 6h profile
        self.assertEqual(
            select_query_profile(profiles, 4 * 3600)["window"], "10s"
        )
        self.assertEqual(
            select_query_profile(profiles, 20 * 3600)["bucket"], "1m"
        )  # 20h -> 24h profile
        self.assertEqual(
            select_query_profile(profiles, 20 * 86400)["bucket"], "5m"
        )  # 20d -> 30d profile

    def test_select_profile_falls_back_to_coarsest(self):
        profiles = self.cfg["query_profiles"]
        self.assertEqual(
            select_query_profile(profiles, 1000 * 86400)["bucket"], "1h"
        )

    def test_select_profile_none_when_empty(self):
        self.assertIsNone(select_query_profile([], 3600))

    def test_resolve_query_bucket(self):
        bucket_key, window = resolve_query_bucket(self.cfg, 3600)
        self.assertEqual((bucket_key, window), ("raw", "1s"))

    def test_resolve_query_bucket_6h_uses_coarser_window(self):
        bucket_key, window = resolve_query_bucket(self.cfg, 6 * 3600)
        self.assertEqual((bucket_key, window), ("raw", "10s"))

    def test_zoom_style_small_range_selects_finer_bucket_than_wide_range(self):
        # A wide 30d view uses a coarse bucket; zooming into a sub-1h window must
        # resolve to the fine raw bucket with the small 1s window (the backend
        # stays the source of truth for bucket selection when the frontend
        # re-queries the zoomed range from end - start).
        wide_bucket, _ = resolve_query_bucket(self.cfg, 30 * 86400)
        self.assertEqual(wide_bucket, "5m")
        # Zoom into 45 minutes -> 1h raw/1s detail profile.
        zoom_bucket, zoom_window = resolve_query_bucket(self.cfg, 45 * 60)
        self.assertEqual((zoom_bucket, zoom_window), ("raw", "1s"))
        # Zoom into 2 hours -> 6h raw/10s profile.
        zoom2_bucket, zoom2_window = resolve_query_bucket(self.cfg, 2 * 3600)
        self.assertEqual((zoom2_bucket, zoom2_window), ("raw", "10s"))


class DeviceFilterTest(unittest.TestCase):
    def test_no_devices_yields_empty_clause(self):
        self.assertEqual(build_device_filter(None), "")
        self.assertEqual(build_device_filter([]), "")

    def test_single_device(self):
        clause = build_device_filter(["WR1"])
        self.assertIn('r.device == "WR1"', clause)

    def test_multiple_devices_use_or(self):
        clause = build_device_filter(["WR1", "WR2"])
        self.assertIn('r.device == "WR1" or r.device == "WR2"', clause)

    def test_device_name_is_escaped(self):
        clause = build_device_filter(['WR"x'])
        self.assertIn(r'r.device == "WR\"x"', clause)

    def test_field_flux_includes_device_filter_when_scoped(self):
        flux = build_field_flux(
            "ems_raw", "zendure_device", "solar", "1m",
            _utc(2024, 1, 1), _utc(2024, 1, 2),
            devices=["WR1"], device_scoped=True, collapse="sum",
        )
        self.assertIn('from(bucket: "ems_raw")', flux)
        self.assertIn('r.device == "WR1"', flux)
        self.assertIn("aggregateWindow(every: 1m", flux)
        self.assertIn("|> sum()", flux)
        self.assertIn("2024-01-01T00:00:00Z", flux)

    def test_field_flux_skips_device_filter_when_not_scoped(self):
        flux = build_field_flux(
            "ems_raw", "shelly_meter", "house_load", "1m",
            _utc(2024, 1, 1), _utc(2024, 1, 2),
            devices=["WR1"], device_scoped=False, collapse="mean",
        )
        self.assertNotIn("r.device", flux)
        self.assertIn("|> mean()", flux)


SAMPLE_CSV = (
    ",result,table,_time,_value,_field,_measurement\r\n"
    ",_result,0,2024-01-01T00:00:00Z,100,solar,zendure_device\r\n"
    ",_result,0,2024-01-01T00:01:00Z,150,solar,zendure_device\r\n"
)


class CsvParseTest(unittest.TestCase):
    def test_parse_series_csv(self):
        points = parse_series_csv(SAMPLE_CSV)
        epoch0 = int(_utc(2024, 1, 1, 0, 0).timestamp())
        epoch1 = int(_utc(2024, 1, 1, 0, 1).timestamp())
        self.assertEqual(points, {epoch0: 100.0, epoch1: 150.0})


class FakeQueryClient:
    """Returns canned CSV keyed by the _field referenced in the flux."""

    def __init__(self, field_csv):
        self.field_csv = field_csv
        self.queries = []

    def query_raw(self, flux, accept="application/csv"):
        self.queries.append(flux)
        for field, csv_text in self.field_csv.items():
            if f'r._field == "{field}"' in flux:
                return csv_text
        return ",result,table,_time,_value\r\n"


def _csv(field, rows):
    header = ",result,table,_time,_value,_field,_measurement\r\n"
    body = "".join(
        f",_result,0,{ts},{value},{field},zendure_device\r\n"
        for ts, value in rows
    )
    return header + body


class InfluxQueryTest(unittest.TestCase):
    def setUp(self):
        self.cfg = normalize_influxdb_config(
            {"enabled": True, "bucket_prefix": "ems"}
        )

    def test_query_builds_aligned_series(self):
        client = FakeQueryClient(
            {
                "solar": _csv(
                    "solar",
                    [("2024-01-01T00:00:00Z", 100), ("2024-01-01T00:01:00Z", 120)],
                ),
                "output": _csv(
                    "output",
                    [("2024-01-01T00:00:00Z", 60)],
                ),
            }
        )
        provider = InfluxHistoryProvider(self.cfg, client=client)
        result = provider.query(
            _utc(2024, 1, 1, 0, 0),
            _utc(2024, 1, 1, 1, 0),
            series=["pv", "output"],
        )
        self.assertEqual(result.source, "influxdb")
        # union of timestamps from both series
        self.assertEqual(len(result.time), 2)
        self.assertEqual(result.series["pv"], [100.0, 120.0])
        # output has no point at the second timestamp -> None gap
        self.assertEqual(result.series["output"][0], 60.0)
        self.assertIsNone(result.series["output"][1])

    def test_battery_series_is_derived(self):
        client = FakeQueryClient(
            {
                "pack_out": _csv("pack_out", [("2024-01-01T00:00:00Z", 200)]),
                "pack_in": _csv("pack_in", [("2024-01-01T00:00:00Z", 50)]),
            }
        )
        provider = InfluxHistoryProvider(self.cfg, client=client)
        result = provider.query(
            _utc(2024, 1, 1, 0, 0),
            _utc(2024, 1, 1, 1, 0),
            series=["battery"],
        )
        self.assertEqual(result.series["battery"], [150.0])

    def test_grid_series_maps_to_meter_grid_power(self):
        client = FakeQueryClient(
            {"grid_power": _csv("grid_power", [("2024-01-01T00:00:00Z", -120)])}
        )
        provider = InfluxHistoryProvider(self.cfg, client=client)
        result = provider.query(
            _utc(2024, 1, 1, 0, 0),
            _utc(2024, 1, 1, 1, 0),
            series=["grid"],
        )
        self.assertEqual(result.series["grid"], [-120.0])
        self.assertIn('r._measurement == "shelly_meter"', client.queries[0])
        self.assertIn('r._field == "grid_power"', client.queries[0])

    def test_target_series_maps_to_ems_runtime_target_output(self):
        client = FakeQueryClient(
            {"target_output": _csv("target_output", [("2024-01-01T00:00:00Z", 640)])}
        )
        provider = InfluxHistoryProvider(self.cfg, client=client)
        result = provider.query(
            _utc(2024, 1, 1, 0, 0),
            _utc(2024, 1, 1, 1, 0),
            series=["target"],
        )
        self.assertEqual(result.series["target"], [640.0])
        self.assertIn('r._measurement == "ems_runtime"', client.queries[0])
        self.assertIn('r._field == "target_output"', client.queries[0])

    def test_query_uses_profile_bucket(self):
        client = FakeQueryClient({})
        provider = InfluxHistoryProvider(self.cfg, client=client)
        # 20h range -> 24h profile -> 1m bucket
        result = provider.query(
            _utc(2024, 1, 1, 0, 0),
            _utc(2024, 1, 1, 20, 0),
            series=["pv"],
        )
        self.assertEqual(result.meta["bucket"], "ems_1m")
        self.assertTrue(client.queries[0].startswith('from(bucket: "ems_1m")'))


if __name__ == "__main__":
    unittest.main()


# --- runtime credential wiring: bundled native host ------------------------

from ems import influx_setup as _influx_setup  # noqa: E402
import ems.history.influx_client as _influx_client_mod  # noqa: E402


def _bundled_provider_config():
    return normalize_influxdb_config(
        {
            "enabled": True,
            "mode": "bundled",
            "bucket_prefix": "ems",
            "url": "http://influxdb:8086",
            "host_url": "http://127.0.0.1:8086",
        }
    )


def test_provider_available_bundled_native_with_secret_file(tmp_path, monkeypatch):
    cfg = _bundled_provider_config()
    monkeypatch.setattr(_influx_setup, "BASE_DIR", str(tmp_path))
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    monkeypatch.delenv("INFLUXDB_TOKEN", raising=False)
    _influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))

    provider = InfluxHistoryProvider(cfg)
    assert provider.available() is True


def test_provider_unavailable_when_no_token_or_secret(tmp_path, monkeypatch):
    cfg = _bundled_provider_config()
    monkeypatch.setattr(_influx_setup, "BASE_DIR", str(tmp_path))
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    monkeypatch.delenv("INFLUXDB_TOKEN", raising=False)
    # No secret file written under tmp_path -> no bundled fallback token.

    provider = InfluxHistoryProvider(cfg)
    assert provider.available() is False


def test_provider_client_uses_resolved_host_url_and_token(tmp_path, monkeypatch):
    cfg = _bundled_provider_config()
    monkeypatch.setattr(_influx_setup, "BASE_DIR", str(tmp_path))
    monkeypatch.setenv("EMS_IN_CONTAINER", "0")
    monkeypatch.delenv("INFLUXDB_TOKEN", raising=False)
    _influx_setup.ensure_secret_file(cfg, base_dir=str(tmp_path))
    secret = _influx_setup.read_secret_file_token(cfg, base_dir=str(tmp_path))

    captured = {}

    class _RecordingClient:
        def __init__(self, url, org, token):
            captured.update(url=url, org=org, token=token)

    monkeypatch.setattr(
        _influx_client_mod, "HistoryInfluxClient", _RecordingClient
    )

    provider = InfluxHistoryProvider(cfg)
    provider.client()
    assert captured["url"] == "http://127.0.0.1:8086"
    assert captured["org"] == cfg["org"]
    assert captured["token"] == secret
    assert secret
