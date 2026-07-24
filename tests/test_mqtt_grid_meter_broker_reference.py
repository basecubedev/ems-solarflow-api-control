# SPDX-License-Identifier: AGPL-3.0-or-later
"""A grid meter selects a broker profile XOR inline connection settings.

``grid_meter.mqtt.broker_ref`` names a broker profile that owns every connection
field (host, port, TLS, credentials). Inlining any broker-owned field alongside
``broker_ref`` is ambiguous — the operator sees the inline value while EMS
silently uses the profile's — so it is rejected with one stable contract
(``mqtt_broker_reference_ambiguous`` + the conflicting field names, no secrets)
by the Core resolver, the Setup preview gate and the Maintenance validator alike.
"""

import pytest

from ems import config as cfg
from ems.config import (
    MQTT_BROKER_CONNECTION_FIELDS,
    MqttBrokerReferenceAmbiguousError,
)

pytestmark = pytest.mark.simulation


def _broker_ref_grid_config(**inline):
    mqtt = {"broker_ref": "home", "topic": "meter/power"}
    mqtt.update(inline)
    return {
        "devices": [
            {"type": "shelly", "name": "inv", "ip": "192.168.1.5", "sn": "SN1"}
        ],
        "grid_meter": {"type": "mqtt", "mqtt": mqtt},
        "zendure_mqtt": {
            "brokers": {
                "home": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "broker.local",
                    "port": 1883,
                    "credentials_ref": "profile-auth",
                }
            }
        },
    }


# --- the central conflict-field list ----------------------------------------


def test_central_broker_connection_field_list_covers_the_contract():
    assert {
        "host",
        "port",
        "tls",
        "tls_mode",
        "tls_insecure",
        "username",
        "password",
        "credentials_ref",
    } <= MQTT_BROKER_CONNECTION_FIELDS


# --- Core resolver ----------------------------------------------------------


def test_broker_ref_plus_credentials_ref_is_ambiguous():
    with pytest.raises(MqttBrokerReferenceAmbiguousError) as caught:
        cfg.resolve_grid_meter_mqtt_settings(
            _broker_ref_grid_config(credentials_ref="inline-auth")
        )
    exc = caught.value
    assert exc.code == "mqtt_broker_reference_ambiguous"
    assert exc.path == "grid_meter.mqtt"
    assert exc.fields == ("credentials_ref",)
    # The operator-facing message names the field, never the secret value.
    assert "inline-auth" not in str(exc)


def test_broker_ref_plus_tls_mode_is_ambiguous():
    with pytest.raises(MqttBrokerReferenceAmbiguousError) as caught:
        cfg.resolve_grid_meter_mqtt_settings(
            _broker_ref_grid_config(tls_mode="insecure")
        )
    assert caught.value.fields == ("tls_mode",)


def test_broker_ref_plus_host_is_still_rejected():
    with pytest.raises(MqttBrokerReferenceAmbiguousError) as caught:
        cfg.resolve_grid_meter_mqtt_settings(
            _broker_ref_grid_config(host="other-host")
        )
    assert "host" in caught.value.fields


def test_broker_ref_plus_username_password_rejected_even_without_profile_values():
    with pytest.raises(MqttBrokerReferenceAmbiguousError) as caught:
        cfg.resolve_grid_meter_mqtt_settings(
            _broker_ref_grid_config(username="u", password="p")
        )
    assert set(caught.value.fields) == {"username", "password"}


def test_multiple_conflict_fields_are_all_listed_without_secrets():
    with pytest.raises(MqttBrokerReferenceAmbiguousError) as caught:
        cfg.resolve_grid_meter_mqtt_settings(
            _broker_ref_grid_config(
                host="other-host",
                credentials_ref="other-auth",
                tls_mode="insecure",
            )
        )
    exc = caught.value
    assert exc.fields == ("credentials_ref", "host", "tls_mode")
    blob = str(exc)
    assert "other-auth" not in blob
    assert "other-host" not in blob


def test_broker_ref_without_inline_broker_fields_is_valid():
    resolved = cfg.resolve_grid_meter_mqtt_settings(_broker_ref_grid_config())
    assert resolved["host"] == "broker.local"
    assert resolved["credentials_ref"] == "profile-auth"
    assert "broker_ref" not in resolved


def test_direct_connection_without_broker_ref_is_valid():
    config = {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {
                "host": "broker.local",
                "port": 1883,
                "credentials_ref": "meter-auth",
                "topic": "meter/power",
            },
        }
    }
    resolved = cfg.resolve_grid_meter_mqtt_settings(config)
    assert resolved["credentials_ref"] == "meter-auth"
    assert resolved["host"] == "broker.local"


# --- Preview / Maintenance / Core react with the SAME stable code -----------


def _preview_codes(config):
    from admin.config_preview import _validate_mqtt_grid_meter_via_core

    validation = {"errors": [], "warnings": [], "info": []}
    _validate_mqtt_grid_meter_via_core(config, validation)
    return validation["errors"]


def _maintenance_codes(config):
    from admin.maintenance_config import _validate

    return _validate(config)["errors"]


def test_preview_and_maintenance_share_the_ambiguous_code():
    config = _broker_ref_grid_config(credentials_ref="inline-auth", tls_mode="insecure")

    preview_issues = _preview_codes(config)
    maintenance_issues = _maintenance_codes(config)

    assert {i["code"] for i in preview_issues} == {"mqtt_broker_reference_ambiguous"}
    assert "mqtt_broker_reference_ambiguous" in {
        i["code"] for i in maintenance_issues
    }
    # Both name the conflicting fields; neither leaks the secret value.
    for issues in (preview_issues, maintenance_issues):
        message = next(
            i["message"]
            for i in issues
            if i["code"] == "mqtt_broker_reference_ambiguous"
        )
        assert "credentials_ref" in message
        assert "tls_mode" in message
        assert "inline-auth" not in message


def test_valid_broker_ref_grid_meter_passes_preview_and_maintenance():
    config = _broker_ref_grid_config()
    assert _preview_codes(config) == []
    assert "mqtt_broker_reference_ambiguous" not in {
        i["code"] for i in _maintenance_codes(config)
    }


# --- a grid meter can select the implicit default beside named brokers -------
# The grid meter resolves ``broker_ref`` through the same effective-profile
# resolver the runtime and credential scanner use, so ``default`` addresses the
# legacy top-level broker even when other named brokers exist.


def _default_plus_named_config(grid_broker_ref="default"):
    return {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"broker_ref": grid_broker_ref, "topic": "meter/power"},
        },
        "zendure_mqtt": {
            "host": "default.local",
            "port": 1883,
            "credentials_ref": "default-auth",
            "brokers": {
                "secondary": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "secondary.local",
                    "port": 1883,
                    "credentials_ref": "secondary-auth",
                }
            },
        },
    }


def test_grid_meter_default_ref_resolves_legacy_top_level_broker():
    resolved = cfg.resolve_grid_meter_mqtt_settings(_default_plus_named_config())
    assert resolved["host"] == "default.local"
    assert resolved["credentials_ref"] == "default-auth"
    assert "broker_ref" not in resolved


def test_grid_meter_secondary_ref_resolves_named_broker():
    config = _default_plus_named_config(grid_broker_ref="secondary")
    resolved = cfg.resolve_grid_meter_mqtt_settings(config)
    assert resolved["host"] == "secondary.local"
    assert resolved["credentials_ref"] == "secondary-auth"


def test_grid_meter_default_ref_without_top_level_host_is_rejected():
    config = {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"broker_ref": "default", "topic": "meter/power"},
        },
        "zendure_mqtt": {
            "brokers": {"secondary": {"host": "secondary.local"}},
        },
    }
    # No legacy default exists (no top-level host + named brokers present).
    with pytest.raises(ValueError) as caught:
        cfg.resolve_grid_meter_mqtt_settings(config)
    assert "default" in str(caught.value)


def test_grid_meter_unknown_ref_is_stable_error():
    config = _default_plus_named_config(grid_broker_ref="nope")
    with pytest.raises(ValueError, match="not a configured"):
        cfg.resolve_grid_meter_mqtt_settings(config)


def test_grid_meter_disabled_named_broker_is_stable_error():
    config = {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"broker_ref": "secondary", "topic": "meter/power"},
        },
        "zendure_mqtt": {
            "brokers": {
                "secondary": {
                    "enabled": False,
                    "source": "local_mqtt",
                    "host": "secondary.local",
                }
            }
        },
    }
    with pytest.raises(ValueError, match="disabled"):
        cfg.resolve_grid_meter_mqtt_settings(config)


def test_grid_meter_cloud_broker_still_rejected():
    config = {
        "grid_meter": {
            "type": "mqtt",
            "mqtt": {"broker_ref": "default", "topic": "meter/power"},
        },
        "zendure_mqtt": {
            "host": "mqtteu.zen-iot.com",
            "source": "zendure_cloud_mqtt",
            "credentials_ref": "cloud-auth",
        },
    }
    with pytest.raises(ValueError, match="not a local_mqtt broker"):
        cfg.resolve_grid_meter_mqtt_settings(config)
