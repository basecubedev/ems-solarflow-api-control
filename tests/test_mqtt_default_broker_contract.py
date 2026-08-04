# SPDX-License-Identifier: AGPL-3.0-or-later
"""``default`` is reserved for the implicit legacy top-level broker.

A legacy single-broker install configures its broker in the top-level
``zendure_mqtt`` fields, which the runtime maps to the implicit ``default``
broker ref. A named ``zendure_mqtt.brokers.default`` profile would collide with
that identity, so the ref is reserved: Core validation, Maintenance Preview and
Maintenance Apply reject it with one stable code, and the runtime safety net
never lets a named ``default`` silently overwrite the legacy default. The
rejection carries the stable code and path only — never a host, username or
credential value.
"""

import pytest

from ems.zendure_mqtt.config_entries import (
    DEFAULT_BROKER_REF,
    RESERVED_MQTT_BROKER_REFS,
    find_reserved_mqtt_broker_ref_issues,
    iter_effective_mqtt_broker_profiles,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.contract,
    pytest.mark.simulation,
]


def _device(sn="SN1", broker_ref=None):
    mqtt = {"topic_family": "iot", "device_id": sn}
    if broker_ref is not None:
        mqtt["broker_ref"] = broker_ref
    return {
        "type": "zendure_mqtt",
        "enabled": True,
        "name": f"dev-{sn}",
        "serial_number": sn,
        "mqtt": mqtt,
    }


def _named_default_with_top_level_host():
    return {
        "devices": [_device()],
        "zendure_mqtt": {
            "host": "top.local",
            "credentials_ref": "legacy-auth",
            "brokers": {
                "default": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "collide.local",
                    "credentials_ref": "collide-auth",
                }
            },
        },
    }


def _named_default_without_top_level_host():
    return {
        "devices": [_device()],
        "zendure_mqtt": {
            "brokers": {
                "default": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "collide.local",
                    "credentials_ref": "collide-auth",
                }
            }
        },
    }


# --- the central reserved set -----------------------------------------------


def test_default_is_the_only_reserved_ref():
    assert RESERVED_MQTT_BROKER_REFS == frozenset({DEFAULT_BROKER_REF})


# --- Core validation --------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [_named_default_with_top_level_host(), _named_default_without_top_level_host()],
)
def test_core_validation_rejects_named_default(config):
    issues = find_reserved_mqtt_broker_ref_issues(config)
    codes = {i["code"] for i in issues}
    assert "mqtt_broker_ref_reserved" in codes


def test_reserved_issue_has_stable_code_and_path_without_secrets():
    issue = next(
        i
        for i in find_reserved_mqtt_broker_ref_issues(
            _named_default_with_top_level_host()
        )
        if i["code"] == "mqtt_broker_ref_reserved"
    )
    assert issue["code"] == "mqtt_broker_ref_reserved"
    assert issue["path"] == "zendure_mqtt.brokers.default"
    assert issue["severity"] == "error"
    blob = repr(issue)
    assert "collide.local" not in blob
    assert "collide-auth" not in blob


def test_valid_named_brokers_are_not_reserved():
    config = {
        "devices": [_device(broker_ref="home")],
        "zendure_mqtt": {
            "brokers": {"home": {"host": "home.local", "source": "local_mqtt"}}
        },
    }
    assert find_reserved_mqtt_broker_ref_issues(config) == []


# --- runtime safety net: never overwrite the legacy default -----------------


def test_effective_resolver_skips_named_default_beside_legacy_default():
    profiles = list(
        iter_effective_mqtt_broker_profiles(_named_default_with_top_level_host())
    )
    default_profiles = [p for p in profiles if p.broker_ref == DEFAULT_BROKER_REF]
    # At most one effective broker carries the default ref, and it is the legacy
    # top-level profile, never the named one.
    assert len(default_profiles) == 1
    assert default_profiles[0].config.get("host") == "top.local"


def test_effective_resolver_drops_named_default_without_legacy_default():
    profiles = list(
        iter_effective_mqtt_broker_profiles(_named_default_without_top_level_host())
    )
    # No top-level host means no legacy default either; the named default is
    # reserved and never resurrected as a broker.
    assert [p.broker_ref for p in profiles if p.broker_ref == DEFAULT_BROKER_REF] == []


def test_runtime_loader_reports_named_default_and_keeps_legacy_default():
    from ems.mqtt_credentials import MqttCredentials
    from ems.zendure_mqtt.runtime import load_zendure_mqtt_broker_configs

    class _Resolver:
        def resolve(self, ref):
            return MqttCredentials("user", "pass", "cid", "ak")

    brokers, errors, _stale = load_zendure_mqtt_broker_configs(
        _named_default_with_top_level_host()["zendure_mqtt"],
        credential_resolver=_Resolver(),
    )
    # The legacy default keeps its top-level identity; the named default is
    # reported as an error, never silently merged onto the same ref.
    assert brokers[DEFAULT_BROKER_REF].host == "top.local"
    assert DEFAULT_BROKER_REF in errors
    assert "collide.local" not in errors[DEFAULT_BROKER_REF]


# --- Maintenance Preview and Apply share the reserved code ------------------


def test_maintenance_validate_rejects_named_default():
    from admin.maintenance_config import _validate

    codes = {
        i["code"] for i in _validate(_named_default_with_top_level_host())["errors"]
    }
    assert "mqtt_broker_ref_reserved" in codes


def test_setup_preview_rejects_named_default():
    from admin.config_preview import _reserved_broker_ref_errors

    validation = {"errors": [], "warnings": [], "info": []}
    _reserved_broker_ref_errors(_named_default_with_top_level_host(), validation)
    assert "mqtt_broker_ref_reserved" in {i["code"] for i in validation["errors"]}
