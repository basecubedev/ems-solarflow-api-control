# SPDX-License-Identifier: AGPL-3.0-or-later
"""Core EMS persistent state store.

This store is intentionally separate from the dashboard database so controller
features keep working when the dashboard is disabled.
"""

import os
import sqlite3
from datetime import datetime, timedelta


BATTERY_FULL_CHARGE_STATE_COLUMNS = (
    "device",
    "has_battery",
    "last_full_charge_at",
    "next_due_at",
    "full_charge_assist_active",
    "assist_started_at",
    "last_attempt_at",
    "last_seen_soc",
    "last_seen_max_soc",
    "last_seen_soc_limit",
    "last_seen_ac_mode",
    "last_seen_ac_status",
    "last_seen_pack_num",
    "last_seen_soc_status",
    "last_seen_battery_calibration_time",
    "last_seen_at",
    "restore_pending",
    "ac_mode_restore_pending",
    "max_soc_request_pending",
    "ac_input_request_pending",
    "updated_at",
)


class BatteryFullChargeStateStore:
    def __init__(self, path):
        self.path = path
        self.initialize()

    def connect(self):
        return sqlite3.connect(self.path)

    def initialize(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS battery_full_charge_state (
                    device TEXT PRIMARY KEY,
                    has_battery INTEGER NOT NULL DEFAULT 0,
                    last_full_charge_at TEXT,
                    next_due_at TEXT,
                    full_charge_assist_active INTEGER NOT NULL DEFAULT 0,
                    assist_started_at TEXT,
                    last_attempt_at TEXT,
                    last_seen_soc REAL,
                    last_seen_max_soc REAL,
                    last_seen_soc_limit INTEGER,
                    last_seen_ac_mode INTEGER,
                    last_seen_ac_status INTEGER,
                    last_seen_pack_num INTEGER,
                    last_seen_soc_status INTEGER,
                    last_seen_battery_calibration_time INTEGER,
                    last_seen_at TEXT,
                    restore_pending INTEGER NOT NULL DEFAULT 0,
                    ac_mode_restore_pending INTEGER NOT NULL DEFAULT 0,
                    max_soc_request_pending INTEGER NOT NULL DEFAULT 0,
                    ac_input_request_pending INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            existing = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(battery_full_charge_state)"
                ).fetchall()
            }
            for column, definition in (
                ("last_seen_pack_num", "INTEGER"),
                ("last_seen_soc_status", "INTEGER"),
                ("last_seen_battery_calibration_time", "INTEGER"),
            ):
                if column not in existing:
                    conn.execute(
                        f"ALTER TABLE battery_full_charge_state "
                        f"ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS battery_full_charge_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT,
                    soc REAL,
                    max_soc REAL,
                    soc_limit INTEGER,
                    ac_mode INTEGER,
                    ac_status INTEGER,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS battery_full_charge_feature_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled_last_seen INTEGER NOT NULL DEFAULT 0,
                    last_enabled_at TEXT,
                    last_disabled_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _timestamp(value):
        return value.isoformat()

    @staticmethod
    def _row_to_dict(row):
        if row is None:
            return None

        data = dict(zip(BATTERY_FULL_CHARGE_STATE_COLUMNS, row))
        for key in (
            "has_battery",
            "full_charge_assist_active",
            "restore_pending",
            "ac_mode_restore_pending",
            "max_soc_request_pending",
            "ac_input_request_pending",
        ):
            data[key] = bool(data.get(key))
        return data

    def default_state(self, device, now):
        return {
            "device": device,
            "has_battery": False,
            "last_full_charge_at": None,
            "next_due_at": None,
            "full_charge_assist_active": False,
            "assist_started_at": None,
            "last_attempt_at": None,
            "last_seen_soc": None,
            "last_seen_max_soc": None,
            "last_seen_soc_limit": None,
            "last_seen_ac_mode": None,
            "last_seen_ac_status": None,
            "last_seen_pack_num": None,
            "last_seen_soc_status": None,
            "last_seen_battery_calibration_time": None,
            "last_seen_at": None,
            "restore_pending": False,
            "ac_mode_restore_pending": False,
            "max_soc_request_pending": False,
            "ac_input_request_pending": False,
            "updated_at": self._timestamp(now),
        }

    def get_device_state(self, device, now=None):
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT device, has_battery, last_full_charge_at, next_due_at,
                       full_charge_assist_active, assist_started_at,
                       last_attempt_at, last_seen_soc, last_seen_max_soc,
                       last_seen_soc_limit, last_seen_ac_mode,
                       last_seen_ac_status, last_seen_pack_num,
                       last_seen_soc_status,
                       last_seen_battery_calibration_time, last_seen_at,
                       restore_pending,
                       ac_mode_restore_pending, max_soc_request_pending,
                       ac_input_request_pending, updated_at
                FROM battery_full_charge_state
                WHERE device = ?
                """,
                (device,),
            ).fetchone()

        if row:
            return self._row_to_dict(row)
        return self.default_state(device, now) if now is not None else None

    def all_device_states(self):
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT device, has_battery, last_full_charge_at, next_due_at,
                       full_charge_assist_active, assist_started_at,
                       last_attempt_at, last_seen_soc, last_seen_max_soc,
                       last_seen_soc_limit, last_seen_ac_mode,
                       last_seen_ac_status, last_seen_pack_num,
                       last_seen_soc_status,
                       last_seen_battery_calibration_time, last_seen_at,
                       restore_pending,
                       ac_mode_restore_pending, max_soc_request_pending,
                       ac_input_request_pending, updated_at
                FROM battery_full_charge_state
                ORDER BY device
                """
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def update_device_state(self, device, now, **values):
        current = self.get_device_state(device, now)
        current.update(values)
        current["device"] = device
        current["updated_at"] = self._timestamp(now)

        row = {
            key: current.get(key)
            for key in BATTERY_FULL_CHARGE_STATE_COLUMNS
        }
        for key in (
            "has_battery",
            "full_charge_assist_active",
            "restore_pending",
            "ac_mode_restore_pending",
            "max_soc_request_pending",
            "ac_input_request_pending",
        ):
            row[key] = 1 if row.get(key) else 0

        placeholders = ", ".join("?" for _ in BATTERY_FULL_CHARGE_STATE_COLUMNS)
        update_clause = ", ".join(
            f"{key}=excluded.{key}"
            for key in BATTERY_FULL_CHARGE_STATE_COLUMNS
            if key != "device"
        )
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO battery_full_charge_state (
                    {', '.join(BATTERY_FULL_CHARGE_STATE_COLUMNS)}
                )
                VALUES ({placeholders})
                ON CONFLICT(device) DO UPDATE SET {update_clause}
                """,
                tuple(row[key] for key in BATTERY_FULL_CHARGE_STATE_COLUMNS),
            )
        return self.get_device_state(device, now)

    def record_observation(self, device, state, has_battery, now, interval_days):
        values = {
            "has_battery": bool(has_battery),
            "last_seen_soc": float(state.soc),
            "last_seen_max_soc": float(state.max_soc),
            "last_seen_soc_limit": int(state.soc_limit),
            "last_seen_ac_mode": int(state.ac_mode),
            "last_seen_ac_status": int(state.ac_status),
            "last_seen_pack_num": int(getattr(state, "pack_num", 0)),
            "last_seen_soc_status": int(getattr(state, "soc_status", 0)),
            "last_seen_battery_calibration_time": getattr(
                state,
                "battery_calibration_time",
                None
            ),
            "last_seen_at": self._timestamp(now),
        }

        if has_battery and int(state.soc_limit) == 1:
            values["last_full_charge_at"] = self._timestamp(now)
            values["next_due_at"] = self._timestamp(
                now + timedelta(days=interval_days)
            )

        return self.update_device_state(device, now, **values)

    def get_full_charge_feature_enabled_state(self):
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT enabled_last_seen
                FROM battery_full_charge_feature_state
                WHERE id = 1
                """
            ).fetchone()

        if row is None:
            return None

        return bool(row[0])

    def set_full_charge_feature_enabled_state(self, enabled, now):
        timestamp = self._timestamp(now)
        enabled_value = 1 if enabled else 0
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO battery_full_charge_feature_state (
                    id, enabled_last_seen, last_enabled_at,
                    last_disabled_at, updated_at
                )
                VALUES (
                    1, ?, CASE WHEN ? THEN ? ELSE NULL END,
                    CASE WHEN ? THEN NULL ELSE ? END, ?
                )
                ON CONFLICT(id) DO UPDATE SET
                    enabled_last_seen = excluded.enabled_last_seen,
                    last_enabled_at = CASE
                        WHEN excluded.enabled_last_seen = 1
                        THEN excluded.last_enabled_at
                        ELSE battery_full_charge_feature_state.last_enabled_at
                    END,
                    last_disabled_at = CASE
                        WHEN excluded.enabled_last_seen = 0
                        THEN excluded.last_disabled_at
                        ELSE battery_full_charge_feature_state.last_disabled_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    enabled_value,
                    enabled_value,
                    timestamp,
                    enabled_value,
                    timestamp,
                    timestamp,
                ),
            )

    def full_charge_has_pending_state(self, record):
        return any(
            bool(record.get(key))
            for key in (
                "full_charge_assist_active",
                "restore_pending",
                "ac_mode_restore_pending",
                "max_soc_request_pending",
                "ac_input_request_pending",
            )
        )

    def seed_full_charge_schedule(
        self,
        device,
        now,
        interval_days,
        *,
        event_type,
        message,
        state=None
    ):
        record = self.update_device_state(
            device,
            now,
            last_full_charge_at=self._timestamp(now),
            next_due_at=self._timestamp(now + timedelta(days=interval_days)),
            full_charge_assist_active=False,
            restore_pending=False,
            ac_mode_restore_pending=False,
            max_soc_request_pending=False,
            ac_input_request_pending=False,
        )
        self.log_event(
            device,
            event_type,
            now,
            state=state,
            message=message
        )
        return record

    def mark_assist_started(self, device, now, ac_charge_mode):
        return self.update_device_state(
            device,
            now,
            full_charge_assist_active=True,
            assist_started_at=self._timestamp(now),
            last_attempt_at=self._timestamp(now),
            restore_pending=True,
            max_soc_request_pending=True,
            ac_input_request_pending=bool(ac_charge_mode),
            ac_mode_restore_pending=bool(ac_charge_mode),
        )

    def mark_assist_completed(self, device, now, interval_days):
        return self.update_device_state(
            device,
            now,
            last_full_charge_at=self._timestamp(now),
            next_due_at=self._timestamp(now + timedelta(days=interval_days)),
            full_charge_assist_active=False,
            max_soc_request_pending=True,
            ac_input_request_pending=False,
            restore_pending=True,
        )

    def log_event(self, device, event_type, now, state=None, message=None):
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO battery_full_charge_events (
                    device, event_type, message, soc, max_soc, soc_limit,
                    ac_mode, ac_status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device,
                    event_type,
                    message,
                    getattr(state, "soc", None),
                    getattr(state, "max_soc", None),
                    getattr(state, "soc_limit", None),
                    getattr(state, "ac_mode", None),
                    getattr(state, "ac_status", None),
                    self._timestamp(now),
                ),
            )


def parse_iso_timestamp(value):
    """Parse an ISO-8601 timestamp stored by BatteryFullChargeStateStore.

    Naive timestamps are assumed to be in the local timezone, matching how
    the controller writes them.
    """

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def describe_full_charge_assist_status(config, enabled, has_battery, record, now):
    """Derive a normalized battery full-charge assist status for the API.

    This is the single place that decides the dashboard-facing ``status``
    and related derived fields, so the browser does not need to duplicate
    assist window/overdue/restore logic.
    """

    config = config or {}
    assist_window_days = int(config.get("assist_window_days") or 0)

    if not enabled:
        return {
            "status": "disabled",
            "inside_assist_window": False,
            "days_until_due": None,
            "window_starts_at": None,
            "message": "Battery full-charge assist disabled",
        }

    if not has_battery:
        return {
            "status": "ignored_no_battery",
            "inside_assist_window": False,
            "days_until_due": None,
            "window_starts_at": None,
            "message": "No battery detected",
        }

    if record is None:
        return {
            "status": "unknown",
            "inside_assist_window": False,
            "days_until_due": None,
            "window_starts_at": None,
            "message": "Full-charge assist state unavailable",
        }

    next_due_at = parse_iso_timestamp(record.get("next_due_at"))
    window_starts_at = None
    inside_assist_window = False
    days_until_due = None
    if next_due_at is not None:
        days_until_due = (next_due_at - now).days
        window_starts_at = next_due_at - timedelta(days=assist_window_days)
        inside_assist_window = window_starts_at <= now <= next_due_at

    if record.get("full_charge_assist_active"):
        status = "active"
        message = "Full-charge assist active"
    elif record.get("restore_pending") or record.get("ac_mode_restore_pending"):
        status = "restore_pending"
        message = "Restore pending"
    elif int(record.get("last_seen_soc_limit") or 0) == 1:
        status = "completed"
        message = "Full-charge assist completed"
    elif next_due_at is not None and now > next_due_at:
        status = "overdue"
        message = "Full-charge assist overdue"
    elif inside_assist_window:
        status = "window"
        message = "Assist window active"
    else:
        status = "ok"
        message = "Full-charge assist scheduled"

    return {
        "status": status,
        "inside_assist_window": inside_assist_window,
        "days_until_due": days_until_due,
        "window_starts_at": (
            window_starts_at.isoformat() if window_starts_at is not None else None
        ),
        "message": message,
    }
