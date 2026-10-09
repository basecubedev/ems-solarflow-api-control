# SPDX-License-Identifier: AGPL-3.0-or-later
"""The devices this project reads over MQTT without being Zendure hardware.

The catalog is a whitelist. An entry fixes how a device's topics are built --
``<prefix>/<device id>/<key>``, or one bundle topic carrying several keys as a
JSON object -- and what the payload behind each known key means. A topic of any
other shape, or a key the entry does not name, is never read and never offered,
because nobody can say what its payload is.

The one entry is the project's own namespace: whatever bridges a device to the
broker publishes into it, so the payload is fixed by this project rather than by
a manufacturer.
"""

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ems.device_identity import is_masked_identity_value
from ems.zendure_mqtt.payloads import parse_json_object

Readings = dict[str, int | float]

_DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PLAIN_NUMBER = re.compile(r"-?[0-9]{1,16}(?:\.[0-9]{1,16})?")
MAX_READING = 1_000_000


@dataclass(frozen=True)
class CatalogKey:
    """One key a catalog device publishes, and the project metrics it becomes."""

    metrics: tuple[str, ...]
    read: Callable[[int | float], Readings | None]


def _non_negative(metric: str) -> CatalogKey:
    return CatalogKey((metric,), lambda value: {metric: value} if value >= 0 else None)


def _percent(metric: str) -> CatalogKey:
    return CatalogKey(
        (metric,), lambda value: {metric: value} if 0 <= value <= 100 else None
    )


def _signed(positive: str, negative: str) -> CatalogKey:
    """A signed reading split into the two unsigned metrics a Zendure reports."""

    return CatalogKey(
        (positive, negative),
        lambda value: {
            positive: value if value > 0 else 0,
            negative: -value if value < 0 else 0,
        },
    )


def _number(value) -> int | float | None:
    """A plain decimal number of plausible size, or ``None``.

    Text must be ASCII digits with an optional sign and fraction, sixteen
    digits at most on either side: Python's own parsers would also take
    ``1_000``, other scripts' digits and exponents, and refuse a few thousand
    digits by raising. A number beyond ``MAX_READING`` is refused before
    anything converts it to a float, which a long enough integer overflows.
    """

    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        text = value.strip()
        if _PLAIN_NUMBER.fullmatch(text) is None:
            return None
        value = float(text) if "." in text else int(text)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if abs(value) > MAX_READING or not math.isfinite(value):
        return None
    return value


def valid_device_id(value) -> bool:
    """Whether ``value`` can name a device in a catalog topic.

    It becomes a topic segment and a subscription filter, so a wildcard or a
    separator would read another device's topics, or none; and the identity
    rules every device shares refuse a placeholder such as ``redacted`` or
    ``your_serial`` as no id at all.
    """

    return (
        isinstance(value, str)
        and _DEVICE_ID.fullmatch(value) is not None
        and not is_masked_identity_value(value)
    )


@dataclass(frozen=True)
class ExternalTopicFamily:
    family: str
    label: str
    prefix: str
    keys: Mapping[str, CatalogKey]
    required_key: str
    bundle_key: str

    def subscription(self, device_id: str = "+") -> str:
        return f"{self.prefix}/{device_id}/+"

    def snapshot_key(self, device_id: str) -> str:
        """Where the runtime keeps this device's readings.

        A Zendure device's snapshot is keyed by its bare device id, and a
        catalog device may carry the same one; the prefix keeps the two apart,
        because neither may write the other's metrics.
        """

        return f"{self.prefix}/{device_id}"

    def reads(self, key: str) -> bool:
        return key == self.bundle_key or key in self.keys

    def readings(self, key: str, payload) -> Readings | None:
        """The metrics a payload on ``key`` carries, or ``None`` when it carries none.

        One answer for discovery and the runtime, so a device is never offered on
        a payload the runtime would not read.
        """

        if key == self.bundle_key:
            return self._bundle_readings(payload)
        spec = self.keys.get(key)
        value = _number(payload) if spec is not None else None
        return spec.read(value) if value is not None else None

    def offerable(self, metrics) -> bool:
        """Whether a device that reported ``metrics`` may be offered for adoption."""

        return set(self.keys[self.required_key].metrics) <= set(metrics)

    def _bundle_readings(self, payload) -> Readings | None:
        data = parse_json_object(payload)
        if data is None:
            return None
        readings: Readings = {}
        for key, spec in self.keys.items():
            value = _number(data.get(key))
            reading = spec.read(value) if value is not None else None
            if reading:
                readings.update(reading)
        return readings or None


EMS_SOLARFLOW = ExternalTopicFamily(
    family="ems_solarflow",
    label="External device",
    prefix="ems-solarflow",
    keys=MappingProxyType(
        {
            "inverterPower": _non_negative("outputHomePower"),
            "solarPower": _non_negative("solarInputPower"),
            "batteryPower": _signed("outputPackPower", "packInputPower"),
            "batterySoc": _percent("electricLevel"),
        }
    ),
    required_key="inverterPower",
    bundle_key="state",
)

EXTERNAL_TOPIC_FAMILIES: Mapping[str, ExternalTopicFamily] = MappingProxyType(
    {entry.family: entry for entry in (EMS_SOLARFLOW,)}
)

_BY_PREFIX = {entry.prefix: entry for entry in EXTERNAL_TOPIC_FAMILIES.values()}


def external_topic_family(family) -> ExternalTopicFamily | None:
    """The catalog entry named ``family``, or ``None``."""

    return EXTERNAL_TOPIC_FAMILIES.get(str(family or "").strip())


def match_external_topic(segments) -> tuple[ExternalTopicFamily, str, str] | None:
    """``(entry, device_id, key)`` for a topic the catalog describes.

    ``key`` is the topic's own last segment; :meth:`ExternalTopicFamily.readings`
    turns its payload into project metrics.
    """

    if len(segments) != 3:
        return None
    prefix, device_id, key = segments
    entry = _BY_PREFIX.get(prefix)
    if entry is None or not valid_device_id(device_id) or not entry.reads(key):
        return None
    return entry, device_id, key


def in_catalog_namespace(topic) -> bool:
    """Whether ``topic`` lies under a catalog entry's prefix, read or not."""

    return isinstance(topic, str) and topic.split("/", 1)[0] in _BY_PREFIX


def external_discovery_subscriptions() -> tuple[str, ...]:
    """One filter per catalog entry, reaching every device it describes."""

    return tuple(entry.subscription() for entry in EXTERNAL_TOPIC_FAMILIES.values())


__all__ = [
    "EMS_SOLARFLOW",
    "EXTERNAL_TOPIC_FAMILIES",
    "CatalogKey",
    "ExternalTopicFamily",
    "external_discovery_subscriptions",
    "external_topic_family",
    "in_catalog_namespace",
    "match_external_topic",
    "valid_device_id",
]
