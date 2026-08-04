# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zendure MQTT topic classification for EMS Core runtime telemetry.

Pure string parsing: no network, no MQTT client, no side effects. Unknown,
empty, malformed or write/control topics never raise — they classify as
``FAMILY_UNKNOWN`` so a hostile broker cannot crash the runtime parser.
"""

from dataclasses import dataclass

# The neutral family identifiers are dependency-free primitives shared with the
# capability authority; they live in ems.mqtt_control.topic_families so importing
# power_capability first can never trigger this package's initializer.
from ems.mqtt_control.topic_families import (
    FAMILY_LEGACY_JSON,
    FAMILY_LEGACY_JSON_ALT,
    FAMILY_UNKNOWN,
    FAMILY_ZENDURE_CLOUD_SCALAR,
    FAMILY_ZENSDK_HA_SCALAR,
    JSON_FAMILIES,
    SCALAR_FAMILIES,
)

# Neutral schema names for the two JSON-report layouts. A topic family names
# the observed topic/payload format only — new ZenSDK devices publish the
# leading-slash JSON report via the cloud broker too, so the "legacy_" prefix
# must never be read as a hardware generation. The stored config values keep
# the legacy_* strings so existing configs stay valid.
FAMILY_ZENDURE_JSON_REPORT = FAMILY_LEGACY_JSON
FAMILY_ZENDURE_JSON_REPORT_LEADING_SLASH = FAMILY_LEGACY_JSON_ALT

# Cloud-prefixed scalar components: `<appKey>/<component>/<device>/<metric>`.
_CLOUD_SCALAR_COMPONENTS = frozenset(
    {"sensor", "number", "switch", "select", "binary_sensor"}
)


@dataclass(frozen=True)
class TopicMatch:
    family: str
    device_id: str | None = None
    serial_number: str | None = None
    metric: str | None = None
    product_key: str | None = None


def classify_topic(topic):
    """Classify an MQTT topic into a Zendure telemetry family.

    Only telemetry-carrying topics are recognized. ``properties/write`` and any
    other control shape is deliberately left as ``FAMILY_UNKNOWN`` so it is never
    consumed as telemetry.
    """

    if not isinstance(topic, str) or not topic:
        return TopicMatch(FAMILY_UNKNOWN)
    segments = topic.split("/")
    if len(segments) >= 4 and segments[0] == "Zendure" and segments[2] and segments[3]:
        device = segments[2]
        return TopicMatch(
            FAMILY_ZENSDK_HA_SCALAR,
            device_id=device,
            serial_number=device,
            metric="/".join(segments[3:]),
        )
    if (
        len(segments) == 5
        and segments[0] == "iot"
        and segments[3] == "properties"
        and segments[4] == "report"
        and segments[1]
        and segments[2]
    ):
        return TopicMatch(
            FAMILY_LEGACY_JSON, device_id=segments[2], product_key=segments[1]
        )
    if (
        len(segments) == 5
        and segments[0] == ""
        and segments[3] == "properties"
        and segments[4] == "report"
        and segments[1]
        and segments[2]
    ):
        return TopicMatch(
            FAMILY_LEGACY_JSON_ALT, device_id=segments[2], product_key=segments[1]
        )
    # `<appKey>/<component>/<device>/<metric>`: the appKey prefix is an account
    # secret, so it is never stored on the match.
    if (
        len(segments) >= 4
        and segments[0]
        and segments[0] not in ("Zendure", "iot")
        and segments[1] in _CLOUD_SCALAR_COMPONENTS
        and segments[2]
        and segments[3]
    ):
        return TopicMatch(
            FAMILY_ZENDURE_CLOUD_SCALAR,
            device_id=segments[2],
            serial_number=segments[2],
            metric="/".join(segments[3:]),
        )
    return TopicMatch(FAMILY_UNKNOWN)


__all__ = [
    "FAMILY_ZENSDK_HA_SCALAR",
    "FAMILY_LEGACY_JSON",
    "FAMILY_LEGACY_JSON_ALT",
    "FAMILY_ZENDURE_CLOUD_SCALAR",
    "FAMILY_UNKNOWN",
    "FAMILY_ZENDURE_JSON_REPORT",
    "FAMILY_ZENDURE_JSON_REPORT_LEADING_SLASH",
    "SCALAR_FAMILIES",
    "JSON_FAMILIES",
    "TopicMatch",
    "classify_topic",
]
