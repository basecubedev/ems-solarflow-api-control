# SPDX-License-Identifier: AGPL-3.0-or-later
"""A physical serial is never an MQTT control route device id.

Reproductions for the write-addressing defect: a control-capable Zendure MQTT
device must carry an explicit ``mqtt.device_id`` route identifier, and the
physical ``serial_number`` must never be silently substituted for it across the
proposal mapper, config validator, control runtime, Cloud subscriptions,
migration, Maintenance readiness and the low-level properties/write builder.

Every test here asserts the required post-fix behavior; each fails on the
Archive-80 baseline ``f1e0468`` where the serial fallback is still accepted.
"""

import pytest

from ems.zendure_mqtt.config_entries import (
    ZENDURE_MQTT_TYPE,
    validate_zendure_mqtt_control_device_config,
    zendure_cloud_device_subscriptions,
)
from ems.zendure_mqtt.config_mapping import map_snapshot_to_proposal
from ems.zendure_mqtt.snapshot import ZendureMqttSnapshot
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON
from ems.zendure_mqtt.write_protocols import (
    PROTOCOL_CUSTOM_PROPERTIES_WRITE,
    build_output_limit_message,
)

pytestmark = pytest.mark.simulation


def _control_device(**over):
    """A profile-backed 800 Pro 2 control entry addressed by a real route id."""

    device = {
        "type": ZENDURE_MQTT_TYPE,
        "name": "Legacy",
        "serial_number": "SERIAL-1",
        "hardware_profile": "solarflow_800_pro_2",
        "power_write_profile": "zensdk_properties_write",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": FAMILY_LEGACY_JSON,
            "device_id": "DEV",
            "product_key": "PK-A",
        },
        "capabilities": {"write_output_limit": True},
    }
    device.update(over)
    return device


def _no_route_control_device():
    """The unsafe entry: product key + physical serial but no ``mqtt.device_id``."""

    device = _control_device()
    device["mqtt"].pop("device_id", None)
    return device


# --- Defect 1: proposal mapper enables writes without mqtt.device_id ---------


def test_supported_snapshot_without_device_id_is_not_writable():
    snap = ZendureMqttSnapshot(
        device_id=None,
        serial_number="SERIAL-1",
        product_key="PK-A",
        product="SolarFlow 800 Pro 2",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"outputLimit": 0, "electricLevel": 50},
        capabilities={"output_control", "battery_storage"},
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.output_control_supported is False
    assert proposal.output_control_reason == "write_target_missing"
    assert proposal.control_block_reason == "write_target_missing"
    fragment = proposal.config_fragment
    assert fragment["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in fragment["mqtt"]


# --- Defect 2: config validation accepts the unaddressable control entry -----


def test_control_validator_rejects_missing_route_device_id():
    issues = validate_zendure_mqtt_control_device_config(_no_route_control_device())
    codes = {issue["code"] for issue in issues if issue.get("severity") == "error"}
    assert "mqtt_device_id_missing" in codes


def test_control_validator_accepts_explicit_route_device_id():
    issues = validate_zendure_mqtt_control_device_config(_control_device())
    codes = {issue["code"] for issue in issues if issue.get("severity") == "error"}
    assert codes == set()


# --- Defect 3: runtime substitutes physical serial into the write route ------


def test_runtime_rejects_control_device_without_route_device_id():
    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

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
        "devices": [_no_route_control_device()],
    }

    class _FakeService:
        def __init__(self, broker_config):
            self.broker_config = broker_config

        def register_reply_handler(self, *args, **kwargs):
            return None

    runtime = build_zendure_mqtt_control_runtime(config, service_factory=_FakeService)
    # The physical serial must never be built into a control client as its route.
    assert all(getattr(dev, "_device_id", None) != "SERIAL-1" for dev in runtime.devices)
    rejected_codes = {
        code
        for entry in runtime.rejected
        for code in (i.get("code") for i in entry.issues)
    }
    assert "mqtt_device_id_missing" in rejected_codes


# --- Defect 4: Cloud subscriptions use the physical serial as route id --------


def test_cloud_subscriptions_never_use_physical_serial():
    devices = [_no_route_control_device()]
    devices[0]["mqtt"]["broker_ref"] = "cloud"
    assert zendure_cloud_device_subscriptions(devices, "cloud") == ()


# --- Defect 5: migration considers the broken entry safe ---------------------


def test_migration_disables_control_without_route_device_id():
    from ems.zendure_mqtt.migration import plan_zendure_mqtt_migration

    config = {"devices": [_no_route_control_device()]}
    changes = plan_zendure_mqtt_migration(config)
    assert [c.action for c in changes] == ["disable_control"]
    assert changes[0].code == "zendure_mqtt_control_disabled_unaddressable"


# --- Defect 6: Maintenance readiness reports the entry as ready --------------


def test_maintenance_readiness_requires_route_device_id():
    from admin.zendure_mqtt_config_draft import zendure_mqtt_device_draft

    draft = zendure_mqtt_device_draft(_no_route_control_device())
    assert draft["control_readiness"]["ready"] is False


# --- Defect 7: low-level properties/write builder permits a null device id ----


def test_custom_properties_write_rejects_null_device_id():
    message = build_output_limit_message(
        PROTOCOL_CUSTOM_PROPERTIES_WRITE,
        topic_family=FAMILY_LEGACY_JSON,
        product_key="PK-A",
        device_id=None,
        output_limit_w=300,
        write_topic="iot/PK-A/DEV/properties/write",
    )
    assert message is None


# --- Config-entry validation coverage ---------------------------------------


def test_telemetry_only_serial_fallback_remains_valid():
    from ems.zendure_mqtt.config_entries import validate_zendure_mqtt_device_config

    telemetry = {
        "type": ZENDURE_MQTT_TYPE,
        "name": "Telemetry",
        "serial_number": "SERIAL-1",
        "mqtt": {"broker_ref": "local_a", "topic_family": FAMILY_LEGACY_JSON},
    }
    codes = {
        i["code"]
        for i in validate_zendure_mqtt_device_config(telemetry)
        if i.get("severity") == "error"
    }
    assert codes == set()


def test_control_profile_without_product_key_is_invalid():
    device = _control_device()
    device["mqtt"].pop("product_key", None)
    codes = {
        i["code"]
        for i in validate_zendure_mqtt_control_device_config(device)
        if i.get("severity") == "error"
    }
    assert "write_target_missing" in codes


def test_serial_equal_to_route_id_still_requires_explicit_device_id():
    # Even when the physical serial happens to equal a route id, the entry must
    # carry an explicit mqtt.device_id; the serial is never read as the route.
    device = _control_device()
    device["serial_number"] = "DEV"
    device["mqtt"].pop("device_id", None)
    codes = {
        i["code"]
        for i in validate_zendure_mqtt_control_device_config(device)
        if i.get("severity") == "error"
    }
    assert "mqtt_device_id_missing" in codes


def test_custom_write_without_device_id_is_invalid():
    device = {
        "type": ZENDURE_MQTT_TYPE,
        "name": "Custom",
        "serial_number": "SERIAL-1",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": FAMILY_LEGACY_JSON,
            "write_protocol": "custom_properties_write",
            "write_topic": "iot/PK-A/DEV/properties/write",
        },
        "capabilities": {"write_output_limit": True},
    }
    codes = {
        i["code"]
        for i in validate_zendure_mqtt_control_device_config(device)
        if i.get("severity") == "error"
    }
    assert "mqtt_device_id_missing" in codes


# --- Route device-id helper contract ----------------------------------------


def test_route_device_id_reads_only_mqtt_device_id():
    from ems.zendure_mqtt.config_entries import (
        zendure_mqtt_device_identifier,
        zendure_mqtt_route_device_id,
    )

    device = _control_device()
    device["serial_number"] = "SERIAL-1"
    device["device_id"] = "TOPLEVEL"
    device["mqtt"].pop("device_id", None)
    # Telemetry fallback still resolves an identifier; the route helper does not.
    assert zendure_mqtt_device_identifier(device) == "SERIAL-1"
    assert zendure_mqtt_route_device_id(device) is None
    device["mqtt"]["device_id"] = "  RouteDev  "
    assert zendure_mqtt_route_device_id(device) == "RouteDev"  # trimmed, case kept


# --- Mapper coverage --------------------------------------------------------


def test_supported_model_without_product_key_is_telemetry_only():
    snap = ZendureMqttSnapshot(
        device_id="DEV",
        serial_number="SERIAL-1",
        product_key=None,
        product="SolarFlow 800 Pro 2",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"outputLimit": 0, "electricLevel": 50},
        capabilities={"output_control", "battery_storage"},
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.output_control_supported is False
    assert proposal.control_block_reason == "write_target_missing"
    assert proposal.config_fragment["capabilities"]["write_output_limit"] is False


def test_complete_route_is_writable():
    snap = ZendureMqttSnapshot(
        device_id="DEV",
        serial_number="SERIAL-1",
        product_key="PK-A",
        product="SolarFlow 800 Pro 2",
        topic_families={FAMILY_LEGACY_JSON},
        metrics={"outputLimit": 0, "electricLevel": 50},
        capabilities={"output_control", "battery_storage"},
    )
    proposal = map_snapshot_to_proposal(snap)
    assert proposal.output_control_supported is True
    assert proposal.config_fragment["mqtt"]["device_id"] == "DEV"
    assert proposal.config_fragment["mqtt"]["product_key"] == "PK-A"


# --- Runtime coverage -------------------------------------------------------


class _FakeService:
    def __init__(self, broker_config):
        self.broker_config = broker_config

    def register_reply_handler(self, *args, **kwargs):
        return None


def _runtime_config(device):
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
        "devices": [device],
    }


def test_runtime_control_client_uses_explicit_route_device_id():
    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

    device = _control_device()
    device["mqtt"]["device_id"] = "RouteDEV"
    device["serial_number"] = "SERIAL-1"
    runtime = build_zendure_mqtt_control_runtime(
        _runtime_config(device), service_factory=_FakeService
    )
    assert runtime.rejected == []
    (client,) = runtime.devices
    assert client._device_id == "RouteDEV"
    # The physical serial is carried only as physical-serial metadata.
    assert client.physical_serial == "SERIAL-1"
    assert client._device_id != client.physical_serial


def test_runtime_rejects_custom_without_write_topic_no_serial_fallback():
    from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

    device = {
        "type": ZENDURE_MQTT_TYPE,
        "name": "Custom",
        "serial_number": "SERIAL-1",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": FAMILY_LEGACY_JSON,
            "write_protocol": "custom_properties_write",
        },
        "capabilities": {"write_output_limit": True},
    }
    runtime = build_zendure_mqtt_control_runtime(
        _runtime_config(device), service_factory=_FakeService
    )
    assert runtime.devices == []
    assert runtime.rejected


# --- Subscription coverage --------------------------------------------------


def test_cloud_subscriptions_preserve_case_sensitive_route():
    device = _control_device()
    device["mqtt"]["broker_ref"] = "cloud"
    device["mqtt"]["product_key"] = "Pk-Mixed"
    device["mqtt"]["device_id"] = "Dev-Mixed"
    assert zendure_cloud_device_subscriptions([device], "cloud") == (
        "/Pk-Mixed/Dev-Mixed/#",
        "iot/Pk-Mixed/Dev-Mixed/#",
    )


# --- Migration coverage -----------------------------------------------------


def test_migration_complete_canonical_route_remains_safe():
    from ems.zendure_mqtt.migration import plan_zendure_mqtt_migration

    assert plan_zendure_mqtt_migration({"devices": [_control_device()]}) == []


def test_migration_missing_product_key_disables_control():
    from ems.zendure_mqtt.migration import plan_zendure_mqtt_migration

    device = _control_device()
    device["mqtt"].pop("product_key", None)
    changes = plan_zendure_mqtt_migration({"devices": [device]})
    assert [c.action for c in changes] == ["disable_control"]
    assert changes[0].code == "zendure_mqtt_control_disabled_unaddressable"


def _custom_control_device(**mqtt_over):
    mqtt = {
        "broker_ref": "local_a",
        "topic_family": FAMILY_LEGACY_JSON,
        "device_id": "DEV",
        "write_protocol": "custom_properties_write",
        "write_topic": "iot/PK-A/DEV/properties/write",
    }
    mqtt.update(mqtt_over)
    return {
        "type": ZENDURE_MQTT_TYPE,
        "name": "Custom",
        "serial_number": "SERIAL-1",
        "mqtt": mqtt,
        "capabilities": {"write_output_limit": True},
    }


def test_migration_custom_route_complete_remains_safe():
    from ems.zendure_mqtt.migration import plan_zendure_mqtt_migration

    assert plan_zendure_mqtt_migration({"devices": [_custom_control_device()]}) == []


def test_migration_custom_without_route_device_id_disables_control():
    from ems.zendure_mqtt.migration import plan_zendure_mqtt_migration

    device = _custom_control_device()
    device["mqtt"].pop("device_id", None)
    changes = plan_zendure_mqtt_migration({"devices": [device]})
    assert [c.action for c in changes] == ["disable_control"]
    assert changes[0].code == "zendure_mqtt_control_disabled_unaddressable"


# --- Builder coverage -------------------------------------------------------


def test_profile_legacy_write_without_device_id_returns_none():
    from ems.zendure_mqtt.write_protocols import (
        PROTOCOL_LEGACY_PROPERTIES_WRITE,
    )

    message = build_output_limit_message(
        PROTOCOL_LEGACY_PROPERTIES_WRITE,
        topic_family=FAMILY_LEGACY_JSON,
        product_key="PK-A",
        device_id=None,
        output_limit_w=300,
    )
    assert message is None


def test_builder_complete_route_preserves_case():
    import json

    message = build_output_limit_message(
        "legacy_properties_write",
        topic_family=FAMILY_LEGACY_JSON,
        product_key="Pk-A",
        device_id="Dev-1",
        output_limit_w=300,
    )
    assert message is not None
    assert message.topic == "iot/Pk-A/Dev-1/properties/write"
    assert json.loads(message.payload)["deviceId"] == "Dev-1"
