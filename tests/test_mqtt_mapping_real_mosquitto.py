# SPDX-License-Identifier: AGPL-3.0-or-later
"""End to end on a real Mosquitto: a Shelly payload in, a device the EMS reads out.

The mapping service runs in-process against the broker, a Shelly-shaped status
message goes to its source topic, and the retained ``state`` it publishes is
found by the Admin's discovery and read by the catalog exactly as the EMS reads
any bridge's output. The message carries an MQTT 5 expiry, so it cannot outlive
a stopped service.
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

STATE_TOPIC = "ems-solarflow/balkon/state"
SOURCE_TOPIC = "shellyplusplugs-balkon/status/switch:0"


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _state_listener(host, port):
    import paho.mqtt.client as mqtt

    received = []
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    client.on_connect = lambda c, *_args: c.subscribe(STATE_TOPIC, qos=1)
    client.on_message = lambda _c, _u, message: received.append(message)
    client.connect(host, port)
    client.loop_start()
    return client, received


def test_a_shelly_payload_becomes_an_external_device_the_ems_reads(tmp_path):
    from admin.mqtt_topic_discovery import discover_broker_topics
    from ems.zendure_mqtt.external_catalog import EMS_SOLARFLOW
    from mqtt_mapping.service import MappingService, ServicePaths

    with mosquitto_broker(tmp_path) as (host, port):
        paths = ServicePaths(
            str(tmp_path / "config" / "mqtt" / "mappings.json"),
            str(tmp_path / "mqtt" / "mapping-credentials.json"),
            str(tmp_path / "data" / "mqtt-mapping-status.json"),
        )
        _write(tmp_path / "mqtt" / "mapping-credentials.json",
               {"schema_version": 1, "brokers": {"ems-mqtt": {"host": host, "port": port}}})
        source = {"topic": SOURCE_TOPIC, "format": "json", "path": "apower", "invert": True, "negative": "zero"}
        _write(
            tmp_path / "config" / "mqtt" / "mappings.json",
            {
                "schema_version": 1,
                "devices": [
                    {
                        "id": "balkon",
                        "broker_ref": "ems-mqtt",
                        "values": {"inverterPower": {"sources": [source]}, "solarPower": {"sources": [source]}},
                    }
                ],
            },
        )
        service = MappingService(paths)
        stop = threading.Event()
        runner = threading.Thread(target=service.run, args=(stop,), daemon=True)
        runner.start()
        listener, received = _state_listener(host, port)
        try:
            wait_until(lambda: any(c.connected for c in service._connections.values()),
                       message="the mapping never connected")
            publish_until(
                lambda: publish_once(host, port, SOURCE_TOPIC, json.dumps({"id": 0, "apower": -612.3})),
                lambda: received,
                message="no state was published",
            )
            message = received[0]

            assert json.loads(message.payload) == {"inverterPower": 612.3, "solarPower": 612.3}
            assert 0 < message.properties.MessageExpiryInterval <= 45
            assert EMS_SOLARFLOW.readings("state", message.payload) == {
                "outputHomePower": 612.3,
                "solarInputPower": 612.3,
            }
            late, late_received = _state_listener(host, port)
            try:
                wait_until(lambda: late_received, message="the state was not retained")
                assert late_received[0].retain
            finally:
                late.loop_stop()
                late.disconnect()
            candidates = discover_broker_topics({"host": host, "port": port}, timeout_s=2.0)
            assert "balkon" in json.dumps(candidates)
        finally:
            listener.loop_stop()
            listener.disconnect()
            stop.set()
            runner.join(timeout=10)

        status = json.loads((tmp_path / "data" / "mqtt-mapping-status.json").read_text())
        assert status["devices"][0]["last_published_at"]
