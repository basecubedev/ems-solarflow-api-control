from datetime import datetime, timedelta, timezone

from dashboard.sqlite_store import DashboardStore, empty_snapshot


def snapshot(timestamp, pv=100):
    return {
        "timestamp": timestamp,
        "devices": {
            "WR1": {
                "soc": 61,
                "pv_input_w": pv,
                "output_w": 80,
                "battery_power_w": -20,
                "target_w": 90,
                "output_limit_w": 100,
            }
        },
        "grid_power_w": 12,
        "home_load_w": 92,
        "pv_total_w": pv,
        "inverter_output_w": 80,
        "battery_power_w": -20,
        "average_soc": 61,
        "controller": {
            "enabled": True,
            "max_total_power_w": 800,
            "min_output_limit_w": 35,
            "allocated_target_total_w": 90,
            "effective_target_total_w": 90,
            "commanded_total_w": 90,
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


def test_store_records_latest_and_history(tmp_path):
    store = DashboardStore(tmp_path / "dashboard.sqlite", retention_hours=48)
    timestamp = datetime.now(timezone.utc).isoformat()

    store.record(snapshot(timestamp, pv=345))

    assert store.latest()["pv_total_w"] == 345
    assert store.history("1h")[0]["devices"]["WR1"]["soc"] == 61
    assert store.latest()["control_explain"] is None


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
