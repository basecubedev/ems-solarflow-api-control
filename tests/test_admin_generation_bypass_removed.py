# SPDX-License-Identifier: AGPL-3.0-or-later
"""A coarse hardware generation can never authorize an MQTT output write.

The removed Admin bypass: selecting a legacy generation (``hub_hyper_legacy``)
and requesting output control synthesized a built-in ``legacy_properties_write``
method, so the device became controllable without any concrete hardware model.
A generation only determines telemetry schema / topic family; control now
requires a concrete, registry-resolved hardware model. Without one the device is
added telemetry-only with an actionable "select the exact model" issue.
"""

import pytest

from admin.zendure_mqtt_config_draft import (
    apply_zendure_mqtt_draft_fields,
    build_manual_zendure_mqtt_fragment,
)

pytestmark = pytest.mark.simulation


def test_admin_generation_cannot_authorize_write():
    fragment, issues = build_manual_zendure_mqtt_fragment(
        {
            "name": "Hyper",
            "generation": "hub_hyper_legacy",
            "serial_number": "SN1",
            "product_key": "PK1",
            "output_control": True,
        },
        "local_a",
    )
    # Telemetry config may still be created, but never a control write.
    assert fragment is not None
    assert fragment["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in fragment["mqtt"]
    assert issues
    assert any(
        i["code"] == "zendure_mqtt_control_requires_model" for i in issues
    )


def test_admin_new_device_projection_generation_cannot_authorize_write():
    device = {}
    apply_zendure_mqtt_draft_fields(
        device,
        {
            "name": "Hyper",
            "serial_number": "SN9",
            "hardware_profile": "hub_hyper_legacy",  # a GENERATION id, not a model
            "output_control": True,
            "product_key": "PK9",
            "mqtt": {
                "broker_ref": "local_a",
                "topic_family": "legacy_zendure_json",
                "device_id": "SN9",
                "product_key": "PK9",
            },
        },
    )
    assert device["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in device["mqtt"]


def test_admin_concrete_model_authorizes_write():
    # With a concrete registry model the same device becomes controllable — and
    # pins the model, never a bare write_protocol.
    fragment, issues = build_manual_zendure_mqtt_fragment(
        {
            "name": "Hyper",
            "generation": "hub_hyper_legacy",
            "power_hardware_profile": "hyper_2000",
            "serial_number": "SN1",
            "product_key": "PK1",
            "output_control": True,
        },
        "local_a",
    )
    assert issues == []
    assert fragment["capabilities"]["write_output_limit"] is True
    assert fragment["hardware_profile"] == "hyper_2000"
    assert "write_protocol" not in fragment["mqtt"]


def test_clearing_profile_removes_stale_write_metadata():
    # An existing writable device whose model is cleared (set to a telemetry-only
    # model) must drop its stale write capability and write metadata.
    device = {
        "type": "zendure_mqtt",
        "name": "Hyper",
        "enabled": True,
        "serial_number": "SN1",
        "hardware_profile": "hyper_2000",
        "power_write_profile": "legacy_object_device_automation",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": "legacy_zendure_json",
            "base_topic": "iot",
            "device_id": "SN1",
            "product_key": "PK1",
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
    }
    apply_zendure_mqtt_draft_fields(
        device,
        {
            "name": "Hyper",
            "original_name": "Hyper",
            "serial_number": "SN1",
            "hardware_profile": "hub_hyper_legacy",
            "power_hardware_profile": "ace_1500",  # telemetry-only model
            "output_control": True,
            "product_key": "PK1",
            "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
            "mqtt": {
                "broker_ref": "local_a",
                "topic_family": "legacy_zendure_json",
                "device_id": "SN1",
                "product_key": "PK1",
            },
        },
    )
    assert device["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in device["mqtt"]
    assert device.get("power_write_profile") in (None, "telemetry_only")
