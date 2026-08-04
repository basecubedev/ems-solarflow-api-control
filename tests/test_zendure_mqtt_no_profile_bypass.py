# SPDX-License-Identifier: AGPL-3.0-or-later
"""No built-in write protocol authorizes control without a concrete profile.

The removed bypass: a control device that pinned no ``hardware_profile`` could
still publish by carrying a built-in ``mqtt.write_protocol`` such as
``legacy_properties_write``. A transport/write-protocol string is not hardware
identity, so it must never authorize a real power write on its own. Only a
concrete, registry-resolved hardware profile (or the isolated, explicit
``custom_properties_write`` escape hatch with its own ``mqtt.write_topic``) may.
"""

import pytest

from ems.zendure_mqtt.config_entries import (
    validate_zendure_mqtt_control_device_config,
)
from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class _FakeService:
    def __init__(self, *_a, **_k):
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


# --- direct device-client bypass --------------------------------------------


def test_device_client_does_not_publish_without_profile():
    # hardware_profile=None + a built-in write_protocol must never publish.
    service = _FakeService()
    dev = ZendureMqttDeviceClient(
        "WR",
        service,
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile=None,
        write_protocol="legacy_properties_write",
    )
    assert dev.write_output_limit(500) is False
    assert service.published == []
    described = dev.describe(now_monotonic=0.0)
    assert described["control_supported"] is False
    assert described["control_block_reason"] == "hardware_profile_missing"


# --- config validation bypass -----------------------------------------------


def _no_profile_control_entry():
    return {
        "type": "zendure_mqtt",
        "name": "Legacy",
        "hardware_profile": None,
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEV",
            "product_key": "PK",
            "write_protocol": "legacy_properties_write",
        },
        "capabilities": {"write_output_limit": True},
    }


def test_legacy_properties_write_without_profile_is_rejected():
    issues = validate_zendure_mqtt_control_device_config(_no_profile_control_entry())
    codes = {i["code"] for i in issues if i.get("severity") == "error"}
    assert "write_protocol_unsupported" in codes


def test_control_runtime_rejects_no_profile_built_in_protocol():
    config = {
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
        "devices": [_no_profile_control_entry()],
    }
    config["devices"][0]["mqtt"]["broker_ref"] = "local_a"
    runtime = build_zendure_mqtt_control_runtime(
        config, service_factory=lambda cfg: _FakeService(cfg)
    )
    assert runtime.devices == []
    assert len(runtime.rejected) == 1
    assert "write_protocol_unsupported" in {
        i["code"] for i in runtime.rejected[0].issues
    }
    for service in runtime.services:
        assert service.published == []
