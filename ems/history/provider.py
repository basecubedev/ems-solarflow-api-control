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
from datetime import datetime, timedelta, timezone

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

# Per-device snapshot fields (under payload["devices"][name]) used when the
# SQLite provider is asked to filter by device. Series absent from this map are
# system-level only and resolve to None when a specific device is selected.
SERIES_DEVICE_FIELD = {
    "pv": "pv_input_w",
    "output": "output_w",
    "battery": "battery_power_w",
    "soc": "soc",
}

# UI period tokens shared by the Dashboard (fixed 24h) and Analytics views.
# Both must map to the same start/end resolution so they share one API.
HISTORY_RANGE_SECONDS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "365d": 365 * 86400,
}

DEFAULT_RANGE = "24h"

# Keep payloads chart-friendly: uPlot stays fast at a few thousand points per
# series, and the wire payload stays small. Raw SQLite snapshots over long
# ranges can blow past this, so the endpoint decimates to at most this many
# points (the Influx provider is already bounded by its aggregate window).
DEFAULT_MAX_POINTS = 2000


def resolve_range(range_name, now=None):
    """Map a UI period token to an aware ``(start, end)`` tuple.

    Unknown tokens fall back to :data:`DEFAULT_RANGE` so the endpoint never
    rejects a request purely on the period name.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    seconds = HISTORY_RANGE_SECONDS.get(
        range_name, HISTORY_RANGE_SECONDS[DEFAULT_RANGE]
    )
    return now - timedelta(seconds=seconds), now


def decimate_history_result(result, max_points=DEFAULT_MAX_POINTS):
    """Stride-sample a :class:`HistoryResult` in place to ``max_points``.

    Keeps the time axis and every series aligned; always keeps the last point so
    the most recent sample is never dropped. No-op when already small enough.
    Returns the (possibly mutated) result for convenience.
    """
    count = len(result.time)
    if max_points <= 0 or count <= max_points:
        return result

    # Evenly spaced indices including both endpoints; at most ``max_points``
    # (set() collapses any rounding collisions). Keeps first and last samples.
    keep = sorted(
        {round(i * (count - 1) / (max_points - 1)) for i in range(max_points)}
    )

    result.time = [result.time[i] for i in keep]
    for name, values in result.series.items():
        if len(values) == count:
            result.series[name] = [values[i] for i in keep]
    result.meta["point_count"] = len(result.time)
    result.meta["decimated"] = True
    return result


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

        device_filter = [d for d in (devices or []) if d]

        rows = self._read_snapshots(start, end)
        for payload in rows:
            ts = _parse_iso(payload.get("timestamp"))
            if ts is None:
                continue
            result.time.append(int(ts.timestamp()))
            for name in series:
                if device_filter:
                    value = _device_series_value(payload, name, device_filter)
                else:
                    spec = SERIES_CATALOG[name]
                    value = _coerce_number(_dig(payload, spec["sqlite_field"]))
                result.series[name].append(value)

        result.devices = device_filter
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


def _device_series_value(payload, series_name, device_names):
    """Aggregate one series across the selected devices in a snapshot.

    Power series are summed; ``soc`` is averaged. Series with no per-device
    field (home/grid/target) are system-level only and resolve to None.
    """
    field = SERIES_DEVICE_FIELD.get(series_name)
    if not field:
        return None
    devices = payload.get("devices")
    if not isinstance(devices, dict):
        return None
    values = []
    for name in device_names:
        device = devices.get(name)
        if not isinstance(device, dict):
            continue
        number = _coerce_number(device.get(field))
        if number is not None:
            values.append(number)
    if not values:
        return None
    if series_name == "soc":
        return sum(values) / len(values)
    return sum(values)


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
