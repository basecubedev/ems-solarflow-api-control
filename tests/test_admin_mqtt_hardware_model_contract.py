# SPDX-License-Identifier: AGPL-3.0-or-later
"""Normalized Admin DTO contracts for concrete Zendure MQTT hardware models."""

import copy

import pytest

from admin.setup_config import build_setup_catalog
from admin.zendure_mqtt_config_draft import (
    apply_zendure_mqtt_draft_fields,
    normalize_zendure_mqtt_draft,
    zendure_mqtt_device_draft,
)

pytestmark = pytest.mark.simulation


def _controlled_device(model="hyper_2000"):
    return {
        "type": "zendure_mqtt",
        "name": "Battery",
        "serial_number": "SERIAL-1",
        "hardware_profile": model,
        "power_write_profile": "legacy_object_device_automation",
        "mqtt": {
            "broker_ref": "local-a",
            "source": "local_mqtt",
            "topic_family": "legacy_zendure_json",
            "base_topic": "iot",
            "device_id": "SERIAL-1",
            "product_key": "PRODUCT-1",
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": True,
        },
    }


def test_old_overloaded_draft_normalizes_to_distinct_fields():
    normalized = normalize_zendure_mqtt_draft(
        {
            "hardware_profile": "hub_hyper_legacy",
            "power_hardware_profile": "hyper_2000",
            "power_write_profile": "forged_browser_value",
        }
    )

    assert normalized["hardware_generation"] == "hub_hyper_legacy"
    assert normalized["hardware_model"] == "hyper_2000"
    assert normalized["power_write_profile"] == "legacy_object_device_automation"
    assert "hardware_profile" not in normalized
    assert "power_hardware_profile" not in normalized


def test_setup_and_maintenance_catalogs_export_registry_models(tmp_path):
    setup_models = build_setup_catalog()["zendure_mqtt_hardware_models"]
    by_id = {entry["id"]: entry for entry in setup_models}

    exact = by_id["solarflow_800_pro_2"]
    assert {
        "id": "solarflow_800_pro_2",
        "label": "SolarFlow 800 Pro 2",
        "generation": "solarflow_zensdk",
        "control_supported": True,
        "supported_operations": ["discharge", "idle"],
        "power_write_profile": "zensdk_properties_write",
        "validation_maturity": "existing_support",
    }.items() <= exact.items()
    assert by_id[""]["control_supported"] is False
    assert "telemetry only" in by_id[""]["label"].lower()


def test_config_emits_normalized_maintenance_draft_and_roundtrips():
    config = _controlled_device()
    draft = zendure_mqtt_device_draft(config)

    assert draft["hardware_generation"] == "hub_hyper_legacy"
    assert draft["hardware_model"] == "hyper_2000"
    assert draft["power_write_profile"] == "legacy_object_device_automation"
    assert "hardware_profile" not in draft
    assert "power_hardware_profile" not in draft

    reloaded = copy.deepcopy(config)
    apply_zendure_mqtt_draft_fields(reloaded, draft)
    assert reloaded == config


def test_normalized_dto_writes_canonical_core_config_fields():
    device = {}
    apply_zendure_mqtt_draft_fields(
        device,
        {
            "name": "SolarFlow",
            "serial_number": "SERIAL-2",
            "hardware_generation": "solarflow_zensdk",
            "hardware_model": "solarflow_800_pro_2",
            "power_write_profile": "forged_browser_value",
            "product_key": "PRODUCT-2",
            "output_control": True,
            "mqtt": {
                "broker_ref": "local-a",
                "source": "local_mqtt",
                "topic_family": "legacy_zendure_json_alt",
                "device_id": "SERIAL-2",
                "product_key": "PRODUCT-2",
            },
        },
    )

    assert device["hardware_profile"] == "solarflow_800_pro_2"
    assert device["power_write_profile"] == "zensdk_properties_write"
    assert device["capabilities"]["write_output_limit"] is True


def test_removing_model_from_controlled_device_removes_control_authority():
    device = _controlled_device()
    draft = zendure_mqtt_device_draft(device)
    draft["hardware_model"] = ""
    draft["output_control"] = True
    draft["capabilities"]["write_output_limit"] = True

    apply_zendure_mqtt_draft_fields(device, draft)

    assert "hardware_profile" not in device
    assert "power_write_profile" not in device
    assert device["capabilities"]["write_output_limit"] is False


def test_generation_change_clears_an_incompatible_model():
    device = _controlled_device()
    draft = zendure_mqtt_device_draft(device)
    draft["hardware_generation"] = "solarflow_zensdk"

    apply_zendure_mqtt_draft_fields(device, draft)

    assert "hardware_profile" not in device
    assert "power_write_profile" not in device
    assert device["capabilities"]["write_output_limit"] is False


def test_model_change_rederives_a_different_write_profile():
    device = _controlled_device()
    draft = zendure_mqtt_device_draft(device)
    draft["hardware_model"] = "hub_2000"

    apply_zendure_mqtt_draft_fields(device, draft)

    assert device["hardware_profile"] == "hub_2000"
    assert device["power_write_profile"] == "legacy_hub_device_automation"
    assert device["capabilities"]["write_output_limit"] is True


@pytest.mark.parametrize("model", ["unknown", "not_a_model"])
def test_unknown_model_is_telemetry_only(model):
    device = _controlled_device()
    draft = zendure_mqtt_device_draft(device)
    draft["hardware_model"] = model
    draft["output_control"] = True

    apply_zendure_mqtt_draft_fields(device, draft)

    assert "hardware_profile" not in device
    assert "power_write_profile" not in device
    assert device["capabilities"]["write_output_limit"] is False
