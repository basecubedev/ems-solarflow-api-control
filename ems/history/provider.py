# SPDX-FileCopyrightText: 2026 basecubedev
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
from urllib.parse import unquote

from ems.models import MAX_PACK_NUMBER

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
    "ac_charge": {
        "label": "AC Charge",
        "color_var": "--grid",
        "sqlite_field": "inverter_charge_w",
    },
    "battery": {
        "label": "Battery Power",
        "color_var": "--battery",
        "sqlite_field": "battery_power_w",
    },
    "soc": {"label": "SoC", "color_var": "--accent", "sqlite_field": "average_soc"},
    "home": {"label": "Home Load", "color_var": "--output", "sqlite_field": "home_load_w"},
    "consumers": {
        "label": "Consumers",
        "color_var": "--output",
        "sqlite_field": "consumer_total_w",
    },
    "home_other": {
        "label": "Other Home Load",
        "color_var": "--output",
        "sqlite_field": "home_other_w",
    },
    "grid": {"label": "Grid", "color_var": "--grid", "sqlite_field": "grid_power_w"},
    "target": {
        "label": "EMS Target",
        "color_var": "--accent2",
        "sqlite_field": "controller.commanded_total_w",
    },
    **{
        name: {"label": label, "color_var": "--m-muted", "sqlite_field": None}
        for name, label in (
            ("device_temp", "Inverter temperature"),
            ("min_soc", "Minimum SoC"),
            ("winter_target", "Winter target"),
            ("heat_state", "Battery heating"),
            ("low_temp", "Too cold"),
            ("error_flag", "Error flag"),
            ("derate", "Derating"),
            ("winter_hold", "Export hold"),
            ("ac_input", "AC charging role"),
            ("charge_avail", "Charge accepted"),
            ("discharge_avail", "Discharge accepted"),
            ("input_limit", "AC charge limit"),
        )
    },
}

DEFAULT_SERIES = ["pv", "output", "battery"]

# A yes/no a device reports or the EMS decided, written as 1 or 0. It keeps
# whether it was on at all over a stretch, in InfluxDB over a window and here
# when a long range is thinned, so a short heating still shows.
STATE_SERIES = ("heat_state", "low_temp", "error_flag", "derate", "winter_hold", "ac_input")

# ``consumer:<name>`` reads one consumer's power, so a chart gets every
# consumer as its own line from one request. The name is percent-encoded, as
# the series list is split on commas and a name may hold one.
CONSUMER_SERIES_PREFIX = "consumer:"
MAX_CONSUMER_SERIES = 32
MAX_CONSUMER_NAME = 256

# ``cell_min:<pack>`` and ``cell_max:<pack>`` read one battery pack's lowest
# and highest cell voltage, the packs numbered as the live telemetry numbers
# them. Each keeps its extreme, across devices and in InfluxDB across a window
# and a downsampling step: a short dip of the lowest cell is what it is read for.
PACK_CELL_SERIES = {"cell_min": "cell_min_v", "cell_max": "cell_max_v"}
PACK_CELL_REDUCERS = {"cell_min_v": "min", "cell_max_v": "max"}
PACK_SERIES = {**PACK_CELL_SERIES, "pack_temp": "temp_c", "pack_soc": "soc"}
PACK_SERIES_REDUCERS = {**PACK_CELL_REDUCERS, "temp_c": "mean", "soc": "mean"}
# A pack's own power, state, temperature and state of charge stay out of the
# cell task. power_w carries no direction -- state does -- so a window's mean
# across a change of direction means nothing, and a field added to the cell
# task would leave every installation's existing task "kept" until an operator
# synced by hand. The temperature and the state of charge are carried by the
# battery task (ems.history.schema BATTERY_DOWNSAMPLED).
PACK_RAW_FIELDS = ("power_w", "state", "temp_c", "soc")
_REDUCE = {"min": min, "max": max, "mean": lambda values: sum(values) / len(values)}

# Per-device snapshot fields (under payload["devices"][name]) used when the
# SQLite provider is asked to filter by device. Series absent from this map are
# system-level only and resolve to None when a specific device is selected.
SERIES_DEVICE_FIELD = {
    "pv": "pv_input_w",
    "output": "output_w",
    "ac_charge": "ac_charge_w",
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
    the most recent sample is never dropped. A pack series keeps its reducer
    over the stretch each point stands for instead (a cell's extreme, so a
    short dip stays in the chart, a temperature's mean), and a state whether
    it was on at all. No-op when already small enough.
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
        if len(values) != count:
            continue
        reducer = _decimation_reducer(name)
        if reducer is None:
            result.series[name] = [values[i] for i in keep]
        else:
            result.series[name] = _stretch_extremes(values, keep, reducer)
    result.meta["point_count"] = len(result.time)
    result.meta["decimated"] = True
    return result


def _decimation_reducer(name):
    """How a thinned series sums up a stretch: a pack series by its reducer, a state by its maximum, else None."""

    pack = pack_cells_of_series(name)
    if pack is not None:
        return PACK_SERIES_REDUCERS[pack[0]]
    return "max" if name in STATE_SERIES else None


def _stretch_extremes(values, keep, reducer):
    """Each kept point's reduction (``reducer``) over the stretch since the one before it.

    A stretch without a value stays without one, so a gap longer than a
    stretch stays a gap.
    """

    pick = _REDUCE[reducer]
    extremes = []
    start = 0
    for index in keep:
        stretch = [value for value in values[start:index + 1] if value is not None]
        extremes.append(pick(stretch) if stretch else None)
        start = index + 1
    return extremes


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


def consumer_of_series(series_name):
    """The consumer a ``consumer:<name>`` series reads, or ``None``."""

    if not isinstance(series_name, str) or not series_name.startswith(CONSUMER_SERIES_PREFIX):
        return None
    name = unquote(series_name[len(CONSUMER_SERIES_PREFIX):])
    return name if 0 < len(name) <= MAX_CONSUMER_NAME else None


def pack_cells_of_series(series_name):
    """``(field, pack)`` a ``cell_min:<n>``, ``cell_max:<n>``, ``pack_temp:<n>`` or ``pack_soc:<n>`` series reads, or ``None``."""

    if not isinstance(series_name, str):
        return None
    kind, separator, number = series_name.partition(":")
    field_name = PACK_SERIES.get(kind)
    if not separator or field_name is None or not (number.isascii() and number.isdigit()):
        return None
    pack = int(number)
    if str(pack) != number or not 1 <= pack <= MAX_PACK_NUMBER:
        return None
    return field_name, pack


def normalize_series(series):
    """Validate requested series ids against the catalog; default if empty."""
    if not series:
        return list(DEFAULT_SERIES)
    consumers = list(dict.fromkeys(name for name in series if consumer_of_series(name)))[:MAX_CONSUMER_SERIES]
    valid = [
        name
        for name in series
        if name in SERIES_CATALOG or name in consumers or pack_cells_of_series(name)
    ]
    return list(dict.fromkeys(valid)) or list(DEFAULT_SERIES)


class HistoryProvider(ABC):
    name = "abstract"

    @abstractmethod
    def available(self):
        """Whether this provider can currently serve queries."""

    @abstractmethod
    def query(self, start, end, window=None, devices=None, series=None):
        """Return a :class:`HistoryResult` for the time range and series."""


MAX_HISTORY_POINTS = 2000

_SUMMARY_FIELDS = ("timestamp", *(spec["sqlite_field"] for spec in SERIES_CATALOG.values() if spec["sqlite_field"]))


_SUMMARY_SELECT = (
    "SELECT json_extract(payload, "
    + ", ".join(f"'$.{field}'" for field in _SUMMARY_FIELDS)
    + ") FROM snapshots WHERE rowid = ?"
)
_SUMMARY_WITH_DEVICES_SELECT = (
    "SELECT json_extract(payload, "
    + ", ".join(f"'$.{field}'" for field in (*_SUMMARY_FIELDS, "devices"))
    + ") FROM snapshots WHERE rowid = ?"
)
# Each consumer's power by its name as a JSON key, so no name has to fit
# inside a JSON path.
_CONSUMERS_SELECT = (
    "SELECT key, json_extract(value, '$.power_w') FROM snapshots, "
    "json_each(snapshots.payload, '$.consumers') WHERE snapshots.rowid = ?"
)


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
        consumers = {name: consumer_of_series(name) for name in series if consumer_of_series(name)}
        packs = {name: cells for name in series if (cells := pack_cells_of_series(name))}

        rows = self._read_snapshots(
            start,
            end,
            result.meta,
            devices=bool(device_filter) or bool(packs),
            consumers=bool(consumers),
        )
        for payload in rows:
            ts = _parse_iso(payload.get("timestamp"))
            if ts is None:
                continue
            result.time.append(int(ts.timestamp()))
            for name in series:
                if name in consumers:
                    value = _consumer_series_value(payload, consumers[name])
                elif name in packs:
                    value = _pack_series_value(payload, *packs[name], device_filter)
                elif device_filter:
                    value = _device_series_value(payload, name, device_filter)
                else:
                    spec = SERIES_CATALOG[name]
                    value = _coerce_number(payload.get(spec["sqlite_field"]))
                result.series[name].append(value)

        result.devices = device_filter
        result.meta["point_count"] = len(result.time)
        return result

    def _read_snapshots(self, start, end, meta=None, *, devices=False, consumers=False):
        """Yield snapshots, thinned to at most ``MAX_HISTORY_POINTS``.

        A 24 h window at a 5 s write interval is 17,280 snapshots of about
        20 KB each; a chart cannot show more points than its width. The rows
        to keep are picked from the timestamp index and only they are read.
        SQLite picks the system-level fields out of each, keyed by their dotted
        catalog path; ``devices`` adds the per-device part under ``devices``
        (the per-device and the pack series), and ``consumers`` each
        consumer's power under ``consumers``. Reading and decoding every
        payload took 30 s for two days on a Raspberry Pi 3, and the chart
        stayed empty that long.
        """

        uri = f"file:{self.database_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        try:
            rowids = [
                rowid
                for (rowid,) in con.execute(
                    """
                    SELECT rowid FROM snapshots
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp ASC
                    """,
                    (_iso(start), _iso(end)),
                )
            ]
            stride = max(1, -(-len(rowids) // MAX_HISTORY_POINTS))
            if meta is not None and stride > 1:
                meta["stride"] = stride
            for rowid in rowids[::stride]:
                try:
                    if devices:
                        row = con.execute(_SUMMARY_WITH_DEVICES_SELECT, (rowid,)).fetchone()
                        values = dict(zip((*_SUMMARY_FIELDS, "devices"), json.loads(row[0])))
                    else:
                        row = con.execute(_SUMMARY_SELECT, (rowid,)).fetchone()
                        values = dict(zip(_SUMMARY_FIELDS, json.loads(row[0])))
                    if consumers:
                        values["consumers"] = {
                            key: {"power_w": power}
                            for key, power in con.execute(_CONSUMERS_SELECT, (rowid,))
                        }
                except (sqlite3.OperationalError, TypeError, ValueError):
                    continue
                yield values
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


def _consumer_series_value(payload, name):
    """One consumer's power from a snapshot, ``None`` while it did not read."""

    consumers = payload.get("consumers")
    consumer = consumers.get(name) if isinstance(consumers, dict) else None
    return _coerce_number(consumer.get("power_w")) if isinstance(consumer, dict) else None


def _pack_series_value(payload, field_name, pack, device_names):
    """One pack's cell voltage or temperature in a snapshot, over the selected devices or all.

    The reducer ``PACK_SERIES_REDUCERS`` names for the field: a cell voltage's
    extreme, a temperature's or a charge's mean. Without a choice the devices the EMS
    controls count, the ones InfluxDB records packs for.
    """

    devices = payload.get("devices")
    if not isinstance(devices, dict):
        return None
    controlled = [name for name, device in devices.items() if isinstance(device, dict) and not device.get("read_only")]
    values = []
    for name in device_names or controlled:
        device = devices.get(name)
        entries = device.get("battery_packs") if isinstance(device, dict) else None
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, dict) and entry.get("pack") == pack:
                number = _coerce_number(entry.get(field_name))
                if number is not None:
                    values.append(number)
    if not values:
        return None
    return _REDUCE[PACK_SERIES_REDUCERS[field_name]](values)


def _device_series_value(payload, series_name, device_names):
    """Aggregate one series across the selected devices in a snapshot.

    Power series are summed; ``soc`` is averaged over the devices that report
    one. Series with no per-device field (home/grid/target) are system-level
    only and resolve to None.
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
        if series_name == "soc" and device.get("soc_reported") is False:
            continue
        number = _coerce_number(device.get(field))
        if number is not None:
            values.append(number)
    if not values:
        return None
    if series_name == "soc":
        return sum(values) / len(values)
    return sum(values)


def _coerce_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
