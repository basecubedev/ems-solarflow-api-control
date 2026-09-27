# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard.telemetry import build_dashboard_snapshot
from dashboard.sqlite_store import DashboardStore, empty_snapshot
from ems.energy_channels import ENERGY_CHANNELS

pytestmark = [
    pytest.mark.integration,
]


def snapshot(timestamp, pv=100, output=80, target=90, grid=12, battery=-20):
    return {
        "timestamp": timestamp,
        "devices": {
            "WR1": {
                "soc": 61,
                "pv_input_w": pv,
                "output_w": output,
                "battery_power_w": battery,
                "target_w": target,
                "output_limit_w": 100,
            }
        },
        "grid_power_w": grid,
        "home_load_w": max(0, output + grid),
        "pv_total_w": pv,
        "inverter_output_w": output,
        "battery_power_w": battery,
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


def _local_date(store, timestamp):
    return (
        datetime.fromisoformat(timestamp)
        .astimezone(store.energy_timezone)
        .date()
        .isoformat()
    )


def daily_channels(path, date_key):
    columns = ", ".join(channel.column for channel in ENERGY_CHANNELS)
    with sqlite3.connect(path) as con:
        row = con.execute(
            f"SELECT {columns} FROM daily_energy_stats WHERE date = ?",
            (date_key,),
        ).fetchone()

    if row is None:
        return None

    return dict(zip((channel.id for channel in ENERGY_CHANNELS), row))


def channel_gaps(path):
    with sqlite3.connect(path) as con:
        rows = con.execute(
            "SELECT channel, date, missed_samples FROM energy_channel_gap"
        ).fetchall()

    return {(row[0], row[1]): row[2] for row in rows}


def channel_coverage(path):
    with sqlite3.connect(path) as con:
        rows = con.execute(
            "SELECT channel, first_date, last_date FROM energy_channel_coverage"
        ).fetchall()

    return {row[0]: (row[1], row[2]) for row in rows}


def insert_daily(
    path,
    date_key,
    wh,
    savings=0,
    peak=0,
    sample_count=1,
    channels=None,
):
    channel_wh = channels or {}
    columns = "".join(f", {channel.column}" for channel in ENERGY_CHANNELS)
    placeholders = ", ?" * len(ENERGY_CHANNELS)
    with sqlite3.connect(path) as con:
        con.execute(
            f"""
            INSERT INTO daily_energy_stats(
                date,
                inverter_output_wh,
                savings_value,
                price_per_kwh,
                currency,
                peak_output_w,
                sample_count,
                updated_at{columns}
            )
            VALUES(?, ?, ?, 0.35, 'EUR', ?, ?, ?{placeholders})
            """,
            (
                date_key,
                wh,
                savings,
                peak,
                sample_count,
                f"{date_key}T12:00:00+00:00",
                *(
                    float(channel_wh.get(channel.id, 0))
                    for channel in ENERGY_CHANNELS
                ),
            ),
        )


def insert_coverage(path, first_date, last_date, channel_ids=None):
    ids = channel_ids or [channel.id for channel in ENERGY_CHANNELS]
    with sqlite3.connect(path) as con:
        con.executemany(
            """
            INSERT INTO energy_channel_coverage(
                channel, first_date, last_date, updated_at
            )
            VALUES(?, ?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
                first_date = excluded.first_date,
                last_date = excluded.last_date
            """,
            [
                (channel_id, first_date, last_date, f"{last_date}T12:00:00+00:00")
                for channel_id in ids
            ],
        )


def test_store_records_latest_and_history(tmp_path):
    store = DashboardStore(tmp_path / "dashboard.sqlite", retention_hours=48)
    timestamp = datetime.now(timezone.utc).isoformat()

    store.record(snapshot(timestamp, pv=345))

    assert store.latest()["pv_total_w"] == 345
    assert store.history("1h")[0]["devices"]["WR1"]["soc"] == 61
    assert store.latest()["control_explain"] is None


def test_dashboard_database_recreates_configured_data_path(tmp_path):
    db_path = tmp_path / "data" / "ems_dashboard.sqlite"

    DashboardStore(db_path)

    assert db_path.exists()
    sidecars = {
        path.name
        for path in db_path.parent.iterdir()
        if path.name.startswith("ems_dashboard.sqlite")
    }
    assert "ems_dashboard.sqlite" in sidecars
    assert sidecars <= {
        "ems_dashboard.sqlite",
        "ems_dashboard.sqlite-wal",
        "ems_dashboard.sqlite-shm",
    }


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


def test_stored_snapshot_omits_the_energy_rollup(tmp_path):
    """The rollup is derived from the daily table and attached on every read.

    Storing it in each snapshot row wrote a 2 KB aggregate 34k times per
    retention window that nothing ever read back.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "price_per_kwh": 0.35},
    )
    # A stored snapshot is subject to the retention cleanup, so it has to be
    # recent enough to survive the same record() call that writes it.
    timestamp = datetime.now(timezone.utc).isoformat()

    store.record(snapshot(timestamp, output=400))

    with sqlite3.connect(path) as con:
        payload = con.execute("SELECT payload FROM snapshots").fetchone()[0]

    assert "energy_stats" not in json.loads(payload)
    assert json.loads(payload)["pv_total_w"] == 100


def test_latest_carries_the_energy_rollup_after_record(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "price_per_kwh": 0.35},
    )
    insert_daily(path, "2026-06-01", 1000, savings=0.35)
    timestamp = datetime.now(timezone.utc).isoformat()

    store.record(snapshot(timestamp, output=400))

    assert store.latest()["energy_stats"]["enabled"] is True
    assert store.latest()["energy_stats"]["lifetime"]["inverter_output_wh"] == 1000


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


def test_energy_sample_integrates_grid_import_and_export_separately(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True})
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    store.record(snapshot(first.isoformat(), grid=600))
    store.record(snapshot((first + timedelta(seconds=6)).isoformat(), grid=600))
    store.record(snapshot((first + timedelta(seconds=12)).isoformat(), grid=-300))

    channels = daily_channels(path, "2026-06-01")
    assert channels["grid_import"] == pytest.approx(600 * 6 / 3600)
    assert channels["grid_export"] == pytest.approx(300 * 6 / 3600)


def test_energy_sample_integrates_battery_charge_and_discharge_separately(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True})
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    store.record(snapshot(first.isoformat(), battery=900))
    store.record(snapshot((first + timedelta(seconds=10)).isoformat(), battery=900))
    store.record(snapshot((first + timedelta(seconds=20)).isoformat(), battery=-450))

    channels = daily_channels(path, "2026-06-01")
    assert channels["battery_charge"] == pytest.approx(900 * 10 / 3600)
    assert channels["battery_discharge"] == pytest.approx(450 * 10 / 3600)


def test_energy_sample_integrates_pv_and_home_consumption(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True})
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    store.record(snapshot(first.isoformat(), pv=1200, output=400, grid=100))
    store.record(
        snapshot(
            (first + timedelta(seconds=9)).isoformat(),
            pv=1200,
            output=400,
            grid=100,
        )
    )

    channels = daily_channels(path, "2026-06-01")
    assert channels["pv_yield"] == pytest.approx(1200 * 9 / 3600)
    assert channels["home_consumption"] == pytest.approx(500 * 9 / 3600)


def test_energy_large_delta_skips_every_channel_not_only_the_output(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "max_sample_delta_seconds": 60},
    )
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    store.record(snapshot(first.isoformat(), grid=600, battery=600))
    store.record(
        snapshot(
            (first + timedelta(hours=1)).isoformat(),
            grid=600,
            battery=600,
        )
    )

    assert set(daily_channels(path, "2026-06-01").values()) == {0}


def test_energy_channel_columns_are_added_to_an_existing_database(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    with sqlite3.connect(path) as con:
        con.execute("""
            CREATE TABLE daily_energy_stats (
                date TEXT PRIMARY KEY,
                inverter_output_wh REAL NOT NULL DEFAULT 0,
                savings_value REAL NOT NULL DEFAULT 0,
                price_per_kwh REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'EUR',
                peak_output_w REAL NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        con.execute(
            """
            INSERT INTO daily_energy_stats(
                date, inverter_output_wh, savings_value, price_per_kwh,
                currency, peak_output_w, sample_count, updated_at
            )
            VALUES('2026-05-30', 4000, 1.4, 0.35, 'EUR', 700, 900,
                   '2026-05-30T23:59:00+00:00')
            """
        )

    DashboardStore(path)

    assert daily_channels(path, "2026-05-30") == {
        channel.id: 0 for channel in ENERGY_CHANNELS
    }
    assert daily_row(path, "2026-05-30")[0] == 4000


def test_energy_channel_coverage_follows_the_integrated_samples(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "timezone": "UTC"},
    )
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    later = first + timedelta(days=2)

    store.record(snapshot(first.isoformat()))
    store.record(snapshot((first + timedelta(seconds=6)).isoformat()))
    # The gap to the third day is longer than the sample window, so that
    # sample integrates nothing; the one after it does.
    store.record(snapshot(later.isoformat()))
    store.record(snapshot((later + timedelta(seconds=6)).isoformat()))

    coverage = channel_coverage(path)
    assert set(coverage) == {channel.id for channel in ENERGY_CHANNELS}
    assert coverage["grid_import"] == ("2026-06-01", "2026-06-03")


def test_a_skipped_interval_does_not_claim_the_day_as_measured(tmp_path):
    """A day whose samples were all skipped measured nothing.

    Claiming it anyway would make a period look covered while its sums stayed
    at zero, which is the one thing coverage exists to prevent.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "timezone": "UTC"},
    )
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    store.record(snapshot(first.isoformat()))
    store.record(snapshot((first + timedelta(hours=3)).isoformat()))

    assert channel_coverage(path) == {}


def test_an_invalid_grid_reading_is_neither_integrated_nor_covered(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(
        path,
        energy_savings={"enabled": True, "timezone": "UTC"},
    )
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    for seconds in (0, 6, 12):
        sample = snapshot(
            (first + timedelta(seconds=seconds)).isoformat(),
            grid=600,
            battery=900,
        )
        sample["grid_power_valid"] = False
        store.record(sample)

    # The sample is skipped whole, so the battery reading in it goes too.
    assert set(daily_channels(path, "2026-06-01").values()) == {0}
    assert channel_coverage(path) == {}


def test_energy_disabled_store_records_no_channel_coverage(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": False})

    store.record(snapshot(datetime.now(timezone.utc).isoformat()))

    assert channel_coverage(path) == {}


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
    assert summary["yesterday"]["inverter_output_wh"] == 0.0
    assert summary["yesterday"]["inverter_output_kwh"] == 0.0
    assert summary["yesterday"]["savings_value"] == 0.0
    assert summary["yesterday"]["peak_output_w"] == 0.0


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
    assert [
        (
            year["year"],
            year["inverter_output_wh"],
            year["inverter_output_kwh"],
            year["savings_value"],
        )
        for year in summary["yearly"]
    ] == [
        (2025, 5000.0, 5.0, 5.0),
        (2026, 8000.0, 8.0, 8.0),
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


def test_energy_summary_reports_channel_totals_per_period(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    insert_daily(
        path,
        "2026-06-01",
        4000,
        savings=1.4,
        channels={"grid_import": 2500, "grid_export": 150, "home_consumption": 6350},
    )
    insert_coverage(path, "2026-05-01", "2026-06-01")

    summary = store.energy_summary(now="2026-06-01T23:00:00+00:00")

    today = summary["today"]
    assert today["channels"]["grid_import"] == {"wh": 2500.0, "kwh": 2.5}
    assert today["channels"]["grid_export"] == {"wh": 150.0, "kwh": 0.15}
    assert today["coverage"] == {}
    assert today["ratios"]["self_sufficiency"] == pytest.approx(
        (6350 - 2500) / 6350
    )


def test_energy_summary_marks_a_period_that_starts_before_the_measurement(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    insert_daily(path, "2026-01-15", 3000, savings=1.05)
    insert_daily(
        path,
        "2026-06-01",
        4000,
        savings=1.4,
        channels={"grid_import": 2500, "home_consumption": 6350},
    )
    insert_coverage(path, "2026-05-20", "2026-06-01")

    summary = store.energy_summary(now="2026-06-01T23:00:00+00:00")

    assert summary["today"]["coverage"] == {}
    assert summary["last_12_months"]["coverage"]["grid_import"] == "partial"
    assert summary["last_12_months"]["ratios"]["self_sufficiency"] is None
    # The best day is 2026-06-01 here, which is covered; the January day is not.
    assert summary["monthly_current_year"][0]["coverage"]["grid_import"] == "none"
    assert summary["monthly_current_year"][0]["channels"]["grid_import"]["wh"] == 0.0


def test_energy_summary_reports_a_ratio_only_for_a_covered_period(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    insert_daily(
        path,
        "2026-06-01",
        4000,
        channels={"grid_import": 1000, "home_consumption": 5000},
    )
    insert_coverage(path, "2026-06-01", "2026-06-01")

    summary = store.energy_summary(now="2026-06-01T23:00:00+00:00")

    assert summary["today"]["ratios"]["self_sufficiency"] == pytest.approx(0.8)
    assert summary["last_7_days"]["ratios"]["self_sufficiency"] is None
    assert summary["last_7_days"]["coverage"]["grid_import"] == "partial"


def test_energy_summary_channel_meta_lists_every_channel_once(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    insert_coverage(path, "2026-05-20", "2026-06-01")

    meta = store.energy_summary(now="2026-06-01T23:00:00+00:00")["channel_meta"]

    assert [entry["id"] for entry in meta] == [
        channel.id for channel in ENERGY_CHANNELS
    ]
    assert meta[0]["label"] == "Grid Import"
    assert meta[0]["unit"] == "Wh"
    assert meta[0]["since"] == "2026-05-20"
    assert meta[0]["until"] == "2026-06-01"


def test_energy_summary_channel_meta_reports_an_unmeasured_channel(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})

    meta = store.energy_summary(now="2026-06-01T23:00:00+00:00")["channel_meta"]

    assert meta[0]["since"] is None
    assert meta[0]["until"] is None


def test_disabled_energy_summary_zeroes_channels_and_drops_ratios(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": False})
    insert_daily(path, "2026-06-01", 4000, channels={"grid_import": 2500})
    insert_coverage(path, "2026-05-01", "2026-06-01")

    summary = store.energy_summary(now="2026-06-01T23:00:00+00:00")

    for period in ("today", "yesterday", "last_7_days", "last_12_months", "best_day"):
        assert summary[period]["channels"]["grid_import"] == {"wh": 0.0, "kwh": 0.0}
        assert summary[period]["ratios"]["self_sufficiency"] is None
        assert summary[period]["coverage"]["grid_import"] == "none"

    assert summary["monthly_current_year"][0]["channels"]["grid_import"]["wh"] == 0.0
    assert summary["lifetime"]["channels"]["grid_import"]["wh"] == 0.0


def test_energy_summary_month_and_year_entries_carry_channels(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    insert_daily(
        path,
        "2026-06-01",
        4000,
        channels={"grid_import": 2500, "battery_charge": 1800},
    )
    insert_coverage(path, "2026-06-01", "2026-06-01")

    summary = store.energy_summary(now="2026-06-01T23:00:00+00:00")

    june = summary["monthly_current_year"][5]
    assert june["label"] == "Jun"
    assert june["channels"]["battery_charge"]["wh"] == 1800.0
    assert june["coverage"] == {}

    year = summary["yearly"][0]
    assert year["year"] == 2026
    assert year["channels"]["grid_import"]["wh"] == 2500.0
    assert summary["lifetime"]["channels"]["grid_import"]["wh"] == 2500.0


def test_a_restored_database_is_brought_forward_before_the_next_write(tmp_path):
    """The Admin restore swaps the file under a live store.

    A backup written by an older version has no channel columns, and the store
    survives the swap: without a schema pass on the way out, every later
    record() raises on a missing column until someone restarts the EMS.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True})
    legacy = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy) as con:
        con.execute("""
            CREATE TABLE snapshots (
                timestamp TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE daily_energy_stats (
                date TEXT PRIMARY KEY,
                inverter_output_wh REAL NOT NULL DEFAULT 0,
                savings_value REAL NOT NULL DEFAULT 0,
                price_per_kwh REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'EUR',
                peak_output_w REAL NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)

    with store.maintenance_pause():
        for suffix in ("", "-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
        shutil.copyfile(legacy, path)

    timestamp = datetime.now(timezone.utc).isoformat()
    store.record(snapshot(timestamp, output=400))

    assert daily_channels(path, _local_date(store, timestamp)) is not None
    assert store.latest()["energy_stats"]["enabled"] is True


def test_the_current_month_and_year_are_covered_up_to_today(tmp_path):
    """A period in progress is not partly measured -- it is just not over.

    Comparing coverage against the calendar end of the month left the current
    month and year marked as partly measured for their whole duration, which
    withheld the ratios every day of the year.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    for day in range(1, 16):
        insert_daily(
            path,
            f"2026-06-{day:02d}",
            4000 / 15,
            channels={"grid_import": 1000 / 15, "home_consumption": 5000 / 15},
        )
    insert_coverage(path, "2026-06-01", "2026-06-15")

    summary = store.energy_summary(now="2026-06-15T18:00:00+00:00")

    june = summary["monthly_current_year"][5]
    assert june["coverage"] == {}
    assert june["ratios"]["self_sufficiency"] == pytest.approx(0.8)

    # The year is still partial, and for the right reason: it began five months
    # before the channels did. Only the end of a range is clamped to today.
    assert summary["yearly"][0]["coverage"]["grid_import"] == "partial"

    # A month that has not started yet stays outside the measurement.
    july = summary["monthly_current_year"][6]
    assert july["coverage"]["grid_import"] == "none"


def test_a_year_measured_from_its_start_is_covered_up_to_today(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    for day in (1, 2, 3):
        insert_daily(
            path,
            f"2026-01-{day:02d}",
            1000,
            channels={"grid_import": 200, "home_consumption": 1200},
        )
    insert_coverage(path, "2025-12-01", "2026-01-03")

    summary = store.energy_summary(now="2026-01-03T18:00:00+00:00")

    assert summary["yearly"][0]["year"] == 2026
    assert summary["yearly"][0]["coverage"] == {}


def _record_day(store, start, samples, overrides=None):
    """Record evenly spaced samples six seconds apart.

    ``overrides`` patches single samples by index, which is how a test puts one
    unreadable sample inside an otherwise measured day.
    """

    patches = overrides or {}
    for index in range(samples):
        sample = snapshot((start + timedelta(seconds=index * 6)).isoformat())
        sample.update(patches.get(index, {}))
        store.record(sample)


def test_the_first_measured_day_starts_the_measurement(tmp_path):
    """The first sample of all is a start, not a hole.

    Marking it would leave every range that contains the start -- the lifetime,
    the first month, the first calendar year -- incomplete for good, and those
    are the ranges whose ratio a reader keeps. The price is one partly measured
    day inside them; the card carries "Measured since" beside it.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    first = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)

    _record_day(store, first, 3)

    summary = store.energy_summary(now="2026-06-01T00:01:00+00:00")
    assert summary["today"]["coverage"] == {}
    assert summary["lifetime"]["coverage"] == {}
    assert summary["lifetime"]["ratios"]["self_sufficiency"] is not None


def test_a_restart_marks_the_day_even_though_the_first_sample_does_not(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    morning = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)

    _record_day(store, morning, 3)
    # Back after two hours: the interval is skipped, and that is a hole.
    _record_day(store, morning + timedelta(hours=2), 3)

    summary = store.energy_summary(now="2026-06-01T12:00:00+00:00")
    assert summary["today"]["coverage"]["grid_import"] == "partial"
    # The hole is bounded by its day and the figures carry the mark, so the
    # ratio stays: withholding it for every restart would mean no installation
    # ever reads one for a month, a year or its lifetime.
    assert summary["today"]["ratios"]["self_sufficiency"] is not None


def test_a_sample_the_meter_could_not_read_is_counted_as_missed(tmp_path):
    """A meter outage inside an otherwise measured day is a hole in the day.

    Without it, a ten-minute outage would leave the day complete, withhold
    nothing, and report a grid export of zero as a measured figure.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    day = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    _record_day(store, day, 4, {2: {"grid_power_valid": False}})

    missed = channel_gaps(path)
    # One unreadable sample, one hole, in every channel alike.
    assert missed[("grid_import", "2026-06-01")] == 1
    assert missed[("battery_charge", "2026-06-01")] == 1

    summary = store.energy_summary(now="2026-06-01T23:00:00+00:00")
    assert summary["today"]["coverage"]["grid_import"] == "partial"
    assert summary["today"]["ratios"]["self_sufficiency"] is not None


def test_downtime_marks_the_day_it_started_and_the_day_it_ended(tmp_path):
    """A gap over midnight belongs to both days, not only to the later one.

    The evening the samples stopped is as unmeasured as the morning they came
    back, and whole days in between have no row at all -- a range that contains
    one is already incomplete by its day count.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})

    _record_day(store, datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc), 3)
    _record_day(store, datetime(2026, 6, 3, 8, 0, tzinfo=timezone.utc), 3)

    dates = {date_key for _, date_key in channel_gaps(path)}
    assert "2026-06-01" in dates
    assert "2026-06-03" in dates


def test_a_sample_taken_while_a_device_was_away_is_skipped_whole(tmp_path):
    """An offline device keeps its last reading; that is not a measurement.

    The valid meter reading in the same sample goes with it. That is the price
    of one basis for every channel, and it is what keeps the two sides of the
    autarky comparable.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    for index in range(3):
        sample = snapshot(
            (first + timedelta(seconds=index * 6)).isoformat(),
            pv=900,
            battery=600,
        )
        sample["device_power_valid"] = False
        store.record(sample)

    assert set(daily_channels(path, "2026-06-01").values()) == {0}
    assert channel_coverage(path) == {}


def test_a_day_the_ems_never_sampled_keeps_a_period_from_being_complete(tmp_path):
    """A total visibly shrinks when a day is missing; a ratio does not.

    Self-sufficiency over a week with a day of downtime would read as the
    week's figure while being the figure of six days, so a range is only
    complete when every one of its days carries samples.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    for day in (1, 2, 4):  # the third is the outage
        insert_daily(
            path,
            f"2026-06-{day:02d}",
            1000,
            channels={"grid_import": 200, "home_consumption": 1200},
        )
    insert_coverage(path, "2026-06-01", "2026-06-04")

    summary = store.energy_summary(now="2026-06-04T18:00:00+00:00")

    assert summary["today"]["coverage"] == {}
    assert summary["last_7_days"]["coverage"]["grid_import"] == "partial"
    assert summary["last_7_days"]["ratios"]["self_sufficiency"] is None


def test_a_range_that_measured_nothing_reports_no_channels(tmp_path):
    """A day inside the measured span that has no samples measured nothing.

    Reporting it as partly measured would print "0.0 kWh" with a mark for a day
    the EMS never saw, which is the zero-for-unmeasured claim this whole
    accounting exists to prevent.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})

    _record_day(store, datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc), 3)
    _record_day(store, datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc), 3)

    summary = store.energy_summary(now="2026-06-03T18:00:00+00:00")

    assert summary["yesterday"]["coverage"]["grid_import"] == "none"
    assert summary["yesterday"]["channels"]["grid_import"]["wh"] == 0.0
    assert summary["yesterday"]["ratios"]["self_sufficiency"] is None


def test_a_period_missing_a_whole_day_still_withholds_the_ratio(tmp_path):
    """A missing day is not bounded the way a hole inside a day is."""

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})

    _record_day(store, datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc), 3)
    _record_day(store, datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc), 3)

    summary = store.energy_summary(now="2026-06-03T18:00:00+00:00")

    assert summary["last_7_days"]["coverage"]["grid_import"] == "partial"
    assert summary["last_7_days"]["ratios"]["self_sufficiency"] is None


def test_the_summary_is_reused_until_a_sample_changes_it(tmp_path):
    """The live snapshot rebuilds the rollup on every read, once per second."""

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    store.record(snapshot(datetime.now(timezone.utc).isoformat()))

    first = store.energy_summary()
    assert store.energy_summary() is first

    store.record(snapshot(datetime.now(timezone.utc).isoformat()))
    assert store.energy_summary() is not first

    # An explicit timestamp is a question about another moment, never cached.
    assert store.energy_summary(now="2026-06-01T12:00:00+00:00") is not store.energy_summary()


def test_an_outage_marks_every_channel_alike_and_keeps_the_ratio(tmp_path):
    """Whatever the reason, one unreadable sample is one hole in every channel.

    Both sides of the division share a basis, so the bounded-hole tolerance
    applies to a device outage exactly as it does to a meter outage -- and a
    single blip can no longer withhold the figure for good.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    day = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    _record_day(store, day, 6, {3: {"device_power_valid": False}})

    missed = channel_gaps(path)
    assert {channel for channel, _ in missed} == {
        channel.id for channel in ENERGY_CHANNELS
    }
    assert len(set(missed.values())) == 1

    summary = store.energy_summary(now="2026-06-01T23:00:00+00:00")
    assert set(summary["today"]["coverage"].values()) == {"partial"}
    assert summary["today"]["ratios"]["self_sufficiency"] is not None


def test_delivered_energy_follows_the_same_gate_as_the_channels(tmp_path):
    """One rule for every figure on the card.

    The delivered energy used to keep integrating through an outage, which put
    a number built on frozen telemetry next to channels reading "not measured".
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    for index in range(3):
        sample = snapshot((first + timedelta(seconds=index * 6)).isoformat(), output=800)
        sample["device_power_valid"] = False
        store.record(sample)

    assert daily_row(path, "2026-06-01")[0] == 0
    assert daily_row(path, "2026-06-01")[1] == 0


def test_a_missing_day_marks_the_period_and_keeps_its_ratio(tmp_path):
    """A day nobody measured takes the same samples from both sides.

    It is the same case as a hole inside a day, and the figure says so with the
    mark rather than disappearing for good.
    """

    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path, energy_savings={"enabled": True, "timezone": "UTC"})

    # June is measured from its first day; the second is the day nobody saw.
    _record_day(store, datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc), 3)
    _record_day(store, datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc), 3)

    summary = store.energy_summary(now="2026-06-03T18:00:00+00:00")
    june = summary["monthly_current_year"][5]

    assert june["coverage"]["grid_import"] == "partial"
    assert june["ratios"]["self_sufficiency"] is not None
    # A range the channels were not measuring across still has no ratio.
    assert summary["last_12_months"]["ratios"]["self_sufficiency"] is None
