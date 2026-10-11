# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime snapshot model and aggregator for Zendure MQTT telemetry.

Consumes classified topics + parsed payloads into per-device snapshots. Read
only: it never writes hardware. The derived alias fields are for display;
``observable_metrics`` is the filter ``parse_device`` reads a report through.
Confirmation and foreign-writer detection read the merged metrics with checks
of their own.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from ems.zendure_mqtt.payloads import coerce_scalar, parse_report_payload
from ems.zendure_mqtt.topics import (
    FAMILY_UNKNOWN,
    JSON_FAMILIES,
    SCALAR_FAMILIES,
    classify_topic,
)

_SOLAR_CHANNELS = tuple(f"solarPower{n}" for n in range(1, 7))


@dataclass
class ZendureMqttSnapshot:
    device_id: str | None
    serial_number: str | None = None
    product_key: str | None = None
    product: str | None = None
    topic_families: set = field(default_factory=set)
    metrics: dict[str, Any] = field(default_factory=dict)
    # Per-key report time: merged snapshots keep old values beside fresh ones.
    metric_monotonic: dict[str, float] = field(default_factory=dict)
    # Exact property keys carried by the most recently observed MQTT message.
    observed_metrics: set[str] = field(default_factory=set)
    battery_packs: list[dict[str, Any]] = field(default_factory=list)
    capabilities: set = field(default_factory=set)
    seen_topics: set = field(default_factory=set)
    # Wall-clock epoch for display; monotonic clock for age/staleness math.
    last_seen_epoch: float | None = None
    last_seen_monotonic: float | None = None


_EXACT_FLOAT_LIMIT = 2**53


def observable_metrics(metrics):
    """A report's metrics as ``parse_device`` may read them: numbers below 2**53 only.

    Any client on the broker can publish a scalar topic, and a scalar topic
    keeps text, and numbers no float can hold, as they came. ``parse_device``
    hands every field straight to the control maths and the dashboard, which
    compare and divide it; dropped here, such a value reads as a missing one.
    So does a number of 2**53 or more: past it a float no longer holds every
    integer, the pack count is read through a float, and the full-charge state
    store keeps it and its neighbours as SQLite integers, which end at 2**63.
    A number a report quotes as text was read as that number when it arrived.
    """

    return {
        key: value
        for key, value in (metrics or {}).items()
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and abs(value) < _EXACT_FLOAT_LIMIT
    }


def pack_witness(snapshot):
    """What ``parse_device`` weighs against a ``packNum`` of 0.

    The aggregator's pack list, or failing that the ``packData`` value the
    aggregator kept, when it is not empty: ``observable_metrics`` keeps numbers
    only, so it would be gone from the properties. A quoted number in it was
    read as that number when the report arrived, so ``"0"`` is no witness.
    """

    packs = getattr(snapshot, "battery_packs", None)
    if packs:
        return packs
    return (getattr(snapshot, "metrics", None) or {}).get("packData")


def _to_number(value):
    number = coerce_scalar(value)
    return number if isinstance(number, (int, float)) and not isinstance(number, bool) else None


def _truthy(value):
    number = _to_number(value)
    if number is not None:
        return number != 0
    if isinstance(value, str):
        return value.strip().lower() in ("on", "true", "yes", "enabled")
    return bool(value)


def infer_capabilities(metrics, battery_packs):
    """Infer device capabilities from observed keys, not from model names.

    Key presence, for discovery display only. ``packNum: 0`` still counts as
    battery evidence here; the control side asks ``battery_presence`` for the
    value instead, and the two are not interchangeable.
    """

    caps = set()
    if battery_packs or "packNum" in metrics or "electricLevel" in metrics:
        caps.add("battery_storage")
    if "solarInputPower" in metrics or any(ch in metrics for ch in _SOLAR_CHANNELS):
        caps.add("pv_input")
    if "outputLimit" in metrics or "inverseMaxPower" in metrics:
        caps.add("output_control")
    if "inputLimit" in metrics or "gridInputPower" in metrics or "acMode" in metrics:
        caps.add("ac_input_control")
    if "gridOffPower" in metrics or "gridOffMode" in metrics:
        caps.add("offgrid_output")
    charge_max = _to_number(metrics.get("chargeMaxLimit"))
    if (
        "solarPower5" in metrics
        or "solarPower6" in metrics
        or "phaseSwitch" in metrics
        or (charge_max is not None and charge_max >= 2400)
    ):
        caps.add("multi_mppt")
    return caps


def _normalized_fields(metrics):
    """Derived alias fields; the metrics, numeric text as its number, are kept alongside."""

    normalized = {}
    if "fanSwitch" in metrics:
        normalized["fan_enabled"] = _truthy(metrics["fanSwitch"])
    for key in ("Fanmode", "fanMode"):
        if key in metrics:
            normalized["fan_mode"] = metrics[key]
    for key in ("fanSpeed", "Fanspeed"):
        if key in metrics:
            normalized["fan_speed"] = metrics[key]
    for key in ("OldMode", "oldMode"):
        if key in metrics:
            normalized["old_mode"] = metrics[key]
    if "BatVolt" in metrics:
        normalized["battery_voltage_raw"] = metrics["BatVolt"]
    channels = {n: metrics[f"solarPower{n}"] for n in range(1, 7) if f"solarPower{n}" in metrics}
    if channels:
        normalized["pv_channels"] = channels
    for key, target in (("socSet", "soc_set_percent"), ("minSoc", "min_soc_percent")):
        number = _to_number(metrics.get(key))
        if number is not None:
            normalized[target] = number / 10 if number > 100 else number
    return normalized


class ZendureMqttAggregator:
    """Groups observed MQTT messages into per-device runtime snapshots.

    Pure apart from its own state: feed ``(topic, payload)`` pairs to ``observe``
    and read back merged snapshots. Grouping key is the device id from the topic.
    """

    def __init__(self, *, monotonic=time.monotonic, wall_clock=time.time):
        self._devices = {}
        self._monotonic = monotonic
        self._wall_clock = wall_clock

    def observe(self, topic, payload=None):
        match = classify_topic(topic)
        if match.family == FAMILY_UNKNOWN:
            return
        device_id = match.device_id or match.serial_number
        if not device_id:
            return
        snap = self._devices.get(device_id)
        if snap is None:
            snap = ZendureMqttSnapshot(device_id=device_id)
            self._devices[device_id] = snap
        snap.last_seen_epoch = self._wall_clock()
        snap.last_seen_monotonic = self._monotonic()
        snap.topic_families.add(match.family)
        snap.seen_topics.add(topic)
        snap.observed_metrics = set()
        if match.serial_number and not snap.serial_number:
            snap.serial_number = match.serial_number
        if match.product_key and not snap.product_key:
            snap.product_key = match.product_key

        if match.family in SCALAR_FAMILIES and match.metric:
            snap.metrics[match.metric] = coerce_scalar(payload)
            snap.metric_monotonic[match.metric] = snap.last_seen_monotonic
            snap.observed_metrics.add(match.metric)
        elif match.family in JSON_FAMILIES:
            self._merge_report(snap, payload)

    def _merge_report(self, snap, payload):
        report = parse_report_payload(payload)
        if report.serial_number:
            snap.serial_number = report.serial_number
        if report.product and not snap.product:
            snap.product = report.product
        snap.metrics.update(
            (key, coerce_scalar(value) if isinstance(value, str) else value)
            for key, value in report.properties.items()
        )
        for key in report.properties:
            snap.metric_monotonic[key] = snap.last_seen_monotonic
        snap.observed_metrics.update(report.properties)
        if report.battery_packs:
            snap.battery_packs = report.battery_packs

    def snapshots(self):
        """Return merged snapshots with normalization and capabilities applied."""

        results = []
        for snap in self._devices.values():
            metrics = dict(snap.metrics)
            metrics.update(_normalized_fields(snap.metrics))
            results.append(
                ZendureMqttSnapshot(
                    device_id=snap.device_id,
                    serial_number=snap.serial_number,
                    product_key=snap.product_key,
                    product=snap.product,
                    topic_families=set(snap.topic_families),
                    metrics=metrics,
                    metric_monotonic=dict(snap.metric_monotonic),
                    observed_metrics=set(snap.observed_metrics),
                    battery_packs=list(snap.battery_packs),
                    capabilities=infer_capabilities(snap.metrics, snap.battery_packs),
                    seen_topics=set(snap.seen_topics),
                    last_seen_epoch=snap.last_seen_epoch,
                    last_seen_monotonic=snap.last_seen_monotonic,
                )
            )
        return results
