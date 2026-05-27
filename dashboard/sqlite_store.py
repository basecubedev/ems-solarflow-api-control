import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone


SUPPORTED_RANGES = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "24h": timedelta(hours=24),
}


class DashboardStore:
    """Small SQLite store for live dashboard snapshots."""

    def __init__(self, path, retention_hours=48):
        self.path = path
        self.retention_hours = max(1, int(retention_hours or 48))
        self._lock = threading.RLock()
        self._latest = None

        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def _init_db(self):
        with self._lock, self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    timestamp TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS telemetry (
                    timestamp TEXT NOT NULL,
                    device TEXT NOT NULL,
                    field TEXT NOT NULL,
                    value REAL NOT NULL
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_telemetry_time
                ON telemetry(timestamp)
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS device_state (
                    device TEXT PRIMARY KEY,
                    soc REAL,
                    pv_power REAL,
                    output_power REAL,
                    battery_power REAL,
                    updated_at TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS rule_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    def record(self, snapshot):
        timestamp = snapshot["timestamp"]
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        rows = []

        for device_name, device in snapshot.get("devices", {}).items():
            for field in (
                "soc",
                "pv_input_w",
                "output_w",
                "battery_power_w",
                "target_w",
                "output_limit_w",
            ):
                rows.append((timestamp, device_name, field, float(device.get(field, 0) or 0)))

        with self._lock, self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO snapshots(timestamp, payload) VALUES(?, ?)",
                (timestamp, payload),
            )
            con.executemany(
                """
                INSERT INTO telemetry(timestamp, device, field, value)
                VALUES(?, ?, ?, ?)
                """,
                rows,
            )
            for device_name, device in snapshot.get("devices", {}).items():
                con.execute(
                    """
                    INSERT OR REPLACE INTO device_state(
                        device,
                        soc,
                        pv_power,
                        output_power,
                        battery_power,
                        updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_name,
                        device.get("soc", 0),
                        device.get("pv_input_w", 0),
                        device.get("output_w", 0),
                        device.get("battery_power_w", 0),
                        timestamp,
                    ),
                )
            for key, value in snapshot.get("rules", {}).items():
                con.execute(
                    """
                    INSERT OR REPLACE INTO rule_state(key, value, updated_at)
                    VALUES(?, ?, ?)
                    """,
                    (key, json.dumps(value, sort_keys=True), timestamp),
                )

            self._cleanup(con)

        self._latest = snapshot

    def latest(self):
        if self._latest is not None:
            return self._latest

        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT payload FROM snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()

        if not row:
            return empty_snapshot()

        self._latest = json.loads(row[0])
        return self._latest

    def history(self, range_name="6h"):
        delta = SUPPORTED_RANGES.get(range_name, SUPPORTED_RANGES["6h"])
        cutoff = (datetime.now(timezone.utc) - delta).isoformat()

        with self._lock, self._connect() as con:
            rows = con.execute(
                """
                SELECT payload FROM snapshots
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (cutoff,),
            ).fetchall()

        return [json.loads(row[0]) for row in rows]

    def _cleanup(self, con):
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=self.retention_hours)
        ).isoformat()

        con.execute("DELETE FROM snapshots WHERE timestamp < ?", (cutoff,))
        con.execute("DELETE FROM telemetry WHERE timestamp < ?", (cutoff,))


def empty_snapshot():
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "devices": {},
        "grid_power_w": 0,
        "home_load_w": 0,
        "pv_total_w": 0,
        "inverter_output_w": 0,
        "battery_power_w": 0,
        "average_soc": 0,
        "controller": {
            "enabled": False,
            "max_total_power_w": 0,
            "min_output_limit_w": 0,
            "allocated_target_total_w": 0,
            "effective_target_total_w": 0,
            "commanded_total_w": 0,
            "filtered_load_w": 0,
            "night_min_soc_idle": False,
        },
        "rules": {},
    }

