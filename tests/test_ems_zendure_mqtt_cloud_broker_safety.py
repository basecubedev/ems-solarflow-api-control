# SPDX-License-Identifier: AGPL-3.0-or-later
"""An unusable Zendure MQTT broker must never open a client connection.

A cloud broker whose runtime credential cannot be resolved reports
``broker_auth_missing``. Such a broker (and any broker with an unusable profile)
must never construct or start an MQTT client: without credentials it would
otherwise dial the Zendure production broker anonymously and sit in a reconnect
loop. A fully usable broker still starts normally.
"""

import base64
import json

import pytest

from admin.credential_store import CredentialStore
from ems.mqtt_credentials import FileMqttCredentialResolver
from ems.zendure_mqtt.runtime import (
    build_zendure_mqtt_runtime,
    load_zendure_mqtt_broker_configs,
)

pytestmark = [pytest.mark.simulation, pytest.mark.power_control]


class _RecordingService:
    """Fake telemetry service that records whether it was ever started."""

    def __init__(self, config):
        self.config = config
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def status(self):
        return {}

    def snapshots(self):
        return {}


def _factory(registry):
    def make(config):
        service = _RecordingService(config)
        registry.append(service)
        return service
    return make


def _cloud_config(credentials_ref="zendure-cloud"):
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
                "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
            },
        ],
    }


def _local_config():
    return {
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                "local_mqtt": {
                    "enabled": True,
                    "source": "local_mqtt",
                    "host": "10.0.0.10",
                    "port": 1883,
                },
            },
        },
        "devices": [
            {
                "type": "zendure_mqtt",
                "name": "Local SolarFlow",
                "serial_number": "LOCALDEV",
                "mqtt": {
                    "broker_ref": "local_mqtt",
                    "topic_family": "zensdk_ha_scalar",
                    "base_topic": "Zendure",
                    "device_id": "LOCALDEV",
                },
                "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
            },
        ],
    }


def test_unusable_cloud_broker_never_starts_a_client(tmp_path):
    # No credential record exists, so the cloud broker is broker_auth_missing.
    resolver = FileMqttCredentialResolver(tmp_path / "secrets")
    services = []
    runtime = build_zendure_mqtt_runtime(
        _cloud_config(),
        service_factory=_factory(services),
        credential_resolver=resolver,
    )
    try:
        # The broker reports the sanitized issue...
        summary = next(
            b for b in runtime.status()["brokers"] if b["broker_ref"] == "zendure_cloud"
        )
        assert summary["issue"] == "broker_auth_missing"
        # ...and starting the runtime must not open a client for it.
        runtime.start()
        assert all(not s.started for s in services), "unusable broker must not dial"
    finally:
        runtime.stop()


def test_usable_local_broker_still_starts(tmp_path):
    resolver = FileMqttCredentialResolver(tmp_path / "secrets")
    services = []
    runtime = build_zendure_mqtt_runtime(
        _local_config(),
        service_factory=_factory(services),
        credential_resolver=resolver,
    )
    try:
        runtime.start()
        assert services and all(s.started for s in services), "usable broker must start"
    finally:
        runtime.stop()


# --- Complete-cloud-credential contract ----------------------------------
#
# A Zendure cloud MQTT profile is usable only when its resolved runtime
# credential carries all four cloud fields (username/password/client_id/
# app_key). A record missing or blank in any one connects but can never
# subscribe to the ``<app_key>/#`` telemetry tree, so it must never start.

_SECRETS_SUBDIR = ("config", "secrets")

CLOUD_USER = "cloud-user"
CLOUD_PASS = "cloud-pass-SECRET"
CLOUD_CLIENT = "cloud-client-id"
CLOUD_APP_KEY = "cloud-app-key-SECRET"


def _secrets_dir(tmp_path):
    return tmp_path.joinpath(*_SECRETS_SUBDIR)


def _save_cloud_secret(tmp_path, ref="zendure-cloud", **fields):
    store = CredentialStore(config_dir=str(tmp_path / "config"))
    return store.save_mqtt_cloud_runtime_secret(ref, **fields)


def _write_raw_cloud_record(tmp_path, ref="zendure-cloud", **fields):
    """Write a partial cloud credential record straight to disk.

    Bypasses the store's username/password pairing so a record can omit or
    blank any single field, exercising exactly what the resolver may otherwise
    treat as present. Values are stored base64-encoded (``_encrypted: False``),
    the format the Core resolver reads.
    """

    secrets = _secrets_dir(tmp_path)
    secrets.mkdir(parents=True, exist_ok=True)
    record = {"version": 1, "ref": ref, "source": "zendure_cloud_mqtt"}
    for name, value in fields.items():
        if value is None:
            continue
        record[name] = base64.b64encode(value.encode("utf-8")).decode("ascii")
        record[f"{name}_encrypted"] = False
    (secrets / f"mqtt-{ref}.json").write_text(json.dumps(record), encoding="utf-8")
    return ref


def _start_runtime(tmp_path, config):
    resolver = FileMqttCredentialResolver(_secrets_dir(tmp_path))
    services = []
    runtime = build_zendure_mqtt_runtime(
        config,
        service_factory=_factory(services),
        credential_resolver=resolver,
    )
    return runtime, services


def _broker_summary(runtime, broker_ref="zendure_cloud"):
    return next(
        b for b in runtime.status()["brokers"] if b["broker_ref"] == broker_ref
    )


def _mixed_config():
    config = _cloud_config()
    config["zendure_mqtt"]["brokers"]["local_mqtt"] = {
        "enabled": True,
        "source": "local_mqtt",
        "host": "10.0.0.10",
        "port": 1883,
    }
    config["devices"].append(
        {
            "type": "zendure_mqtt",
            "name": "Local SolarFlow",
            "serial_number": "LOCALDEV",
            "mqtt": {
                "broker_ref": "local_mqtt",
                "topic_family": "zensdk_ha_scalar",
                "base_topic": "Zendure",
                "device_id": "LOCALDEV",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": False,
            },
        }
    )
    return config


def _assert_cloud_unusable(runtime, services):
    assert _broker_summary(runtime)["issue"] == "broker_auth_missing"
    runtime.start()
    started = [s for s in services if s.started]
    assert not started, "incomplete cloud broker must never start a client"


# Contract A — a record missing app_key is unusable.
def test_cloud_missing_app_key_is_unusable(tmp_path):
    _save_cloud_secret(
        tmp_path, username=CLOUD_USER, password=CLOUD_PASS, client_id=CLOUD_CLIENT
    )
    runtime, services = _start_runtime(tmp_path, _cloud_config())
    try:
        _assert_cloud_unusable(runtime, services)
    finally:
        runtime.stop()


# Contract B — a record missing client_id is unusable.
def test_cloud_missing_client_id_is_unusable(tmp_path):
    _save_cloud_secret(
        tmp_path, username=CLOUD_USER, password=CLOUD_PASS, app_key=CLOUD_APP_KEY
    )
    runtime, services = _start_runtime(tmp_path, _cloud_config())
    try:
        _assert_cloud_unusable(runtime, services)
    finally:
        runtime.stop()


# Contract C — a record missing username is unusable.
def test_cloud_missing_username_is_unusable(tmp_path):
    _write_raw_cloud_record(
        tmp_path, password=CLOUD_PASS, client_id=CLOUD_CLIENT, app_key=CLOUD_APP_KEY
    )
    runtime, services = _start_runtime(tmp_path, _cloud_config())
    try:
        _assert_cloud_unusable(runtime, services)
    finally:
        runtime.stop()


# Contract D — a record missing password is unusable.
def test_cloud_missing_password_is_unusable(tmp_path):
    _write_raw_cloud_record(
        tmp_path, username=CLOUD_USER, client_id=CLOUD_CLIENT, app_key=CLOUD_APP_KEY
    )
    runtime, services = _start_runtime(tmp_path, _cloud_config())
    try:
        _assert_cloud_unusable(runtime, services)
    finally:
        runtime.stop()


# Contract E — empty or whitespace-only values are treated as missing.
@pytest.mark.parametrize("field", ["username", "password", "client_id", "app_key"])
@pytest.mark.parametrize("value", ["", "   "])
def test_cloud_blank_field_is_unusable(tmp_path, field, value):
    fields = {
        "username": CLOUD_USER,
        "password": CLOUD_PASS,
        "client_id": CLOUD_CLIENT,
        "app_key": CLOUD_APP_KEY,
    }
    fields[field] = value
    _write_raw_cloud_record(tmp_path, **fields)
    runtime, services = _start_runtime(tmp_path, _cloud_config())
    try:
        _assert_cloud_unusable(runtime, services)
    finally:
        runtime.stop()


# Contract F — a complete record is usable, starts, and subscribes to the tree.
def test_complete_cloud_record_starts_and_subscribes(tmp_path):
    _save_cloud_secret(
        tmp_path,
        username=CLOUD_USER,
        password=CLOUD_PASS,
        client_id=CLOUD_CLIENT,
        app_key=CLOUD_APP_KEY,
    )
    config = _cloud_config()
    runtime, services = _start_runtime(tmp_path, config)
    try:
        assert _broker_summary(runtime)["issue"] is None
        runtime.start()
        assert services and all(s.started for s in services)
    finally:
        runtime.stop()

    brokers, errors, _stale = load_zendure_mqtt_broker_configs(
        config["zendure_mqtt"],
        credential_resolver=FileMqttCredentialResolver(_secrets_dir(tmp_path)),
    )
    assert errors == {}
    subscriptions = brokers["zendure_cloud"].client_config().resolved_subscriptions()
    assert f"{CLOUD_APP_KEY}/#" in subscriptions


# Contract G — a valid local broker still starts while an incomplete cloud
# broker beside it does not, and neither failure leaks across services.
def test_mixed_local_starts_while_incomplete_cloud_does_not(tmp_path):
    _save_cloud_secret(
        tmp_path, username=CLOUD_USER, password=CLOUD_PASS, client_id=CLOUD_CLIENT
    )
    runtime, services = _start_runtime(tmp_path, _mixed_config())
    try:
        runtime.start()
        started_refs = {s.config.broker_ref for s in services if s.started}
        assert "local_mqtt" in started_refs
        assert "zendure_cloud" not in started_refs
        assert _broker_summary(runtime, "local_mqtt")["issue"] is None
        assert _broker_summary(runtime, "zendure_cloud")["issue"] == "broker_auth_missing"
    finally:
        runtime.stop()
