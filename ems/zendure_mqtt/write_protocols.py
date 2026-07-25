# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit MQTT output-write protocols for Zendure control devices.

A control device must resolve to a named, supported write protocol before it can
publish. This closes the unsafe path where a telemetry family that was never
proven writable (scalar / unknown) would silently fall back to the legacy
``iot/.../properties/write`` shape.

Two protocols exist, with deliberately different authority:

``legacy_properties_write``
    ``iot/<productKey>/<deviceId>/properties/write`` (for every topic family —
    devices on the leading-slash report family still accept commands only on
    ``iot/…``) with a ``{deviceId, messageId, timestamp,
    properties}`` payload. It is a *message shape* only — the runtime uses it to
    build the properties/write publish for a concrete ZenSDK hardware profile.
    It is NOT a config-authorizing protocol: an ``mqtt.write_protocol`` of
    ``legacy_properties_write`` never authorizes a no-profile control device.

``custom_properties_write``
    The same properties payload published to an explicit ``mqtt.write_topic``.
    This is the single isolated, operator-verified custom escape hatch: it is
    the only ``mqtt.write_protocol`` that may authorize control without a pinned
    hardware profile, and only together with an explicit ``mqtt.write_topic``.
    It is never inferred from a Zendure generation or topic family.

Config authorization (``resolve_write_protocol``) is therefore restricted to
``custom_properties_write``; ``build_output_limit_message`` still accepts both
shapes because a concrete hardware profile selects the legacy properties/write
shape directly, without going through config-level authorization.
"""

import itertools
import json
import time
from dataclasses import dataclass

PROTOCOL_LEGACY_PROPERTIES_WRITE = "legacy_properties_write"
PROTOCOL_CUSTOM_PROPERTIES_WRITE = "custom_properties_write"

# Write-protocol *shapes* the message builder can construct. Both are valid
# message shapes; this set governs ``build_output_limit_message`` only.
SUPPORTED_WRITE_PROTOCOLS = frozenset(
    {PROTOCOL_LEGACY_PROPERTIES_WRITE, PROTOCOL_CUSTOM_PROPERTIES_WRITE}
)

# Write protocols that may *authorize* a no-profile control device from config.
# Only the explicit custom escape hatch qualifies: the built-in
# ``legacy_properties_write`` shape is reachable exclusively through a concrete
# hardware profile, never through a bare ``mqtt.write_protocol``.
CONFIG_AUTHORIZED_WRITE_PROTOCOLS = frozenset({PROTOCOL_CUSTOM_PROPERTIES_WRITE})

_WRITE_SUFFIX = "properties/write"

# QoS 1 makes broker delivery observable via PUBACK; control writes are never
# retained (a retained setpoint would replay on every reconnect).
CONTROL_PUBLISH_QOS = 1

# MQTT topic-name limit is 65535 UTF-8 bytes; a control publish topic is short,
# so a much tighter bound rejects obviously malformed/hostile values.
MAX_PUBLISH_TOPIC_BYTES = 512

# Process-wide monotonic message-id source; deterministic, never random.
_message_ids = itertools.count(1)


def publish_topic_error(topic) -> str | None:
    """Return a machine-readable reason a topic is not a valid publish topic.

    A publish topic (unlike a subscription filter) must not contain the wildcards
    ``+``/``#`` or a NUL, must be non-empty valid UTF-8 and within a bounded
    length. Returns ``None`` when the topic is safe to publish to.
    """

    if not isinstance(topic, str):
        return "topic_not_a_string"
    text = topic.strip()
    if not text:
        return "topic_empty"
    if "+" in text or "#" in text:
        return "topic_wildcard"
    if "\x00" in text:
        return "topic_nul"
    try:
        encoded = text.encode("utf-8")
    except (UnicodeEncodeError, UnicodeError):
        return "topic_invalid_utf8"
    if len(encoded) > MAX_PUBLISH_TOPIC_BYTES:
        return "topic_too_long"
    return None


@dataclass(frozen=True)
class MqttPublishMessage:
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False


def next_message_id() -> int:
    """Return the next monotonic message id for a control write."""

    return next(_message_ids)


def resolve_write_protocol(topic_family, explicit=None) -> str | None:
    """Return the config-authorizing MQTT write protocol name, or ``None``.

    Only the explicit ``custom_properties_write`` escape hatch authorizes control
    without a pinned hardware profile. A topic family never yields a write
    protocol, and the built-in ``legacy_properties_write`` shape is deliberately
    NOT authorizing here: hardware writability is decided by the pinned hardware
    profile via :mod:`ems.mqtt_control.power_capability`, and the properties/write
    shape is reached only through a concrete ZenSDK profile.
    """

    if explicit is None:
        return None
    name = str(explicit).strip().lower()
    return name if name in CONFIG_AUTHORIZED_WRITE_PROTOCOLS else None


def _legacy_topic(family, product_key, device_id) -> str | None:
    if not product_key or not device_id:
        return None
    # Commands always go to iot/… — leading-slash-family devices report on /…
    # but accept writes on iot/… only (live capture + reference implementation).
    return f"iot/{product_key}/{device_id}/{_WRITE_SUFFIX}"


def _properties_payload(device_id, properties, *, message_id, timestamp) -> bytes:
    body = {
        "deviceId": device_id,
        "messageId": message_id,
        "timestamp": timestamp,
        "properties": {key: int(value) for key, value in properties.items()},
    }
    return json.dumps(body).encode("utf-8")


def build_properties_write_message(
    protocol,
    *,
    properties,
    topic_family=None,
    product_key=None,
    device_id=None,
    write_topic=None,
    message_id=None,
    timestamp=None,
    qos=0,
    retain=False,
) -> MqttPublishMessage | None:
    """Build the publish message for a properties write, or ``None``.

    ``properties`` is a mapping of already-validated integer device properties.
    Returns ``None`` when the protocol is unsupported, the device is not
    addressable for that protocol, or the property set is empty. Protocol-
    specific topic/payload construction lives here, never in the controller.
    """

    if protocol not in SUPPORTED_WRITE_PROTOCOLS:
        return None
    if not isinstance(properties, dict) or not properties:
        return None
    if message_id is None:
        message_id = next_message_id()
    if timestamp is None:
        timestamp = int(time.time())

    if protocol == PROTOCOL_CUSTOM_PROPERTIES_WRITE:
        topic = write_topic.strip() if isinstance(write_topic, str) and write_topic.strip() else None
    else:  # legacy_properties_write
        topic = (
            write_topic.strip()
            if isinstance(write_topic, str) and write_topic.strip()
            else _legacy_topic(topic_family, product_key, device_id)
        )
    if topic is None or publish_topic_error(topic) is not None:
        # Never publish to an empty, wildcard, NUL-bearing or malformed topic.
        return None

    payload = _properties_payload(
        device_id, properties, message_id=message_id, timestamp=timestamp
    )
    return MqttPublishMessage(topic=topic, payload=payload, qos=qos, retain=retain)


def build_output_limit_message(
    protocol,
    *,
    topic_family=None,
    product_key=None,
    device_id=None,
    output_limit_w,
    write_topic=None,
    message_id=None,
    timestamp=None,
    qos=0,
) -> MqttPublishMessage | None:
    """Build the publish message for an ``outputLimit`` write, or ``None``."""

    return build_properties_write_message(
        protocol,
        properties={"outputLimit": int(output_limit_w)},
        topic_family=topic_family,
        product_key=product_key,
        device_id=device_id,
        write_topic=write_topic,
        message_id=message_id,
        timestamp=timestamp,
        qos=qos,
    )
