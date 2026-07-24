# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin exposes an exact hardware-model selector and an EMS-owned migration review.

The concrete model selector is derived from the registry: an "Automatically
detected" option plus every canonical model, telemetry-only models flagged, each
carrying its supported operations and write profile. The Maintenance migration
review orchestrates the EMS-owned dry-run/apply (never a second algorithm),
exposing exact before/after changes, control-disable warnings and the final
validity of the result.
"""

import pytest

pytestmark = pytest.mark.simulation


# --- concrete model selector -------------------------------------------------


def test_hardware_profile_selector_options_cover_registry():
    from ems.mqtt_control.zendure_profiles import hardware_profile_selector_options

    options = hardware_profile_selector_options()
    values = [o["value"] for o in options]
    # Explicit telemetry-only sentinel first, then every concrete model.
    assert options[0]["auto"] is False
    assert options[0]["value"] == ""
    for expected in (
        "solarflow_800_pro_2",
        "hyper_2000",
        "aio_2400",
        "hub_1200",
        "hub_2000",
        "ace_1500",
        "superbase_v4600",
        "superbase_v6400",
    ):
        assert expected in values


def test_selector_flags_telemetry_only_models():
    from ems.mqtt_control.zendure_profiles import hardware_profile_selector_options

    by_value = {o["value"]: o for o in hardware_profile_selector_options()}
    assert by_value["hyper_2000"]["telemetry_only"] is False
    assert "charge" in by_value["hyper_2000"]["supported_operations"]
    assert by_value["hyper_2000"]["power_write_profile"] == "legacy_object_device_automation"
    # ACE 1500 is telemetry only, so labelled and flagged.
    assert by_value["ace_1500"]["telemetry_only"] is True
    assert "telemetry only" in by_value["ace_1500"]["label"].lower()
    assert by_value["ace_1500"]["supported_operations"] == []


def test_admin_exposes_model_selector_options():
    from admin.zendure_mqtt_config_draft import zendure_hardware_profile_options

    options = zendure_hardware_profile_options()
    labels = {o["label"] for o in options}
    assert "Hyper 2000" in labels
    assert "SolarFlow 800 Pro 2" in labels


# --- EMS-owned Maintenance migration review ----------------------------------


def _control_device(**over):
    device = {
        "type": "zendure_mqtt",
        "name": "Legacy",
        "mqtt": {
            "broker_ref": "local_a",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEV",
            "product_key": "PK",
        },
        "capabilities": {"write_output_limit": True},
    }
    device.update(over)
    return device


def test_migration_review_reports_pin_with_concrete_changes():
    from admin.zendure_mqtt_migration_review import zendure_mqtt_migration_review

    config = {"devices": [_control_device(product="Hyper 2000")]}
    review = zendure_mqtt_migration_review(config)
    assert review["needs_migration"] is True
    assert review["final_valid"] is True
    change = review["changes"][0]
    assert change["action"] == "pin_profile"
    assert change["index"] == 0
    assert change["device_id"] == "DEV"
    paths = {c["path"] for c in change["changes"]}
    assert "devices[0].hardware_profile" in paths


def test_migration_review_flags_disabled_control_warning():
    from admin.zendure_mqtt_migration_review import zendure_mqtt_migration_review

    config = {"devices": [_control_device()]}  # no model evidence
    review = zendure_mqtt_migration_review(config)
    change = review["changes"][0]
    assert change["action"] == "disable_control"
    assert change["disables_control"] is True
    assert review["warnings_disabling_control"]


def test_migration_review_is_read_only():
    from admin.zendure_mqtt_migration_review import zendure_mqtt_migration_review
    import copy

    config = {"devices": [_control_device(product="Hyper 2000")]}
    before = copy.deepcopy(config)
    zendure_mqtt_migration_review(config)
    assert config == before  # dry-run never mutates


def test_admin_apply_uses_ems_owned_migration():
    from admin.zendure_mqtt_migration_review import apply_zendure_mqtt_migration

    config = {"devices": [_control_device(product="Hyper 2000")]}
    migrated, warnings = apply_zendure_mqtt_migration(config)
    assert migrated["devices"][0]["hardware_profile"] == "hyper_2000"
    assert warnings
