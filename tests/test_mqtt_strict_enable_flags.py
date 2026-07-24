# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict JSON-boolean parsing of Zendure MQTT per-profile enable flags.

The telemetry feature itself is always on: the legacy top-level ``enabled`` key
is ignored entirely (any type, any value) and a broker is active iff its host
is configured. Per-profile and per-device flags stay strict: a string
``"false"`` (or a numeric ``0``/``1``) must never toggle a broker or a device
via truthiness. Covers both the telemetry and control builders.
"""

import pytest

from ems.mqtt_credentials import MqttCredentialError
from ems.zendure_mqtt.config_entries import config_entry_enabled
from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime
from ems.zendure_mqtt.runtime import (
    build_zendure_mqtt_runtime,
    load_zendure_mqtt_broker_configs,
)

pytestmark = [pytest.mark.simulation]


def _named_broker(enabled):
    return {
        "brokers": {
            "b": {
                "enabled": enabled,
                "source": "local_mqtt",
                "host": "h",
                "port": 1883,
            }
        },
    }


# --- legacy top-level enabled is ignored ----------------------------------


@pytest.mark.parametrize("legacy", [True, False, "false", "true", 0, 1, [], {}])
def test_top_level_enabled_key_is_ignored(legacy):
    raw = {"enabled": legacy, "host": "h", "port": 1883, "source": "local_mqtt"}
    brokers, errors, _stale = load_zendure_mqtt_broker_configs(raw)
    assert errors == {}
    assert brokers["default"].enabled is True


def test_legacy_top_level_enabled_false_does_not_disable_runtime():
    raw = {"enabled": False, "host": "h", "port": 1883, "source": "local_mqtt"}
    runtime = build_zendure_mqtt_runtime(
        {
            "zendure_mqtt": raw,
            "devices": [
                {
                    "type": "zendure_mqtt",
                    "name": "Battery",
                    "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "DEV1"},
                }
            ],
        }
    )
    assert runtime.enabled is True


def test_missing_host_is_inactive_without_error():
    raw = {"port": 1883, "source": "local_mqtt"}
    brokers, errors, _stale = load_zendure_mqtt_broker_configs(raw)
    assert errors == {}
    assert brokers["default"].enabled is False


# --- per-profile enabled -------------------------------------------------


@pytest.mark.parametrize("bad", ["false", "true", 0, 1])
def test_profile_enabled_non_bool_is_rejected(bad):
    brokers, errors, _stale = load_zendure_mqtt_broker_configs(_named_broker(bad))
    assert "b" not in brokers
    assert "b" in errors and "enabled" in errors["b"]


def test_profile_enabled_false_string_does_not_enable_broker():
    raw = _named_broker("false")
    runtime = build_zendure_mqtt_runtime({"zendure_mqtt": raw})
    assert runtime.enabled is False


def test_profile_enabled_valid_true():
    brokers, errors, _stale = load_zendure_mqtt_broker_configs(_named_broker(True))
    assert errors == {}
    assert brokers["b"].enabled is True


def test_profile_enabled_valid_false():
    brokers, errors, _stale = load_zendure_mqtt_broker_configs(_named_broker(False))
    assert errors == {}
    assert brokers["b"].enabled is False


# --- disabled profile skips credential resolution ------------------------


def _raising_resolver(*_args, **_kwargs):
    class _Resolver:
        def resolve(self, ref):
            raise MqttCredentialError(f"MQTT credential reference '{ref}' not found")

    return _Resolver()


def test_disabled_profile_with_missing_credentials_ref_is_not_resolved():
    raw = {
        "enabled": True,
        "brokers": {
            "b": {
                "enabled": False,
                "source": "local_mqtt",
                "host": "h",
                "port": 1883,
                "credentials_ref": "missing-secret",
            }
        },
    }
    brokers, errors, _stale = load_zendure_mqtt_broker_configs(
        raw, credential_resolver=_raising_resolver()
    )
    # A canonically disabled profile must not resolve its secret, so a missing
    # credentials_ref does not raise or produce an error.
    assert errors == {}
    assert brokers["b"].enabled is False


def test_enabled_profile_with_missing_credentials_ref_errors():
    raw = {
        "enabled": True,
        "brokers": {
            "b": {
                "enabled": True,
                "source": "local_mqtt",
                "host": "h",
                "port": 1883,
                "credentials_ref": "missing-secret",
            }
        },
    }
    brokers, errors, _stale = load_zendure_mqtt_broker_configs(
        raw, credential_resolver=_raising_resolver()
    )
    assert "b" in errors
    assert "b" not in brokers


# --- control runtime builder --------------------------------------------


def _control_config(enabled):
    return {
        "zendure_mqtt": {
            "enabled": True,
            "host": "h",
            "port": 1883,
            "source": "local_mqtt",
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "ctl",
                "enabled": enabled,
                "hardware_profile": "solarflow_800_pro_2",
                "mqtt": {
                    "topic_family": "legacy_zendure_json",
                    "device_id": "SN1",
                    "product_key": "PK1",
                },
                "capabilities": {"write_output_limit": True},
                "max_power": 800,
            }
        ],
    }


def test_control_device_enabled_false_string_is_not_built():
    runtime = build_zendure_mqtt_control_runtime(_control_config("false"))
    assert runtime.devices == []
    assert runtime.rejected == []


def test_control_device_enabled_true_is_built():
    runtime = build_zendure_mqtt_control_runtime(_control_config(True))
    assert len(runtime.devices) == 1


# --- shared helper -------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        (None, True),
        ("false", False),
        ("true", False),
        (0, False),
        (1, False),
    ],
)
def test_config_entry_enabled_is_strict(value, expected):
    item = {"enabled": value} if value is not None else {}
    assert config_entry_enabled(item) is expected
