# SPDX-License-Identifier: AGPL-3.0-or-later
"""The devices this project reads over MQTT without being Zendure hardware.

The catalog is a whitelist. An entry fixes how a device's topics are built --
``<prefix>/<device id>/<key>`` -- and what the payload behind each known key
means. A topic of any other shape, or a key the entry does not name, is never
read and never offered, because nobody can say what its payload is.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ems.zendure_mqtt.payloads import coerce_scalar


@dataclass(frozen=True)
class ExternalTopicFamily:
    family: str
    label: str
    prefix: str
    metrics: Mapping[str, str]

    def subscription(self, device_id: str = "+") -> str:
        return f"{self.prefix}/{device_id}/+"


KOSTAL_PIKO = ExternalTopicFamily(
    family="kostal_piko",
    label="Kostal Piko",
    prefix="KostalPiko",
    metrics=MappingProxyType({"solarPower": "outputHomePower"}),
)

EXTERNAL_TOPIC_FAMILIES: Mapping[str, ExternalTopicFamily] = MappingProxyType(
    {entry.family: entry for entry in (KOSTAL_PIKO,)}
)

_BY_PREFIX = {entry.prefix: entry for entry in EXTERNAL_TOPIC_FAMILIES.values()}


def external_topic_family(family) -> ExternalTopicFamily | None:
    """The catalog entry named ``family``, or ``None``."""

    return EXTERNAL_TOPIC_FAMILIES.get(str(family or "").strip())


def match_external_topic(segments) -> tuple[ExternalTopicFamily, str, str] | None:
    """``(entry, device_id, metric)`` for a topic the catalog describes.

    ``metric`` is the project's name for the key, so a reading needs no
    translation downstream.
    """

    if len(segments) != 3:
        return None
    prefix, device_id, key = segments
    entry = _BY_PREFIX.get(prefix)
    if entry is None or not device_id:
        return None
    metric = entry.metrics.get(key)
    if metric is None:
        return None
    return entry, device_id, metric


def catalog_reading(payload) -> int | float | None:
    """The number a catalog payload carries, or ``None`` for anything else.

    One answer for discovery and the runtime, so a device is never offered on a
    payload the runtime would not read.
    """

    value = coerce_scalar(payload)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def external_discovery_subscriptions() -> tuple[str, ...]:
    """One filter per catalog entry, reaching every device it describes."""

    return tuple(entry.subscription() for entry in EXTERNAL_TOPIC_FAMILIES.values())


__all__ = [
    "EXTERNAL_TOPIC_FAMILIES",
    "ExternalTopicFamily",
    "KOSTAL_PIKO",
    "catalog_reading",
    "external_discovery_subscriptions",
    "external_topic_family",
    "match_external_topic",
]
