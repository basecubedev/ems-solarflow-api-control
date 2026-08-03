# SPDX-License-Identifier: AGPL-3.0-or-later
"""Legacy telemetry + control round trip against a real local Mosquitto broker.

Publishes a legacy ``iot/<pk>/<dev>/properties/report`` to a real broker, lets the
production telemetry runtime turn it into a device snapshot (parser -> DeviceState),
then issues a supported ``outputLimit`` write through the production control device
client and asserts a real subscriber receives the canonical ``properties/write``
topic and payload. Only the implemented write protocol is exercised.
"""

import json
import threading

import pytest

from tests.helpers.mosquitto import (
    mosquitto_broker,
    publish_once,
    publish_until,
    require_real_broker_environment,
    wait_until,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.e2e,
    pytest.mark.docker,
]

require_real_broker_environment()

PRODUCT_KEY = "PKLEG"
DEVICE_ID = "DEVLEG"
REPORT_TOPIC = f"iot/{PRODUCT_KEY}/{DEVICE_ID}/properties/report"
WRITE_TOPIC = f"iot/{PRODUCT_KEY}/{DEVICE_ID}/properties/write"


def _config(host, port):
    return {
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "local": {
                    "enabled": True, "source": "local_mqtt",
                    "host": host, "port": port,
                }
            },
        },
        "devices": [
            {
                "type": "zendure_mqtt", "name": "LEG",
                "hardware_profile": "solarflow_800_pro_2",
                "mqtt": {
                    "broker_ref": "local",
                    "topic_family": "legacy_zendure_json",
                    "device_id": DEVICE_ID,
                    "product_key": PRODUCT_KEY,
                },
                "capabilities": {"write_output_limit": True},
                "max_power": 800,
            }
        ],
    }


def _legacy_report():
    body = {
        "sn": DEVICE_ID,
        "product": "Hub 2000",
        "properties": {
            "electricLevel": 74,
            "solarInputPower": 520,
            "outputLimit": 200,
            "acMode": 2,
        },
    }
    return json.dumps(body).encode("utf-8")


def _snapshot(service):
    snap = service.snapshots().get(DEVICE_ID)
    if snap is not None and (getattr(snap, "metrics", None) or {}):
        return snap
    return None


def test_legacy_report_parses_and_control_write_reaches_broker(tmp_path):
    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

    import paho.mqtt.client as mqtt

    with mosquitto_broker(tmp_path) as (host, port):
        runtime = build_zendure_mqtt_control_runtime(_config(host, port))
        assert not runtime.has_rejections
        device = runtime.devices[0]
        service = runtime.services_by_ref["local"]

        # An independent subscriber captures the control write on the broker.
        received = []
        try:
            watcher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):
            watcher = mqtt.Client()
        subscribed = threading.Event()
        watcher.on_subscribe = lambda *args: subscribed.set()
        watcher.on_message = lambda c, u, m: received.append((m.topic, m.payload))
        # Subscribe from on_connect so the SUBSCRIBE is sent after CONNACK; a
        # subscribe issued before the connection is up is silently dropped.
        watcher.on_connect = lambda *a, **k: watcher.subscribe(WRITE_TOPIC, qos=1)
        watcher.connect(host, port, keepalive=10)
        watcher.loop_start()

        runtime.start()
        try:
            # Wait for the watcher's SUBACK so the later control write cannot race
            # ahead of its subscription.
            wait_until(subscribed.is_set,
                       message="watcher never subscribed to the write topic")

            # Re-publish the legacy report until the production runtime turns it
            # into a snapshot; a single publish can race the runtime's SUBACK.
            snap = publish_until(
                lambda: publish_once(host, port, REPORT_TOPIC, _legacy_report()),
                lambda: _snapshot(service),
                message="legacy report never became a snapshot",
            )
            metrics = getattr(snap, "metrics", None) or {}
            assert "electricLevel" in metrics

            # A supported outputLimit write reaches the canonical write topic.
            assert device.write_output_limit(600) is True
            wait_until(lambda: received,
                       message="control write never reached the broker")
            topic, payload = received[0]
            assert topic == WRITE_TOPIC
            body = json.loads(payload)
            assert body["deviceId"] == DEVICE_ID
            assert body["properties"]["outputLimit"] == 600
        finally:
            runtime.stop()
            watcher.loop_stop()
            watcher.disconnect()
