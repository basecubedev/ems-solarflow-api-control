# SPDX-License-Identifier: AGPL-3.0-or-later
"""Broker-level end-to-end control: publish -> reply -> confirmation.

Drives the real control runtime against the in-process fake broker to prove the
full live path: a model-aware command is published (retain=false), a correlated
reply acknowledges it, fresh telemetry confirms it, and a wrong/absent reply
never confirms. Restart from persisted config keeps reply routing functional.
"""

import json

import pytest

from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
from tests.helpers.fake_mqtt import FakeClock, FakeMqttNetwork

pytestmark = pytest.mark.simulation


def _config(hardware_profile="hyper_2000"):
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
                "name": "Hyper",
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


def _build(config, clock=None):
    network = FakeMqttNetwork(clock=clock)
    runtime = build_zendure_mqtt_control_runtime(
        config, service_factory=network.control_service_factory()
    )
    runtime.start()
    return runtime, network


def _reply_payload(broker, *, success=1, output="success"):
    # Read messageId from the last invoke publish so the reply correlates.
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


def test_hyper_broker_e2e_success_reply_and_telemetry_confirm():
    # Real monotonic clock so the command's publish time and the telemetry's
    # observed time share one clock (the confirmation newer-than check relies on
    # a single monotonic source, as in production).
    runtime, network = _build(_config("hyper_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")

    assert dev.write_output_limit(500) is True
    invoke = [r for r in broker.publish_calls if r.topic.endswith("function/invoke")]
    assert len(invoke) == 1
    assert invoke[0].topic == "iot/PK/DEV/function/invoke"
    # No hardware-control publish is retained.
    assert all(r.retain is False for r in broker.publish_calls)
    assert dev._active_command.state == "published"

    # A correlated success reply acknowledges the command.
    broker.inject("iot/PK/DEV/function/invoke/reply", _reply_payload(broker))
    assert dev._active_command.state == "acknowledged"

    # Fresh telemetry (observed after the command) at the target confirms it.
    broker.inject(
        "iot/PK/DEV/properties/report",
        json.dumps({"sn": "DEV", "properties": {"outputLimit": 500}}).encode(),
    )
    dev.fetch()
    assert dev._last_command.state == "telemetry_confirmed"


def test_hub_broker_e2e_scalar_invoke_and_charge_rejected():
    runtime, network = _build(_config("hub_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")

    # Discharge publishes a scalar function/invoke.
    assert dev.write_output_limit(400) is True
    invoke = [r for r in broker.publish_calls if r.topic.endswith("function/invoke")]
    assert json.loads(invoke[-1].payload)["arguments"][0]["autoModelValue"] == 400

    # Idle is a changed target while 400 is still in flight: accepted and held as
    # the single pending target (no second publish yet).
    assert dev.write_output_limit(0) is True
    # Charge is unsupported for a Hub: rejected before publish, no pending stored.
    before = len(broker.publish_calls)
    assert dev.write_output_limit(-200) is False
    assert len(broker.publish_calls) == before


def test_wrong_device_reply_never_acknowledges_e2e():
    runtime, network = _build(_config("hyper_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    dev.write_output_limit(500)
    # A reply for a different device id must be ignored.
    broker.inject(
        "iot/PK/DEV/function/invoke/reply",
        json.dumps(
            {"messageId": dev._active_command.message_id, "deviceId": "OTHER", "success": 1}
        ).encode(),
    )
    assert dev._active_command.state == "published"


def test_no_reply_times_out_e2e():
    clock = FakeClock(start=1000.0)
    runtime, network = _build(_config("hyper_2000"), clock=clock)
    dev = runtime.devices[0]
    dev.write_output_limit(500)
    created = dev._active_command.created_monotonic
    dev.describe(now_monotonic=created + 6.0)
    assert dev._last_command.state == "timed_out"


def test_restart_preserves_profile_and_reply_routing():
    config = _config("hyper_2000")
    # First runtime.
    runtime1, network1 = _build(json.loads(json.dumps(config)))
    dev1 = runtime1.devices[0]
    broker1 = network1.broker("local_a")
    dev1.write_output_limit(500)
    broker1.inject("iot/PK/DEV/function/invoke/reply", _reply_payload(broker1))
    assert dev1._active_command.state == "acknowledged"

    # Restart: a fresh runtime from the same persisted config, no discovery cache.
    runtime2, network2 = _build(json.loads(json.dumps(config)))
    dev2 = runtime2.devices[0]
    assert dev2 is not dev1
    assert dev2.hardware_profile == "hyper_2000"
    broker2 = network2.broker("local_a")
    dev2.write_output_limit(600)
    broker2.inject("iot/PK/DEV/function/invoke/reply", _reply_payload(broker2))
    assert dev2._active_command.state == "acknowledged"


def test_hyper_failure_reply_rejects_the_command():
    runtime, network = _build(_config("hyper_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    dev.write_output_limit(500)
    broker.inject(
        "iot/PK/DEV/function/invoke/reply",
        _reply_payload(broker, success=0, output="error"),
    )
    assert dev._last_command.state == "rejected"
    assert dev._active_command is None


def test_hub_success_reply_acknowledges_scalar_command():
    runtime, network = _build(_config("hub_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    dev.write_output_limit(400)
    broker.inject("iot/PK/DEV/function/invoke/reply", _reply_payload(broker))
    assert dev._active_command.state == "acknowledged"


def test_changed_target_coalesces_over_the_broker():
    runtime, network = _build(_config("hyper_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    dev.write_output_limit(500)
    dev.write_output_limit(300)
    invoke = [r for r in broker.publish_calls if r.topic.endswith("function/invoke")]
    assert len(invoke) == 1
    assert dev._pending_target == 300
    # Rejecting the active command flushes the pending target once.
    broker.inject(
        "iot/PK/DEV/function/invoke/reply",
        _reply_payload(broker, success=0, output="error"),
    )
    invoke = [r for r in broker.publish_calls if r.topic.endswith("function/invoke")]
    assert len(invoke) == 2
    assert dev._active_command.target_w == 300


def test_wrong_message_id_reply_never_acknowledges_e2e():
    runtime, network = _build(_config("hyper_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    dev.write_output_limit(500)
    broker.inject(
        "iot/PK/DEV/function/invoke/reply",
        json.dumps(
            {"messageId": dev._active_command.message_id + 5000, "deviceId": "DEV", "success": 1}
        ).encode(),
    )
    assert dev._active_command.state == "published"


def test_duplicate_reply_after_ack_is_ignored_e2e():
    runtime, network = _build(_config("hyper_2000"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    dev.write_output_limit(500)
    broker.inject("iot/PK/DEV/function/invoke/reply", _reply_payload(broker))
    assert dev._active_command.state == "acknowledged"
    # A duplicate/late failure reply cannot flip an already-acknowledged command.
    broker.inject(
        "iot/PK/DEV/function/invoke/reply",
        _reply_payload(broker, success=0, output="error"),
    )
    assert dev._active_command.state == "acknowledged"


def test_confirmation_times_out_after_ack_without_telemetry_e2e():
    runtime, network = _build(_config("hyper_2000"))
    dev = runtime.devices[0]
    # Tight confirmation deadline; no telemetry ever arrives.
    dev._confirmation_timeout_s = 5.0
    broker = network.broker("local_a")
    dev.write_output_limit(500)
    broker.inject("iot/PK/DEV/function/invoke/reply", _reply_payload(broker))
    rec = dev._active_command
    dev.describe(now_monotonic=rec.acknowledged_monotonic + 6.0)
    assert dev._last_command.state == "confirmation_timed_out"
    assert dev._active_command is None


def test_unknown_profile_is_rejected_and_publishes_nothing():
    runtime, network = _build(_config("typo_profile"))
    # An unknown hardware profile is rejected at build; no control device runs.
    assert runtime.devices == []
    assert runtime.has_rejections
    broker = network.broker("local_a")
    assert broker.publish_calls == []


def test_negative_zensdk_target_publishes_nothing_e2e():
    runtime, network = _build(_config("solarflow_800_pro_2"))
    dev = runtime.devices[0]
    broker = network.broker("local_a")
    # ZenSDK cannot charge: a negative target is rejected before any publish, and
    # the properties/write path never carries a negative outputLimit.
    assert dev.write_output_limit(-500) is False
    assert broker.publish_calls == []
