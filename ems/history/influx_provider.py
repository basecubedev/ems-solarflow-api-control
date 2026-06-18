# SPDX-License-Identifier: AGPL-3.0-or-later
"""InfluxDB-backed history provider: query profiles, device filtering, series.

Query profiles map a requested time range to the bucket/window that serves it
fast (raw for short ranges, downsampled for long ones), mirroring the
"aggregate-bucket-first" strategy of the developer query scripts. Device
filtering restricts per-device measurements to a selected device set; house
level measurements (grid meter) stay unfiltered.

The Flux builders and CSV parser are pure functions so they can be unit-tested
without a live InfluxDB.

Import-side-effect-free.
"""

from datetime import datetime, timezone

from ems.config import influx_duration_seconds
from ems.influx_setup import runtime_influx_token, runtime_influx_url
from ems.history.provider import (
    HistoryProvider,
    HistoryResult,
    normalize_series,
)
from ems.history.schema import bucket_name

# Maps catalog series ids to their InfluxDB source. ``collapse`` is how values
# from multiple devices/sources combine within a window: power sums, SoC
# averages. ``derived`` series are computed from two fields in Python.
INFLUX_SERIES = {
    "pv": {
        "measurement": "zendure_device",
        "field": "solar",
        "device_scoped": True,
        "collapse": "sum",
    },
    "output": {
        "measurement": "zendure_device",
        "field": "output",
        "device_scoped": True,
        "collapse": "sum",
    },
    "soc": {
        "measurement": "zendure_device",
        "field": "soc",
        "device_scoped": True,
        "collapse": "mean",
    },
    "home": {
        "measurement": "shelly_meter",
        "field": "house_load",
        "device_scoped": False,
        "collapse": "mean",
    },
    # grid = meter exchange power (positive import, negative export).
    "grid": {
        "measurement": "shelly_meter",
        "field": "grid_power",
        "device_scoped": False,
        "collapse": "mean",
    },
    # target = EMS effective output target after limits/safety logic.
    "target": {
        "measurement": "ems_runtime",
        "field": "target_output",
        "device_scoped": False,
        "collapse": "mean",
    },
    # battery power = discharge(pack_out) - charge(pack_in)
    "battery": {
        "measurement": "zendure_device",
        "derived": ("pack_out", "pack_in"),
        "device_scoped": True,
        "collapse": "sum",
    },
}


def select_query_profile(profiles, range_seconds):
    """Pick the profile for a range: smallest ``max_range`` that covers it.

    Profiles are expected sorted ascending by ``max_range`` (config
    normalization guarantees this). Ranges larger than every profile fall back
    to the coarsest (last) profile. Returns ``None`` when there are no profiles.
    """
    if not profiles:
        return None

    for profile in profiles:
        if range_seconds <= influx_duration_seconds(profile["max_range"]):
            return profile

    return profiles[-1]


def resolve_query_bucket(influx_config, range_seconds):
    """Return (bucket_key, window) for a requested range via query profiles."""
    profile = select_query_profile(
        influx_config.get("query_profiles", []), range_seconds
    )
    if profile is None:
        return "raw", "1m"
    return profile["bucket"], profile["window"]


def build_device_filter(devices):
    """Flux predicate restricting r.device to the selected devices, or ''."""
    selected = [str(d) for d in (devices or []) if str(d).strip()]
    if not selected:
        return ""
    clause = " or ".join(f'r.device == "{_escape(name)}"' for name in selected)
    return f"  |> filter(fn: (r) => {clause})\n"


def build_field_flux(
    bucket,
    measurement,
    field_name,
    window,
    start,
    stop,
    *,
    devices=None,
    device_scoped=False,
    collapse="sum",
):
    """Render a Flux query for one field, collapsed to one value per window."""
    reducer = "sum" if collapse == "sum" else "mean"
    device_filter = build_device_filter(devices) if device_scoped else ""

    return (
        f'from(bucket: "{bucket}")\n'
        f"  |> range(start: {_flux_time(start)}, stop: {_flux_time(stop)})\n"
        f'  |> filter(fn: (r) => r._measurement == "{measurement}")\n'
        f'  |> filter(fn: (r) => r._field == "{field_name}")\n'
        f"{device_filter}"
        f"  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)\n"
        '  |> group(columns: ["_time"])\n'
        f"  |> {reducer}()\n"
        '  |> sort(columns: ["_time"])\n'
    )


def parse_series_csv(csv_text):
    """Parse Influx CSV into a {epoch_seconds: value} dict, dropping nulls."""
    from scripts.influx_utils import coerce_value, parse_influx_csv

    out = {}
    for row in parse_influx_csv(csv_text):
        ts = _parse_iso(row.get("_time"))
        if ts is None:
            continue
        value = coerce_value(row.get("_value"))
        if value is None or isinstance(value, bool):
            continue
        try:
            out[int(ts.timestamp())] = float(value)
        except (TypeError, ValueError):
            continue
    return out


class InfluxHistoryProvider(HistoryProvider):
    name = "influxdb"

    def __init__(self, influx_config, client=None):
        self.config = influx_config
        self._client = client

    def client(self):
        if self._client is None:
            from ems.history.influx_client import HistoryInfluxClient

            self._client = HistoryInfluxClient(
                runtime_influx_url(self.config),
                self.config["org"],
                runtime_influx_token(self.config),
            )
        return self._client

    def available(self):
        if not self.config.get("enabled"):
            return False
        return bool(runtime_influx_url(self.config)) and bool(
            runtime_influx_token(self.config)
        )

    def query(self, start, end, window=None, devices=None, series=None):
        series = normalize_series(series)
        range_seconds = max(0, int((end - start).total_seconds()))
        bucket_key, profile_window = resolve_query_bucket(
            self.config, range_seconds
        )
        window = window or profile_window
        bucket = bucket_name(self.config["bucket_prefix"], bucket_key)

        result = HistoryResult(
            source=self.name,
            start=start,
            end=end,
            window=window,
            devices=list(devices or []),
            meta={"bucket": bucket, "bucket_key": bucket_key},
        )

        per_series_points = {}
        all_timestamps = set()

        for name in series:
            spec = INFLUX_SERIES.get(name)
            if spec is None:
                # Series with no InfluxDB mapping; leave empty rather than fail.
                per_series_points[name] = {}
                continue
            points = self._query_series(spec, bucket, window, devices, start, end)
            per_series_points[name] = points
            all_timestamps.update(points.keys())

        timeline = sorted(all_timestamps)
        result.time = timeline
        for name in series:
            points = per_series_points.get(name, {})
            result.series[name] = [points.get(ts) for ts in timeline]

        result.meta["point_count"] = len(timeline)
        return result

    def _query_series(self, spec, bucket, window, devices, start, end):
        client = self.client()

        if "derived" in spec:
            positive_field, negative_field = spec["derived"]
            positive = parse_series_csv(
                client.query_raw(
                    build_field_flux(
                        bucket,
                        spec["measurement"],
                        positive_field,
                        window,
                        start,
                        end,
                        devices=devices,
                        device_scoped=spec["device_scoped"],
                        collapse=spec["collapse"],
                    )
                )
            )
            negative = parse_series_csv(
                client.query_raw(
                    build_field_flux(
                        bucket,
                        spec["measurement"],
                        negative_field,
                        window,
                        start,
                        end,
                        devices=devices,
                        device_scoped=spec["device_scoped"],
                        collapse=spec["collapse"],
                    )
                )
            )
            combined = {}
            for ts in set(positive) | set(negative):
                combined[ts] = positive.get(ts, 0.0) - negative.get(ts, 0.0)
            return combined

        return parse_series_csv(
            client.query_raw(
                build_field_flux(
                    bucket,
                    spec["measurement"],
                    spec["field"],
                    window,
                    start,
                    end,
                    devices=devices,
                    device_scoped=spec["device_scoped"],
                    collapse=spec["collapse"],
                )
            )
        )


# -- helpers ---------------------------------------------------------------


def _escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _flux_time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _parse_iso(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
