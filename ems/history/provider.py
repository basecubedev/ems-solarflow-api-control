# SPDX-License-Identifier: AGPL-3.0-or-later
"""History provider abstraction.

The dashboard must not depend on where history is stored. ``HistoryProvider``
is the seam: the default :class:`SqliteHistoryProvider` reads the existing local
SQLite snapshot store (zero extra dependencies), and :class:`InfluxHistory
Provider` (in :mod:`ems.history.influx_provider`) serves the InfluxDB-backed
analytics experience. Both return the same columnar :class:`HistoryResult`
shape, ready for a uPlot chart.

Import-side-effect-free.
"""

import json
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Canonical chart series. Each entry maps a stable series id to the snapshot
# field used by the SQLite provider. The InfluxDB provider maps the same ids to
# measurements/fields in ems.history.influx_provider. Dotted paths descend into
# nested objects in the SQLite snapshot payload.
SERIES_CATALOG = {
    "pv": {"label": "PV Input", "color_var": "--pv", "sqlite_field": "pv_total_w"},
    "output": {
        "label": "Inverter Output",
        "color_var": "--output",
        "sqlite_field": "inverter_output_w",
    },
    "battery": {
        "label": "Battery Power",
        "color_var": "--battery",
        "sqlite_field": "battery_power_w",
    },
    "soc": {"label": "SoC", "color_var": "--accent", "sqlite_field": "average_soc"},
    "home": {"label": "Home Load", "color_var": "--output", "sqlite_field": "home_load_w"},
    "grid": {"label": "Grid", "color_var": "--grid", "sqlite_field": "grid_power_w"},
    "target": {
        "label": "EMS Target",
        "color_var": "--accent2",
        "sqlite_field": "controller.commanded_total_w",
    },
}

DEFAULT_SERIES = ["pv", "output", "battery"]


@dataclass
class HistoryResult:
    """Columnar history result aligned on a shared time axis (for uPlot).

    ``time`` is a list of epoch-second timestamps. ``series`` maps each series
    id to a list of values parallel to ``time`` (``None`` for gaps).
    """

    source: str
    start: datetime
    end: datetime
    window: str
    time: list = field(default_factory=list)
    series: dict = field(default_factory=dict)
    devices: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "source": self.source,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "window": self.window,
            "time": self.time,
            "series": self.series,
            "devices": self.devices,
            "meta": self.meta,
        }


def normalize_series(series):
    """Validate requested series ids against the catalog; default if empty."""
    if not series:
        return list(DEFAULT_SERIES)
    valid = [name for name in series if name in SERIES_CATALOG]
    return valid or list(DEFAULT_SERIES)


class HistoryProvider(ABC):
    name = "abstract"

    @abstractmethod
    def available(self):
        """Whether this provider can currently serve queries."""

    @abstractmethod
    def query(self, start, end, window=None, devices=None, series=None):
        """Return a :class:`HistoryResult` for the time range and series."""


class SqliteHistoryProvider(HistoryProvider):
    """History from the existing local dashboard SQLite snapshot store.

    Reads the ``snapshots`` table read-only; it does not modify the database or
    depend on the dashboard package.
    """

    name = "sqlite"

    def __init__(self, database_path):
        self.database_path = database_path

    def available(self):
        return bool(self.database_path) and os.path.exists(self.database_path)

    def query(self, start, end, window=None, devices=None, series=None):
        series = normalize_series(series)
        result = HistoryResult(
            source=self.name,
            start=start,
            end=end,
            window=window or "raw",
            series={name: [] for name in series},
        )

        if not self.available():
            result.meta["unavailable"] = True
            return result

        rows = self._read_snapshots(start, end)
        for payload in rows:
            ts = _parse_iso(payload.get("timestamp"))
            if ts is None:
                continue
            result.time.append(int(ts.timestamp()))
            for name in series:
                spec = SERIES_CATALOG[name]
                result.series[name].append(
                    _coerce_number(_dig(payload, spec["sqlite_field"]))
                )

        result.meta["point_count"] = len(result.time)
        return result

    def _read_snapshots(self, start, end):
        uri = f"file:{self.database_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        try:
            cursor = con.execute(
                """
                SELECT payload FROM snapshots
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC
                """,
                (_iso(start), _iso(end)),
            )
            out = []
            for (raw,) in cursor.fetchall():
                try:
                    out.append(json.loads(raw))
                except (TypeError, ValueError):
                    continue
            return out
        finally:
            con.close()


def create_history_provider(
    influxdb_config,
    sqlite_database_path,
    *,
    influx_client=None,
):
    """Pick the active history provider.

    InfluxDB is used only when explicitly enabled in config; otherwise the
    zero-dependency SQLite provider keeps the default install working.
    ``influx_client`` may be injected for testing.
    """
    if influxdb_config and influxdb_config.get("enabled"):
        from ems.history.influx_provider import InfluxHistoryProvider

        return InfluxHistoryProvider(influxdb_config, client=influx_client)

    return SqliteHistoryProvider(sqlite_database_path)


# -- helpers ---------------------------------------------------------------


def _iso(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def _parse_iso(value):
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _dig(payload, dotted):
    current = payload
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _coerce_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
