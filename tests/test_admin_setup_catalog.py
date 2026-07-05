# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup-facing config catalog view and feature application."""

import json

import pytest

from admin.setup_config import apply_setup_features, build_setup_catalog

pytestmark = pytest.mark.simulation


def _sections():
    return {section["id"]: section for section in build_setup_catalog()["sections"]}


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

    assert {"shelly", "shelly_3em_gen1", "ecotracker", "tasmota_http", "zendure_smartmeter_d0", "mqtt"} == set(
        variants
    )
    assert variants["mqtt"]["fields"]
    assert "grid_meter.mqtt.host" in variants["mqtt"]["fields"]


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


def test_apply_setup_features_accepts_empty_or_missing():
    assert apply_setup_features({}, None) == []
    assert apply_setup_features({}, {}) == []
