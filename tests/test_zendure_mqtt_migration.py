# SPDX-License-Identifier: AGPL-3.0-or-later
"""Safe migration of legacy Zendure MQTT control configs.

A pre-flip control device relied on topic-family write inference. Migration must
never silently keep that: with exact model evidence it pins the resolved
hardware profile; without it, output control is disabled (telemetry-only) with an
actionable warning. Already-safe devices (pinned profile or explicit protocol)
are untouched.
"""

import copy

import pytest

from ems.zendure_mqtt.migration import (
    migrate_zendure_mqtt_control_configs,
    plan_zendure_mqtt_migration,
    zendure_mqtt_control_configs_need_migration,
)

pytestmark = pytest.mark.simulation


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


def _config(device):
    return {"devices": [device]}


def test_legacy_control_with_model_evidence_pins_profile():
    device = _control_device(product="Hyper 2000")
    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["hardware_profile"] == "hyper_2000"
    assert device["power_write_profile"] == "legacy_object_device_automation"
    assert device["capabilities"]["write_output_limit"] is True
    assert any(w["code"] == "zendure_mqtt_control_model_pinned" for w in warnings)


def test_legacy_control_without_model_evidence_is_disabled():
    device = _control_device()
    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["capabilities"]["write_output_limit"] is False
    assert "hardware_profile" not in device
    warning = next(
        w for w in warnings if w["code"] == "zendure_mqtt_control_disabled_unknown_model"
    )
    assert warning["severity"] == "warning"
    assert "model" in warning["message"].lower()


def test_legacy_control_with_ambiguous_model_is_disabled():
    # A bare family word is not exact evidence and must not enable control.
    device = _control_device(product="Hyper")
    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["capabilities"]["write_output_limit"] is False
    assert any(
        w["code"] == "zendure_mqtt_control_disabled_unknown_model" for w in warnings
    )


def test_device_with_pinned_profile_is_untouched():
    device = _control_device(hardware_profile="hub_2000")
    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["capabilities"]["write_output_limit"] is True
    assert "hardware_profile" in device
    assert warnings == []


def test_explicit_write_protocol_without_profile_is_not_safe():
    # The legacy properties/write escape hatch no longer authorizes control: a
    # control device pinned only to a write_protocol (no concrete model) is
    # unsafe and migration disables control and strips the write protocol.
    device = _control_device()
    device["mqtt"]["write_protocol"] = "legacy_properties_write"
    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in device["mqtt"]
    assert any(
        w["code"] == "zendure_mqtt_control_disabled_unknown_model" for w in warnings
    )


# --- dry-run / apply / idempotency -----------------------------------------


def test_dry_run_reports_exact_diff_without_mutating():
    device = _control_device(product="Hyper 2000")
    config = _config(device)
    before = copy.deepcopy(config)
    changes = plan_zendure_mqtt_migration(config)
    # Dry-run never mutates the config it inspects.
    assert config == before
    assert len(changes) == 1
    change = changes[0]
    assert change.action == "pin_profile"
    assert change.hardware_profile == "hyper_2000"
    assert change.power_write_profile == "legacy_object_device_automation"


def test_dry_run_reports_disable_for_unknown_model():
    device = _control_device()
    changes = plan_zendure_mqtt_migration(_config(device))
    assert [c.action for c in changes] == ["disable_control"]


def test_needs_migration_predicate():
    assert zendure_mqtt_control_configs_need_migration(
        _config(_control_device())
    ) is True
    assert zendure_mqtt_control_configs_need_migration(
        _config(_control_device(hardware_profile="hub_2000"))
    ) is False


def test_migration_is_idempotent():
    device = _control_device(product="Hyper 2000")
    config = _config(device)
    migrate_zendure_mqtt_control_configs(config)
    # A second pass is a no-op: the pinned device is already safe.
    assert zendure_mqtt_control_configs_need_migration(config) is False
    _cfg, warnings = migrate_zendure_mqtt_control_configs(config)
    assert warnings == []
    assert plan_zendure_mqtt_migration(config) == []


def test_disabled_device_is_idempotent():
    device = _control_device()
    config = _config(device)
    migrate_zendure_mqtt_control_configs(config)
    assert zendure_mqtt_control_configs_need_migration(config) is False
    _cfg, warnings = migrate_zendure_mqtt_control_configs(config)
    assert warnings == []


def test_scalar_model_evidence_incompatible_transport_is_disabled():
    # A ZenSDK model whose config transport is scalar cannot take an MQTT write.
    device = _control_device(product="SolarFlow 800")
    device["mqtt"]["topic_family"] = "zensdk_ha_scalar"
    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["capabilities"]["write_output_limit"] is False
    assert any(
        w["code"] == "zendure_mqtt_control_disabled_unknown_model" for w in warnings
    )
