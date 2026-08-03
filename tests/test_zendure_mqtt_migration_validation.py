# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration must validate and canonicalize its result, never write an invalid config.

Before applying, migration builds the complete result in memory, strips obsolete
write metadata, derives canonical registry metadata, and runs the normal control
validation — refusing to write an invalid result. A pin needs complete write
addressing; exact model evidence with incomplete addressing disables control and
preserves telemetry rather than reporting success while leaving
``write_target_missing``. Every planned change carries a stable device identity
and exact before/after paths, so duplicate device names are never ambiguous.
"""

import copy

import pytest

from ems.zendure_mqtt.migration import (
    ACTION_DISABLE_CONTROL,
    ACTION_PIN_PROFILE,
    migrate_zendure_mqtt_control_configs,
    plan_zendure_mqtt_migration,
    validate_migrated_zendure_mqtt_config,
    validate_zendure_mqtt_control_configs,
    zendure_mqtt_control_migration_startup_error,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def _control_device(**over):
    device = {
        "type": "zendure_mqtt",
        "name": "Legacy",
        "mqtt": {
            "broker_ref": "local_a",
            # The broker profile is authoritative for the transport; a stored
            # entry commonly mirrors it, and it is the write carrier the
            # capability layer reads when no profile map is supplied.
            "source": "local_mqtt",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEV",
            "product_key": "PK",
        },
        "capabilities": {"write_output_limit": True},
    }
    device.update(over)
    return device


def _config(*devices):
    return {
        "zendure_mqtt": {
            "brokers": {
                "local_a": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.10",
                    "port": 1883,
                }
            }
        },
        "devices": list(devices),
    }


# --- canonicalization --------------------------------------------------------


def test_pin_removes_stale_write_protocol():
    device = _control_device(product="Hyper 2000")
    device["mqtt"]["write_protocol"] = "legacy_properties_write"
    migrate_zendure_mqtt_control_configs(_config(device))
    assert device["hardware_profile"] == "hyper_2000"
    assert "write_protocol" not in device["mqtt"]


def test_pin_overwrites_inconsistent_power_write_profile():
    device = _control_device(
        product="Hyper 2000", power_write_profile="zensdk_properties_write"
    )
    migrate_zendure_mqtt_control_configs(_config(device))
    assert device["power_write_profile"] == "legacy_object_device_automation"


def test_pinned_device_with_mismatched_power_write_profile_is_canonicalized():
    # Already pinned + addressable, but a stale mismatched power_write_profile —
    # migration canonicalizes it rather than reporting the config already safe.
    device = _control_device(
        hardware_profile="hyper_2000",
        power_write_profile="zensdk_properties_write",
    )
    changes = plan_zendure_mqtt_migration(_config(device))
    assert len(changes) == 1
    assert changes[0].action == ACTION_PIN_PROFILE
    migrate_zendure_mqtt_control_configs(_config(device))
    assert device["power_write_profile"] == "legacy_object_device_automation"


# --- final validation --------------------------------------------------------


def test_migrated_result_passes_normal_validation():
    device = _control_device(product="Hyper 2000")
    config = _config(device)
    assert validate_migrated_zendure_mqtt_config(config) == []


def test_raw_validator_detects_write_target_missing():
    # An already-pinned control device with no product_key/write_topic is invalid.
    device = _control_device(hardware_profile="hyper_2000")
    device["mqtt"].pop("product_key")
    errors = validate_zendure_mqtt_control_configs(_config(device))
    assert any(e["code"] == "write_target_missing" for e in errors)


def test_migrated_config_is_never_write_target_missing():
    device = _control_device(product="Hyper 2000")
    device["mqtt"].pop("product_key")  # exact model but no addressing
    config = _config(device)
    assert validate_migrated_zendure_mqtt_config(config) == []


# --- incomplete addressing ---------------------------------------------------


def test_incomplete_addressing_disables_control_instead_of_pinning():
    device = _control_device(product="Hyper 2000")
    device["mqtt"].pop("product_key")  # no product_key and no write_topic
    changes = plan_zendure_mqtt_migration(_config(device))
    assert [c.action for c in changes] == [ACTION_DISABLE_CONTROL]
    migrate_zendure_mqtt_control_configs(_config(device))
    assert device["capabilities"]["write_output_limit"] is False
    assert "hardware_profile" not in device
    # Telemetry is preserved (the device entry itself remains).
    assert device["mqtt"]["device_id"] == "DEV"


def test_incomplete_addressing_warning_is_actionable():
    device = _control_device(product="Hyper 2000")
    device["mqtt"].pop("product_key")
    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert warnings
    assert any("address" in w["message"].lower() for w in warnings)


# --- stable identity + concrete diff -----------------------------------------


def test_plan_exposes_concrete_before_after_changes():
    device = _control_device(product="Hyper 2000")
    changes = plan_zendure_mqtt_migration(_config(device))
    change = changes[0]
    entries = {c["path"]: c for c in change.changes}
    assert entries["devices[0].hardware_profile"]["before"] is None
    assert entries["devices[0].hardware_profile"]["after"] == "hyper_2000"
    assert entries["devices[0].power_write_profile"]["after"] == (
        "legacy_object_device_automation"
    )


def test_plan_carries_stable_device_identity():
    device = _control_device(product="Hyper 2000")
    change = plan_zendure_mqtt_migration(_config(device))[0]
    assert change.index == 0
    assert change.device_id == "DEV"


def test_duplicate_device_names_are_not_ambiguous():
    a = _control_device(name="WR", product="Hyper 2000")
    a["mqtt"]["device_id"] = "DEV_A"
    b = _control_device(name="WR")  # same name, no evidence → disable
    b["mqtt"]["device_id"] = "DEV_B"
    config = _config(a, b)
    changes = plan_zendure_mqtt_migration(config)
    assert {c.index for c in changes} == {0, 1}
    assert {c.device_id for c in changes} == {"DEV_A", "DEV_B"}
    # Startup identifies both blocking control devices distinctly.
    error = zendure_mqtt_control_migration_startup_error(config)
    assert error["count"] == 2


# --- invariant: applied result always validates clean ------------------------


@pytest.mark.parametrize(
    "device",
    [
        _control_device(product="Hyper 2000"),
        _control_device(),  # unknown model → disable
        _control_device(product="Hyper", ),  # ambiguous → disable
        _control_device(product="ACE 1500"),  # telemetry-only → disable
        _control_device(product="Hyper 2000", power_write_profile="zensdk_properties_write"),
    ],
)
def test_applied_migration_result_is_always_valid(device):
    config = _config(copy.deepcopy(device))
    migrate_zendure_mqtt_control_configs(config)
    # A second full validation of the mutated config finds nothing to fix/flag.
    assert validate_zendure_mqtt_control_configs(config) == []
    assert plan_zendure_mqtt_migration(config) == []


# --- obsolete profile write_topic: warned, never blocked ---------------------


def _pinned_pro2_with_write_topic():
    device = _control_device(
        hardware_profile="solarflow_800_pro_2",
        power_write_profile="zensdk_properties_write",
    )
    device["mqtt"]["write_topic"] = "/PK/DEV/properties/report"
    return device


def test_profile_write_topic_is_a_warning_not_an_error():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    device = _pinned_pro2_with_write_topic()
    issues = validate_zendure_mqtt_control_device_config(device)
    obsolete = [i for i in issues if i["code"] == "profile_write_topic_obsolete"]
    assert len(obsolete) == 1
    assert obsolete[0]["severity"] == "warning"
    # A warning must not make the device unwritable: no error, no startup block.
    assert [i for i in issues if i["severity"] == "error"] == []
    assert zendure_mqtt_control_migration_startup_error(_config(device)) is None


def test_normalization_clears_the_obsolete_write_topic_warning():
    from ems.zendure_mqtt.config_entries import (
        validate_zendure_mqtt_control_device_config,
    )

    device = _pinned_pro2_with_write_topic()
    config = _config(device)
    migrate_zendure_mqtt_control_configs(config)
    issues = validate_zendure_mqtt_control_device_config(device)
    assert [i for i in issues if i["code"] == "profile_write_topic_obsolete"] == []
