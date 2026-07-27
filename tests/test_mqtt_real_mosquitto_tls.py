# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real Mosquitto TLS release gate with a throwaway CA and server certificate.

Deterministic and self-contained: the CA and server cert live inside the test's
temporary directory. Proves a verified connection succeeds, an untrusted server
cert is rejected, insecure-no-verify only bypasses verification when explicitly
configured, and invalid TLS metadata prevents any connection attempt.
"""

import time

import pytest

from ems.clients import MqttGridMeterClient, create_grid_meter_client
from tests.helpers.mosquitto import (
    require_real_broker_environment,
    mosquitto_tls_broker,
    publish_once,
    publish_until,
)

pytestmark = pytest.mark.docker


require_real_broker_environment("cryptography")


def _poll_power(client, expected, timeout=10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.get_power()
        if last == expected:
            return last
        time.sleep(0.05)
    return last


def test_verified_connection_succeeds_with_trusted_ca(tmp_path):
    # A verifying client that trusts the temp CA connects and receives a value.
    topic = "Zendure/sensor/D0TLS/totalPower"
    with mosquitto_tls_broker(tmp_path) as (host, port, ca_path):
        import paho.mqtt.client as mqtt

        received = []
        try:
            sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):
            sub = mqtt.Client()
        sub.tls_set(ca_certs=str(ca_path))
        sub.on_message = lambda c, u, m: received.append(m.payload.decode())
        # Subscribe from on_connect so the SUBSCRIBE is sent after CONNACK.
        sub.on_connect = lambda *a, **k: sub.subscribe(topic, qos=1)
        sub.connect(host, port, keepalive=10)
        sub.loop_start()
        try:
            publish_until(
                lambda: publish_once(host, port, topic, "-210", tls_ca=ca_path),
                lambda: received,
                message="verified TLS subscriber never received the message",
            )
            assert received[0] == "-210"
        finally:
            sub.loop_stop()
            sub.disconnect()


def test_untrusted_certificate_is_rejected(tmp_path):
    # The grid-meter client verifies against the system trust store, which does
    # not contain the temp CA, so the TLS handshake never authenticates.
    topic = "Zendure/sensor/D0UNTRUST/totalPower"
    with mosquitto_tls_broker(tmp_path) as (host, port, ca_path):
        client = MqttGridMeterClient(
            host, port, topic, payload_format="number", tls=True, tls_insecure=False
        )
        try:
            # No trusted CA, so the handshake never authenticates: the published
            # value can never be read, bounded by the poll timeout.
            publish_once(host, port, topic, "-9", tls_ca=ca_path)
            assert _poll_power(client, -9.0, timeout=2.0) != -9.0
        finally:
            client.close()


def test_insecure_no_verify_succeeds_only_when_configured(tmp_path, monkeypatch):
    # The server cert is CA-valid but its SAN does not match 127.0.0.1. With the
    # CA trusted, a verifying client still rejects the hostname mismatch, while an
    # explicitly insecure (no-verify) client tolerates it — proving no-verify only
    # relaxes verification when configured.
    topic = "Zendure/sensor/D0INSEC/totalPower"
    with mosquitto_tls_broker(tmp_path, san_dns="wrong.host.invalid") as (
        host,
        port,
        ca_path,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))

        strict = MqttGridMeterClient(
            host, port, topic, payload_format="number", tls=True, tls_insecure=False
        )
        try:
            # A verifying client rejects the hostname mismatch, so it never reads.
            publish_once(host, port, topic, "-5", tls_ca=ca_path, tls_insecure=True)
            assert _poll_power(strict, -5.0, timeout=2.0) != -5.0
        finally:
            strict.close()

        insecure = MqttGridMeterClient(
            host, port, topic, payload_format="number", tls=True, tls_insecure=True
        )
        try:
            publish_until(
                lambda: publish_once(host, port, topic, "-64", tls_ca=ca_path, tls_insecure=True),
                lambda: insecure.get_power() == -64.0,
                message="insecure TLS client never received -64",
            )
        finally:
            insecure.close()


def test_insecure_no_verify_tolerates_untrusted_chain(tmp_path):
    # The Zendure cloud broker presents a self-signed chain that no system CA
    # validates. With tls_insecure the grid-meter client must still connect and
    # read — chain verification is skipped, not only the hostname check. The CA
    # is deliberately NOT trusted here (no SSL_CERT_FILE), unlike the wrong-SAN
    # scenario above.
    topic = "Zendure/sensor/D0CHAIN/totalPower"
    with mosquitto_tls_broker(tmp_path) as (host, port, ca_path):
        insecure = MqttGridMeterClient(
            host, port, topic, payload_format="number", tls=True, tls_insecure=True
        )
        try:
            publish_until(
                lambda: publish_once(
                    host, port, topic, "-77", tls_ca=ca_path, tls_insecure=True
                ),
                lambda: insecure.get_power() == -77.0,
                message="insecure TLS client never received -77 over an untrusted chain",
            )
        finally:
            insecure.close()


def test_zendure_read_client_insecure_tolerates_untrusted_chain(tmp_path):
    # Same contract for the Zendure MQTT read client (the control client
    # inherits this connect path): tls_insecure must tolerate a self-signed,
    # untrusted broker chain and deliver telemetry snapshots.
    from ems.zendure_mqtt import ZendureMqttClientConfig, ZendureMqttReadClient

    topic = "Zendure/sensor/DEVTLS/electricLevel"
    with mosquitto_tls_broker(tmp_path) as (host, port, ca_path):
        client = ZendureMqttReadClient(
            ZendureMqttClientConfig(
                host=host, port=port, tls=True, tls_insecure=True
            )
        )
        client.start()
        try:
            publish_until(
                lambda: publish_once(
                    host, port, topic, "55", tls_ca=ca_path, tls_insecure=True
                ),
                lambda: "DEVTLS" in client.snapshots(),
                message="zendure read client never received telemetry over an untrusted chain",
            )
            assert client.snapshots()["DEVTLS"].metrics["electricLevel"] == 55
        finally:
            client.stop()


def test_invalid_tls_metadata_prevents_connection_attempt():
    # A contradictory TLS flag/mode is rejected before any client is built.
    with pytest.raises(ValueError):
        create_grid_meter_client(
            {
                "type": "mqtt",
                "mqtt": {
                    "host": "broker.local",
                    "topic": "meter/power",
                    "tls": False,
                    "tls_mode": "system_ca",
                },
            },
            object(),
        )
