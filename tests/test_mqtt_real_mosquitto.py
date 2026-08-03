# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional real-broker integration against ephemeral Mosquitto containers.

Docker-marked and skipped cleanly without Docker. These exercise the actual paho
network path (subscription, auth, broker isolation) that the fake harness cannot,
using random host ports and guaranteed container cleanup. They are not part of
the fast default simulation subset.
"""

import pytest

from ems.clients import MqttGridMeterClient, create_grid_meter_client
from ems.mqtt_credentials import FileMqttCredentialResolver, MqttCredentialError
from tests.helpers.mosquitto import (
    require_real_broker_environment,
    mosquitto_broker,
    publish_once,
    publish_until,
    wait_until,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.e2e,
    pytest.mark.docker,
]


require_real_broker_environment()


def _reads(client, expected):
    return lambda: client.get_power() == expected


def _wait_connect_error(client, timeout=5.0):
    return wait_until(
        lambda: getattr(client, "_connect_error", None) is not None,
        timeout=timeout,
        message="client never reported a connection failure",
    )


# --- Mosquitto 01: Anonymous plain MQTT -------------------------------------
def test_anonymous_broker_receives_d0_total_power(tmp_path):
    topic = "Zendure/sensor/D0REAL/totalPower"
    with mosquitto_broker(tmp_path) as (host, port):
        client = MqttGridMeterClient(host, port, topic, payload_format="number")
        try:
            publish_until(
                lambda: publish_once(host, port, topic, "-357"),
                _reads(client, -357.0),
                message="grid meter never received -357",
            )
        finally:
            client.close()


# --- Mosquitto 02: Authenticated broker -------------------------------------
def test_authenticated_broker_requires_correct_credentials(tmp_path):
    topic = "Zendure/sensor/D0AUTH/totalPower"
    user, pw = "emsuser", "emspass"
    with mosquitto_broker(tmp_path, username=user, password=pw) as (host, port):
        good = MqttGridMeterClient(host, port, topic, username=user, password=pw,
                                   payload_format="number")
        try:
            publish_until(
                lambda: publish_once(host, port, topic, "-120", username=user, password=pw),
                _reads(good, -120.0),
                message="authenticated grid meter never received -120",
            )
        finally:
            good.close()

        # Wrong credentials never connect and never expose the secret.
        bad = MqttGridMeterClient(host, port, topic, username=user,
                                  password="wrong", payload_format="number")
        try:
            _wait_connect_error(bad)
            assert bad.get_power() == 0  # no value read on a failed auth
            assert "emspass" not in repr(getattr(bad, "_connect_error", ""))
        finally:
            bad.close()


# --- Mosquitto 03: Two independent brokers ----------------------------------
def test_two_brokers_stay_isolated(tmp_path):
    topic_a = "Zendure/sensor/D0A/totalPower"
    topic_b = "Zendure/sensor/D0B/totalPower"
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    with mosquitto_broker(dir_a) as (host_a, port_a), mosquitto_broker(dir_b) as (
        host_b, port_b
    ):
        client_a = MqttGridMeterClient(host_a, port_a, topic_a, payload_format="number")
        client_b = MqttGridMeterClient(host_b, port_b, topic_b, payload_format="number")
        try:
            # A message on broker A is only observed through broker A.
            publish_until(
                lambda: publish_once(host_a, port_a, topic_a, "-500"),
                _reads(client_a, -500.0),
                message="broker A message never observed on client A",
            )
            # Broker B never saw broker A's device.
            assert client_b.get_power() == 0
            # And vice versa.
            publish_until(
                lambda: publish_once(host_b, port_b, topic_b, "-900"),
                _reads(client_b, -900.0),
                message="broker B message never observed on client B",
            )
        finally:
            client_a.close()
            client_b.close()


# --- Mosquitto 04: credentials_ref resolves to a real authenticated client ---
def _save_secret(secrets_dir, ref, username, password):
    from admin.credential_store import CredentialStore

    store = CredentialStore(config_dir=secrets_dir.parent)
    store.save_mqtt_broker_secret(ref, username, password)


def _grid_meter_config(host, port, topic, ref):
    # Config carries only a credentials_ref — no inline username/password.
    return {
        "type": "zendure_smartmeter_d0",
        "mqtt": {
            "host": host,
            "port": port,
            "topic": topic,
            "credentials_ref": ref,
            "payload_format": "number",
        },
    }


def test_credentials_ref_authenticates_via_production_factory(tmp_path):
    topic = "Zendure/sensor/D0REF/totalPower"
    user, pw = "refuser", "refpass"
    secrets_dir = tmp_path / "config" / "secrets"
    secrets_dir.mkdir(parents=True)
    _save_secret(secrets_dir, "home", user, pw)
    resolver = FileMqttCredentialResolver(secrets_dir)

    with mosquitto_broker(tmp_path, username=user, password=pw) as (host, port):
        client = create_grid_meter_client(
            _grid_meter_config(host, port, topic, "home"),
            object(),
            mqtt_credential_resolver=resolver,
        )
        try:
            publish_until(
                lambda: publish_once(host, port, topic, "-321", username=user, password=pw),
                _reads(client, -321.0),
                message="credentials_ref grid meter never received -321",
            )
        finally:
            client.close()


def test_credentials_ref_unknown_fails_without_leaking(tmp_path):
    secrets_dir = tmp_path / "config" / "secrets"
    secrets_dir.mkdir(parents=True)
    resolver = FileMqttCredentialResolver(secrets_dir)
    with pytest.raises(MqttCredentialError) as caught:
        create_grid_meter_client(
            _grid_meter_config("127.0.0.1", 1883, "t/p", "missing"),
            object(),
            mqtt_credential_resolver=resolver,
        )
    assert "was not found" in str(caught.value) or "not found" in str(caught.value)


def test_credentials_ref_wrong_secret_never_reads_and_hides_password(tmp_path):
    topic = "Zendure/sensor/D0WRONG/totalPower"
    secrets_dir = tmp_path / "config" / "secrets"
    secrets_dir.mkdir(parents=True)
    # Store a credential that does not match the broker's real account.
    _save_secret(secrets_dir, "home", "refuser", "the-wrong-password")
    resolver = FileMqttCredentialResolver(secrets_dir)

    with mosquitto_broker(tmp_path, username="refuser", password="realpass") as (
        host,
        port,
    ):
        client = create_grid_meter_client(
            _grid_meter_config(host, port, topic, "home"),
            object(),
            mqtt_credential_resolver=resolver,
        )
        try:
            # Wrong stored credentials never authenticate, so no value is read.
            _wait_connect_error(client)
            publish_once(host, port, topic, "-77", username="refuser", password="realpass")
            assert client.get_power() != -77.0
            assert "the-wrong-password" not in repr(getattr(client, "_connect_error", ""))
        finally:
            client.close()
