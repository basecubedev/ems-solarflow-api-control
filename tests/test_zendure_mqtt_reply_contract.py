# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verified Zendure reply-topic contracts.

The uploaded Zendure reference implementations acknowledge a ``function/invoke``
automation command on ``function/invoke/reply`` (leading-slash family:
``/<pk>/<dev>/function/invoke/reply``). The invented ``function/reply`` and
``properties/read_reply`` channels are *not* part of any verified contract and
must never acknowledge a command. The reply contract is selected from the
device's resolved power-write profile, and a broad telemetry subscription can
never stand in for the verified reply topic.
"""

import json

import pytest

from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON, FAMILY_LEGACY_JSON_ALT
from tests.helpers.fake_mqtt import FakeMqttNetwork

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.contract,
    pytest.mark.simulation,
]


class _FakeService:
    def __init__(self):
        self.published = []

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _device(hardware_profile="hyper_2000", topic_family=FAMILY_LEGACY_JSON, **kwargs):
    return ZendureMqttDeviceClient(
        "WR",
        _FakeService(),
        device_id="DEVICE_ID",
        topic_family=topic_family,
        source="local_mqtt",
        product_key="PK",
        hardware_profile=hardware_profile,
        **kwargs,
    )


# --- reply-topic construction ------------------------------------------------


def test_hyper_reply_topic_is_function_invoke_reply():
    dev = _device("hyper_2000")
    assert dev.reply_topics() == ("iot/PK/DEVICE_ID/function/invoke/reply",)


def test_hub_reply_topic_is_function_invoke_reply():
    dev = _device("hub_2000")
    assert dev.reply_topics() == ("iot/PK/DEVICE_ID/function/invoke/reply",)


def test_invented_function_reply_topic_is_not_subscribed():
    dev = _device("hyper_2000")
    assert "iot/PK/DEVICE_ID/function/reply" not in dev.reply_topics()


def test_properties_read_reply_is_not_subscribed():
    dev = _device("hyper_2000")
    assert not any(t.endswith("properties/read_reply") for t in dev.reply_topics())
    assert not any(t.endswith("properties/read/reply") for t in dev.reply_topics())


def test_leading_slash_reply_topic_is_constructed_correctly():
    dev = _device("hyper_2000", topic_family=FAMILY_LEGACY_JSON_ALT)
    assert dev.reply_topics() == ("/PK/DEVICE_ID/function/invoke/reply",)


def test_zensdk_has_no_verified_reply_topic():
    # properties/write has no verified reply contract in this codebase; a ZenSDK
    # device subscribes to no reply topic rather than an invented one.
    dev = _device("solarflow_zensdk")
    assert dev.reply_topics() == ()


# --- contract abstraction ----------------------------------------------------


def test_reply_contract_selected_from_write_profile():
    from ems.mqtt_control.reply_contracts import (
        reply_contract_for_write_profile,
    )
    from ems.mqtt_control.zendure_profiles import (
        WRITE_PROFILE_LEGACY_HUB,
        WRITE_PROFILE_LEGACY_OBJECT,
        WRITE_PROFILE_ZENSDK_PROPERTIES,
    )

    invoke = reply_contract_for_write_profile(WRITE_PROFILE_LEGACY_OBJECT)
    assert invoke.request_suffix == "function/invoke"
    assert invoke.reply_suffixes == ("function/invoke/reply",)
    assert invoke.correlation_fields == ("messageId", "deviceId")
    assert invoke.supports_acknowledgement is True

    hub = reply_contract_for_write_profile(WRITE_PROFILE_LEGACY_HUB)
    assert hub.reply_suffixes == ("function/invoke/reply",)

    # properties/write has no verified acknowledgement contract.
    zensdk = reply_contract_for_write_profile(WRITE_PROFILE_ZENSDK_PROPERTIES)
    assert zensdk.supports_acknowledgement is False
    assert zensdk.reply_suffixes == ()


# --- live routing over the fake broker ---------------------------------------


def _control_config(hardware_profile="hyper_2000"):
    return {
        "zendure_mqtt": {
            "brokers": {
                "local_a": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "a",
                    "port": 1883,
                }
            }
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "WR",
                "hardware_profile": hardware_profile,
                "command_ack_timeout_seconds": 5.0,
                "mqtt": {
                    "broker_ref": "local_a",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEV",
                    "product_key": "PK",
                },
                "capabilities": {"write_output_limit": True},
            }
        ],
    }


def _build(config):
    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_control_runtime(
        config, service_factory=network.control_service_factory()
    )
    runtime.start()
    return runtime, network


def _reply_payload(broker, *, success=1, output="success"):
    invoke = [r for r in broker.publish_calls if r.topic.endswith("function/invoke")][-1]
    body = json.loads(invoke.payload)
    return json.dumps(
        {
            "messageId": body["messageId"],
            "deviceId": body["deviceId"],
            "function": "deviceAutomation",
            "output": output,
            "success": success,
        }
    ).encode()


def test_function_invoke_reply_acknowledges_active_hyper_command():
    runtime, network = _build(_control_config("hyper_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    dev.write_output_limit(500)
    broker.inject("iot/PK/DEV/function/invoke/reply", _reply_payload(broker))
    assert dev._active_command.state == "acknowledged"


def test_function_invoke_reply_acknowledges_active_hub_command():
    runtime, network = _build(_control_config("hub_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    dev.write_output_limit(400)
    broker.inject("iot/PK/DEV/function/invoke/reply", _reply_payload(broker))
    assert dev._active_command.state == "acknowledged"


def test_invented_function_reply_does_not_satisfy_the_verified_contract():
    runtime, network = _build(_control_config("hyper_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    dev.write_output_limit(500)
    # A correlated reply on the invented function/reply channel must be ignored.
    broker.inject("iot/PK/DEV/function/reply", _reply_payload(broker))
    assert dev._active_command.state == "published"


def test_broad_telemetry_subscription_alone_is_not_acknowledgement():
    runtime, network = _build(_control_config("hyper_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    dev.write_output_limit(500)
    body = json.loads(
        [r for r in broker.publish_calls if r.topic.endswith("function/invoke")][-1].payload
    )
    # A reply-shaped payload delivered over the broad telemetry report topic
    # (which the read client subscribes to) must never acknowledge a command.
    broker.inject(
        "iot/PK/DEV/properties/report",
        json.dumps(
            {
                "messageId": body["messageId"],
                "deviceId": body["deviceId"],
                "success": 1,
                "output": "success",
            }
        ).encode(),
    )
    assert dev._active_command.state == "published"
