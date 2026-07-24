# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real-broker Zendure control lifecycle against an ephemeral Mosquitto.

Docker-marked and skipped cleanly without Docker. This exercises the actual paho
network path the in-process fake harness cannot: a real subscribe, a real
``function/invoke`` publish, a real ``function/invoke/reply`` delivery, the
control client's callback routing, and the correlated acknowledgement. It is
protocol-level verification — not physical-hardware validation.
"""

import contextlib
import json
import subprocess
import threading

import pytest

from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
from tests.helpers.mosquitto import (
    mosquitto_broker,
    publish_once,
    publish_until,
    require_real_broker_environment,
    wait_until,
)

pytestmark = pytest.mark.docker

require_real_broker_environment()


def _config(host, port, hardware_profile="hyper_2000"):
    return {
        "zendure_mqtt": {
            "brokers": {
                "local_a": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": host,
                    "port": port,
                    "keepalive_seconds": 2,
                }
            }
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "Hyper",
                "hardware_profile": hardware_profile,
                "command_ack_timeout_seconds": 30.0,
                "confirmation_timeout_seconds": 2.0,
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


def _report(output_limit, *, device_id="DEV"):
    return json.dumps(
        {
            "sn": device_id,
            "properties": {"outputLimit": output_limit, "electricLevel": 70},
        }
    )


def _snapshot_output(dev):
    snapshot = dev._service.snapshots().get(dev._device_id)
    if snapshot is None:
        return None
    return (getattr(snapshot, "metrics", None) or {}).get("outputLimit")


def _publish_report_until(host, port, dev, value, predicate):
    topic = f"iot/PK/{dev._device_id}/properties/report"

    def observed():
        if _snapshot_output(dev) != value:
            return False
        dev.fetch()
        return predicate()

    return publish_until(
        lambda: publish_once(host, port, topic, _report(value, device_id=dev._device_id)),
        observed,
        message=f"{value} W telemetry was not observed",
    )


@contextlib.contextmanager
def _watch_topic(host, port, topic):
    import paho.mqtt.client as mqtt

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except (AttributeError, TypeError):
        client = mqtt.Client()
    subscribed = threading.Event()
    received = []

    def on_connect(connected_client, *_args):
        connected_client.subscribe(topic, qos=1)

    client.on_connect = on_connect
    client.on_subscribe = lambda *_args: subscribed.set()
    client.on_message = lambda _client, _userdata, message: received.append(
        (message.topic, message.payload)
    )
    client.connect(host, port, keepalive=10)
    client.loop_start()
    try:
        wait_until(subscribed.is_set, message=f"watcher did not subscribe to {topic}")
        yield received
    finally:
        client.loop_stop()
        client.disconnect()


def _reply(message_id, *, success=1, output="success"):
    return json.dumps(
        {
            "messageId": message_id,
            "deviceId": "DEV",
            "function": "deviceAutomation",
            "output": output,
            "success": success,
        }
    )


def test_real_mosquitto_publish_to_reply_routing_acknowledges(tmp_path):
    with mosquitto_broker(tmp_path) as (host, port):
        runtime = build_zendure_mqtt_control_runtime(_config(host, port))
        runtime.start()
        try:
            dev = runtime.devices[0]
            wait_until(
                lambda: getattr(dev._service, "connected", False),
                message="control service never connected to Mosquitto",
            )
            # A real function/invoke publish over the wire.
            assert dev.write_output_limit(500) is True
            rec = dev._active_command
            assert rec.topic == "iot/PK/DEV/function/invoke"
            # A real correlated function/invoke/reply, routed through the callback.
            publish_until(
                lambda: publish_once(
                    host, port, "iot/PK/DEV/function/invoke/reply", _reply(rec.message_id)
                ),
                lambda: rec.state == "acknowledged",
                message="reply never acknowledged the command over Mosquitto",
            )
            assert rec.state == "acknowledged"
        finally:
            runtime.stop()


def test_real_mosquitto_failure_reply_rejects(tmp_path):
    with mosquitto_broker(tmp_path) as (host, port):
        runtime = build_zendure_mqtt_control_runtime(_config(host, port))
        runtime.start()
        try:
            dev = runtime.devices[0]
            wait_until(
                lambda: getattr(dev._service, "connected", False),
                message="control service never connected to Mosquitto",
            )
            assert dev.write_output_limit(500) is True
            rec = dev._active_command
            publish_until(
                lambda: publish_once(
                    host,
                    port,
                    "iot/PK/DEV/function/invoke/reply",
                    _reply(rec.message_id, success=0, output="error"),
                ),
                lambda: rec.state == "rejected",
                message="failure reply never rejected the command over Mosquitto",
            )
            assert rec.state == "rejected"
        finally:
            runtime.stop()


def test_real_mosquitto_wrong_device_reply_never_acknowledges(tmp_path):
    with mosquitto_broker(tmp_path) as (host, port):
        runtime = build_zendure_mqtt_control_runtime(_config(host, port))
        runtime.start()
        try:
            dev = runtime.devices[0]
            wait_until(
                lambda: getattr(dev._service, "connected", False),
                message="control service never connected to Mosquitto",
            )
            assert dev.write_output_limit(500) is True
            rec = dev._active_command
            wrong = json.dumps(
                {"messageId": rec.message_id, "deviceId": "OTHER", "success": 1}
            )
            # Publish the wrong-device reply a few times; it must never acknowledge.
            for _ in range(5):
                publish_once(host, port, "iot/PK/DEV/function/invoke/reply", wrong)
            wait_until(lambda: True, timeout=0.5, message="settle")
            assert rec.state == "published"
        finally:
            runtime.stop()


def test_real_mosquitto_no_ack_telemetry_confirmation_and_timeout(tmp_path):
    with mosquitto_broker(tmp_path) as (host, port):
        runtime = build_zendure_mqtt_control_runtime(
            _config(host, port, "solarflow_800_pro_2")
        )
        runtime.start()
        try:
            dev = runtime.devices[0]
            wait_until(
                lambda: dev._service.connected,
                message="no-ack control service never connected",
            )
            # Telemetry observed before publish is retained in the snapshot but
            # cannot confirm the later command.
            publish_until(
                lambda: publish_once(
                    host, port, "iot/PK/DEV/properties/report", _report(100)
                ),
                lambda: _snapshot_output(dev) == 100,
                message="pre-command telemetry was not observed",
            )
            assert dev.write_output_limit(500) is True
            command = dev._active_command
            dev.fetch()
            assert command.state == "published"

            # Fresh but mismatched telemetry also cannot confirm it.
            _publish_report_until(
                host, port, dev, 350, lambda: command.state == "published"
            )
            assert command.state == "published"

            # Only fresh matching telemetry confirms a properties/write command.
            _publish_report_until(
                host,
                port,
                dev,
                500,
                lambda: command.state == "telemetry_confirmed",
            )
            assert command.state == "telemetry_confirmed"

            # With no later telemetry, the next no-ack command reaches the honest
            # confirmation timeout (never an acknowledgement timeout).
            assert dev.write_output_limit(700) is True
            timed = dev._active_command
            dev.describe(now_monotonic=timed.published_monotonic + 3.0)
            assert timed.state == "confirmation_timed_out"
        finally:
            runtime.stop()


def test_real_mosquitto_pending_flush_and_safety_preemption(tmp_path):
    with mosquitto_broker(tmp_path) as (host, port):
        runtime = build_zendure_mqtt_control_runtime(
            _config(host, port, "solarflow_800_pro_2")
        )
        runtime.start()
        try:
            dev = runtime.devices[0]
            wait_until(lambda: dev._service.connected, message="broker never connected")
            events = []
            dev.set_dispatch_observer(events.append)

            assert dev.write_output_limit(600) is True
            first = dev._active_command
            queued = dev.dispatch_output_limit(550)
            assert queued.status.value == "queued_latest"
            _publish_report_until(
                host,
                port,
                dev,
                600,
                lambda: dev._active_command is not None
                and dev._active_command.target_w == 550,
            )
            assert events[-1].status.value == "published"
            assert events[-1].correlation_id == queued.correlation_id
            assert first.state == "telemetry_confirmed"

            # Settle the flushed target, then prove the exact 600 -> 0 W safety
            # transition publishes immediately and supersedes its predecessor.
            _publish_report_until(
                host,
                port,
                dev,
                550,
                lambda: dev._active_command is None,
            )
            assert dev.write_output_limit(600) is True
            superseded = dev._active_command
            stop = dev.dispatch_output_limit(0)
            assert stop.status.value == "published"
            assert superseded.state == "superseded"

            # Late telemetry for 600 W cannot confirm the replacement stop.
            replacement = dev._active_command
            _publish_report_until(
                host,
                port,
                dev,
                600,
                lambda: replacement.state == "published",
            )
            assert replacement.state == "published"
            _publish_report_until(
                host,
                port,
                dev,
                0,
                lambda: replacement.state == "telemetry_confirmed",
            )
        finally:
            runtime.stop()


def test_real_mosquitto_duplicate_and_late_replies_are_ignored(tmp_path):
    with mosquitto_broker(tmp_path) as (host, port):
        runtime = build_zendure_mqtt_control_runtime(_config(host, port))
        dev = runtime.devices[0]
        service = dev._service
        topics, original_handler = service._reply_registrations[0]
        received_replies = []

        def counted_handler(payload):
            received_replies.append(payload)
            return original_handler(payload)

        service._reply_registrations[0] = (topics, counted_handler)
        runtime.start()
        try:
            wait_until(lambda: service.connected, message="broker never connected")
            dev.write_output_limit(500)
            command = dev._active_command
            success = _reply(command.message_id)
            publish_until(
                lambda: publish_once(
                    host, port, "iot/PK/DEV/function/invoke/reply", success
                ),
                lambda: command.state == "acknowledged",
                message="success reply was not routed",
            )
            before_duplicate = len(received_replies)
            publish_until(
                lambda: publish_once(
                    host,
                    port,
                    "iot/PK/DEV/function/invoke/reply",
                    _reply(command.message_id, success=0, output="error"),
                ),
                lambda: len(received_replies) > before_duplicate,
                message="duplicate reply was not delivered",
            )
            assert command.state == "acknowledged"

            _publish_report_until(
                host,
                port,
                dev,
                500,
                lambda: command.state == "telemetry_confirmed",
            )
            before_late = len(received_replies)
            publish_until(
                lambda: publish_once(
                    host,
                    port,
                    "iot/PK/DEV/function/invoke/reply",
                    _reply(command.message_id, success=0, output="late"),
                ),
                lambda: len(received_replies) > before_late,
                message="late reply was not delivered",
            )
            assert command.state == "telemetry_confirmed"
            assert dev._active_command is None
        finally:
            runtime.stop()


def test_real_mosquitto_disconnect_reconnect_restores_lifecycle(tmp_path):
    with mosquitto_broker(tmp_path, include_container_name=True) as (
        host,
        port,
        container,
    ):
        runtime = build_zendure_mqtt_control_runtime(
            _config(host, port, "solarflow_800_pro_2")
        )
        runtime.start()
        try:
            dev = runtime.devices[0]
            wait_until(lambda: dev._service.connected, message="broker never connected")
            restart = subprocess.Popen(
                ["docker", "restart", "--time", "1", container],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            wait_until(
                lambda: not dev._service.connected,
                timeout=8.0,
                message="control service never observed broker disconnect",
            )
            stdout, stderr = restart.communicate(timeout=15)
            assert restart.returncode == 0, stderr or stdout
            wait_until(
                lambda: dev._service.connected,
                timeout=15.0,
                message="control service never reconnected",
            )
            assert dev.write_output_limit(400) is True
            command = dev._active_command
            _publish_report_until(
                host,
                port,
                dev,
                400,
                lambda: command.state == "telemetry_confirmed",
            )
        finally:
            runtime.stop()


def test_real_mosquitto_multiple_control_brokers_stay_isolated(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    with mosquitto_broker(dir_a) as (host_a, port_a), mosquitto_broker(dir_b) as (
        host_b,
        port_b,
    ):
        config = {
            "zendure_mqtt": {
                "brokers": {
                    "a": {
                        "enabled": True,
                        "source": "local_mqtt",
                        "host": host_a,
                        "port": port_a,
                    },
                    "b": {
                        "enabled": True,
                        "source": "local_mqtt",
                        "host": host_b,
                        "port": port_b,
                    },
                }
            },
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "A",
                    "hardware_profile": "solarflow_800_pro_2",
                    "mqtt": {
                        "broker_ref": "a",
                        "topic_family": "legacy_zendure_json",
                        "device_id": "DEVA",
                        "product_key": "PKA",
                    },
                    "capabilities": {"write_output_limit": True},
                },
                {
                    "type": "zendure_mqtt",
                    "name": "B",
                    "hardware_profile": "solarflow_800_pro_2",
                    "mqtt": {
                        "broker_ref": "b",
                        "topic_family": "legacy_zendure_json",
                        "device_id": "DEVB",
                        "product_key": "PKB",
                    },
                    "capabilities": {"write_output_limit": True},
                },
            ],
        }
        runtime = build_zendure_mqtt_control_runtime(config)
        runtime.start()
        try:
            by_name = {device.name: device for device in runtime.devices}
            wait_until(
                lambda: all(device._service.connected for device in by_name.values()),
                message="control services did not connect to both brokers",
            )
            topic_a = "iot/PKA/DEVA/properties/write"
            topic_b = "iot/PKB/DEVB/properties/write"
            with _watch_topic(host_a, port_a, "iot/+/+/properties/write") as seen_a:
                with _watch_topic(host_b, port_b, "iot/+/+/properties/write") as seen_b:
                    assert by_name["A"].write_output_limit(400) is True
                    assert by_name["B"].write_output_limit(700) is True
                    wait_until(
                        lambda: len(seen_a) == 1 and len(seen_b) == 1,
                        message="isolated control writes were not observed",
                    )
                    assert [item[0] for item in seen_a] == [topic_a]
                    assert [item[0] for item in seen_b] == [topic_b]
        finally:
            runtime.stop()
