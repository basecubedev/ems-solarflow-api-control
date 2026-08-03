# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ems.config import BATTERY_FULL_CHARGE_ASSIST_DEFAULTS
from ems.config_catalog import (
    GRID_METER_KNOWN_MQTT_KEYS,
    GRID_METER_KNOWN_TOP_KEYS,
    build_default_template,
    get_config_catalog,
    get_config_feature_field_index,
    grid_meter_variant_field_spec,
    render_default_template,
)

pytestmark = [
    pytest.mark.config,
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config" / "config.template.json"
GENERATOR = ROOT / "tools" / "build_config_template.py"


def _template_paths(value, path=""):
    if isinstance(value, dict):
        result = set()
        for key, child in value.items():
            if key.startswith("_comment"):
                continue
            child_path = f"{path}.{key}" if path else key
            result.update(_template_paths(child, child_path))
        return result
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return _template_paths(value[0], f"{path}[]")
    return {path}


def test_committed_template_matches_catalog_output():
    assert TEMPLATE.read_text(encoding="utf-8") == render_default_template()


def test_generator_check_passes_on_committed_tree():
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_catalog_covers_every_non_comment_template_path():
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    assert _template_paths(template) <= set(get_config_feature_field_index())


def test_catalog_contains_all_supported_grid_meter_variants():
    variants = get_config_catalog()["grid_meter_variants"]
    assert set(variants) == {
        "shelly",
        "shelly_3em_gen1",
        "ecotracker",
        "zendure_grid_meter_http",
        "zendure_smartmeter_3ct_http",
        "zendure_smartmeter_d0_http",
        "tasmota_http",
        "zendure_smartmeter_d0",
        "mqtt",
        "ha",
    }
    assert variants["ha"]["level"] == "deprecated"
    assert variants["zendure_grid_meter_http"]["fields"] == ("grid_meter.ip",)
    assert variants["zendure_smartmeter_3ct_http"]["fields"] == ("grid_meter.ip",)
    assert variants["zendure_smartmeter_d0_http"]["fields"] == ("grid_meter.ip",)


def test_catalog_splits_zendure_d0_by_transport():
    # The D0 model is reachable two ways. Both entries share the same hardware
    # model but differ by connection type, and neither leaks into the other's
    # config surface: the HTTP entry is IP-only, the MQTT entry is broker-only.
    variants = get_config_catalog()["grid_meter_variants"]

    d0_http = variants["zendure_smartmeter_d0_http"]
    d0_mqtt = variants["zendure_smartmeter_d0"]

    # User-facing labels name the model and distinguish the connection path.
    assert "D0" in d0_http["label"]
    assert "Local API" in d0_http["label"]
    assert "D0" in d0_mqtt["label"]
    # The HTTP D0 must never be presented or aliased as a 3CT.
    assert "3CT" not in d0_http["label"]

    # HTTP D0 needs an IP/host; MQTT D0 needs the broker/topic fields.
    assert d0_http["fields"] == ("grid_meter.ip",)
    assert any(field.startswith("grid_meter.mqtt.") for field in d0_mqtt["fields"])
    assert not any(field == "grid_meter.ip" for field in d0_mqtt["fields"])

    # The 3CT HTTP meter keeps its distinct local-API identity.
    assert "3CT" in variants["zendure_smartmeter_3ct_http"]["label"]
    assert "Local API" in variants["zendure_smartmeter_3ct_http"]["label"]


def test_grid_meter_type_sets_are_derived_from_the_catalog():
    # Admin (setup preview + maintenance) must not keep its own hand-maintained
    # grid-meter type lists: both derive from the single catalog source so a new
    # variant becomes selectable/validatable everywhere by editing the catalog.
    from ems.config_catalog import grid_meter_types, http_grid_meter_types
    from admin.config_preview import _GRID_TYPE_CHOICES
    from admin.maintenance_config import _HOST_GRID_METER_TYPES

    assert grid_meter_types() == set(get_config_catalog()["grid_meter_variants"])
    assert _GRID_TYPE_CHOICES == grid_meter_types()
    assert _HOST_GRID_METER_TYPES == http_grid_meter_types()

    http = http_grid_meter_types()
    # Both Zendure local-API meters and the generic HTTP type need an ip/host.
    assert {
        "zendure_smartmeter_3ct_http",
        "zendure_smartmeter_d0_http",
        "zendure_grid_meter_http",
    } <= http
    # MQTT meters and the fieldless deprecated HA meter carry no HTTP endpoint.
    assert not (http & {"zendure_smartmeter_d0", "mqtt", "ha"})


def test_grid_meter_variant_field_spec_zendure_d0_http():
    # The HTTP D0 shares the flat http ip/port key surface (no mqtt block),
    # exactly like the 3CT and generic Zendure local-HTTP meters.
    d0_http = grid_meter_variant_field_spec("zendure_smartmeter_d0_http")
    assert d0_http["keys"] == frozenset({"type", "ip", "port"})
    assert d0_http["mqtt_keys"] == frozenset()
    assert grid_meter_variant_field_spec("zendure_smartmeter_3ct_http")["keys"] == (
        d0_http["keys"]
    )


def test_grid_meter_variant_field_spec_matches_v070_catalog():
    # HTTP variants carry the shared ip/port plus their own fields.
    assert grid_meter_variant_field_spec("zendure_smartmeter_3ct_http")["keys"] == (
        frozenset({"type", "ip", "port"})
    )
    assert grid_meter_variant_field_spec("tasmota_http")["keys"] == frozenset(
        {"type", "ip", "port", "url", "power_path"}
    )
    assert grid_meter_variant_field_spec("shelly")["keys"] == frozenset(
        {"type", "ip", "port", "channels"}
    )
    # MQTT variants carry only the mqtt block; D0 excludes value_path.
    d0 = grid_meter_variant_field_spec("zendure_smartmeter_d0")
    assert d0["keys"] == frozenset({"type", "mqtt"})
    assert "value_path" not in d0["mqtt_keys"]
    assert "topic" in d0["mqtt_keys"]
    assert "value_path" in grid_meter_variant_field_spec("mqtt")["mqtt_keys"]
    # Unknown types are not spec'd.
    assert grid_meter_variant_field_spec("does_not_exist") is None
    # The known-key unions only contain real variant fields.
    assert GRID_METER_KNOWN_TOP_KEYS == frozenset(
        {"ip", "port", "channels", "url", "power_path", "mqtt"}
    )
    assert "value_path" in GRID_METER_KNOWN_MQTT_KEYS


def test_catalog_defines_local_api_inverter_connection():
    variants = get_config_catalog()["inverter_connection_variants"]

    assert set(variants) == {"zendure_local_api"}
    assert variants["zendure_local_api"]["default_port"] == 80
    assert variants["zendure_local_api"]["manual_setup"] is True


def test_default_template_order_and_legacy_ha_defaults():
    template = build_default_template()
    assert list(template) == [
        "_comment",
        "_comment_docs",
        "config_schema_version",
        "config_upgrade",
        "system",
        "grid_meter",
        "_comment_devices",
        "devices",
        "zendure_mqtt",
        "winter",
        "battery_full_charge_assist",
        "energy_savings",
        "dashboard",
        "influxdb",
        "ha",
    ]
    assert template["ha"]["enabled"] is False
    assert template["ha"]["control_enabled"] is False
    assert not (ROOT / "config.template.json").exists()


def test_device_count_is_configurable():
    devices = build_default_template(device_count=3)["devices"]
    assert [device["name"] for device in devices] == ["INV_1", "INV_2", "INV_3"]
    assert [device["ip"] for device in devices] == [
        "192.168.1.100",
        "192.168.1.101",
        "192.168.1.102",
    ]


# The core control-tuning knobs are promoted to primary (normal) so operators
# can tune them without opening Advanced settings; the remaining output-control
# smoothing/bypass fields stay expert. All of them keep control_stability risk.
PROMOTED_CONTROL_FIELDS = {
    "system.max_total_power",
    "system.loop_interval",
    "system.deadband",
    "system.output_control.target_deadband_w",
    "system.output_control.ramp_up_w_per_cycle",
    "system.output_control.ramp_down_w_per_cycle",
    "system.output_control.device_ramp_up_w_per_cycle",
    "system.output_control.device_ramp_down_w_per_cycle",
}


def test_control_tuning_is_expert_and_ha_is_deprecated():
    fields = get_config_feature_field_index()
    tuning = {
        path: field
        for path, field in fields.items()
        if path.startswith("system.output_control.")
    }
    assert tuning
    promoted_oc = PROMOTED_CONTROL_FIELDS & set(tuning)
    assert promoted_oc
    # Promoted output-control fields are primary; the rest stay expert.
    assert all(tuning[path]["level"] == "normal" for path in promoted_oc)
    assert all(
        field["level"] == "expert"
        for path, field in tuning.items()
        if path not in PROMOTED_CONTROL_FIELDS
    )
    # Every output-control tuning knob keeps its control-stability risk.
    assert all(field["risk"] == "control_stability" for field in tuning.values())
    assert fields["ha.enabled"]["level"] == "deprecated"


def test_template_preserves_documented_assist_power_default_mismatch():
    template_default = get_config_feature_field_index()[
        "battery_full_charge_assist.ac_charge_power"
    ]["default"]
    assert template_default == 600
    assert BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["ac_charge_power"] == 200
