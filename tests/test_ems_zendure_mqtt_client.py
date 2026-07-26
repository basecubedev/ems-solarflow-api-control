# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the read-only Zendure MQTT client.

Deterministic and broker-free: a fake paho-style client is injected so the
subscribe/connect/lifecycle behaviour is asserted without a network broker.
"""

import json
import logging

import pytest

from ems.zendure_mqtt import (
    DEFAULT_LOCAL_SUBSCRIPTIONS,
    ZendureMqttClientConfig,
    ZendureMqttClientError,
    ZendureMqttReadClient,
)


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


class FakeMqttClient:
    """Minimal paho-style stand-in that records calls and delivers messages."""

    def __init__(self, *, fail_connect=False):
        self.fail_connect = fail_connect
        self.connect_timeout = None
        self.subscriptions = []
        self.tls_set_called = False
        self.tls_set_calls = []
        self.tls_insecure_called = False
        self.tls_insecure_calls = []
        self.call_order = []
        self.username_pw = None
        self.connected_args = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.publish_calls = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def tls_set(self, *args, **kwargs):
        self.tls_set_called = True
        self.tls_set_calls.append((args, kwargs))
        self.call_order.append("tls_set")

    def tls_insecure_set(self, value):
        self.tls_insecure_called = bool(value)
        self.tls_insecure_calls.append(bool(value))
        self.call_order.append("tls_insecure_set")

    def username_pw_set(self, username, password=None):
        self.username_pw = (username, password)

    def connect(self, host, port, keepalive=0):
        self.call_order.append("connect")
        if self.fail_connect:
            raise OSError("connection refused")
        self.connected_args = (host, port, keepalive)

    def loop_start(self):
        self.loop_started = True
        if self.on_connect is not None:
            self.on_connect(self, None, None, 0)

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)

    def publish(self, *args, **kwargs):  # pragma: no cover - must never be called
        self.publish_calls.append((args, kwargs))

    def deliver(self, topic, payload):
        self.on_message(self, None, FakeMessage(topic, payload))


def _build(**kwargs):
    config = ZendureMqttClientConfig(host="broker.local", **kwargs)
    fake = FakeMqttClient()
    client = ZendureMqttReadClient(config, client_factory=lambda _cfg: fake)
    return client, fake


def test_default_subscriptions_are_known_local_families():
    client, fake = _build()
    client.start()
    assert tuple(fake.subscriptions) == DEFAULT_LOCAL_SUBSCRIPTIONS
    assert "#" not in fake.subscriptions


def test_app_key_adds_cloud_family_without_global_wildcard():
    client, fake = _build(app_key="secretAppKey")
    client.start()
    assert "secretAppKey/#" in fake.subscriptions
    assert "#" not in fake.subscriptions
    assert set(DEFAULT_LOCAL_SUBSCRIPTIONS).issubset(set(fake.subscriptions))


def test_subscribe_failure_log_does_not_expose_raw_cloud_topic(caplog):
    topic = "iot/PRODUCT_SECRET/ACCOUNT_ROUTE_1234/#"
    client, fake = _build(subscriptions=(topic,))

    def reject_subscribe(_topic, qos=0):
        raise OSError("subscribe failed")

    fake.subscribe = reject_subscribe
    with caplog.at_level(logging.DEBUG, logger="ems.zendure_mqtt.client"):
        client.start()

    assert "event=zendure_mqtt_subscribe_failed" in caplog.text
    assert topic not in caplog.text
    assert "PRODUCT_SECRET" not in caplog.text
    assert "ACCOUNT_ROUTE_1234" not in caplog.text


def test_zensdk_scalar_topic_updates_snapshot():
    client, fake = _build()
    client.start()
    fake.deliver("Zendure/sensor/DEV123/electricLevel", "43")
    snapshots = client.snapshots()
    assert "DEV123" in snapshots
    assert snapshots["DEV123"].metrics["electricLevel"] == 43


def test_legacy_iot_report_updates_snapshot():
    client, fake = _build()
    client.start()
    payload = json.dumps({"sn": "SN9", "properties": {"outputLimit": 301}})
    fake.deliver("iot/prodKey/DEVLEG/properties/report", payload)
    snapshots = client.snapshots()
    assert snapshots["DEVLEG"].metrics["outputLimit"] == 301
    assert snapshots["DEVLEG"].serial_number == "SN9"


def test_legacy_slash_topic_updates_snapshot():
    client, fake = _build()
    client.start()
    payload = json.dumps({"properties": {"solarInputPower": 620}})
    fake.deliver("/prodKey/DEVSLASH/properties/report", payload)
    snapshots = client.snapshots()
    assert snapshots["DEVSLASH"].metrics["solarInputPower"] == 620


def test_malformed_json_does_not_crash():
    client, fake = _build()
    client.start()
    fake.deliver("iot/prodKey/DEVBAD/properties/report", b"\xff\xfe not json {")
    # Snapshot exists from the topic; no exception propagated.
    assert "DEVBAD" in client.snapshots()


def test_unknown_topic_does_not_crash_or_create_snapshot():
    client, fake = _build()
    client.start()
    fake.deliver("some/random/write/topic", "whatever")
    assert client.snapshots() == {}


def test_password_not_exposed_in_repr_or_errors():
    secret = "sup3r-secret-pw"
    config = ZendureMqttClientConfig(
        host="broker.local", username="user", password=secret
    )
    assert secret not in repr(config)
    fake = FakeMqttClient(fail_connect=True)
    client = ZendureMqttReadClient(config, client_factory=lambda _cfg: fake)
    with pytest.raises(ZendureMqttClientError) as excinfo:
        client.start()
    message = str(excinfo.value)
    assert secret not in message
    assert "user" not in message
    assert "broker.local" in message


def test_stop_is_idempotent():
    client, fake = _build()
    client.start()
    client.stop()
    client.stop()
    assert fake.loop_stopped and fake.disconnected


def test_read_client_never_publishes():
    client, fake = _build()
    client.start()
    fake.deliver("Zendure/sensor/DEV123/electricLevel", "43")
    client.stop()
    assert fake.publish_calls == []
    assert not hasattr(client, "publish")


def test_connect_timeout_applied_to_client():
    client, fake = _build(connect_timeout_seconds=4.5)
    client.start()
    assert fake.connect_timeout == 4.5


def test_credentials_applied_and_tls_configured():
    client, fake = _build(username="u", password="p", tls=True, tls_insecure=True)
    client.start()
    assert fake.username_pw == ("u", "p")
    assert fake.tls_set_called and fake.tls_insecure_called


def test_tls_insecure_disables_chain_and_hostname_verification():
    # The Zendure cloud broker presents a self-signed chain: insecure mode must
    # skip certificate-chain verification (CERT_NONE), not only hostname checks.
    import ssl

    client, fake = _build(tls=True, tls_insecure=True)
    client.start()
    assert fake.tls_set_calls == [((), {"cert_reqs": ssl.CERT_NONE})]
    assert fake.tls_insecure_calls == [True]
    assert fake.call_order.index("tls_set") < fake.call_order.index("connect")


def test_tls_without_insecure_keeps_full_verification():
    client, fake = _build(tls=True)
    client.start()
    assert fake.tls_set_calls == [((), {})]
    assert fake.tls_insecure_calls == []
    assert fake.call_order.index("tls_set") < fake.call_order.index("connect")
