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

import logging
import threading
import time
from datetime import datetime, timezone

from ems.config import influx_duration_seconds
from ems.influx_setup import (
    INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS,
    runtime_influx_token,
    runtime_influx_url,
)
from ems.logging_utils import log_event
from ems.history.provider import (
    HistoryProvider,
    HistoryResult,
    normalize_series,
)
from ems.history.schema import bucket_name, planned_bucket_names

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
    "ac_charge": {
        "measurement": "zendure_device",
        "field": "grid_input",
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


# How long a missing-bucket answer is reused. It only changes when someone
# runs a schema sync, and the dashboard asks once per failed request.
MISSING_BUCKETS_CACHE_SECONDS = 60

# How long the whole probe may take. It runs on a request thread, after a query
# that already failed, and an InfluxDB that drops packets would otherwise hold
# that thread for one client timeout per planned bucket.
MISSING_BUCKETS_PROBE_SECONDS = 10
# How many probes one hold may spend. A slow InfluxDB settles as little as one
# bucket per probe -- the one the asking range reads, which goes first -- and
# the next request picks up the rest; without a cap that is one probe per poll
# against an InfluxDB that never answers.
MISSING_BUCKETS_PROBE_ROUNDS = 4


class _SchemaHold:
    """What one cache window knows, and how much probing it has paid for."""

    __slots__ = ("expires_at", "settled", "rounds")

    def __init__(self, expires_at):
        self.expires_at = expires_at
        self.settled = {}
        self.rounds = 0


def _now():
    """Seam for the probe clock, so a test never patches the stdlib module."""

    return time.monotonic()


class InfluxHistoryProvider(HistoryProvider):
    name = "influxdb"

    def __init__(self, influx_config, client=None):
        self.config = influx_config
        self._client = client
        self._missing_buckets_cache = None
        self._missing_buckets_lock = threading.Lock()
        # Single flight belongs to the provider, not to one hold: a probe still
        # running when its hold expires would otherwise release a guard that a
        # freshly installed hold no longer holds.
        self._probe_in_flight = False

    def client(self):
        if self._client is None:
            from ems.history.influx_client import HistoryInfluxClient

            self._client = HistoryInfluxClient(
                runtime_influx_url(self.config),
                self.config["org"],
                runtime_influx_token(self.config),
            )
        return self._client

    def bucket_for_range(self, start, end):
        """The bucket a query over this range reads, by the config's profiles."""

        range_seconds = max(0, int((end - start).total_seconds()))
        bucket_key, _window = resolve_query_bucket(self.config, range_seconds)

        return bucket_name(self.config["bucket_prefix"], bucket_key)

    def is_planned_bucket(self, name):
        """Whether a schema sync would create this bucket.

        A query profile can name one that no downsampling entry produces, and
        that bucket is exactly the one that will never exist -- so "run influx
        sync" is not the answer for it.
        """

        return name in planned_bucket_names(self.config)

    def missing_buckets(self, first=None):
        """Planned buckets known not to exist, in pipeline order.

        Returns ``None`` when the schema could not be read, which is not the
        same answer as an empty list and must not be confused with it.

        Worth asking only after a query failed: InfluxDB answers a query against
        a bucket that is not there with the same 404 it uses for an unknown org,
        so the cause cannot be read off the failure itself.

        ``first`` names the bucket the caller's answer hinges on. It is looked
        up before the rest, and an answer that does not cover it is ``None``
        rather than a list it is absent from -- "could not check" would
        otherwise read as "exists".

        The answer is held briefly because it only changes when someone runs a
        schema sync, while the question is asked once per failed request.
        """

        planned = planned_bucket_names(self.config)
        if first is not None and first not in planned:
            # A query profile may name a bucket the downsampling plan never
            # creates. It is still the bucket this range reads, and the only one
            # that can explain its failure, so it is probed and reported.
            planned = [first] + planned

        with self._missing_buckets_lock:
            hold = self._missing_buckets_cache
            if hold is None or _now() >= hold.expires_at:
                hold = _SchemaHold(_now() + MISSING_BUCKETS_CACHE_SECONDS)
                self._missing_buckets_cache = hold
            todo = [name for name in planned if name not in hold.settled]
            # Only when the asking range has no answer yet. The rest of the
            # list is context, and a probe round costs this request thread ten
            # seconds; paying that to fill in context for a payload that is
            # already decided is the wrong trade on the hardware this is for.
            if first is not None and first in hold.settled:
                todo = []
            # Rounds are counted rather than names claimed up front: a probe
            # that runs out of budget leaves the rest for a later request, so a
            # range whose bucket was never reached is not answered with silence
            # for the whole hold. The cap bounds that against an InfluxDB that
            # never answers, and one probe at a time keeps a request that
            # arrives mid-probe from starting a second.
            probe = (
                bool(todo)
                and not self._probe_in_flight
                and hold.rounds < MISSING_BUCKETS_PROBE_ROUNDS
            )
            if probe:
                self._probe_in_flight = True
                hold.rounds += 1

        # Outside the lock: a second request must degrade to the plain failure,
        # not queue behind a ten-second probe on a request thread.
        if probe:
            try:
                self._probe_buckets(todo, first, hold)
            finally:
                with self._missing_buckets_lock:
                    self._probe_in_flight = False

        # Copied under the lock: another thread's probe may still be writing
        # into it, and an answer assembled from a dict mid-fill lists fewer
        # missing buckets than the same question would a moment later.
        with self._missing_buckets_lock:
            settled = dict(hold.settled)

        if not settled or (first is not None and first not in settled):
            return None

        return [name for name in planned if settled.get(name) is False]

    def _probe_buckets(self, todo, first, hold):
        """Look buckets up into the hold, as far as the budget allows.

        What a lookup settled is kept when a later one fails or the budget runs
        out, so a probe that gives up halfway can still answer for the bucket it
        was asked about.
        """

        order = list(todo)
        if first in order:
            order.remove(first)
            order.insert(0, first)

        deadline = _now() + MISSING_BUCKETS_PROBE_SECONDS

        try:
            client = self.client()
            for name in order:
                remaining = deadline - _now()
                if remaining <= 0:
                    return
                # The same per-phase tolerance the rest of the project gives
                # these lookups, bounded by what is left. Halving the remainder
                # each round instead would make every lookup after the first
                # stricter than the one before it, on the hardware where that
                # matters -- and the budget here is the request thread's, not
                # the schema's.
                phase = max(
                    0.1,
                    min(INFLUX_PROBE_REQUEST_TIMEOUT_SECONDS, remaining / 2),
                )
                found = client.find_bucket(name, timeout=(phase, phase)) is not None
                # Under the lock, so the answer another thread assembles from
                # this dict is one state of it rather than a mid-fill snapshot.
                # Never held across the lookup above.
                with self._missing_buckets_lock:
                    hold.settled[name] = found
        except Exception as exc:
            log_event(
                logging.WARNING,
                "analytics_schema_probe_failed",
                error=type(exc).__name__,
                settled=len(hold.settled),
            )

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
