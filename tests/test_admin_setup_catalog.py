# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup-facing config catalog view and feature application."""

import json

import pytest

from admin.setup_config import (
    _setup_field_index,
    apply_device_config_values,
    apply_setup_features,
    build_setup_catalog,
)
from ems.config_catalog import get_config_feature_field_index

pytestmark = pytest.mark.simulation


def _sections():
    return {section["id"]: section for section in build_setup_catalog()["sections"]}


def test_catalog_exposes_top_level_setup_groups_in_order():
    catalog = build_setup_catalog()

    assert [group["id"] for group in catalog["groups"]] == [
        "hardware",
        "features",
        "advanced",
    ]
    for group in catalog["groups"]:
        assert group["title"].strip()


def test_hardware_group_contains_grid_meter_and_devices():
    sections = _sections()

    assert sections["grid_meter"]["setup_group"] == "hardware"
    assert sections["grid_meter"]["kind"] == "hardware"
    assert sections["devices"]["setup_group"] == "hardware"
    assert sections["devices"]["kind"] == "hardware"


def test_optional_features_stay_in_the_features_group():
    sections = _sections()

    for feature in (
        "winter",
        "battery_full_charge_assist",
        "energy_savings",
        "dashboard",
        "influxdb",
    ):
        assert sections[feature]["setup_group"] == "features"
        assert sections[feature]["kind"] == "feature"


def test_system_section_is_grouped_as_advanced():
    assert _sections()["system"]["setup_group"] == "advanced"


def test_device_output_limit_is_configured_per_device():
    fields = get_config_feature_field_index()

    device_limit = fields["devices[].max_power"]
    assert device_limit["label"] == "Device output limit"

    global_cap = fields["system.max_device_power"]
    # The global cap must not masquerade as the normal per-device output setting.
    assert global_cap["label"] == "Global per-device cap"
    assert global_cap["label"] != "Per-device output limit"
    assert global_cap["level"] in ("advanced", "expert")
    assert global_cap["level"] != "normal"


def test_catalog_reports_setup_mode_and_expected_section_order():
    catalog = build_setup_catalog()

    assert catalog["mode"] == "setup"
    assert [section["id"] for section in catalog["sections"]] == [
        "system",
        "grid_meter",
        "devices",
        "winter",
        "battery_full_charge_assist",
        "energy_savings",
        "dashboard",
        "influxdb",
    ]


def test_sections_carry_title_description_level_and_fields():
    for section in build_setup_catalog()["sections"]:
        assert section["title"].strip()
        assert section["description"].strip()
        assert section["level"]
        assert isinstance(section["fields"], list) and section["fields"]


def test_home_assistant_is_not_a_recommended_setup_section():
    catalog = build_setup_catalog()
    ids = {section["id"] for section in catalog["sections"]}

    # Legacy HA control and the maintenance-only config upgrade must not surface
    # as normal setup features.
    assert "ha" not in ids
    assert "config_upgrade" not in ids
    assert "ha" not in catalog["grid_meter_variants"]


def test_winter_metadata_lists_expected_normal_and_advanced_fields():
    winter = _sections()["winter"]
    by_level = {}
    for field in winter["fields"]:
        by_level.setdefault(field["level"], set()).add(field["path"])

    assert winter["enabled_path"] == "winter.enabled"
    assert {"winter.months", "winter.summer_min_soc", "winter.winter_min_soc"} <= by_level["normal"]
    assert {"winter.ramp_step_percent", "winter.adjust_hour", "winter.ac_charge_power"} <= by_level[
        "advanced"
    ]


def test_grid_meter_variants_are_exposed_for_setup():
    catalog = build_setup_catalog()
    variants = catalog["grid_meter_variants"]

    assert {
        "shelly",
        "shelly_3em_gen1",
        "ecotracker",
        "zendure_smartmeter_3ct_http",
        "tasmota_http",
        "zendure_smartmeter_d0",
        "mqtt",
    } == set(variants)
    assert variants["mqtt"]["fields"]
    assert "grid_meter.mqtt.host" in variants["mqtt"]["fields"]
    assert list(variants["zendure_smartmeter_3ct_http"]["fields"]) == ["grid_meter.ip"]


def test_manual_hardware_variants_are_role_specific():
    variants = build_setup_catalog()["hardware_variants"]

    assert [item["id"] for item in variants["inverter"]] == ["zendure_local_api"]
    assert variants["inverter"][0]["default_port"] == 80
    assert variants["inverter"][0]["default"] is True
    assert variants["inverter"][0]["required_fields"] == ["host", "port", "serial"]
    assert {item["id"] for item in variants["grid_meter"]} == {
        "shelly",
        "shelly_3em_gen1",
        "ecotracker",
        "zendure_smartmeter_3ct_http",
        "tasmota_http",
    }
    assert not ({item["id"] for item in variants["inverter"]} & {
        item["id"] for item in variants["grid_meter"]
    })
    assert next(item for item in variants["grid_meter"] if item["default"])["id"] == "shelly"


def test_secret_fields_are_marked_and_never_carry_a_value():
    fields = {
        field["path"]: field
        for section in build_setup_catalog()["sections"]
        for field in section["fields"]
    }

    password = fields["grid_meter.mqtt.password"]
    assert password["secret"] is True
    assert "default" not in password

    token = fields["influxdb.token"]
    assert token["secret"] is True
    assert "default" not in token


def test_catalog_payload_is_json_serializable():
    json.dumps(build_setup_catalog(), allow_nan=False)


def test_apply_setup_features_coerces_and_sets_known_paths():
    config = {"winter": {"enabled": False}, "dashboard": {"port": 8080}}
    applied = apply_setup_features(
        config,
        {
            "winter.enabled": "true",
            "winter.months": "10, 11, 12",
            "winter.winter_min_soc": "40",
            "dashboard.port": "9090",
            "unknown.path": 1,
        },
    )

    assert set(applied) == {
        "winter.enabled",
        "winter.months",
        "winter.winter_min_soc",
        "dashboard.port",
    }
    assert config["winter"]["enabled"] is True
    assert config["winter"]["months"] == [10, 11, 12]
    assert config["winter"]["winter_min_soc"] == 40
    assert config["dashboard"]["port"] == 9090


def test_apply_setup_features_creates_nested_grid_meter_mqtt():
    config = {"grid_meter": {"type": "shelly", "ip": "192.0.2.1"}}
    apply_setup_features(
        config,
        {"grid_meter.type": "mqtt", "grid_meter.mqtt.host": "192.0.2.9"},
    )

    assert config["grid_meter"]["type"] == "mqtt"
    assert config["grid_meter"]["mqtt"]["host"] == "192.0.2.9"


def test_apply_setup_features_ignores_hidden_and_device_paths():
    config = {}
    applied = apply_setup_features(
        config,
        {
            "config_schema_version": 99,
            "dashboard.database_path": "/tmp/evil.sqlite",
            "devices[].max_power": 1200,
        },
    )

    assert applied == []
    assert config == {}


def test_apply_setup_features_ignores_maintenance_only_ha_field():
    config = {"ha": {"enabled": False}}
    applied = apply_setup_features(config, {"ha.enabled": True})

    assert applied == []
    assert config == {"ha": {"enabled": False}}


def test_apply_setup_features_ignores_config_upgrade_field():
    config = {"config_upgrade": {"on_startup": "check"}}
    applied = apply_setup_features(config, {"config_upgrade.on_startup": "apply"})

    assert applied == []
    assert config == {"config_upgrade": {"on_startup": "check"}}


def test_apply_setup_features_applies_normal_setup_fields():
    config = {"winter": {"enabled": False}, "grid_meter": {"type": "shelly"}}
    applied = apply_setup_features(
        config,
        {"winter.enabled": "true", "grid_meter.type": "ecotracker"},
    )

    assert set(applied) == {"winter.enabled", "grid_meter.type"}
    assert config["winter"]["enabled"] is True
    assert config["grid_meter"]["type"] == "ecotracker"


def test_setup_field_index_excludes_maintenance_and_deprecated_fields():
    index = _setup_field_index()

    assert "ha.enabled" not in index
    assert "ha.control_enabled" not in index
    assert "config_upgrade.on_startup" not in index
    assert "winter.enabled" in index
    assert "grid_meter.type" in index


def test_apply_setup_features_accepts_empty_or_missing():
    assert apply_setup_features({}, None) == []


def test_apply_device_config_values_coerces_known_device_fields():
    device = {"name": "WR1", "ip": "192.0.2.1", "sn": "SN1"}
    applied = apply_device_config_values(
        device,
        {"max_power": "800", "min_soc": "15", "pv_kwp": "1.5"},
    )

    assert set(applied) == {"max_power", "min_soc", "pv_kwp"}
    assert device["max_power"] == 800
    assert device["min_soc"] == 15
    assert device["pv_kwp"] == 1.5


def test_apply_device_config_values_ignores_unknown_and_identity_keys():
    device = {"name": "WR1", "ip": "192.0.2.1", "sn": "SN1"}
    applied = apply_device_config_values(
        device,
        {
            "name": "evil",
            "ip": "10.0.0.1",
            "sn": "hacked",
            "bogus_key": 1,
            "system.max_total_power": 99,
        },
    )

    # Identity fields and unknown/non-device paths are never applied.
    assert applied == []
    assert device == {"name": "WR1", "ip": "192.0.2.1", "sn": "SN1"}


def test_apply_device_config_values_accepts_empty_or_missing():
    assert apply_device_config_values({}, None) == []
    assert apply_device_config_values({}, {}) == []
    assert apply_device_config_values(None, {"max_power": 1}) == []
    assert apply_setup_features({}, {}) == []
