# SPDX-License-Identifier: AGPL-3.0-or-later
"""The broker-source verdict must hold on every layer that can enable a write.

The capability rule is only worth as much as its weakest consumer. These
contracts follow one unverified combination — a supported ZenSDK model observed
through scalar telemetry on a **local** broker with a complete write route —
from config validation through the control runtime into the actual dispatch
call, and back out through the discovery proposal and both Admin manual
editors. The same path on the Zendure cloud broker must stay fully controllable.
"""

import pytest

from admin.models import MqttHardwareCandidate
from admin.zendure_mqtt_config_draft import (
    build_manual_zendure_mqtt_fragment,
    manual_output_control_capability,
    zendure_mqtt_device_draft,
)
from admin.zendure_mqtt_config_proposals import build_proposals
from ems.mqtt_control.power_capability import (
    BLOCK_BROKER_SOURCE_UNKNOWN,
    BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED,
    BROKER_SOURCE_LOCAL_MQTT,
    BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
)
from ems.zendure_mqtt.config_entries import (
    validate_zendure_mqtt_control_device_config,
)
from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
from ems.zendure_mqtt.migration import plan_zendure_mqtt_migration
from ems.zendure_mqtt.topics import FAMILY_ZENSDK_HA_SCALAR

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]

PRODUCT_KEY = "TESTPK0001"
ROUTE_DEVICE_ID = "TESTROUTE01"
ZENSDK_MODEL = "solarflow_800_pro_2"


class _FakeService:
    def __init__(self, *_a, **_k):
        self.published = []

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True

    def publish_command(self, *args, **kwargs):
        self.published.append((args, kwargs))
        return True


def _control_entry(**mqtt_overrides):
    mqtt = {
        "broker_ref": "b1",
        "topic_family": FAMILY_ZENSDK_HA_SCALAR,
        "device_id": ROUTE_DEVICE_ID,
        "product_key": PRODUCT_KEY,
    }
    mqtt.update(mqtt_overrides)
    return {
        "type": "zendure_mqtt",
        "name": "INV_1",
        "enabled": True,
        "serial_number": "TESTSN000001",
        "hardware_profile": ZENSDK_MODEL,
        "power_write_profile": "zensdk_properties_write",
        "mqtt": mqtt,
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
    }


def _config(source):
    broker = {"enabled": True, "source": source, "host": "broker.example", "port": 1883}
    if source == BROKER_SOURCE_ZENDURE_CLOUD_MQTT:
        broker.update(
            {
                "port": 8883,
                "tls": True,
                "username": "u",
                "password": "p",
                "client_id": "c",
                "app_key": "a",
            }
        )
    return {
        "zendure_mqtt": {"brokers": {"b1": broker}},
        "devices": [_control_entry()],
    }


def _codes(issues):
    return {issue["code"] for issue in issues if issue.get("severity") == "error"}


# --- config validation --------------------------------------------------------


def test_validation_rejects_local_scalar_control():
    issues = validate_zendure_mqtt_control_device_config(
        _control_entry(), broker_sources={"b1": BROKER_SOURCE_LOCAL_MQTT}
    )

    assert BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED in _codes(issues)


def test_validation_accepts_cloud_scalar_control():
    issues = validate_zendure_mqtt_control_device_config(
        _control_entry(), broker_sources={"b1": BROKER_SOURCE_ZENDURE_CLOUD_MQTT}
    )

    assert _codes(issues) == set()


def test_validation_fails_closed_without_a_resolvable_source():
    issues = validate_zendure_mqtt_control_device_config(_control_entry())

    assert BLOCK_BROKER_SOURCE_UNKNOWN in _codes(issues)


def test_validation_reads_an_entry_stated_source_when_no_broker_map_is_given():
    issues = validate_zendure_mqtt_control_device_config(
        _control_entry(source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT)
    )

    assert _codes(issues) == set()


# --- control runtime ----------------------------------------------------------


def test_control_runtime_rejects_a_local_scalar_control_device():
    runtime = build_zendure_mqtt_control_runtime(
        _config(BROKER_SOURCE_LOCAL_MQTT), service_factory=_FakeService
    )

    assert runtime.devices == []
    assert len(runtime.rejected) == 1
    assert BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED in {
        issue["code"] for issue in runtime.rejected[0].issues
    }


def test_control_runtime_accepts_a_cloud_scalar_control_device():
    runtime = build_zendure_mqtt_control_runtime(
        _config(BROKER_SOURCE_ZENDURE_CLOUD_MQTT), service_factory=_FakeService
    )

    assert runtime.rejected == []
    assert len(runtime.devices) == 1
    assert runtime.devices[0].describe(now_monotonic=0.0)["control_supported"] is True


# --- dispatch -----------------------------------------------------------------


def _device_client(source):
    from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient

    service = _FakeService()
    device = ZendureMqttDeviceClient(
        "INV_1",
        service,
        device_id=ROUTE_DEVICE_ID,
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        source=source,
        broker_ref="b1",
        product_key=PRODUCT_KEY,
        hardware_profile=ZENSDK_MODEL,
        power_write_profile="zensdk_properties_write",
        max_power=2400,
    )
    return device, service


def test_dispatch_refuses_a_local_scalar_write():
    device, service = _device_client(BROKER_SOURCE_LOCAL_MQTT)

    result = device.dispatch_output_limit(500)

    assert result.status.value == "rejected"
    assert result.reason == BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED
    assert service.published == []
    described = device.describe(now_monotonic=0.0)
    assert described["control_supported"] is False
    assert described["control_block_reason"] == BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED


def test_dispatch_publishes_a_cloud_scalar_write():
    device, service = _device_client(BROKER_SOURCE_ZENDURE_CLOUD_MQTT)

    result = device.dispatch_output_limit(500)

    assert result.status.value == "published"
    assert service.published
    topic = service.published[0][0]
    assert topic == f"iot/{PRODUCT_KEY}/{ROUTE_DEVICE_ID}/properties/write"


def test_dispatch_fails_closed_on_an_unresolved_source():
    device, service = _device_client(None)

    assert device.write_output_limit(500) is False
    assert service.published == []
    assert (
        device.describe(now_monotonic=0.0)["control_block_reason"]
        == BLOCK_BROKER_SOURCE_UNKNOWN
    )


# --- discovery proposal -------------------------------------------------------


def _candidate(**overrides):
    fields = dict(
        broker_id="b1",
        broker_host="10.0.0.5",
        broker_port=1883,
        topic_family=FAMILY_ZENSDK_HA_SCALAR,
        device_id=ROUTE_DEVICE_ID,
        serial_number="TESTSN000001",
        product_key=PRODUCT_KEY,
        model_hint="SolarFlow 800 Pro 2",
        metrics_seen=["electricLevel", "outputHomePower", "solarInputPower", "outputLimit"],
    )
    fields.update(overrides)
    return MqttHardwareCandidate(**fields)


def test_local_scalar_proposal_is_not_control_capable():
    proposal = build_proposals(
        [_candidate(source_type=BROKER_SOURCE_LOCAL_MQTT).to_dict()]
    )[0]

    assert proposal["output_control_supported"] is False
    assert proposal["control_block_reason"] == BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED
    assert proposal["config_fragment"]["capabilities"]["write_output_limit"] is False


def test_cloud_scalar_proposal_stays_control_capable():
    proposal = build_proposals(
        [
            _candidate(
                source_type=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
                broker_host="mqtteu.zen-iot.com",
                broker_port=8883,
                tls_mode="encrypted_no_verify",
            ).to_dict()
        ]
    )[0]

    assert proposal["output_control_supported"] is True
    assert proposal["config_fragment"]["capabilities"]["write_output_limit"] is True


# --- manual Setup -------------------------------------------------------------


def _manual_item(**overrides):
    item = {
        "name": "INV_1",
        "serial_number": "TESTSN000001",
        "mqtt_device_id": ROUTE_DEVICE_ID,
        "product_key": PRODUCT_KEY,
        "hardware_generation": "solarflow_zensdk",
        "hardware_model": ZENSDK_MODEL,
        "capabilities": {"write_output_limit": True},
    }
    item.update(overrides)
    return item


def test_manual_local_scalar_entry_is_added_telemetry_only():
    fragment, issues = build_manual_zendure_mqtt_fragment(
        _manual_item(), "b1", broker_source=BROKER_SOURCE_LOCAL_MQTT
    )

    assert fragment is not None, issues
    assert fragment["capabilities"]["write_output_limit"] is False
    assert "zendure_mqtt_control_unavailable" in [issue["code"] for issue in issues]


def test_manual_cloud_scalar_entry_keeps_output_control():
    fragment, issues = build_manual_zendure_mqtt_fragment(
        _manual_item(), "b1", broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT
    )

    assert fragment is not None, issues
    assert fragment["capabilities"]["write_output_limit"] is True
    assert issues == []


def test_manual_projection_reports_the_source_axis():
    local = manual_output_control_capability(
        "solarflow_zensdk",
        ZENSDK_MODEL,
        product_key=PRODUCT_KEY,
        device_id=ROUTE_DEVICE_ID,
        broker_source=BROKER_SOURCE_LOCAL_MQTT,
    )
    assert local["supported"] is False
    assert local["reason"] == BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED
    assert local["broker_source_supported"] is False

    cloud = manual_output_control_capability(
        "solarflow_zensdk",
        ZENSDK_MODEL,
        product_key=PRODUCT_KEY,
        device_id=ROUTE_DEVICE_ID,
        broker_source=BROKER_SOURCE_ZENDURE_CLOUD_MQTT,
    )
    assert cloud["supported"] is True
    assert cloud["broker_source_supported"] is True


# --- Maintenance draft --------------------------------------------------------


def test_maintenance_draft_reports_the_source_axis():
    device = _control_entry()

    local = zendure_mqtt_device_draft(
        device, broker_sources={"b1": BROKER_SOURCE_LOCAL_MQTT}
    )
    assert local["supports_output_control"] is False
    assert local["control_readiness"]["reason"] == BLOCK_BROKER_SOURCE_WRITE_UNVERIFIED

    cloud = zendure_mqtt_device_draft(
        device, broker_sources={"b1": BROKER_SOURCE_ZENDURE_CLOUD_MQTT}
    )
    assert cloud["supports_output_control"] is True
    assert cloud["control_readiness"]["ready"] is True


# --- migration ----------------------------------------------------------------


def test_migration_disables_control_on_an_unverified_broker_source():
    changes = plan_zendure_mqtt_migration(_config(BROKER_SOURCE_LOCAL_MQTT))

    assert len(changes) == 1
    assert changes[0].action == "disable_control"
    assert changes[0].code == "zendure_mqtt_control_disabled_broker_source"


def test_migration_leaves_a_cloud_control_device_untouched():
    assert plan_zendure_mqtt_migration(_config(BROKER_SOURCE_ZENDURE_CLOUD_MQTT)) == []
