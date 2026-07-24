# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from pathlib import Path

import pytest

from admin.config_feature_metadata import (
    LEVELS,
    RISKS,
    SCOPES,
    get_config_feature_field_index,
    get_config_feature_sections,
)


TEMPLATE_PATH = Path(__file__).parents[1] / "config" / "config.template.json"
SECTION_KEYS = {
    "title",
    "summary",
    "description",
    "level",
    "scope",
    "order",
}
FIELD_KEYS = {
    "path",
    "label",
    "description",
    "type",
    "level",
    "scope",
    "required",
    "restart_required",
    "backup_recommended",
    "risk",
}


def _template_field_paths(value, path=""):
    if isinstance(value, dict):
        result = set()
        for key, child in value.items():
            if key.startswith("_comment"):
                continue
            child_path = f"{path}.{key}" if path else key
            result.update(_template_field_paths(child, child_path))
        return result
    if isinstance(value, list) and value and all(
        isinstance(item, dict) for item in value
    ):
        return _template_field_paths(value[0], f"{path}[]")
    return {path}


@pytest.fixture(scope="module")
def template():
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def test_metadata_covers_every_non_comment_template_field(template):
    assert _template_field_paths(template) <= set(get_config_feature_field_index())


def test_every_metadata_path_exists_in_canonical_template(template):
    template_paths = _template_field_paths(template)

    template_fields = {
        path
        for path, field in get_config_feature_field_index().items()
        if field.get("template") and not path.startswith("_comment")
    }
    assert template_fields == template_paths


def test_metadata_is_json_serializable():
    json.dumps(get_config_feature_sections(), allow_nan=False)
    json.dumps(get_config_feature_field_index(), allow_nan=False)


def test_sections_have_stable_required_metadata():
    sections = get_config_feature_sections()

    assert [section["id"] for section in sections] == [
        "config_upgrade",
        "system",
        "grid_meter",
        "devices",
        "zendure_mqtt",
        "winter",
        "battery_full_charge_assist",
        "energy_savings",
        "dashboard",
        "influxdb",
        "ha",
    ]
    assert [section["order"] for section in sections] == list(range(1, 12))
    for section in sections:
        assert SECTION_KEYS <= section.keys()
        assert section["level"] in LEVELS
        assert section["scope"] in SCOPES
        assert isinstance(section["collapsible"], bool)


def test_zendure_mqtt_feature_has_no_enable_toggle():
    # The telemetry feature is always on: the section exposes no enable toggle
    # and no enabled_path, and the config key does not exist anymore.
    sections = {section["id"]: section for section in get_config_feature_sections()}
    assert sections["zendure_mqtt"].get("enabled_path") is None
    assert "zendure_mqtt.enabled" not in get_config_feature_field_index()


def test_fields_have_stable_required_metadata():
    for path, field in get_config_feature_field_index().items():
        assert FIELD_KEYS <= field.keys(), path
        assert field["path"] == path
        assert field["level"] in LEVELS
        assert field["scope"] in SCOPES
        assert field["risk"] in RISKS
        assert isinstance(field["required"], bool)
        assert isinstance(field["restart_required"], bool)
        assert isinstance(field["backup_recommended"], bool)


def test_nested_groups_describe_expert_and_storage_settings():
    sections = {
        section["id"]: section for section in get_config_feature_sections()
    }
    groups = {
        group["id"]: group
        for section in sections.values()
        for group in section.get("groups", [])
    }

    assert groups["output_control"]["risk"] == "control_stability"
    assert groups["output_control"]["level"] == "expert"
    assert groups["retention"]["risk"] == "data_loss"
    assert groups["downsampling"]["risk"] == "data_loss"
    assert groups["query_profiles"]["risk"] == "data_loss"
    for group in groups.values():
        assert SECTION_KEYS <= group.keys()
        assert group["scope"] in SCOPES


def test_user_facing_descriptions_are_nonempty_and_short():
    for section in get_config_feature_sections():
        assert 0 < len(section["description"].strip()) <= 240
        for field in section["fields"]:
            assert 0 < len(field["description"].strip()) <= 240, field["path"]


def test_home_assistant_metadata_is_deprecated_and_maintenance_only():
    fields = get_config_feature_field_index()

    for path in ("ha.enabled", "ha.control_enabled", "ha.url", "ha.token"):
        assert fields[path]["level"] == "deprecated"
        assert fields[path]["scope"] == "maintenance"


# Promoted to primary (normal) so they render outside Advanced settings.
_PROMOTED_OUTPUT_CONTROL = {
    "system.output_control.target_deadband_w",
    "system.output_control.ramp_up_w_per_cycle",
    "system.output_control.ramp_down_w_per_cycle",
    "system.output_control.device_ramp_up_w_per_cycle",
    "system.output_control.device_ramp_down_w_per_cycle",
}


def test_output_control_fields_are_expert_control_stability_settings():
    fields = get_config_feature_field_index()
    tuning = {
        path: field
        for path, field in fields.items()
        if path.startswith("system.output_control.")
    }

    assert tuning
    # The promoted ramp/deadband knobs are primary; the remaining smoothing
    # and bypass tuning stays expert. All keep control_stability risk.
    assert all(
        tuning[path]["level"] == "normal" for path in _PROMOTED_OUTPUT_CONTROL
    )
    assert all(
        field["level"] == "expert"
        for path, field in tuning.items()
        if path not in _PROMOTED_OUTPUT_CONTROL
    )
    assert all(field["risk"] == "control_stability" for field in tuning.values())


def test_metadata_includes_template_defaults():
    fields = get_config_feature_field_index()

    assert fields["system.enabled"]["default"] is True
    assert fields["devices[].max_power"]["default"] == 800
    # All three write gates default on in the catalog/template.
    assert fields["system.allow_hardware_writes"]["default"] is True
    assert fields["system.allow_mqtt_local_control_writes"]["default"] is True
    assert fields["system.allow_mqtt_zendure_control_writes"]["default"] is True


def test_schema_version_is_read_only_maintenance_metadata():
    schema = get_config_feature_field_index()["config_schema_version"]

    assert schema["editable"] is False
    assert schema["scope"] == "hidden"


def test_setup_mode_excludes_maintenance_only_sections():
    setup_ids = {
        section["id"] for section in get_config_feature_sections(mode="setup")
    }
    maintenance_ids = {
        section["id"]
        for section in get_config_feature_sections(mode="maintenance")
    }

    assert "ha" not in setup_ids
    assert "config_upgrade" not in setup_ids
    assert {"ha", "config_upgrade"} <= maintenance_ids


def test_metadata_results_are_safe_to_mutate():
    sections = get_config_feature_sections()
    sections[0]["fields"][0]["label"] = "changed"

    assert get_config_feature_sections()[0]["fields"][0]["label"] != "changed"


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError, match="mode"):
        get_config_feature_sections(mode="invalid")


def test_metadata_uses_canonical_template_not_legacy_root_path():
    assert TEMPLATE_PATH.parts[-2:] == ("config", "config.template.json")
