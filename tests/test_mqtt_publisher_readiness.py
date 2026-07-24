# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helper-level contract: the real-broker publisher waits for CONNACK.

Paho's network loop delivers CONNACK asynchronously, so publishing straight
after ``connect()`` can race the connection and fail with ``MQTT_ERR_NO_CONN``
on a slow runner. These tests drive the readiness protocol against a fake Paho
client (no Docker, no network) to prove a publish is never attempted before a
successful connection and that every failure mode is bounded and actionable.
"""

import threading
import time

import pytest

from tests.helpers.mosquitto import (
    PublishReadinessError,
    _await_connack_and_publish,
)

pytestmark = pytest.mark.simulation

pytest.importorskip("paho.mqtt.client")


class _FakeInfo:
    def __init__(self, rc, *, published=True):
        self.rc = rc
        self._published = published

    def wait_for_publish(self, timeout=None):
        return None

    def is_published(self):
        return self._published


class _FakePahoClient:
    """A minimal Paho stand-in that delivers CONNACK on its own timer.

    Records every lifecycle call in ``events`` so a test can assert ordering:
    a publish must never appear before ``on_connect``.
    """

    def __init__(self, *, connack_delay=0.0, connack_reason=0,
                 deliver_connack=True, connected_after_connack=True,
                 publish_rc=0, published=True):
        self.events = []
        self._connack_delay = connack_delay
        self._connack_reason = connack_reason
        self._deliver_connack = deliver_connack
        self._connected_after_connack = connected_after_connack
        self._publish_rc = publish_rc
        self._published = published
        self.on_connect = None
        self._timer = None

    def connect(self, host, port, keepalive=10):
        self.events.append("connect")

    def loop_start(self):
        self.events.append("loop_start")
        if not self._deliver_connack:
            return

        def _fire():
            if self.on_connect is not None:
                self.events.append("on_connect")
                self.on_connect(self, None, {}, self._connack_reason, None)

        self._timer = threading.Timer(self._connack_delay, _fire)
        self._timer.daemon = True
        self._timer.start()

    def is_connected(self):
        return self._connected_after_connack

    def publish(self, topic, payload, qos=1):
        self.events.append("publish")
        return _FakeInfo(self._publish_rc, published=self._published)

    def loop_stop(self):
        self.events.append("loop_stop")
        if self._timer is not None:
            self._timer.cancel()

    def disconnect(self):
        self.events.append("disconnect")


def _publish(client, **kwargs):
    kwargs.setdefault("host", "127.0.0.1")
    kwargs.setdefault("port", 1883)
    kwargs.setdefault("topic", "Zendure/sensor/D0/totalPower")
    kwargs.setdefault("payload", "-357")
    kwargs.setdefault("timeout", 2.0)
    return _await_connack_and_publish(client, **kwargs)


def test_publish_waits_for_delayed_connack_before_publishing():
    client = _FakePahoClient(connack_delay=0.2)

    _publish(client)

    assert "on_connect" in client.events
    assert "publish" in client.events
    # The publish must come strictly after the connection was acknowledged.
    assert client.events.index("on_connect") < client.events.index("publish")
    # And the network loop is always torn down.
    assert client.events.index("loop_stop") > client.events.index("publish")


def test_publish_proceeds_immediately_on_successful_connack():
    client = _FakePahoClient(connack_delay=0.0)

    _publish(client)

    assert "publish" in client.events


def test_connect_timeout_fails_bounded_without_publishing():
    client = _FakePahoClient(deliver_connack=False)

    start = time.monotonic()
    with pytest.raises(PublishReadinessError) as caught:
        _publish(client, timeout=0.3)
    elapsed = time.monotonic() - start

    assert "did not acknowledge" in str(caught.value)
    assert "publish" not in client.events
    assert elapsed < 2.0  # bounded by the readiness timeout, not the caller


def test_non_success_connack_fails_bounded_without_publishing():
    # Reason code 5 (not authorized) as an integer, exercising the v1 mapping.
    client = _FakePahoClient(connack_reason=5)

    with pytest.raises(PublishReadinessError) as caught:
        _publish(client)

    assert "refused the connection" in str(caught.value)
    assert "connect_rc=5" in str(caught.value)
    assert "publish" not in client.events


def test_disconnect_before_publish_fails_with_connection_state():
    client = _FakePahoClient(connected_after_connack=False)

    with pytest.raises(PublishReadinessError) as caught:
        _publish(client)

    assert "disconnected before publish" in str(caught.value)
    assert "publish" not in client.events


def test_rejected_publish_reports_publish_reason_code():
    client = _FakePahoClient(publish_rc=4)

    with pytest.raises(PublishReadinessError) as caught:
        _publish(client)

    assert "rejected the publish" in str(caught.value)
    assert "publish_rc=4" in str(caught.value)


def test_readiness_error_contains_only_nonsecret_diagnostics():
    # Credentials are applied to the client before the readiness protocol runs,
    # so they can never reach the diagnostics: the error names only endpoint,
    # topic and reason codes.
    client = _FakePahoClient(deliver_connack=False)

    with pytest.raises(PublishReadinessError) as caught:
        _publish(client, host="broker.test", port=8883, topic="t/p", timeout=0.2)

    message = str(caught.value)
    assert "host=broker.test" in message
    assert "port=8883" in message
    assert "topic=t/p" in message
    assert "connect_rc=" in message
    # Nothing resembling a credential field is present.
    assert "password" not in message
    assert "username" not in message
