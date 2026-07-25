# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish metadata and broker-delivery state for MQTT control writes.

Every control/property publish must reach the paho client with the builder's
QoS (1) and retain (False) unchanged, and the command record must distinguish
local submission (rc==0) from observed broker delivery (PUBACK/on_publish),
with a bounded, observational delivery timeout that never blocks the loop.
"""

from types import SimpleNamespace

import pytest

from ems.zendure_mqtt.control import (
    PublishSubmission,
    ZendureMqttControlClient,
    ZendureMqttControlService,
)
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.service import ZendureMqttRuntimeConfig
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON
from ems.zendure_mqtt.write_protocols import CONTROL_PUBLISH_QOS

pytestmark = [pytest.mark.simulation, pytest.mark.power_control]


class FakePahoClient:
    def __init__(self):
        self.publish_calls = []
        self.subscriptions = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.on_publish = None
        self.next_mid = 0
        self.fail_publish = False

    def tls_set(self, *a, **k):
        pass

    def tls_insecure_set(self, v):
        pass

    def username_pw_set(self, u, p=None):
        pass

    def connect(self, host, port, keepalive=0):
        pass

    def loop_start(self):
        if self.on_connect is not None:
            self.on_connect(self, None, None, 0)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        self.publish_calls.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )
        if self.fail_publish:
            return SimpleNamespace(rc=1, mid=None)
        self.next_mid += 1
        return SimpleNamespace(rc=0, mid=self.next_mid)

    def deliver(self):
        """Simulate the broker acknowledging the most recent publish (PUBACK)."""

        if self.on_publish is not None:
            self.on_publish(self, None, self.next_mid)


def _service(started=True):
    fake = FakePahoClient()
    runtime = ZendureMqttRuntimeConfig(enabled=True, host="broker.local")
    service = ZendureMqttControlService(
        runtime,
        read_client_factory=lambda _cfg: ZendureMqttControlClient(
            _cfg, client_factory=lambda _c: fake
        ),
    )
    if started:
        service.start()
    return service, fake


def _device(service, hardware_profile="solarflow_800_pro_2", **kwargs):
    return ZendureMqttDeviceClient(
        "INV",
        service,
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="zendure_cloud_mqtt",
        product_key="PK",
        hardware_profile=hardware_profile,
        max_power=2000,
        **kwargs,
    )


# --- metadata reaches paho unchanged -----------------------------------------


@pytest.mark.parametrize("profile", ["solarflow_800_pro_2", "hyper_2000", "hub_2000"])
def test_every_power_publish_is_qos1_never_retained(profile):
    service, fake = _service()
    dev = _device(service, hardware_profile=profile)
    assert dev.write_output_limit(300) is True
    call = fake.publish_calls[-1]
    assert call["qos"] == CONTROL_PUBLISH_QOS == 1
    assert call["retain"] is False


def test_property_write_is_qos1_never_retained():
    service, fake = _service()
    dev = _device(service)
    assert bool(dev.write_properties({"acMode": 2}, reason="restore")) is True
    call = fake.publish_calls[-1]
    assert call["qos"] == 1
    assert call["retain"] is False


# --- delivery lifecycle ------------------------------------------------------


def test_delivery_confirmed_after_broker_ack():
    service, fake = _service()
    dev = _device(service)
    dev.write_output_limit(300)
    record = dev._active_command
    assert record.broker_delivery == "pending"
    fake.deliver()
    dev.describe(now_monotonic=record.published_monotonic + 0.5)
    assert record.broker_delivery == "delivered"
    assert record.delivered_monotonic is not None
    # Broker delivery is NOT device acceptance: the command stays unconfirmed.
    assert record.state == "published"


def test_delivery_timeout_is_observational_not_terminal():
    service, fake = _service()
    dev = _device(
        service, command_ack_timeout_seconds=5.0, confirmation_timeout_seconds=30.0
    )
    dev.write_output_limit(300)
    record = dev._active_command
    dev.describe(now_monotonic=record.published_monotonic + 6.0)
    assert record.broker_delivery == "timeout"
    # The command itself is still governed by the confirmation lifecycle.
    assert record.state == "published"
    dev.describe(now_monotonic=record.published_monotonic + 31.0)
    assert record.state == "confirmation_timed_out"


def test_publish_failure_is_a_failed_dispatch_with_no_delivery_state():
    service, fake = _service()
    fake.fail_publish = True
    dev = _device(service)
    result = dev.dispatch_output_limit(300)
    assert bool(result) is False
    assert dev._last_command.state == "rejected"
    assert dev._last_command.broker_delivery is None


def test_disconnected_client_rejects_publish_locally():
    service, _fake = _service(started=False)
    submission = service.publish_message(
        SimpleNamespace(topic="iot/PK/DEV/properties/write", payload=b"{}", qos=1, retain=False)
    )
    assert submission == PublishSubmission(False)


def test_delivered_mid_history_is_bounded():
    service, fake = _service()
    client = service._client
    for mid in range(1, 700):
        client._on_publish(fake, None, mid)
    assert client.delivery_confirmed(699) is True
    assert client.delivery_confirmed(1) is False
    assert len(client._delivered_mids) <= 512
