# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zendure cloud discovery-to-runtime credential contract.

The Admin cloud discovery path can only connect because ``deviceList`` returns
temporary MQTT username/password/client_id and the account app key. The applied
EMS config stores only a non-secret ``credentials_ref``. This contract pins that
Core can resolve that reference to real in-memory cloud connection material at
startup — with no Admin process, no cloud call, and no secret in config/status —
so a valid applied cloud proposal never reports ``broker_auth_missing``.

The secret is persisted once through the Admin-owned credential store (as apply
does) and then read back only through the Core-owned resolver, exactly as EMS
does at startup and on restart.
"""

import json

import pytest

from admin.credential_store import CredentialStore
from ems.mqtt_credentials import FileMqttCredentialResolver
from ems.zendure_mqtt.runtime import (
    build_zendure_mqtt_runtime,
    load_zendure_mqtt_broker_configs,
)
from tests.helpers.fake_mqtt import FakeMqttNetwork

pytestmark = [pytest.mark.simulation, pytest.mark.power_control]

CLOUD_REF = "zendure-cloud"
CLOUD_USER = "cloud-user-xyz"
CLOUD_PASS = "cloud-pass-SECRET"
CLOUD_CLIENT = "cloud-client-id"
CLOUD_APP_KEY = "cloud-app-key-SECRET"
SECRET_TOKENS = (CLOUD_PASS, CLOUD_APP_KEY)


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EMS_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config" / "secrets"


def _save_cloud_runtime_secret(tmp_path, *, password=CLOUD_PASS, app_key=CLOUD_APP_KEY):
    store = CredentialStore(config_dir=str(tmp_path / "config"))
    return store.save_mqtt_cloud_runtime_secret(
        CLOUD_REF,
        username=CLOUD_USER,
        password=password,
        client_id=CLOUD_CLIENT,
        app_key=app_key,
    )


def _cloud_config(credentials_ref=CLOUD_REF):
    return {
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "zendure_cloud": {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "tls": True,
                    "tls_insecure": True,
                    "credentials_ref": credentials_ref,
                },
            },
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "Cloud SolarFlow",
                "serial_number": "CLOUDDEV",
                "mqtt": {
                    "broker_ref": "zendure_cloud",
                    "topic_family": "zendure_cloud_scalar",
                    "device_id": "CLOUDDEV",
                    "product_key": "PKCLOUD",
                },
                "capabilities": {
                    "read_power": True,
                    "read_soc": True,
                    "write_output_limit": False,
                },
            },
        ],
    }


def _cloud_broker_config(config, resolver):
    brokers, errors, _stale = load_zendure_mqtt_broker_configs(
        config["zendure_mqtt"], credential_resolver=resolver
    )
    return brokers.get("zendure_cloud"), errors


def _cloud_summary(config, resolver):
    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_runtime(
        config,
        service_factory=network.telemetry_service_factory(),
        credential_resolver=resolver,
    )
    try:
        for broker in runtime.status()["brokers"]:
            if broker["broker_ref"] == "zendure_cloud":
                return broker
        return None
    finally:
        runtime.stop()


# --- Core resolves the persisted cloud runtime record --------------------


def test_cloud_runtime_resolves_persisted_credentials_without_admin(secrets_dir, tmp_path):
    _save_cloud_runtime_secret(tmp_path)
    resolver = FileMqttCredentialResolver(secrets_dir)
    config = _cloud_config()

    cfg, errors = _cloud_broker_config(config, resolver)
    assert errors == {}
    assert cfg is not None
    assert cfg.username == CLOUD_USER
    assert cfg.password == CLOUD_PASS
    assert cfg.client_id == CLOUD_CLIENT
    assert cfg.app_key == CLOUD_APP_KEY

    summary = _cloud_summary(config, resolver)
    assert summary is not None
    assert summary["issue"] != "broker_auth_missing"
    assert summary["issue"] is None


# --- Restart works without Admin -----------------------------------------


def test_cloud_runtime_restart_reuses_persisted_secret(secrets_dir, tmp_path):
    _save_cloud_runtime_secret(tmp_path)
    config = _cloud_config()
    # Two independent Core resolvers over the same on-disk secret == two EMS
    # starts with no Admin process in between.
    first, _ = _cloud_broker_config(config, FileMqttCredentialResolver(secrets_dir))
    second, _ = _cloud_broker_config(config, FileMqttCredentialResolver(secrets_dir))
    assert (first.username, first.password, first.client_id, first.app_key) == (
        second.username,
        second.password,
        second.client_id,
        second.app_key,
    )


# --- Rotation replaces the runtime secret transactionally ----------------


def test_cloud_credential_rotation_replaces_material(secrets_dir, tmp_path):
    _save_cloud_runtime_secret(tmp_path)
    config = _cloud_config()
    before, _ = _cloud_broker_config(config, FileMqttCredentialResolver(secrets_dir))
    assert before.password == CLOUD_PASS

    _save_cloud_runtime_secret(tmp_path, password="rotated-pass-SECRET2", app_key="rotated-app-SECRET2")
    after, _ = _cloud_broker_config(config, FileMqttCredentialResolver(secrets_dir))
    assert after.password == "rotated-pass-SECRET2"
    assert after.app_key == "rotated-app-SECRET2"
    # The config referencing the record never changed.
    assert config["zendure_mqtt"]["brokers"]["zendure_cloud"]["credentials_ref"] == CLOUD_REF


# --- Missing record: honest broker_auth_missing, never a crash -----------


def test_missing_cloud_record_reports_auth_missing_not_crash(secrets_dir, tmp_path):
    # No record persisted (legacy config that stored only credentials_ref).
    resolver = FileMqttCredentialResolver(secrets_dir)
    config = _cloud_config()
    cfg, errors = _cloud_broker_config(config, resolver)
    # The broker still builds (no abort of the whole block); the runtime reports
    # the sanitized broker_auth_missing so config and runtime agree it is unusable.
    assert cfg is not None
    assert errors == {}
    summary = _cloud_summary(config, resolver)
    assert summary["issue"] == "broker_auth_missing"


# --- Secrets never leak into status or config ----------------------------


def test_cloud_secret_absent_from_status_and_config(secrets_dir, tmp_path):
    _save_cloud_runtime_secret(tmp_path)
    resolver = FileMqttCredentialResolver(secrets_dir)
    config = _cloud_config()
    network = FakeMqttNetwork()
    runtime = build_zendure_mqtt_runtime(
        config,
        service_factory=network.telemetry_service_factory(),
        credential_resolver=resolver,
    )
    try:
        status_blob = json.dumps(runtime.status())
    finally:
        runtime.stop()
    for token in SECRET_TOKENS:
        assert token not in status_blob
        assert token not in json.dumps(config)


# --- Core credential resolution has no Admin dependency ------------------


def test_core_credential_module_has_no_admin_import():
    import ems.mqtt_credentials as core_credentials

    source = __import__("inspect").getsource(core_credentials)
    assert "import admin" not in source
    assert "from admin" not in source
