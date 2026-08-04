# SPDX-License-Identifier: AGPL-3.0-or-later
"""An explicitly configured but invalid hardware profile fails closed.

A control device may only publish when its pinned ``hardware_profile`` resolves
to a known, writable model. A misspelled or otherwise unknown explicit profile
must never fall back to the legacy topic-family ``properties/write`` path: config
validation rejects it, the runtime refuses to build a publishing client, and the
device client itself fails closed as defense in depth. No MQTT publish may occur.
"""

import pytest

from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class FakeService:
    def __init__(self, broker_config=None):
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


def _client(hardware_profile):
    return ZendureMqttDeviceClient(
        "WR-MQTT",
        FakeService(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PRODUCT_KEY",
        max_power=2000,
        hardware_profile=hardware_profile,
    )


def _control_config(hardware_profile):
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
                "name": "Typo",
                "hardware_profile": hardware_profile,
                "mqtt": {
                    "broker_ref": "local_a",
                    "topic_family": "legacy_zendure_json",
                    "device_id": "DEVICE_ID",
                    "product_key": "PRODUCT_KEY",
                },
                "capabilities": {"write_output_limit": True},
            }
        ],
    }


# --- device client fails closed ---------------------------------------------


def test_invalid_explicit_hardware_profile_never_publishes():
    dev = _client("typo_profile")
    service = dev._service
    assert dev.write_output_limit(123) is False
    assert service.published == []


def test_invalid_explicit_hardware_profile_records_write_failure():
    dev = _client("typo_profile")
    assert dev.write_output_limit(123) is False
    # The write is reported unhealthy, never silently dropped.
    assert dev.write_health.consecutive_failures >= 1


# --- config validation rejects the entry ------------------------------------


def test_config_validation_flags_unknown_hardware_profile():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    item = _control_config("typo_profile")["devices"][0]
    issues = validate_zendure_mqtt_control_device_config(
        item, known_broker_refs={"local_a"}, brokers_defined=True
    )
    codes = {issue["code"] for issue in issues}
    assert "hardware_profile_unknown" in codes


# --- runtime construction fails closed --------------------------------------


def test_control_runtime_rejects_unknown_hardware_profile():
    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

    runtime = build_zendure_mqtt_control_runtime(
        _control_config("typo_profile"),
        service_factory=lambda broker_cfg: FakeService(broker_cfg),
    )
    # No publishing client is built for an invalid-profile control device.
    assert runtime.devices == []
    assert len(runtime.rejected) == 1
    codes = {i["code"] for i in runtime.rejected[0].issues}
    assert "hardware_profile_unknown" in codes
    # Nothing was published while assembling the runtime.
    for service in runtime.services:
        assert getattr(service, "published", []) == []
