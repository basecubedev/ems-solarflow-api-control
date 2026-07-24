# SPDX-License-Identifier: AGPL-3.0-or-later
"""The custom_properties_write escape hatch is strictly validated.

The custom escape hatch is the only write authority without a pinned model. Its
explicit publish topic must be a real publish topic: non-empty, valid UTF-8,
bounded, and never an MQTT subscription filter (no ``+``, ``#`` or NUL). A custom
write still obeys strict integer targets, a non-negative outputLimit, the
configured maximum and ``retain=false``. Config validation rejects an invalid
custom topic, the message builder refuses to build one, and the runtime device
fails closed.
"""

import json

import pytest

from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON
from ems.zendure_mqtt.write_protocols import (
    PROTOCOL_CUSTOM_PROPERTIES_WRITE,
    build_output_limit_message,
    publish_topic_error,
)

pytestmark = pytest.mark.simulation


# --- topic validation --------------------------------------------------------


def test_valid_publish_topic_accepted():
    assert publish_topic_error("iot/PK/DEV/properties/write") is None


@pytest.mark.parametrize(
    "topic",
    [
        "iot/+/DEV/properties/write",
        "iot/PK/+/properties/write",
        "iot/PK/DEV/#",
        "#",
        "+",
        "a/+/b",
    ],
)
def test_wildcard_topics_are_rejected(topic):
    assert publish_topic_error(topic) is not None


def test_empty_topic_rejected():
    assert publish_topic_error("") is not None
    assert publish_topic_error("   ") is not None
    assert publish_topic_error(None) is not None


def test_nul_in_topic_rejected():
    assert publish_topic_error("iot/PK/DEV\x00/properties/write") is not None


def test_overlong_topic_rejected():
    assert publish_topic_error("a/" * 5000) is not None


def test_non_string_topic_rejected():
    assert publish_topic_error(123) is not None
    assert publish_topic_error(["iot/PK/DEV"]) is not None


# --- message builder refuses invalid custom topics ---------------------------


def test_build_message_refuses_wildcard_custom_topic():
    message = build_output_limit_message(
        PROTOCOL_CUSTOM_PROPERTIES_WRITE,
        device_id="DEV",
        output_limit_w=100,
        write_topic="iot/+/DEV/properties/write",
    )
    assert message is None


def test_build_message_custom_topic_is_not_retained():
    message = build_output_limit_message(
        PROTOCOL_CUSTOM_PROPERTIES_WRITE,
        device_id="DEV",
        output_limit_w=100,
        write_topic="custom/topic/write",
    )
    assert message is not None
    assert message.retain is False


# --- runtime device fails closed ---------------------------------------------


class _FakeService:
    def __init__(self):
        self.published = []

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _custom_device(write_topic, **kwargs):
    from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient

    return ZendureMqttDeviceClient(
        "WR",
        _FakeService(),
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        write_topic=write_topic,
        write_protocol="custom_properties_write",
        max_power=2000,
        **kwargs,
    )


def test_custom_device_fails_closed_on_wildcard_topic():
    dev = _custom_device("iot/+/DEV/properties/write")
    assert dev.write_output_limit(100) is False
    assert dev._service.published == []


def test_custom_device_publishes_valid_topic():
    dev = _custom_device("custom/DEV/write")
    assert dev.write_output_limit(100) is True
    topic, payload = dev._service.published[-1]
    assert topic == "custom/DEV/write"
    assert json.loads(payload)["properties"]["outputLimit"] == 100


def test_custom_device_rejects_negative_output_limit():
    dev = _custom_device("custom/DEV/write")
    assert dev.write_output_limit(-100) is False
    assert dev._service.published == []


# --- config validation -------------------------------------------------------


def _custom_config_device(write_topic):
    return {
        "type": "zendure_mqtt",
        "name": "Custom",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEV",
            "product_key": "PK",
            "write_protocol": "custom_properties_write",
            "write_topic": write_topic,
        },
        "capabilities": {"write_output_limit": True},
    }


def test_config_validation_rejects_wildcard_custom_topic():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    issues = validate_zendure_mqtt_control_device_config(
        _custom_config_device("iot/+/DEV/properties/write")
    )
    assert any(i["code"] == "write_topic_invalid" for i in issues)


def test_config_validation_accepts_valid_custom_topic():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    issues = validate_zendure_mqtt_control_device_config(
        _custom_config_device("custom/DEV/write")
    )
    assert not any(i["code"] == "write_topic_invalid" for i in issues)
