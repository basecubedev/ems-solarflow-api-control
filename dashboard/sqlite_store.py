import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_ENERGY_SAVINGS = {
    "enabled": True,
    "price_per_kwh": 0.0,
    "currency": "EUR",
    "max_sample_delta_seconds": 60,
    "timezone": "Europe/Berlin",
}

SUPPORTED_RANGES = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "24h": timedelta(hours=24),
}

MONTH_LABELS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


class DashboardStore:
    """Small SQLite store for live dashboard snapshots."""

    def __init__(self, path, retention_hours=48, energy_savings=None):
        self.path = os.fspath(path)
        self.retention_hours = max(1, int(retention_hours or 48))
        self.energy_savings = {
            **DEFAULT_ENERGY_SAVINGS,
            **(energy_savings or {}),
        }
        self.energy_enabled = _as_bool(self.energy_savings.get("enabled"), True)
        self.energy_price_per_kwh = _as_float(
            self.energy_savings.get("price_per_kwh"),
            DEFAULT_ENERGY_SAVINGS["price_per_kwh"],
            minimum=0,
        )
        self.energy_currency = str(
            self.energy_savings.get(
                "currency",
                DEFAULT_ENERGY_SAVINGS["currency"],
            )
            or DEFAULT_ENERGY_SAVINGS["currency"]
        )
        self.max_energy_sample_delta_seconds = _as_float(
            self.energy_savings.get("max_sample_delta_seconds"),
            DEFAULT_ENERGY_SAVINGS["max_sample_delta_seconds"],
            minimum=1,
        )
        timezone_name = str(
            self.energy_savings.get(
                "timezone",
                DEFAULT_ENERGY_SAVINGS["timezone"],
            )
            or DEFAULT_ENERGY_SAVINGS["timezone"]
        )
        try:
            self.energy_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            self.energy_timezone = ZoneInfo(DEFAULT_ENERGY_SAVINGS["timezone"])
        self._lock = threading.RLock()
        self._latest = None

        parent = os.path.dirname(self.path)
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
            con.execute("""
                CREATE TABLE IF NOT EXISTS daily_energy_stats (
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
            con.execute("""
                CREATE TABLE IF NOT EXISTS energy_integration_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    def record(self, snapshot):
        timestamp = snapshot["timestamp"]
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
            if self.energy_enabled:
                self._record_energy_sample(con, snapshot)

            snapshot = {
                **snapshot,
                "energy_stats": self._energy_summary(con, timestamp),
            }
            payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))

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
            self._latest["energy_stats"] = self.energy_summary()
            return self._latest

        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT payload FROM snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()

        if not row:
            snapshot = empty_snapshot()
            snapshot["energy_stats"] = self.energy_summary()
            return snapshot

        self._latest = json.loads(row[0])
        self._latest["energy_stats"] = self.energy_summary()
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

    def energy_summary(self, now=None):
        with self._lock, self._connect() as con:
            return self._energy_summary(con, now)

    def _record_energy_sample(self, con, snapshot):
        sample_time = _parse_timestamp(snapshot.get("timestamp"))
        if sample_time is None:
            return

        inverter_output_w = max(
            0.0,
            _as_float(snapshot.get("inverter_output_w"), 0.0),
        )
        delta_wh = 0.0

        row = con.execute(
            """
            SELECT value FROM energy_integration_state
            WHERE key = 'last_sample_timestamp'
            """
        ).fetchone()

        if row:
            last_sample_time = _parse_timestamp(row[0])
            if last_sample_time is not None:
                delta_seconds = (sample_time - last_sample_time).total_seconds()
                if 0 < delta_seconds <= self.max_energy_sample_delta_seconds:
                    delta_wh = inverter_output_w * delta_seconds / 3600.0
                # Larger, zero, or negative intervals are skipped and the
                # baseline timestamp is advanced to avoid restart/downtime jumps.

        date_key = sample_time.astimezone(self.energy_timezone).date().isoformat()
        updated_at = sample_time.astimezone(timezone.utc).isoformat()
        self._upsert_daily_energy(
            con,
            date_key,
            inverter_output_w,
            delta_wh,
            updated_at,
        )
        con.execute(
            """
            INSERT INTO energy_integration_state(key, value, updated_at)
            VALUES('last_sample_timestamp', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (sample_time.astimezone(timezone.utc).isoformat(), updated_at),
        )

    def _upsert_daily_energy(self, con, date_key, output_w, delta_wh, updated_at):
        row = con.execute(
            """
            SELECT price_per_kwh, currency
            FROM daily_energy_stats
            WHERE date = ?
            """,
            (date_key,),
        ).fetchone()

        if row:
            price_per_kwh = _as_float(row[0], 0.0, minimum=0)
            currency = row[1] or self.energy_currency
        else:
            price_per_kwh = self.energy_price_per_kwh
            currency = self.energy_currency

        savings_value = (delta_wh / 1000.0) * price_per_kwh
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
            VALUES(?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(date) DO UPDATE SET
                inverter_output_wh = daily_energy_stats.inverter_output_wh
                    + excluded.inverter_output_wh,
                savings_value = daily_energy_stats.savings_value
                    + excluded.savings_value,
                peak_output_w = MAX(
                    daily_energy_stats.peak_output_w,
                    excluded.peak_output_w
                ),
                sample_count = daily_energy_stats.sample_count + 1,
                updated_at = excluded.updated_at
            """,
            (
                date_key,
                float(delta_wh),
                float(savings_value),
                float(price_per_kwh),
                currency,
                float(output_w),
                updated_at,
            ),
        )

    def _energy_summary(self, con, now=None):
        current_time = _parse_timestamp(now) or datetime.now(timezone.utc)
        today = current_time.astimezone(self.energy_timezone).date()

        summary = {
            "enabled": bool(self.energy_enabled),
            "currency": self.energy_currency,
            "price_per_kwh": self.energy_price_per_kwh,
            "today": self._range_summary(
                con,
                today.isoformat(),
                today.isoformat(),
                include_peak=True,
            ),
            "yesterday": self._range_summary(
                con,
                (today - timedelta(days=1)).isoformat(),
                (today - timedelta(days=1)).isoformat(),
                include_peak=True,
            ),
            "last_7_days": self._range_summary(
                con,
                (today - timedelta(days=6)).isoformat(),
                today.isoformat(),
            ),
            "last_4_weeks": self._range_summary(
                con,
                (today - timedelta(days=27)).isoformat(),
                today.isoformat(),
            ),
            "last_12_months": self._range_summary(
                con,
                (today - timedelta(days=364)).isoformat(),
                today.isoformat(),
            ),
            "best_day": self._best_day(con),
            "monthly_current_year": self._monthly_summary(con, today.year),
            "yearly": self._yearly_summary(con),
            "lifetime": self._lifetime_summary(con),
        }

        if not self.energy_enabled:
            summary.update({
                "today": _energy_payload(0, 0, peak_output_w=0),
                "yesterday": _energy_payload(0, 0, peak_output_w=0),
                "last_7_days": _energy_payload(0, 0),
                "last_4_weeks": _energy_payload(0, 0),
                "last_12_months": _energy_payload(0, 0),
                "best_day": _energy_payload(0, 0, date=None),
                "monthly_current_year": [
                    {
                        "month": month,
                        "label": MONTH_LABELS[month - 1],
                        **_energy_payload(0, 0),
                    }
                    for month in range(1, 13)
                ],
                "yearly": [],
                "lifetime": {
                    **_energy_payload(0, 0),
                    "since_date": None,
                },
            })

        return summary

    def _range_summary(self, con, start_date, end_date, include_peak=False):
        row = con.execute(
            """
            SELECT
                COALESCE(SUM(inverter_output_wh), 0),
                COALESCE(SUM(savings_value), 0),
                COALESCE(MAX(peak_output_w), 0)
            FROM daily_energy_stats
            WHERE date BETWEEN ? AND ?
            """,
            (start_date, end_date),
        ).fetchone()

        return _energy_payload(
            row[0],
            row[1],
            peak_output_w=row[2] if include_peak else None,
        )

    def _best_day(self, con):
        row = con.execute(
            """
            SELECT date, inverter_output_wh, savings_value, peak_output_w
            FROM daily_energy_stats
            ORDER BY inverter_output_wh DESC, date ASC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            return _energy_payload(0, 0, date=None)

        return _energy_payload(row[1], row[2], date=row[0], peak_output_w=row[3])

    def _monthly_summary(self, con, year):
        rows = con.execute(
            """
            SELECT
                CAST(substr(date, 6, 2) AS INTEGER) AS month,
                COALESCE(SUM(inverter_output_wh), 0),
                COALESCE(SUM(savings_value), 0)
            FROM daily_energy_stats
            WHERE substr(date, 1, 4) = ?
            GROUP BY month
            """,
            (f"{year:04d}",),
        ).fetchall()
        values = {int(row[0]): (row[1], row[2]) for row in rows}

        return [
            {
                "month": month,
                "label": MONTH_LABELS[month - 1],
                **_energy_payload(*values.get(month, (0, 0))),
            }
            for month in range(1, 13)
        ]

    def _yearly_summary(self, con):
        rows = con.execute(
            """
            SELECT
                CAST(substr(date, 1, 4) AS INTEGER) AS year,
                COALESCE(SUM(inverter_output_wh), 0),
                COALESCE(SUM(savings_value), 0)
            FROM daily_energy_stats
            GROUP BY year
            ORDER BY year ASC
            """
        ).fetchall()

        return [
            {
                "year": int(row[0]),
                **_energy_payload(row[1], row[2]),
            }
            for row in rows
        ]

    def _lifetime_summary(self, con):
        row = con.execute(
            """
            SELECT
                COALESCE(SUM(inverter_output_wh), 0),
                COALESCE(SUM(savings_value), 0),
                MIN(date)
            FROM daily_energy_stats
            WHERE sample_count > 0
            """
        ).fetchone()

        payload = _energy_payload(row[0], row[1])
        payload["since_date"] = row[2]
        return payload

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
        "control_explain": None,
        "energy_stats": {
            "enabled": False,
            "currency": "EUR",
            "price_per_kwh": 0.0,
        },
    }


def _parse_timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_float(value, default=0.0, minimum=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default

    if minimum is not None:
        parsed = max(minimum, parsed)

    return parsed


def _energy_payload(wh, savings, date=None, peak_output_w=None):
    payload = {
        "inverter_output_wh": round(float(wh or 0), 9),
        "inverter_output_kwh": round(float(wh or 0) / 1000.0, 9),
        "savings_value": round(float(savings or 0), 9),
    }

    if date is not None:
        payload["date"] = date

    if peak_output_w is not None:
        payload["peak_output_w"] = round(float(peak_output_w or 0), 9)

    return payload
