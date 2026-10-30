# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a mapped device would publish now, and why it would not.

Pure: the latest payload of each topic goes in, a ``state`` object or a reason
comes out. The service publishes with it and the Admin preview shows it, so a
preview is what the service would really do. Every value passes the external
catalog's own rule last, so nothing is published that the EMS would refuse.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from ems.zendure_mqtt.external_catalog import EMS_SOLARFLOW
from ems.zendure_mqtt.payload_values import extract_number

PAYLOAD_PREVIEW_CHARS = 200
SILENCE_REASONS = (
    "disabled",
    "source_missing",
    "stale",
    "source_unreadable",
    "catalog_refused",
)


@dataclass(frozen=True)
class Received:
    """The latest payload of one topic and when it arrived, on the monotonic clock."""

    payload: bytes
    at: float


@dataclass(frozen=True)
class SourceReading:
    topic: str
    path: str | None
    value: int | float | None
    reason: str | None
    age_seconds: float | None
    payload_preview: str | None


@dataclass(frozen=True)
class TargetReading:
    key: str
    value: int | float | None
    reason: str | None


@dataclass(frozen=True)
class Evaluation:
    """A device's ``state`` when every source is fresh and every value passes, else a reason."""

    device_id: str
    state: dict | None
    reason: str | None
    targets: tuple[TargetReading, ...] = ()
    sources: tuple[SourceReading, ...] = field(default_factory=tuple)


def payload_preview(payload) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        text = bytes(payload[: PAYLOAD_PREVIEW_CHARS * 4]).decode("utf-8", errors="replace")
    else:
        text = str(payload)
    return text[:PAYLOAD_PREVIEW_CHARS]


def source_value(source, payload):
    """``(value, None)`` after factor, sign and the negative rule, or ``(None, reason)``."""

    number, reason = extract_number(payload, source.format, source.path)
    if number is None:
        return None, reason
    value = number * source.factor
    if source.invert:
        value = -value
    if source.negative == "zero" and value < 0:
        value = 0
    return value, None


def _read_source(source, received, now):
    if received is None:
        return SourceReading(source.topic, source.path, None, "source_missing", None, None)
    age = max(0.0, now - received.at)
    preview = payload_preview(received.payload)
    if age > source.max_age_seconds:
        return SourceReading(source.topic, source.path, None, "stale", age, preview)
    value, reason = source_value(source, received.payload)
    return SourceReading(source.topic, source.path, value, reason, age, preview)


def _clean(value):
    value = round(value, 3)
    return int(value) if value == int(value) else value


def _first_reason(readings):
    for wanted in ("source_missing", "stale"):
        if any(reading.reason == wanted for reading in readings):
            return wanted
    return "source_unreadable"


def evaluate_device(device, latest: Mapping[str, Received], now: float) -> Evaluation:
    """What ``device`` would publish at ``now`` from ``latest`` (topic → :class:`Received`)."""

    if not device.enabled:
        return Evaluation(device.id, None, "disabled")

    def read(group):
        return [_read_source(source, latest.get(source.topic), now) for source in group]

    sources = []
    targets = []
    state = {}
    silent_reason = None
    for target in device.targets:
        plain, charge, discharge = read(target.sources), read(target.charge), read(target.discharge)
        group = plain + charge + discharge
        sources.extend(group)
        unread = [reading for reading in group if reading.value is None]
        if unread:
            reason = _first_reason(unread)
            targets.append(TargetReading(target.key, None, reason))
            silent_reason = silent_reason or reason
            continue
        total = _clean(
            sum(reading.value for reading in plain + charge)
            - sum(reading.value for reading in discharge)
        )
        if EMS_SOLARFLOW.readings(target.key, total) is None:
            targets.append(TargetReading(target.key, total, "catalog_refused"))
            silent_reason = silent_reason or "catalog_refused"
            continue
        targets.append(TargetReading(target.key, total, None))
        state[target.key] = total
    if silent_reason is not None:
        return Evaluation(device.id, None, silent_reason, tuple(targets), tuple(sources))
    return Evaluation(device.id, state, None, tuple(targets), tuple(sources))


def preview_device(device, payloads: Mapping[str, object]) -> Evaluation:
    """What ``device`` would publish if each topic had just carried the given payload."""

    received = {
        topic: Received(payload if isinstance(payload, bytes) else str(payload).encode("utf-8"), 0.0)
        for topic, payload in payloads.items()
    }
    return evaluate_device(device, received, 0.0)
