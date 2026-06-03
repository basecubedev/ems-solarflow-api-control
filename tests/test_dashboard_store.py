import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot
from dashboard.sqlite_store import DashboardStore, empty_snapshot


def snapshot(timestamp, pv=100, output=80, target=90):
    return {
        "timestamp": timestamp,
        "devices": {
            "WR1": {
                "soc": 61,
                "pv_input_w": pv,
                "output_w": output,
                "battery_power_w": -20,
                "target_w": target,
                "output_limit_w": 100,
            }
        },
        "grid_power_w": 12,
        "home_load_w": output + 12,
        "pv_total_w": pv,
        "inverter_output_w": output,
        "battery_power_w": -20,
        "average_soc": 61,
        "controller": {
            "enabled": True,
            "max_total_power_w": 800,
            "min_output_limit_w": 35,
            "allocated_target_total_w": target,
            "effective_target_total_w": target,
            "commanded_total_w": target,
            "filtered_load_w": 12,
            "night_min_soc_idle": False,
        },
        "rules": {
            "ems_enabled": {
                "active": True,
                "reason": "active",
            }
        },
        "control_explain": None,
    }


def daily_row(path, date_key):
    with sqlite3.connect(path) as con:
        return con.execute(
            """
            SELECT
                inverter_output_wh,
                savings_value,
                price_per_kwh,
                currency,
                peak_output_w,
                sample_count
            FROM daily_energy_stats
            WHERE date = ?
            """,
            (date_key,),
        ).fetchone()


def insert_daily(path, date_key, wh, savings=0, peak=0, sample_count=1):
    with sqlite3.connect(path) as con:
        con.execute(
            """
            INSERT INTO daily_energy_stats(
                date,
                inverter_output_wh,
                savings_value,
                price_per_kwh,
                currency,
                peak_output_w,
                sample_count,
                updated_at
            )
            VALUES(?, ?, ?, 0.35, 'EUR', ?, ?, ?)
            """,
            (
                date_key,
                wh,
                savings,
                peak,
                sample_count,
                f"{date_key}T12:00:00+00:00",
            ),
        )


def test_store_records_latest_and_history(tmp_path):
    store = DashboardStore(tmp_path / "dashboard.sqlite", retention_hours=48)
    timestamp = datetime.now(timezone.utc).isoformat()

    store.record(snapshot(timestamp, pv=345))

    assert store.latest()["pv_total_w"] == 345
    assert store.history("1h")[0]["devices"]["WR1"]["soc"] == 61
    assert store.latest()["control_explain"] is None


def test_latest_refreshes_energy_stats_from_daily_aggregates(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    DashboardStore(path)
    insert_daily(path, "2026-05-31", 1000, savings=1)
    timestamp = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc).isoformat()

    with sqlite3.connect(path) as con:
        stale_snapshot = snapshot(timestamp)
        stale_snapshot["energy_stats"] = {
            "enabled": True,
            "currency": "EUR",
            "lifetime": {
                "inverter_output_wh": 0,
                "inverter_output_kwh": 0,
                "savings_value": 0,
            },
        }
        con.execute(
            "INSERT INTO snapshots(timestamp, payload) VALUES(?, ?)",
            (timestamp, json.dumps(stale_snapshot)),
        )

    store = DashboardStore(path)
    latest = store.latest()

    assert latest["energy_stats"]["lifetime"]["inverter_output_wh"] == 1000
    assert latest["energy_stats"]["lifetime"]["since_date"] == "2026-05-31"


def test_store_cleanup_uses_retention(tmp_path):
    store = DashboardStore(tmp_path / "dashboard.sqlite", retention_hours=1)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()

    store.record(snapshot(old, pv=10))
    store.record(snapshot(fresh, pv=20))

    history = store.history("24h")

    assert [item["pv_total_w"] for item in history] == [20]


def test_empty_snapshot_exposes_null_control_explain():
    assert empty_snapshot()["control_explain"] is None


def test_energy_first_sample_stores_timestamp_without_wh(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "price_per_kwh": 0.35},
    )
    timestamp = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc).isoformat()

    store.record(snapshot(timestamp, output=400))

    row = daily_row(path, "2026-06-01")
    assert row[0] == 0
    assert row[1] == 0
    assert row[2] == 0.35
    assert row[3] == "EUR"
    assert row[4] == 400
    assert row[5] == 1

    with sqlite3.connect(path) as con:
        state = con.execute(
            """
            SELECT value FROM energy_integration_state
            WHERE key = 'last_sample_timestamp'
            """
        ).fetchone()

    assert state[0] == timestamp


def test_energy_integration_uses_actual_elapsed_seconds(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True})
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    store.record(snapshot(first.isoformat(), output=400))
    store.record(snapshot((first + timedelta(seconds=5)).isoformat(), output=400))

    row = daily_row(path, "2026-06-01")
    assert row[0] == pytest.approx(400 * 5 / 3600)
    assert row[5] == 2


def test_energy_integration_uses_measured_output_not_control_target(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True})
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        _dashboard_capabilities=[],
        commanded_total_w=800,
        filtered_load_w=0,
        last_control_explanation=None,
    )
    state = SimpleNamespace(
        solar=0,
        output=100,
        pack_in=0,
        pack_out=0,
        soc=50,
        output_limit=800,
    )
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    for offset in (0, 5):
        item = build_dashboard_snapshot(
            controller,
            0,
            [state],
            [800],
            [800],
            800,
            800,
            enabled=True,
            max_total_power=800,
            min_output_limit=35,
        )
        item["timestamp"] = (first + timedelta(seconds=offset)).isoformat()
        store.record(item)

    row = daily_row(path, "2026-06-01")
    assert row[0] == pytest.approx(100 * 5 / 3600)
    assert row[0] != pytest.approx(800 * 5 / 3600)


def test_energy_large_delta_is_skipped(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "max_sample_delta_seconds": 60},
    )
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    store.record(snapshot(first.isoformat(), output=500))
    store.record(snapshot((first + timedelta(hours=1)).isoformat(), output=500))

    row = daily_row(path, "2026-06-01")
    assert row[0] == 0
    assert row[4] == 500
    assert row[5] == 2


def test_energy_same_day_aggregation_updates_peak_and_savings(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "price_per_kwh": 0.50},
    )
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    store.record(snapshot(first.isoformat(), output=100))
    store.record(snapshot((first + timedelta(seconds=5)).isoformat(), output=400))
    store.record(snapshot((first + timedelta(seconds=10)).isoformat(), output=800))

    expected_wh = (400 * 5 / 3600) + (800 * 5 / 3600)
    row = daily_row(path, "2026-06-01")
    assert row[0] == pytest.approx(expected_wh)
    assert row[1] == pytest.approx((expected_wh / 1000) * 0.50)
    assert row[4] == 800
    assert row[5] == 3


def test_energy_price_change_preserves_historical_daily_savings(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "price_per_kwh": 0.35},
    )
    store.record(snapshot(first.isoformat(), output=1000))
    store.record(snapshot((first + timedelta(seconds=5)).isoformat(), output=1000))

    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "price_per_kwh": 0.42},
    )
    store.record(snapshot(day_two.isoformat(), output=1000))
    store.record(snapshot((day_two + timedelta(seconds=5)).isoformat(), output=1000))

    day_one_row = daily_row(path, "2026-06-01")
    day_two_row = daily_row(path, "2026-06-02")
    assert day_one_row[2] == 0.35
    assert day_two_row[2] == 0.42

    expected_day_one_savings = ((1000 * 5 / 3600) / 1000) * 0.35
    expected_day_two_savings = ((1000 * 5 / 3600) / 1000) * 0.42
    summary = store.energy_summary(now=day_two.isoformat())
    assert summary["lifetime"]["savings_value"] == pytest.approx(
        expected_day_one_savings + expected_day_two_savings
    )


def test_energy_rolling_summaries_and_best_day(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    DashboardStore(path)
    insert_daily(path, "2026-06-29", 1000, savings=1, peak=400)
    insert_daily(path, "2026-06-28", 2000, savings=2, peak=500)
    insert_daily(path, "2026-06-23", 3000, savings=3, peak=600)
    insert_daily(path, "2026-06-22", 4000, savings=4, peak=700)
    insert_daily(path, "2025-07-01", 5000, savings=5, peak=800)
    insert_daily(path, "2025-06-29", 6000, savings=6, peak=900)

    store = DashboardStore(path)
    summary = store.energy_summary(
        now=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    )

    assert summary["today"]["inverter_output_wh"] == 1000
    assert summary["today"]["peak_output_w"] == 400
    assert summary["yesterday"]["inverter_output_wh"] == 2000
    assert summary["yesterday"]["peak_output_w"] == 500
    assert summary["last_7_days"]["inverter_output_wh"] == 6000
    assert summary["last_4_weeks"]["inverter_output_wh"] == 10000
    assert summary["last_12_months"]["inverter_output_wh"] == 15000
    assert summary["lifetime"]["inverter_output_wh"] == 21000
    assert summary["lifetime"]["since_date"] == "2025-06-29"
    assert summary["best_day"]["date"] == "2025-06-29"
    assert summary["best_day"]["inverter_output_wh"] == 6000


def test_energy_yesterday_is_zero_when_previous_day_is_missing(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    DashboardStore(path)
    insert_daily(path, "2026-06-29", 1000, savings=1, peak=400)

    store = DashboardStore(path)
    summary = store.energy_summary(
        now=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    )

    assert summary["today"]["inverter_output_wh"] == 1000
    assert summary["yesterday"]["inverter_output_wh"] == 0
    assert summary["yesterday"]["inverter_output_kwh"] == 0
    assert summary["yesterday"]["savings_value"] == 0
    assert summary["yesterday"]["peak_output_w"] == 0


def test_energy_periods_use_configured_timezone_instead_of_utc(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    DashboardStore(path, energy_savings={"timezone": "Europe/Berlin"})
    insert_daily(path, "2026-06-02", 2000, savings=2, peak=500)
    insert_daily(path, "2026-06-03", 3000, savings=3, peak=600)

    store = DashboardStore(path, energy_savings={"timezone": "Europe/Berlin"})
    summary = store.energy_summary(
        now=datetime(2026, 6, 2, 22, 30, tzinfo=timezone.utc)
    )

    assert summary["today"]["inverter_output_wh"] == 3000
    assert summary["today"]["peak_output_w"] == 600
    assert summary["yesterday"]["inverter_output_wh"] == 2000
    assert summary["yesterday"]["peak_output_w"] == 500


def test_energy_sample_date_key_uses_configured_timezone(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(
        path,
        energy_savings={
            "enabled": True,
            "timezone": "Europe/Berlin",
        },
    )

    store.record(snapshot("2026-06-02T22:30:00+00:00", output=400))

    assert daily_row(path, "2026-06-03") is not None
    assert daily_row(path, "2026-06-02") is None


def test_energy_disabled_summary_includes_zero_yesterday(tmp_path):
    store = DashboardStore(
        tmp_path / "dashboard.sqlite",
        energy_savings={"enabled": False},
    )

    summary = store.energy_summary(
        now=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    )

    assert summary["enabled"] is False
    assert summary["yesterday"] == {
        "inverter_output_wh": 0.0,
        "inverter_output_kwh": 0.0,
        "savings_value": 0.0,
        "peak_output_w": 0.0,
    }


def test_energy_lifetime_since_date_is_null_without_daily_stats(tmp_path):
    store = DashboardStore(tmp_path / "dashboard.sqlite")

    summary = store.energy_summary(
        now=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    )

    assert summary["lifetime"]["inverter_output_wh"] == 0
    assert summary["lifetime"]["since_date"] is None


def test_energy_lifetime_since_date_uses_first_collected_day_across_gaps(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    DashboardStore(path)
    insert_daily(path, "2026-06-01", 1000, savings=1)
    insert_daily(path, "2026-06-08", 2000, savings=2)

    store = DashboardStore(path)
    summary = store.energy_summary(
        now=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    )

    assert summary["lifetime"]["inverter_output_wh"] == 3000
    assert summary["lifetime"]["since_date"] == "2026-06-01"


def test_energy_lifetime_since_date_ignores_zero_sample_days(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    DashboardStore(path)
    insert_daily(path, "2026-05-31", 5000, savings=5, sample_count=0)
    insert_daily(path, "2026-06-01", 1000, savings=1)
    insert_daily(path, "2026-06-02", 2000, savings=2)

    store = DashboardStore(path)
    summary = store.energy_summary(
        now=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    )

    assert summary["lifetime"]["inverter_output_wh"] == 3000
    assert summary["lifetime"]["since_date"] == "2026-06-01"


def test_energy_monthly_and_yearly_summaries(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    DashboardStore(path)
    insert_daily(path, "2025-12-31", 5000, savings=5)
    insert_daily(path, "2026-01-01", 1000, savings=1)
    insert_daily(path, "2026-03-01", 3000, savings=3)
    insert_daily(path, "2026-03-02", 4000, savings=4)

    store = DashboardStore(path)
    summary = store.energy_summary(
        now=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    )

    monthly = summary["monthly_current_year"]
    assert len(monthly) == 12
    assert monthly[0]["month"] == 1
    assert monthly[0]["inverter_output_wh"] == 1000
    assert monthly[1]["inverter_output_wh"] == 0
    assert monthly[2]["inverter_output_wh"] == 7000
    assert summary["yearly"] == [
        {
            "year": 2025,
            "inverter_output_wh": 5000.0,
            "inverter_output_kwh": 5.0,
            "savings_value": 5.0,
        },
        {
            "year": 2026,
            "inverter_output_wh": 8000.0,
            "inverter_output_kwh": 8.0,
            "savings_value": 8.0,
        },
    ]


def test_energy_stats_table_is_created_for_existing_dashboard_database(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    with sqlite3.connect(path) as con:
        con.execute("""
            CREATE TABLE snapshots (
                timestamp TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
        """)

    DashboardStore(path)

    with sqlite3.connect(path) as con:
        table = con.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'daily_energy_stats'
            """
        ).fetchone()

    assert table == ("daily_energy_stats",)
