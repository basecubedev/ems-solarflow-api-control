# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime device adapter routes power targets by hardware profile.

When a control device carries a resolved ``hardware_profile``, its write path is
selected by that profile's ``power_write_profile``: legacy hub/object models
publish ``function/invoke`` deviceAutomation commands, ZenSDK keeps its
``properties/write`` path, and a telemetry-only profile never publishes. The
controller's neutral signed target selects discharge / idle / charge; an
unsupported operation is rejected without a publish.
"""

import json

import pytest

from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class FakeService:
    def __init__(self):
        self.published = []

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _device(hardware_profile, **kwargs):
    return ZendureMqttDeviceClient(
        "WR-MQTT",
        FakeService(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PRODUCT_KEY",
        max_power=2000,
        hardware_profile=hardware_profile,
        **kwargs,
    )


def _published_payload(dev):
    topic, payload = dev._service.published[-1]
    return topic, json.loads(payload)


# --- object automation (Hyper 2000) -----------------------------------------


def test_hyper_profile_publishes_object_discharge_command():
    dev = _device("hyper_2000")
    assert dev.write_output_limit(500) is True
    topic, payload = _published_payload(dev)
    assert topic == "iot/PRODUCT_KEY/DEVICE_ID/function/invoke"
    arg = payload["arguments"][0]
    assert arg["autoModelProgram"] == 2
    assert arg["autoModelValue"] == {
        "chargingType": 0,
        "chargingPower": 0,
        "freq": 0,
        "outPower": 500,
    }


def test_hyper_profile_zero_target_publishes_idle_command():
    dev = _device("hyper_2000")
    assert dev.write_output_limit(0) is True
    _topic, payload = _published_payload(dev)
    assert payload["arguments"][0]["autoModelProgram"] == 0
    assert payload["arguments"][0]["autoModelValue"]["outPower"] == 0


def test_hyper_profile_negative_target_publishes_charge_command():
    dev = _device("hyper_2000")
    assert dev.write_output_limit(-500) is True
    _topic, payload = _published_payload(dev)
    arg = payload["arguments"][0]
    assert arg["autoModelProgram"] == 1
    assert arg["autoModelValue"]["chargingPower"] == 500


# --- scalar automation (Hub 2000) -------------------------------------------


def test_hub_profile_publishes_scalar_discharge_command():
    dev = _device("hub_2000")
    assert dev.write_output_limit(500) is True
    topic, payload = _published_payload(dev)
    assert topic == "iot/PRODUCT_KEY/DEVICE_ID/function/invoke"
    assert payload["arguments"][0]["autoModelValue"] == 500


def test_hub_profile_rejects_negative_target_without_publish():
    dev = _device("hub_2000")
    service = dev._service
    assert dev.write_output_limit(-500) is False
    assert service.published == []


def test_aio_profile_rejects_negative_target_without_publish():
    dev = _device("aio_2400")
    service = dev._service
    assert dev.write_output_limit(-500) is False
    assert service.published == []


# --- ZenSDK publishes the atomic mode+power properties write -----------------


def test_zensdk_profile_publishes_atomic_properties_write():
    # Source contract (Zendure-HA ZendureZenSdk.discharge): the power command
    # carries smartMode/acMode/outputLimit/inputLimit in ONE properties write —
    # a bare outputLimit is ignored by a device in an inactive mode.
    dev = _device("solarflow_800_pro_2")
    assert dev.write_output_limit(300) is True
    topic, payload = _published_payload(dev)
    assert topic == "iot/PRODUCT_KEY/DEVICE_ID/properties/write"
    assert payload["properties"] == {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 300,
        "inputLimit": 0,
    }


# --- telemetry-only hardware never writes -----------------------------------


def test_telemetry_only_profile_never_publishes():
    dev = _device("ace_1500")
    service = dev._service
    assert dev.write_output_limit(500) is False
    assert service.published == []


def test_describe_reports_hardware_profile_and_power_write_profile():
    dev = _device("hyper_2000")
    described = dev.describe(now_monotonic=0.0)
    assert described["hardware_profile"] == "hyper_2000"
    assert described["power_write_profile"] == "legacy_object_device_automation"
    assert set(described["supported_operations"]) == {"discharge", "idle", "charge"}


# --- config accessor + control-runtime wiring -------------------------------


def test_hardware_profile_accessor_reads_config_entry():
    from ems.zendure_mqtt.config_entries import zendure_mqtt_hardware_profile

    assert zendure_mqtt_hardware_profile({"hardware_profile": "hyper_2000"}) == "hyper_2000"
    assert (
        zendure_mqtt_hardware_profile({"mqtt": {"hardware_profile": "hub_2000"}})
        == "hub_2000"
    )
    assert zendure_mqtt_hardware_profile({}) is None
    assert zendure_mqtt_hardware_profile({"hardware_profile": "  "}) is None


class _FakeControlService:
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
                "name": "Hyper",
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


def test_control_runtime_routes_configured_hardware_profile_to_invoke():
    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

    runtime = build_zendure_mqtt_control_runtime(
        _control_config("hyper_2000"),
        service_factory=lambda broker_cfg: _FakeControlService(broker_cfg),
    )
    assert runtime.rejected == []
    dev = runtime.devices[0]
    assert dev.hardware_profile == "hyper_2000"
    assert dev.write_output_limit(600) is True
    topic, payload = dev._service.published[-1]
    assert topic == "iot/PRODUCT_KEY/DEVICE_ID/function/invoke"
    assert json.loads(payload)["arguments"][0]["autoModelValue"]["outPower"] == 600
