# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime power-write routing survives a restart with no discovery cache.

The runtime selects its write adapter only from the persisted, validated
``hardware_profile`` — never from a discovery cache, last-seen telemetry, or
unpersisted proposal data. Rebuilding the runtime from the same config (a
process restart) keeps the exact same command contract; a tampered profile in
persisted config fails validation and never publishes.
"""

import json

import pytest

from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

pytestmark = pytest.mark.simulation


class FakeControlService:
    def __init__(self, broker_config):
        self.published = []
        self.connected = False

    def start(self):
        self.connected = True

    def stop(self):
        self.connected = False

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _persisted_config(hardware_profile):
    # Represents config.json after a discovery proposal was reviewed + applied.
    return {
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "local_a": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "a",
                    "port": 1883,
                }
            },
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "Hyper",
                "hardware_profile": hardware_profile,
                "power_write_profile": "legacy_object_device_automation",
                "mqtt": {
                    "broker_ref": "local_a",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEVID",
                    "product_key": "PKID",
                },
                "capabilities": {"write_output_limit": True},
            }
        ],
    }


def _build(config):
    return build_zendure_mqtt_control_runtime(
        config, service_factory=lambda cfg: FakeControlService(cfg)
    )


def _invoke_out_power(dev):
    topic, payload = dev._service.published[-1]
    body = json.loads(payload)
    return topic, body["arguments"][0]["autoModelValue"]["outPower"]


def test_hyper_route_survives_restart_from_persisted_config():
    # Round-trip the persisted config through JSON to mimic reload from disk.
    config = json.loads(json.dumps(_persisted_config("hyper_2000")))

    first = _build(config)
    assert first.rejected == []
    dev1 = first.devices[0]
    assert dev1.write_output_limit(600) is True
    topic1, out1 = _invoke_out_power(dev1)
    assert topic1 == "iot/PKID/DEVID/function/invoke"
    assert out1 == 600

    # Restart: a fresh runtime built from the same persisted config, no shared
    # state and no discovery cache, keeps the exact same command contract.
    reloaded = json.loads(json.dumps(config))
    second = _build(reloaded)
    assert second.rejected == []
    dev2 = second.devices[0]
    assert dev2 is not dev1
    assert dev2.hardware_profile == "hyper_2000"
    assert dev2.write_output_limit(600) is True
    topic2, out2 = _invoke_out_power(dev2)
    assert (topic2, out2) == (topic1, out1)


def test_tampered_profile_after_restart_fails_closed():
    config = json.loads(json.dumps(_persisted_config("hyper_9000_tampered")))
    runtime = _build(config)
    # No publishing client is built for a tampered/unknown profile.
    assert runtime.devices == []
    assert len(runtime.rejected) == 1
    assert "hardware_profile_unknown" in {i["code"] for i in runtime.rejected[0].issues}
    for service in runtime.services:
        assert service.published == []


def test_runtime_rejects_contradictory_power_write_profile_after_restart():
    # A persisted power_write_profile that contradicts the pinned model is a
    # tampered/corrupt config: the control device is rejected (no writer built)
    # rather than silently trusting the registry.
    config = _persisted_config("hub_2000")
    config["devices"][0]["power_write_profile"] = "legacy_object_device_automation"  # wrong
    runtime = _build(config)
    assert runtime.devices == []
    assert len(runtime.rejected) == 1
    assert "power_write_profile_mismatch" in {
        i["code"] for i in runtime.rejected[0].issues
    }
    for service in runtime.services:
        assert service.published == []


def test_runtime_accepts_matching_power_write_profile_after_restart():
    # The matching informational value is fine and the adapter still comes from
    # the registry (Hub uses the scalar automation value).
    config = _persisted_config("hub_2000")
    config["devices"][0]["power_write_profile"] = "legacy_hub_device_automation"
    runtime = _build(config)
    assert runtime.rejected == []
    dev = runtime.devices[0]
    assert dev.write_output_limit(400) is True
    topic, payload = dev._service.published[-1]
    assert json.loads(payload)["arguments"][0]["autoModelValue"] == 400
