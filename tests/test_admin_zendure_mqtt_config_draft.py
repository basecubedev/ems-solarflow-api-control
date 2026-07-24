# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Zendure MQTT config-draft contract: first-class control support.

Manual entry and the maintenance new-device projection build a *controllable*
device for a supported hardware generation when output control is selected, and
stay telemetry-only for unsupported generations or when control is not selected.
Capability is decided by the shared EMS helper (topic family + write method),
never by trusting a browser flag on an unsupported family.
"""

import pytest

from admin.zendure_mqtt_config_draft import (
    apply_zendure_mqtt_draft_fields,
    build_manual_zendure_mqtt_fragment,
)

pytestmark = pytest.mark.simulation


# --- manual entry -----------------------------------------------------------


def test_manual_supported_model_control_selected_enables_write():
    # A concrete registry model (not the generation) authorizes control.
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
    assert fragment["mqtt"]["product_key"] == "PK1"


def test_manual_supported_generation_telemetry_only_when_control_not_selected():
    fragment, issues = build_manual_zendure_mqtt_fragment(
        {
            "name": "Hyper",
            "generation": "hub_hyper_legacy",
            "serial_number": "SN1",
            "product_key": "PK1",
            "output_control": False,
        },
        "local_a",
    )
    assert issues == []
    assert fragment["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in fragment["mqtt"]


def test_manual_default_is_telemetry_only():
    fragment, issues = build_manual_zendure_mqtt_fragment(
        {"name": "Hyper", "generation": "hub_hyper_legacy",
         "serial_number": "SN1", "product_key": "PK1"},
        "local_a",
    )
    assert issues == []
    assert fragment["capabilities"]["write_output_limit"] is False


def test_manual_unsupported_generation_control_request_falls_back_to_telemetry():
    # A scalar generation has no verified write method: an output-control request
    # cannot enable writes, and the device is added telemetry-only.
    fragment, issues = build_manual_zendure_mqtt_fragment(
        {
            "name": "SF800",
            "generation": "solarflow_zensdk",
            "serial_number": "SN2",
            "output_control": True,
        },
        "local_a",
    )
    assert fragment is not None
    assert fragment["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in fragment["mqtt"]
    # The user is told why control is unavailable rather than silently getting it.
    assert any("control" in issue["message"].lower() for issue in issues)


def test_manual_control_requires_write_target():
    # A legacy control device must be addressable (product_key) to derive its
    # write topic; requesting control without one is a clear, actionable error.
    fragment, issues = build_manual_zendure_mqtt_fragment(
        {
            "name": "Hyper",
            "generation": "hub_hyper_legacy",
            "power_hardware_profile": "hyper_2000",
            "serial_number": "SN3",
            "output_control": True,
        },
        "local_a",
    )
    assert fragment is None
    assert issues
    assert any("product" in issue["message"].lower() for issue in issues)


# --- maintenance new-device projection --------------------------------------


def test_new_device_projection_enables_control_for_concrete_model():
    device = {}
    apply_zendure_mqtt_draft_fields(
        device,
        {
            "name": "Hyper",
            "serial_number": "SN9",
            "hardware_profile": "hub_hyper_legacy",
            "power_hardware_profile": "hyper_2000",
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
    assert device["capabilities"]["write_output_limit"] is True
    assert device["hardware_profile"] == "hyper_2000"
    assert "write_protocol" not in device["mqtt"]


def test_new_device_projection_stays_telemetry_only_without_control():
    device = {}
    apply_zendure_mqtt_draft_fields(
        device,
        {
            "name": "Hyper",
            "serial_number": "SN9",
            "hardware_profile": "hub_hyper_legacy",
            "mqtt": {
                "broker_ref": "local_a",
                "topic_family": "legacy_zendure_json",
                "device_id": "SN9",
                "product_key": "PK9",
            },
        },
    )
    assert device["capabilities"]["write_output_limit"] is False


def test_new_device_projection_cannot_force_control_on_unsupported_family():
    # A forged control request on a scalar family never yields a control device.
    device = {}
    apply_zendure_mqtt_draft_fields(
        device,
        {
            "name": "SF800",
            "serial_number": "SN9",
            "hardware_profile": "solarflow_zensdk",
            "output_control": True,
            "mqtt": {
                "broker_ref": "local_a",
                "topic_family": "zensdk_ha_scalar",
                "device_id": "SN9",
            },
        },
    )
    assert device["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in device["mqtt"]


# --- maintenance existing-device edit ----------------------------------------


def _existing_supported_device(write_output_limit):
    return {
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
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": write_output_limit,
        },
    }


def _edit_item(device, output_control):
    return {
        "name": device["name"],
        "original_name": device["name"],
        "serial_number": device["serial_number"],
        "hardware_profile": "hub_hyper_legacy",
        "power_hardware_profile": device.get("hardware_profile", ""),
        "product_key": device["mqtt"].get("product_key", ""),
        "output_control": output_control,
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": output_control,
        },
        "mqtt": dict(device["mqtt"]),
    }


def test_existing_device_edit_enables_control_for_supported_family():
    device = _existing_supported_device(False)
    apply_zendure_mqtt_draft_fields(device, _edit_item(device, True))
    assert device["capabilities"]["write_output_limit"] is True
    assert device["hardware_profile"] == "hyper_2000"
    assert "write_protocol" not in device["mqtt"]


def test_existing_device_edit_disables_control():
    device = _existing_supported_device(True)
    apply_zendure_mqtt_draft_fields(device, _edit_item(device, False))
    assert device["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in device["mqtt"]


def test_existing_device_edit_noop_preserves_control_state():
    enabled = _existing_supported_device(True)
    apply_zendure_mqtt_draft_fields(enabled, _edit_item(enabled, True))
    assert enabled["capabilities"]["write_output_limit"] is True
    assert enabled["hardware_profile"] == "hyper_2000"
    assert "write_protocol" not in enabled["mqtt"]

    disabled = _existing_supported_device(False)
    apply_zendure_mqtt_draft_fields(disabled, _edit_item(disabled, False))
    assert disabled["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in disabled["mqtt"]


def test_existing_device_edit_without_control_field_preserves_state():
    # A draft that never mentions output control (no output_control key, no
    # capabilities block) leaves the stored opt-in untouched in both directions.
    for stored in (True, False):
        device = _existing_supported_device(stored)
        item = _edit_item(device, stored)
        del item["output_control"]
        del item["capabilities"]
        apply_zendure_mqtt_draft_fields(device, item)
        assert device["capabilities"]["write_output_limit"] is stored


def test_existing_device_cannot_be_forced_writable_on_unsupported_family():
    device = {
        "type": "zendure_mqtt",
        "name": "SF800",
        "enabled": True,
        "serial_number": "SN2",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": "SN2",
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }
    item = {
        "name": "SF800",
        "original_name": "SF800",
        "serial_number": "SN2",
        "hardware_profile": "solarflow_800_pro_2",
        "output_control": True,
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
        "mqtt": dict(device["mqtt"]),
    }
    apply_zendure_mqtt_draft_fields(device, item)
    assert device["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in device["mqtt"]


def test_existing_alt_layout_device_stays_controllable_with_concrete_model():
    # A device on the leading-slash JSON layout resolves no generation label, but
    # a pinned concrete registry model keeps it controllable — the bare topic
    # layout alone (and a built-in write_protocol) no longer authorizes control.
    from admin.zendure_mqtt_config_draft import zendure_mqtt_device_draft

    device = {
        "type": "zendure_mqtt",
        "name": "Pro2",
        "enabled": True,
        "serial_number": "P2SN",
        "hardware_profile": "solarflow_800_pro_2",
        "power_write_profile": "zensdk_properties_write",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": "legacy_zendure_json_alt",
            "base_topic": None,
            "device_id": "P2DEV",
            "product_key": "PKP2",
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }
    draft = zendure_mqtt_device_draft(device)
    assert draft["supports_output_control"] is True
    assert draft["hardware_generation"] == "solarflow_zensdk"
    assert draft["hardware_model"] == "solarflow_800_pro_2"
    assert draft["alternative_layout"] is True

    draft["output_control"] = True
    draft["capabilities"]["write_output_limit"] = True
    apply_zendure_mqtt_draft_fields(device, draft)
    assert device["capabilities"]["write_output_limit"] is True
    assert device["hardware_profile"] == "solarflow_800_pro_2"
    assert "write_protocol" not in device["mqtt"]
    assert device["mqtt"]["topic_family"] == "legacy_zendure_json_alt"


def test_existing_pro2_toggle_persists_product_key_and_control_intent():
    """Regression: the Maintenance checkbox must not be projected back off."""

    from admin.zendure_mqtt_config_draft import zendure_mqtt_device_draft

    device = {
        "type": "zendure_mqtt",
        "name": "Pro2",
        "enabled": True,
        "serial_number": "P2SN",
        "hardware_profile": "solarflow_800_pro_2",
        "power_write_profile": "zensdk_properties_write",
        "mqtt": {
            "broker_ref": "zendure_cloud",
            "source": "zendure_cloud_mqtt",
            "topic_family": "legacy_zendure_json_alt",
            "base_topic": None,
            "device_id": "P2DEV",
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": False,
        },
    }
    draft = zendure_mqtt_device_draft(device)
    assert draft["control_readiness"] == {
        "ready": False,
        "reason": "write_target_missing",
    }

    draft["product_key"] = "PKP2"
    draft["mqtt"]["product_key"] = "PKP2"
    draft["output_control"] = True
    draft["capabilities"]["write_output_limit"] = True
    apply_zendure_mqtt_draft_fields(device, draft)

    assert device["mqtt"]["product_key"] == "PKP2"
    assert device["capabilities"]["write_output_limit"] is True


def test_supported_toggle_without_write_target_remains_validation_visible():
    device = {}
    apply_zendure_mqtt_draft_fields(
        device,
        {
            "name": "Pro2",
            "serial_number": "P2SN",
            "hardware_generation": "solarflow_zensdk",
            "hardware_model": "solarflow_800_pro_2",
            "output_control": True,
            "mqtt": {
                "broker_ref": "zendure_cloud",
                "source": "zendure_cloud_mqtt",
                "topic_family": "legacy_zendure_json_alt",
                "device_id": "P2DEV",
            },
            "capabilities": {"read_power": True, "read_soc": True},
        },
    )
    assert device["capabilities"]["write_output_limit"] is True
