# SPDX-License-Identifier: AGPL-3.0-or-later
"""Existing legacy single-broker configs keep working unchanged.

Recognizing the legacy top-level broker as the implicit ``default`` credential
consumer must not make an anonymous legacy install suddenly require a credential,
and must not rewrite the config on a plain read: an anonymous single broker stays
valid with no ``credentials_ref``, an authenticated one requires a complete
record, and neither is migrated into a ``brokers.default`` profile just by being
read.
"""

import copy

import pytest

from admin.credential_store import CredentialStore
from admin.mqtt_runtime_provisioning import (
    runtime_credential_requirements,
    validate_all_runtime_credentials,
    validate_config_credential_references,
)
from ems.mqtt_credentials import (
    FileMqttCredentialResolver,
    collect_mqtt_credential_consumers,
    find_mqtt_credential_consumer_issues,
)
from ems.zendure_mqtt.runtime import build_zendure_mqtt_runtime
from tests.helpers.fake_mqtt import FakeMqttNetwork

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def _device():
    return {
        "type": "zendure_mqtt",
        "enabled": True,
        "name": "Legacy Inv",
        "serial_number": "SN-LEGACY1",
        "mqtt": {"topic_family": "iot", "device_id": "SN-LEGACY1"},
    }


def _anonymous_config():
    return {
        "devices": [_device()],
        "zendure_mqtt": {"enabled": True, "host": "broker", "port": 1883},
    }


def _authenticated_config(credentials_ref="legacy-auth"):
    return {
        "devices": [_device()],
        "zendure_mqtt": {
            "enabled": True,
            "host": "broker",
            "port": 1883,
            "credentials_ref": credentials_ref,
        },
    }


def _runtime_issues(config, resolver=None):
    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_runtime(
        config,
        service_factory=network.telemetry_service_factory(),
        credential_resolver=resolver,
    )
    try:
        return {b["broker_ref"]: b["issue"] for b in runtime.status()["brokers"]}
    finally:
        runtime.stop()


# --- anonymous legacy single broker stays valid, no credentials required -----


def test_anonymous_legacy_default_requires_no_credential():
    config = _anonymous_config()
    assert collect_mqtt_credential_consumers(config) == ()
    assert runtime_credential_requirements(config) == {"local": set(), "cloud": set()}
    assert find_mqtt_credential_consumer_issues(config) == []
    validate_config_credential_references(config)  # must not raise


def test_anonymous_legacy_default_runtime_has_no_auth_issue():
    issues = _runtime_issues(_anonymous_config())
    assert issues.get("default") is None, issues


# --- authenticated legacy single broker requires a complete record -----------


def test_authenticated_legacy_default_requires_full_record():
    config = _authenticated_config()
    assert runtime_credential_requirements(config) == {
        "local": {"legacy-auth"},
        "cloud": set(),
    }


def test_authenticated_legacy_default_missing_record_blocks(tmp_path):
    config = _authenticated_config()
    store = CredentialStore(config_dir=tmp_path / "config")
    # No record seeded: the referenced credential does not resolve, so an apply
    # would block on it.
    affected = validate_all_runtime_credentials(config, credential_store=store)
    assert affected == ["legacy-auth"]


def test_authenticated_legacy_default_with_record_reconstructs(tmp_path):
    config = _authenticated_config()
    store = CredentialStore(config_dir=tmp_path / "config")
    store.save_mqtt_broker_secret("legacy-auth", "user", "pw")
    assert validate_all_runtime_credentials(config, credential_store=store) == []
    issues = _runtime_issues(config, FileMqttCredentialResolver(store.secrets_dir))
    assert issues.get("default") is None, issues


# --- a plain read never migrates the legacy block into brokers.default -------


@pytest.mark.parametrize("config", [_anonymous_config(), _authenticated_config()])
def test_reading_does_not_migrate_to_brokers_default(config):
    snapshot = copy.deepcopy(config)
    collect_mqtt_credential_consumers(config)
    runtime_credential_requirements(config)
    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_runtime(
        config, service_factory=network.telemetry_service_factory()
    )
    runtime.stop()
    # The input config is untouched: no synthesized ``brokers`` block appears.
    assert config == snapshot
    assert "brokers" not in config["zendure_mqtt"]
