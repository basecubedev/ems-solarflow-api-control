# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance/setup hardware-catalog parity contract.

The Maintenance "Configuration & hardware" editor renders the same hardware
metadata (device fields, grid-meter variants) as the fresh-install Config step.
Both flows must derive that metadata from the central catalog in
``ems.config_catalog`` — never from flow-local copies — so a new device field
or grid-meter variant lands in both UIs at once.
"""

import json

import pytest

from admin.maintenance_config import load_maintenance_config
from admin.setup_config import build_setup_catalog
from ems.config_catalog import get_config_feature_field_index

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _maintenance_catalog(tmp_path):
    _write_config(
        tmp_path,
        {
            "system": {"max_total_power": 1600},
            "devices": [
                {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800},
            ],
            "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
        },
    )
    result = load_maintenance_config(base_dir=str(tmp_path))
    assert result["status"] == "ok"
    return result["catalog"]


def test_maintenance_grid_meter_variants_match_setup_catalog(tmp_path):
    """Both flows offer the identical grid-meter variant map (ids + fields)."""

    catalog = _maintenance_catalog(tmp_path)
    setup_variants = build_setup_catalog()["grid_meter_variants"]
    assert catalog["grid_meter_variants"] == setup_variants
    # The legacy maintenance-only type list is gone; the shared variant map is
    # the single source for meter types and their visible fields.
    assert "grid_meter_types" not in catalog
    # The variant map is the field-visibility contract the UI switches on.
    assert "tasmota_http" in catalog["grid_meter_variants"]
    assert (
        "grid_meter.power_path"
        in catalog["grid_meter_variants"]["tasmota_http"]["fields"]
    )
    assert "ha" not in catalog["grid_meter_variants"], "deprecated variants stay out"


def test_maintenance_hardware_sections_come_from_central_catalog(tmp_path):
    """grid_meter + devices sections carry the central catalog field metadata."""

    catalog = _maintenance_catalog(tmp_path)
    sections = {section["id"]: section for section in catalog["hardware_sections"]}
    assert set(sections) == {"grid_meter", "devices"}

    index = get_config_feature_field_index()
    for section in sections.values():
        assert section["fields"], section["id"]
        for field in section["fields"]:
            reference = index[field["path"]]
            for key in ("label", "description", "type", "level"):
                assert field[key] == reference[key], field["path"]
            for key in ("unit", "options"):
                if key in reference:
                    assert field[key] == reference[key], field["path"]

    device_paths = {field["path"] for field in sections["devices"]["fields"]}
    # The full device field set (not a hand-picked numeric subset) is exposed.
    assert {
        "devices[].name",
        "devices[].ip",
        "devices[].sn",
        "devices[].max_power",
        "devices[].min_soc",
        "devices[].max_soc",
        "devices[].pv_kwp",
        "devices[].battery_kwh",
        "devices[].pv_priority_factor",
        "devices[].smart_mode",
    } <= device_paths


def test_maintenance_hardware_sections_mark_secrets_and_carry_no_values(tmp_path):
    """Secret fields (e.g. grid_meter.mqtt.password) are flagged and value-free."""

    catalog = _maintenance_catalog(tmp_path)
    sections = {section["id"]: section for section in catalog["hardware_sections"]}
    grid_fields = {field["path"]: field for field in sections["grid_meter"]["fields"]}
    password = grid_fields["grid_meter.mqtt.password"]
    assert password["secret"] is True
    assert "default" not in password
    for section in sections.values():
        for field in section["fields"]:
            assert "secret" in field
            if field["secret"]:
                assert "default" not in field, field["path"]


def test_feature_sections_stay_hardware_free(tmp_path):
    """The generic feature editor never gains write access to hardware paths."""

    catalog = _maintenance_catalog(tmp_path)
    for section in catalog["feature_sections"]:
        assert section.get("setup_group") != "hardware"
        for field in section["fields"]:
            assert not field["path"].startswith("grid_meter"), field["path"]
            assert not field["path"].startswith("devices"), field["path"]


def test_maintenance_catalog_is_json_serializable(tmp_path):
    catalog = _maintenance_catalog(tmp_path)
    json.dumps(catalog)


def test_setup_catalog_variants_still_built_from_shared_helper():
    """Setup keeps working off the same variant builder (no re-duplication)."""

    variants = build_setup_catalog()["grid_meter_variants"]
    assert set(variants) >= {"shelly", "tasmota_http", "mqtt", "zendure_smartmeter_d0"}
    for variant in variants.values():
        assert {"id", "label", "description", "fields", "level"} <= set(variant)
