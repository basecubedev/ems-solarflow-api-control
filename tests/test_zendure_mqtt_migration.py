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


def _cloud_config(device):
    config = _config(device)
    config["zendure_mqtt"]["brokers"]["local_a"] = {
        "enabled": True,
        "source": "zendure_cloud_mqtt",
        "host": "mqtteu.zen-iot.com",
        "port": 8883,
        "tls": True,
        "credentials_ref": "cloud-cred",
    }
    return config


def _config(device):
    # A legacy control config always had a broker profile; its source is the
    # write carrier and therefore a capability axis the migration reads.
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
        "devices": [device],
    }


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


# --- obsolete profile-backed write_topic normalization ----------------------


def _pinned_pro2(**over):
    device = _control_device(
        hardware_profile="solarflow_800_pro_2",
        power_write_profile="zensdk_properties_write",
    )
    device.update(over)
    return device


def test_profile_backed_obsolete_write_topic_is_normalized_away():
    from ems.zendure_mqtt.migration import (
        zendure_mqtt_control_migration_startup_error,
    )

    device = _pinned_pro2()
    device["mqtt"]["write_topic"] = "/PK/DEV/properties/report"
    device["mqtt"]["vendor_extension"] = "keep-me"
    config = _config(device)

    changes = plan_zendure_mqtt_migration(config)
    assert [c.action for c in changes] == ["normalize_write_topic"]
    assert changes[0].severity == "info"
    assert changes[0].changes[0]["path"] == "devices[0].mqtt.write_topic"

    # A pure normalization must never hard-block startup: the runtime already
    # ignores the obsolete override and publishes to the canonical topic.
    assert zendure_mqtt_control_migration_startup_error(config) is None

    _cfg, warnings = migrate_zendure_mqtt_control_configs(config)
    assert "write_topic" not in device["mqtt"]
    assert device["mqtt"]["product_key"] == "PK"
    assert device["mqtt"]["device_id"] == "DEV"
    assert device["mqtt"]["vendor_extension"] == "keep-me"
    assert device["hardware_profile"] == "solarflow_800_pro_2"
    assert any(
        w["code"] == "zendure_mqtt_control_write_topic_normalized" for w in warnings
    )

    # Idempotent: a re-run has nothing left to do.
    assert plan_zendure_mqtt_migration(config) == []


def test_pin_change_also_strips_a_stored_write_topic():
    device = _control_device(product="SolarFlow 800 Pro 2")
    device["mqtt"]["write_topic"] = "/PK/DEV/properties/report"
    _cfg, _warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["hardware_profile"] == "solarflow_800_pro_2"
    assert "write_topic" not in device["mqtt"]


def test_custom_protocol_write_topic_is_preserved_by_migration():
    # The custom escape hatch legitimately needs its explicit topic; migration
    # must never strip it (there is no pinned profile to derive a canonical one).
    device = _control_device()
    device["mqtt"].pop("product_key", None)
    device["mqtt"]["write_protocol"] = "custom_properties_write"
    device["mqtt"]["write_topic"] = "iot/PK/DEV/properties/write"
    _cfg, _warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["mqtt"]["write_topic"] == "iot/PK/DEV/properties/write"


def test_write_topic_only_addressed_profile_device_keeps_its_address():
    # A profile device addressed solely by write_topic (no product_key) must not
    # have its only address stripped — canonical topic cannot be built for it.
    device = _pinned_pro2()
    device["mqtt"].pop("product_key", None)
    device["mqtt"]["write_topic"] = "iot/PK/DEV/properties/write"
    changes = plan_zendure_mqtt_migration(_config(device))
    assert all(c.action != "normalize_write_topic" for c in changes)


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


def test_scalar_transport_with_model_evidence_keeps_control_on_the_cloud_broker():
    # The write route is iot/<pk>/<dev>/properties/write on every family, so a
    # ZenSDK model with a complete route stays controllable on a scalar one —
    # on the broker source that carries that route.
    device = _control_device(product="SolarFlow 800")
    device["mqtt"]["topic_family"] = "zensdk_ha_scalar"
    device["mqtt"]["source"] = "zendure_cloud_mqtt"
    _cfg, warnings = migrate_zendure_mqtt_control_configs(
        _cloud_config(device)
    )
    assert device["hardware_profile"] == "solarflow_800"
    assert device["capabilities"]["write_output_limit"] is True
    assert any(w["code"] == "zendure_mqtt_control_model_pinned" for w in warnings)


def test_local_scalar_control_is_disabled_but_keeps_its_resolved_model():
    # Same device on a local broker: the write route is not verified there, so
    # migration turns control off while preserving the identity it resolved.
    device = _control_device(
        product="SolarFlow 800", hardware_profile="solarflow_800"
    )
    device["mqtt"]["topic_family"] = "zensdk_ha_scalar"
    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["capabilities"]["write_output_limit"] is False
    assert device["hardware_profile"] == "solarflow_800"
    assert any(
        w["code"] == "zendure_mqtt_control_disabled_broker_source" for w in warnings
    )


def test_scalar_transport_without_model_evidence_is_still_disabled():
    device = _control_device()
    device["mqtt"]["topic_family"] = "zensdk_ha_scalar"
    _cfg, warnings = migrate_zendure_mqtt_control_configs(_config(device))
    assert device["capabilities"]["write_output_limit"] is False
    assert any(
        w["code"] == "zendure_mqtt_control_disabled_unknown_model" for w in warnings
    )
