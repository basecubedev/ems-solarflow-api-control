# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stored power_write_profile metadata must agree with the hardware registry.

The registry is authoritative, but a persisted ``power_write_profile`` that
contradicts the pinned ``hardware_profile`` is a corruption/tampering signal: the
config validator rejects it with a stable, actionable error and the runtime
device adapter fails closed rather than silently trusting the registry, so a
contradictory config can never publish a power write.
"""

import pytest

from ems.zendure_mqtt.config_entries import (
    validate_zendure_mqtt_control_device_config,
    validate_zendure_mqtt_device_config,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def _control_entry(**top):
    entry = {
        "type": "zendure_mqtt",
        "name": "Ctrl",
        "mqtt": {
            "source": "local_mqtt",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEV",
            "product_key": "PK",
        },
        "capabilities": {"write_output_limit": True},
    }
    entry.update(top)
    return entry


def _codes(issues):
    return {i["code"] for i in issues}


def test_matching_profile_pair_validates():
    entry = _control_entry(
        hardware_profile="hub_2000",
        power_write_profile="legacy_hub_device_automation",
    )
    assert validate_zendure_mqtt_control_device_config(entry) == []


def test_mismatched_profile_pair_fails():
    entry = _control_entry(
        hardware_profile="hub_2000",
        power_write_profile="legacy_object_device_automation",
    )
    issues = validate_zendure_mqtt_control_device_config(entry)
    assert "power_write_profile_mismatch" in _codes(issues)
    message = next(
        i["message"] for i in issues if i["code"] == "power_write_profile_mismatch"
    )
    assert "hub_2000" in message
    assert "legacy_hub_device_automation" in message


def test_missing_informational_profile_is_regenerated_safely():
    # No stored power_write_profile: the registry re-derives it, so no error.
    entry = _control_entry(hardware_profile="hub_2000")
    assert validate_zendure_mqtt_control_device_config(entry) == []


def test_telemetry_only_profile_rejects_writable_metadata():
    # A read-only model tagged with a writable power_write_profile is a mismatch.
    entry = {
        "type": "zendure_mqtt",
        "name": "Ace",
        "mqtt": {"source": "local_mqtt", "topic_family": "legacy_zendure_json", "device_id": "DEV"},
        "hardware_profile": "ace_1500",
        "power_write_profile": "legacy_object_device_automation",
    }
    issues = validate_zendure_mqtt_device_config(entry)
    assert "power_write_profile_mismatch" in _codes(issues)


def test_telemetry_only_profile_with_matching_metadata_validates():
    entry = {
        "type": "zendure_mqtt",
        "name": "Ace",
        "mqtt": {"source": "local_mqtt", "topic_family": "legacy_zendure_json", "device_id": "DEV"},
        "hardware_profile": "ace_1500",
        "power_write_profile": "telemetry_only",
    }
    assert _codes(validate_zendure_mqtt_device_config(entry)) == set()


def test_runtime_adapter_fails_closed_on_mismatch():
    # Even if validation is bypassed, the device adapter must not publish.
    from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient

    class _StubService:
        connected = True

        def publish_output_limit(self, topic, payload):  # pragma: no cover
            raise AssertionError("a mismatched device must never publish")

        def snapshot_status(self, *a, **k):
            raise AssertionError("not needed")

    dev = ZendureMqttDeviceClient(
        "Ctrl",
        _StubService(),
        device_id="DEV",
        topic_family="legacy_zendure_json",
        source="local_mqtt",
        product_key="PK",
        hardware_profile="hub_2000",
        power_write_profile="legacy_object_device_automation",
    )
    assert dev.write_output_limit(250) is False
