# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from pathlib import Path
import subprocess
import sys

from ems.config import BATTERY_FULL_CHARGE_ASSIST_DEFAULTS
from ems.config_catalog import (
    build_default_template,
    get_config_catalog,
    get_config_feature_field_index,
    render_default_template,
)

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
        "zendure_smartmeter_3ct_http",
        "tasmota_http",
        "zendure_smartmeter_d0",
        "mqtt",
        "ha",
    }
    assert variants["ha"]["level"] == "deprecated"
    assert variants["zendure_smartmeter_3ct_http"]["fields"] == ("grid_meter.ip",)


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
    assert [device["name"] for device in devices] == ["WR1", "WR2", "WR3"]
    assert [device["ip"] for device in devices] == [
        "192.168.1.100",
        "192.168.1.101",
        "192.168.1.102",
    ]


def test_control_tuning_is_expert_and_ha_is_deprecated():
    fields = get_config_feature_field_index()
    tuning = [
        field
        for path, field in fields.items()
        if path.startswith("system.output_control.")
    ]
    assert tuning
    assert all(field["level"] == "expert" for field in tuning)
    assert all(field["risk"] == "control_stability" for field in tuning)
    assert fields["ha.enabled"]["level"] == "deprecated"


def test_template_preserves_documented_assist_power_default_mismatch():
    template_default = get_config_feature_field_index()[
        "battery_full_charge_assist.ac_charge_power"
    ]["default"]
    assert template_default == 600
    assert BATTERY_FULL_CHARGE_ASSIST_DEFAULTS["ac_charge_power"] == 200
