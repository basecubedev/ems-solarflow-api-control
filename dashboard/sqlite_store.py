# SPDX-License-Identifier: AGPL-3.0-or-later
import bisect
import calendar
import contextlib
import functools
import json
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_ENERGY_SAVINGS = {
    "enabled": True,
    "price_per_kwh": 0.0,
    "currency": "EUR",
    "max_sample_delta_seconds": 20,
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

@functools.lru_cache(maxsize=1)
def _energy_channels_module():
    """Import ``ems.energy_channels`` on first use, not at import time.

    ``dashboard.server`` imports this module, and it deliberately pulls in no
    ``ems`` module eagerly -- see
    ``tests/test_analytics_schema_gap.py::test_the_dashboard_still_imports_no_ems_module_eagerly``.
    """

    from ems import energy_channels

    return energy_channels


def _channels():
    return _energy_channels_module().ENERGY_CHANNELS


@functools.lru_cache(maxsize=1)
def _channel_columns_sql():
    """Plain column list. Built from the frozen registry, never from input."""

    return ", ".join(channel.column for channel in _channels())


@functools.lru_cache(maxsize=1)
def _channel_sums_sql():
    return ", ".join(
        f"COALESCE(SUM({channel.column}), 0)" for channel in _channels()
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
        # The rollup changes only when this store writes a sample, and the
        # live snapshot rebuilds it on every read -- once per second per SSE
        # client, against a table that grows by a row a day. Both the coverage
        # map and the finished summary are cached here and dropped on write.
        self._coverage = None
        self._summary = None

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
            self._init_energy_channels(con)

    def _init_energy_channels(self, con):
        """Add the channel columns and the coverage table to any database.

        A new database takes the same path as an upgraded one, so the migration
        runs on every start instead of only once on an old file.
        """

        existing = {
            row[1] for row in con.execute("PRAGMA table_info(daily_energy_stats)")
        }
        for channel in _channels():
            if channel.column in existing:
                continue
            con.execute(
                f"ALTER TABLE daily_energy_stats "
                f"ADD COLUMN {channel.column} REAL NOT NULL DEFAULT 0"
            )

        con.execute("""
            CREATE TABLE IF NOT EXISTS energy_channel_coverage (
                channel TEXT PRIMARY KEY,
                first_date TEXT NOT NULL,
                last_date TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Sparse: a row exists only for a day that is not fully measured, which
        # is the first day of a channel and any day a sample could not be read.
        con.execute("""
            CREATE TABLE IF NOT EXISTS energy_channel_gap (
                channel TEXT NOT NULL,
                date TEXT NOT NULL,
                missed_samples INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(channel, date)
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
                self._coverage = None
                self._summary = None

            # The rollup belongs to the daily table, and latest() attaches a
            # fresh one on every read. A stored copy was pure write
            # amplification: 2.2 KB of a 7.9 KB row, written once per dashboard
            # write interval and never read back.
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

    @contextlib.contextmanager
    def maintenance_pause(self):
        """Hold the store lock and drop cached state for a DB file swap.

        Used by the maintenance restore path so no record()/latest() runs (and
        opens a connection) while the SQLite file is being replaced. The cached
        ``latest`` snapshot is cleared so the next read re-opens the restored DB.
        """
        with self._lock:
            self._latest = None
            self._coverage = None
            self._summary = None
            restore_failed = False
            try:
                yield
            except BaseException:
                restore_failed = True
                raise
            finally:
                self._latest = None
                self._coverage = None
                self._summary = None
                # The restored file can come from an older version, and this
                # store outlives the swap. Without bringing the schema forward
                # here, every later record() raises on a missing column until
                # someone restarts the EMS -- and a restore that fails after
                # replacing the file is exactly when that happens.
                try:
                    self._init_db()
                except sqlite3.Error:
                    # A restore that already failed leaves the file in an
                    # unknown state; its own error is the one worth reporting.
                    if not restore_failed:
                        raise

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
        """Return the rollup, reusing the last one until a sample changes it.

        The cached payload is handed out as it is rather than copied: it is
        rebuilt on the next write, and every caller here serializes it.
        """

        with self._lock:
            if now is None:
                today = (
                    datetime.now(timezone.utc)
                    .astimezone(self.energy_timezone)
                    .date()
                    .isoformat()
                )
                if self._summary is not None and self._summary[0] == today:
                    # Before opening a connection: that open is most of what a
                    # cache hit would otherwise cost.
                    return self._summary[1]

            with self._connect() as con:
                if now is not None:
                    return self._energy_summary(con, now)

                summary = self._energy_summary(con, None)
                self._summary = (today, summary)
                return summary

    def _record_energy_sample(self, con, snapshot):
        sample_time = _parse_timestamp(snapshot.get("timestamp"))
        if sample_time is None:
            return

        inverter_output_w = max(
            0.0,
            _as_float(snapshot.get("inverter_output_w"), 0.0),
        )
        elapsed_hours = 0.0
        last_sample_time = None

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
                    elapsed_hours = delta_seconds / 3600.0
                # Larger, zero, or negative intervals are skipped and the
                # baseline timestamp is advanced to avoid restart/downtime jumps.

        # A skipped interval integrated nothing, so it may not claim the day as
        # measured either.
        measured = (
            _energy_channels_module().measured_channel_ids(snapshot)
            if elapsed_hours > 0
            else ()
        )
        # The delivered energy follows the same gate as the channels beside it.
        # It used to keep integrating through an outage, which put a figure
        # built on frozen telemetry next to channels reading "not measured".
        delta_wh = inverter_output_w * elapsed_hours if measured else 0.0
        channel_wh = _energy_channels_module().channel_sample_wh(
            snapshot,
            elapsed_hours,
        )

        date_key = sample_time.astimezone(self.energy_timezone).date().isoformat()
        updated_at = sample_time.astimezone(timezone.utc).isoformat()
        self._upsert_daily_energy(
            con,
            date_key,
            inverter_output_w,
            delta_wh,
            channel_wh,
            updated_at,
        )
        self._record_channel_coverage(con, date_key, updated_at, measured)
        if elapsed_hours > 0:
            missed = [
                channel.id for channel in _channels() if channel.id not in measured
            ]
            self._record_channel_gap(con, [date_key], updated_at, missed)
        elif last_sample_time is not None:
            # Time passed that nothing integrated -- a restart, an outage. The
            # hole belongs to the day the samples stopped and to the day they
            # came back; whole days in between have no row at all, and a range
            # that contains one is incomplete by the day count.
            #
            # A sample with no predecessor at all is the start of the
            # measurement rather than a hole in it. Marking it would leave
            # every range containing the start -- the lifetime, the first
            # month, the first calendar year -- incomplete for good.
            every_channel = [channel.id for channel in _channels()]
            self._record_channel_gap(
                con,
                self._gap_boundary_dates(last_sample_time, sample_time),
                updated_at,
                every_channel,
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

    def _record_channel_coverage(self, con, date_key, updated_at, channel_ids):
        """Remember the first and last day each channel was measured.

        The first day a channel measured is its start, not a hole, even though
        the channel began somewhere inside it. Marking it would leave every
        range that contains the start -- the lifetime, the first month, the
        first calendar year -- incomplete for good, and those are the ranges
        whose ratio a reader keeps. What the first day costs is one partly
        measured day inside ranges that also carry ``Measured since`` on the
        same card, which says when the measuring began.
        """

        if not channel_ids:
            return

        con.executemany(
            """
            INSERT INTO energy_channel_coverage(
                channel,
                first_date,
                last_date,
                updated_at
            )
            VALUES(?, ?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
                first_date = MIN(
                    energy_channel_coverage.first_date,
                    excluded.first_date
                ),
                last_date = MAX(
                    energy_channel_coverage.last_date,
                    excluded.last_date
                ),
                updated_at = excluded.updated_at
            """,
            [
                (channel_id, date_key, date_key, updated_at)
                for channel_id in channel_ids
            ],
        )

    def _gap_boundary_dates(self, last_sample_time, sample_time):
        """The days a skipped interval leaves a hole in."""

        dates = {sample_time.astimezone(self.energy_timezone).date().isoformat()}
        if last_sample_time is not None:
            dates.add(
                last_sample_time.astimezone(self.energy_timezone).date().isoformat()
            )
        return sorted(dates)

    def _record_channel_gap(self, con, date_keys, updated_at, channel_ids):
        """Mark days on which these channels have a hole."""

        if not channel_ids or not date_keys:
            return

        con.executemany(
            """
            INSERT INTO energy_channel_gap(
                channel,
                date,
                missed_samples,
                updated_at
            )
            VALUES(?, ?, 1, ?)
            ON CONFLICT(channel, date) DO UPDATE SET
                missed_samples = energy_channel_gap.missed_samples + 1,
                updated_at = excluded.updated_at
            """,
            [
                (channel_id, date_key, updated_at)
                for channel_id in channel_ids
                for date_key in date_keys
            ],
        )

    def _upsert_daily_energy(
        self,
        con,
        date_key,
        output_w,
        delta_wh,
        channel_wh,
        updated_at,
    ):
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
        # The channel columns come from the frozen registry, never from input.
        channel_columns = "".join(
            f",\n                {channel.column}" for channel in _channels()
        )
        channel_placeholders = ", ?" * len(_channels())
        channel_updates = "".join(
            f",\n                {channel.column} = daily_energy_stats.{channel.column}"
            f" + excluded.{channel.column}"
            for channel in _channels()
        )
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
                updated_at{channel_columns}
            )
            VALUES(?, ?, ?, ?, ?, ?, 1, ?{channel_placeholders})
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
                updated_at = excluded.updated_at{channel_updates}
            """,
            (
                date_key,
                float(delta_wh),
                float(savings_value),
                float(price_per_kwh),
                currency,
                float(output_w),
                updated_at,
                *(
                    float(channel_wh.get(channel.id, 0.0))
                    for channel in _channels()
                ),
            ),
        )

    def _energy_summary(self, con, now=None):
        current_time = _parse_timestamp(now) or datetime.now(timezone.utc)
        today = current_time.astimezone(self.energy_timezone).date()
        coverage = self._channel_coverage(con)

        summary = {
            "enabled": bool(self.energy_enabled),
            "currency": self.energy_currency,
            "price_per_kwh": self.energy_price_per_kwh,
            "channel_meta": _channel_meta(coverage),
            "today": self._range_summary(
                con,
                coverage,
                today.isoformat(),
                today.isoformat(),
                include_peak=True,
            ),
            "yesterday": self._range_summary(
                con,
                coverage,
                (today - timedelta(days=1)).isoformat(),
                (today - timedelta(days=1)).isoformat(),
                include_peak=True,
            ),
            "last_7_days": self._range_summary(
                con,
                coverage,
                (today - timedelta(days=6)).isoformat(),
                today.isoformat(),
            ),
            "last_4_weeks": self._range_summary(
                con,
                coverage,
                (today - timedelta(days=27)).isoformat(),
                today.isoformat(),
            ),
            "last_12_months": self._range_summary(
                con,
                coverage,
                (today - timedelta(days=364)).isoformat(),
                today.isoformat(),
            ),
            "best_day": self._best_day(con, coverage),
            "monthly_current_year": self._monthly_summary(
                con,
                coverage,
                today.year,
                today,
            ),
            "yearly": self._yearly_summary(con, coverage, today),
            "lifetime": self._lifetime_summary(con, coverage),
        }

        if not self.energy_enabled:
            summary.update({
                "channel_meta": _channel_meta({}),
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

    def _channel_coverage(self, con):
        """Return the measured range and the incomplete days of each channel."""

        if self._coverage is not None:
            return self._coverage

        rows = con.execute(
            """
            SELECT channel, first_date, last_date
            FROM energy_channel_coverage
            """
        ).fetchall()
        gaps = con.execute(
            """
            SELECT channel, date
            FROM energy_channel_gap
            ORDER BY channel, date
            """
        ).fetchall()

        by_channel = {}
        for channel_id, date_key in gaps:
            by_channel.setdefault(channel_id, []).append(date_key)

        self._coverage = {
            row[0]: _ChannelCoverage(row[1], row[2], tuple(by_channel.get(row[0], ())))
            for row in rows
        }
        return self._coverage

    def _range_summary(
        self,
        con,
        coverage,
        start_date,
        end_date,
        include_peak=False,
    ):
        row = con.execute(
            f"""
            SELECT
                COALESCE(SUM(inverter_output_wh), 0),
                COALESCE(SUM(savings_value), 0),
                COALESCE(MAX(peak_output_w), 0),
                COALESCE(SUM(CASE WHEN sample_count > 0 THEN 1 ELSE 0 END), 0),
                {_channel_sums_sql()}
            FROM daily_energy_stats
            WHERE date BETWEEN ? AND ?
            """,
            (start_date, end_date),
        ).fetchone()

        return _energy_payload(
            row[0],
            row[1],
            peak_output_w=row[2] if include_peak else None,
            channel_wh=_channel_totals(row[4:]),
            coverage=coverage,
            start_date=start_date,
            end_date=end_date,
            measured_days=row[3],
        )

    def _best_day(self, con, coverage):
        row = con.execute(
            f"""
            SELECT
                date,
                inverter_output_wh,
                savings_value,
                peak_output_w,
                {_channel_columns_sql()}
            FROM daily_energy_stats
            ORDER BY inverter_output_wh DESC, date ASC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            return _energy_payload(0, 0, date=None)

        return _energy_payload(
            row[1],
            row[2],
            date=row[0],
            peak_output_w=row[3],
            channel_wh=_channel_totals(row[4:]),
            coverage=coverage,
            start_date=row[0],
            end_date=row[0],
            measured_days=1,
        )

    def _monthly_summary(self, con, coverage, year, today=None):
        rows = con.execute(
            f"""
            SELECT
                CAST(substr(date, 6, 2) AS INTEGER) AS month,
                COALESCE(SUM(inverter_output_wh), 0),
                COALESCE(SUM(savings_value), 0),
                COALESCE(SUM(CASE WHEN sample_count > 0 THEN 1 ELSE 0 END), 0),
                {_channel_sums_sql()}
            FROM daily_energy_stats
            WHERE substr(date, 1, 4) = ?
            GROUP BY month
            """,
            (f"{year:04d}",),
        ).fetchall()
        values = {int(row[0]): row for row in rows}

        months = []
        for month in range(1, 13):
            row = values.get(month)
            last_day = calendar.monthrange(year, month)[1]
            end_date = _elapsed_end(
                f"{year:04d}-{month:02d}-{last_day:02d}",
                today,
            )
            months.append({
                "month": month,
                "label": MONTH_LABELS[month - 1],
                **_energy_payload(
                    row[1] if row else 0,
                    row[2] if row else 0,
                    channel_wh=_channel_totals(row[4:]) if row else None,
                    coverage=coverage,
                    start_date=f"{year:04d}-{month:02d}-01",
                    end_date=end_date,
                    measured_days=row[3] if row else 0,
                ),
            })

        return months

    def _yearly_summary(self, con, coverage, today=None):
        rows = con.execute(
            f"""
            SELECT
                CAST(substr(date, 1, 4) AS INTEGER) AS year,
                COALESCE(SUM(inverter_output_wh), 0),
                COALESCE(SUM(savings_value), 0),
                COALESCE(SUM(CASE WHEN sample_count > 0 THEN 1 ELSE 0 END), 0),
                {_channel_sums_sql()}
            FROM daily_energy_stats
            GROUP BY year
            ORDER BY year ASC
            """
        ).fetchall()

        return [
            {
                "year": int(row[0]),
                **_energy_payload(
                    row[1],
                    row[2],
                    channel_wh=_channel_totals(row[4:]),
                    coverage=coverage,
                    start_date=f"{int(row[0]):04d}-01-01",
                    end_date=_elapsed_end(f"{int(row[0]):04d}-12-31", today),
                    measured_days=row[3],
                ),
            }
            for row in rows
        ]

    def _lifetime_summary(self, con, coverage):
        row = con.execute(
            f"""
            SELECT
                COALESCE(SUM(inverter_output_wh), 0),
                COALESCE(SUM(savings_value), 0),
                MIN(date),
                MAX(date),
                COUNT(*),
                {_channel_sums_sql()}
            FROM daily_energy_stats
            WHERE sample_count > 0
            """
        ).fetchone()

        payload = _energy_payload(
            row[0],
            row[1],
            channel_wh=_channel_totals(row[5:]),
            coverage=coverage,
            start_date=row[2],
            end_date=row[3],
            measured_days=row[4],
        )
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


def _channel_totals(values):
    """Map a row tail of channel sums back onto the channel ids."""

    return {
        channel.id: value
        for channel, value in zip(_channels(), values or ())
    }


def _days_between(start_date, end_date):
    """Number of calendar days a range covers, or None when it is not one."""

    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except (TypeError, ValueError):
        return None

    if end < start:
        return None

    return (end - start).days + 1


def _elapsed_end(end_date, today):
    """Clamp a calendar end to today.

    A month or a year in progress is not partly measured because it has not
    happened yet: coverage is about the days that exist, so the range ends at
    the last day there can be data for.
    """

    if today is None:
        return end_date

    today_key = today.isoformat()
    return min(end_date, today_key)


class _ChannelCoverage(NamedTuple):
    """What a channel measured: its range, and the days inside it with a hole.

    ``gap_dates`` is sorted, because it is searched rather than walked: the
    table has a row per holed day and nothing prunes it.
    """

    first_date: str
    last_date: str
    gap_dates: tuple = ()

    def has_gap_between(self, start_date, end_date):
        index = bisect.bisect_left(self.gap_dates, start_date)
        return index < len(self.gap_dates) and self.gap_dates[index] <= end_date


def _coverage_state(
    coverage,
    channel_id,
    start_date,
    end_date,
    every_day=True,
    measured_any=True,
    ignore_holes=False,
):
    """Classify how much of a date range this channel actually measured.

    "full" means the channel measured every day of the range and read every
    sample it took. ``ignore_holes`` answers the narrower question the ratios
    ask -- whether the range itself is covered -- and is explained there.
    """

    entry = (coverage or {}).get(channel_id)
    if not entry or not start_date or not end_date or not measured_any:
        return "none"

    first_date, last_date = entry.first_date, entry.last_date
    if not first_date or not last_date:
        return "none"
    if last_date < start_date or first_date > end_date:
        return "none"
    if (
        every_day
        and first_date <= start_date
        and last_date >= end_date
        and (ignore_holes or not entry.has_gap_between(start_date, end_date))
    ):
        return "full"

    return "partial"


def _channel_meta(coverage):
    return [
        {
            "id": channel.id,
            "label": channel.label,
            "unit": "Wh",
            "since": getattr((coverage or {}).get(channel.id), "first_date", None),
            "until": getattr((coverage or {}).get(channel.id), "last_date", None),
        }
        for channel in _channels()
    ]


def _channel_value(wh):
    value = float(wh or 0)
    return {
        "wh": round(value, 3),
        "kwh": round(value / 1000.0, 6),
    }


def _ratio_value(value):
    return None if value is None else round(value, 6)


RATIO_CHANNELS = ("home_consumption", "grid_import")


def _energy_ratios(channels, range_states):
    """Derive the ratio, for a range the channels measured from end to end.

    ``range_states`` asks the narrow question: were the channels measuring
    across this range at all. Time inside it that nobody measured -- a hole in
    a day, or a day the EMS never ran -- marks the figures instead of hiding
    the number, because a sample is integrated for every channel or for none,
    so both sides of the division lose exactly the same samples.

    The price is stated plainly: after an outage, the ratio is computed from
    the part of the period that was measured, and carries the mark. Withholding
    it instead would mean no installation ever sees a ratio for a month, a year
    or its lifetime, because every one of them restarts eventually -- an honest
    number nobody can ever read is worth less than a marked one.

    Both sides of the division share one basis: a sample is integrated for
    every channel or for none of them (``ems.energy_channels``), so a hole is
    always the same hole on both sides of it.
    """

    # Only the two channels it divides: a seventh channel added to the registry
    # later would otherwise null this figure for the lifetime and every past
    # year, permanently, for a quantity it does not read.
    divisor = channels.get("home_consumption")
    dividend = channels.get("grid_import")
    if divisor is None or dividend is None:
        return {"self_sufficiency": None}
    if any(range_states.get(channel_id) != "full" for channel_id in RATIO_CHANNELS):
        return {"self_sufficiency": None}

    sufficiency = _energy_channels_module().self_sufficiency(
        divisor["wh"],
        dividend["wh"],
    )

    return {"self_sufficiency": _ratio_value(sufficiency)}


def _energy_payload(
    wh,
    savings,
    date=None,
    peak_output_w=None,
    channel_wh=None,
    coverage=None,
    start_date=None,
    end_date=None,
    measured_days=None,
):
    payload = {
        "inverter_output_wh": round(float(wh or 0), 9),
        "inverter_output_kwh": round(float(wh or 0) / 1000.0, 9),
        "savings_value": round(float(savings or 0), 9),
    }

    if date is not None:
        payload["date"] = date

    if peak_output_w is not None:
        payload["peak_output_w"] = round(float(peak_output_w or 0), 9)

    totals = channel_wh or {}
    payload["channels"] = {
        channel.id: _channel_value(totals.get(channel.id, 0))
        for channel in _channels()
    }

    expected_days = _days_between(start_date, end_date)
    every_day = (
        True
        if measured_days is None or expected_days is None
        else measured_days >= expected_days
    )
    # A range with no measured day at all is not partly measured; it is
    # unmeasured, and a zero from it must not read as a figure.
    measured_any = measured_days is None or measured_days > 0

    def state_of(channel_id, ignore_holes=False):
        return _coverage_state(
            coverage,
            channel_id,
            start_date,
            end_date,
            every_day,
            measured_any,
            ignore_holes,
        )

    states = {channel.id: state_of(channel.id) for channel in _channels()}
    # The ratio asks only whether the channels were measuring across this
    # range. A day the EMS never sampled takes the same samples from both sides
    # of the division as a hole inside a day does, so it marks the figures
    # without withholding the number.
    range_states = {
        channel.id: _coverage_state(
            coverage,
            channel.id,
            start_date,
            end_date,
            True,
            measured_any,
            True,
        )
        for channel in _channels()
    }
    # Only the exceptions travel: a channel the client does not find here has
    # measured the whole range.
    payload["coverage"] = {
        channel_id: state
        for channel_id, state in states.items()
        if state != "full"
    }
    payload["ratios"] = _energy_ratios(payload["channels"], range_states)

    return payload
