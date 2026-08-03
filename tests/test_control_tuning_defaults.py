# SPDX-License-Identifier: AGPL-3.0-or-later
"""Focused regression tests for the promoted core control-tuning parameters.

Covers the "promote control tuning + update new-install defaults" change:
new-config defaults, existing-value preservation, primary catalogue hierarchy,
the Fresh Install / Maintenance Admin workflows, per-device isolation, and
cross-source consistency (catalogue == template == Core).
"""

import json
from pathlib import Path

import pytest

from admin.maintenance_config import (
    build_maintenance_draft,
    load_maintenance_config,
    preview_maintenance_config,
)
from admin.setup_config import (
    apply_device_config_values,
    apply_setup_features,
    build_setup_catalog,
)
from ems.config import (
    OUTPUT_CONTROL_DEFAULTS,
    apply_runtime_config_defaults,
    default_safe_config,
)
from ems.config_catalog import (
    build_default_template,
    get_config_feature_field_index,
    render_default_template,
)

pytestmark = [
    pytest.mark.config,
    pytest.mark.power_control,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "config" / "config.template.json"

# Canonical field -> (user-facing label, new default) for the promoted knobs.
PROMOTED = {
    "system.max_total_power": ("Maximum system output", 800),
    "system.loop_interval": ("Loop interval", 5),
    "system.output_control.target_deadband_w": ("System deadband", 5),
    "system.output_control.ramp_up_w_per_cycle": ("System ramp up", 500),
    "system.output_control.ramp_down_w_per_cycle": ("System ramp down", 300),
    "system.deadband": ("Device deadband", 2),
    "system.output_control.device_ramp_up_w_per_cycle": ("Device ramp up", 400),
    "system.output_control.device_ramp_down_w_per_cycle": ("Device ramp down", 200),
}

# The old explicit values an existing installation may carry on disk.
LEGACY_VALUES = {
    "system.loop_interval": 3,
    "system.output_control.target_deadband_w": 10,
    "system.output_control.ramp_up_w_per_cycle": 400,
    "system.output_control.ramp_down_w_per_cycle": 500,
    "system.deadband": 10,
    "system.output_control.device_ramp_up_w_per_cycle": 300,
    "system.output_control.device_ramp_down_w_per_cycle": 400,
}


def _template_get(template, path):
    cursor = template
    for part in path.split("."):
        cursor = cursor[part]
    return cursor


def _legacy_config():
    return {
        "system": {
            "loop_interval": LEGACY_VALUES["system.loop_interval"],
            "deadband": LEGACY_VALUES["system.deadband"],
            "output_control": {
                "target_deadband_w": LEGACY_VALUES[
                    "system.output_control.target_deadband_w"
                ],
                "ramp_up_w_per_cycle": LEGACY_VALUES[
                    "system.output_control.ramp_up_w_per_cycle"
                ],
                "ramp_down_w_per_cycle": LEGACY_VALUES[
                    "system.output_control.ramp_down_w_per_cycle"
                ],
                "device_ramp_up_w_per_cycle": LEGACY_VALUES[
                    "system.output_control.device_ramp_up_w_per_cycle"
                ],
                "device_ramp_down_w_per_cycle": LEGACY_VALUES[
                    "system.output_control.device_ramp_down_w_per_cycle"
                ],
            },
        }
    }


# --- new-config defaults -------------------------------------------------


def test_new_config_template_uses_promoted_control_defaults():
    template = build_default_template()
    system = template["system"]
    oc = system["output_control"]

    assert system["loop_interval"] == 5
    assert system["deadband"] == 2
    assert oc["target_deadband_w"] == 5
    assert oc["ramp_up_w_per_cycle"] == 500
    assert oc["ramp_down_w_per_cycle"] == 300
    assert oc["device_ramp_up_w_per_cycle"] == 400
    assert oc["device_ramp_down_w_per_cycle"] == 200
    # Maximum system output is unchanged by this task.
    assert system["max_total_power"] == 800


# --- catalogue hierarchy (primary vs advanced) ---------------------------


def test_promoted_fields_are_primary_normal_level():
    fields = get_config_feature_field_index()
    for path, (label, default) in PROMOTED.items():
        field = fields[path]
        assert field["level"] == "normal", path
        assert field["label"] == label, path
        assert field["default"] == default, path


def test_promoted_output_control_fields_left_the_expert_group():
    fields = get_config_feature_field_index()
    for path in PROMOTED:
        if path.startswith("system.output_control."):
            assert "group" not in fields[path], path
            # They stay control-stability sensitive even when primary.
            assert fields[path]["risk"] == "control_stability", path


def test_fresh_install_renders_promoted_fields_outside_advanced():
    # In the setup UI, normal-level fields render inline; advanced/expert fields
    # collapse into "Advanced settings" / "Developer settings" details blocks.
    catalog = build_setup_catalog()
    system = next(s for s in catalog["sections"] if s["id"] == "system")
    by_path = {field["path"]: field for field in system["fields"]}
    for path in PROMOTED:
        assert path in by_path, path
        assert by_path[path]["level"] == "normal", path


# --- config consistency (catalogue == template == Core) ------------------


def test_catalogue_defaults_match_config_template_defaults():
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    fields = get_config_feature_field_index()
    for path, (_, default) in PROMOTED.items():
        assert fields[path]["default"] == _template_get(template, path), path
        assert _template_get(template, path) == default, path


def test_committed_template_is_regenerated():
    assert TEMPLATE_PATH.read_text(encoding="utf-8") == render_default_template()


def test_core_output_control_defaults_match_generated_template():
    template = build_default_template()
    template_oc = {
        key: value
        for key, value in template["system"]["output_control"].items()
        if not key.startswith("_comment")
    }
    assert template_oc == OUTPUT_CONTROL_DEFAULTS


def test_core_runtime_defaults_match_generated_config_defaults():
    # Loading a freshly generated config into Core yields the new defaults.
    merged = apply_runtime_config_defaults(build_default_template())
    system = merged["system"]
    oc = system["output_control"]
    assert system["loop_interval"] == 5
    assert system["deadband"] == 2
    assert oc["target_deadband_w"] == 5
    assert oc["ramp_up_w_per_cycle"] == 500
    assert oc["ramp_down_w_per_cycle"] == 300
    assert oc["device_ramp_up_w_per_cycle"] == 400
    assert oc["device_ramp_down_w_per_cycle"] == 200

    # The safe-config fallbacks Core resolves missing keys with agree too.
    safe = default_safe_config()
    assert safe["system"]["loop_interval"] == 5
    assert safe["system"]["deadband"] == 2


def test_no_javascript_only_fallback_disagrees_with_core():
    # admin.js is fully catalogue-driven: it must not hardcode a numeric default
    # for any promoted control field (which could silently disagree with Core).
    admin_js = (ROOT / "admin" / "static" / "admin.js").read_text(encoding="utf-8")
    for key in (
        "loop_interval",
        "max_total_power",
        "target_deadband_w",
        "ramp_up_w_per_cycle",
        "ramp_down_w_per_cycle",
        "device_ramp_up_w_per_cycle",
        "device_ramp_down_w_per_cycle",
    ):
        assert key not in admin_js, key


# --- existing-value preservation (never silently replaced) ---------------


def test_existing_explicit_values_are_preserved_by_core_defaults():
    merged = apply_runtime_config_defaults(_legacy_config())
    system = merged["system"]
    oc = system["output_control"]
    assert system["loop_interval"] == 3
    assert system["deadband"] == 10
    assert oc["target_deadband_w"] == 10
    assert oc["ramp_up_w_per_cycle"] == 400
    assert oc["ramp_down_w_per_cycle"] == 500
    assert oc["device_ramp_up_w_per_cycle"] == 300
    assert oc["device_ramp_down_w_per_cycle"] == 400


def test_maintenance_renders_existing_values():
    draft = build_maintenance_draft(_legacy_config())
    features = draft["features"]
    assert features["system.loop_interval"] == 3
    assert features["system.deadband"] == 10
    assert features["system.output_control.target_deadband_w"] == 10
    assert features["system.output_control.ramp_down_w_per_cycle"] == 500
    assert features["system.output_control.device_ramp_down_w_per_cycle"] == 400


# --- Admin workflows: Fresh Install generate + Maintenance preview --------


def test_fresh_install_generated_config_contains_selected_values():
    config = {"system": {"output_control": {}}}
    applied = apply_setup_features(
        config,
        {
            "system.loop_interval": "4",
            "system.deadband": "7",
            "system.output_control.target_deadband_w": "8",
            "system.output_control.ramp_down_w_per_cycle": "250",
            "system.output_control.device_ramp_down_w_per_cycle": "150",
        },
    )
    assert set(applied) == {
        "system.loop_interval",
        "system.deadband",
        "system.output_control.target_deadband_w",
        "system.output_control.ramp_down_w_per_cycle",
        "system.output_control.device_ramp_down_w_per_cycle",
    }
    assert config["system"]["loop_interval"] == 4
    assert config["system"]["deadband"] == 7
    oc = config["system"]["output_control"]
    assert oc["target_deadband_w"] == 8
    assert oc["ramp_down_w_per_cycle"] == 250
    assert oc["device_ramp_down_w_per_cycle"] == 150


def test_maintenance_preview_preserves_unchanged_control_values(
    tmp_path, isolated_install_root
):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(_legacy_config()), encoding="utf-8"
    )

    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    # Change one unrelated feature so the preview is a real edit.
    draft["features"]["system.loop_interval"] = 3  # unchanged, still explicit
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))

    assert preview["status"] == "ok"
    system = preview["preview"]["system"]
    oc = system["output_control"]
    # The catalogue default change never replaces the operator's on-disk values.
    assert system["loop_interval"] == 3
    assert system["deadband"] == 10
    assert oc["target_deadband_w"] == 10
    assert oc["ramp_down_w_per_cycle"] == 500
    assert oc["device_ramp_down_w_per_cycle"] == 400


# --- per-device isolation ------------------------------------------------


def test_changing_one_device_does_not_modify_another():
    device_a = {"name": "WR1", "ip": "192.0.2.1", "sn": "SN1", "max_power": 800}
    device_b = {"name": "WR2", "ip": "192.0.2.2", "sn": "SN2", "max_power": 800}

    applied = apply_device_config_values(
        device_a, {"max_power": "600", "min_soc": "20"}
    )

    assert set(applied) == {"max_power", "min_soc"}
    assert device_a["max_power"] == 600
    assert device_a["min_soc"] == 20
    # The second device is untouched.
    assert device_b == {
        "name": "WR2",
        "ip": "192.0.2.2",
        "sn": "SN2",
        "max_power": 800,
    }
