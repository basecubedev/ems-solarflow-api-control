# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reproduction of the live cloud-MQTT no-power-change symptom.

Live observation (SolarFlow 800 Pro 2, Zendure cloud broker): the EMS logs
``write_output_limit_published`` for changing targets, yet the inverter output
never follows. These tests model each code-level cause so the failure is
reproducible offline:

* the ZenSDK power command carried only ``outputLimit`` — a device sitting in an
  inactive mode (``smartMode=0`` / ``acMode=1``) ignores a bare setpoint; the
  reference implementation always writes the atomic mode+power property set;
* the leading-slash telemetry family selected a leading-slash *write* topic,
  while the captured hardware evidence shows devices accept commands on the
  ``iot/…`` topics only;
* QoS metadata was dropped between the message builder and the paho client, so
  every control publish went out as QoS 0 with no broker delivery evidence;
* a local paho ``rc == 0`` was the only "success" signal, indistinguishable
  from a message the broker never accepted.

These tests began life as strict xfails against the defective code and now pin
the corrected contract; the honest-behaviour tests held before and after.
"""

import json

import pytest

from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON, FAMILY_LEGACY_JSON_ALT

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]

# Telemetry captured from the live symptom: device healthy but not in AC output
# mode. A bare outputLimit write has no physical effect in this state.
INACTIVE_MODE_METRICS = {
    "smartMode": 0,
    "acMode": 1,
    "inputLimit": 0,
    "outputLimit": 0,
    "outputHomePower": 0,
    "electricLevel": 60,
}


class _FakeSnapshot:
    def __init__(self, metrics, last_seen_monotonic):
        self.metrics = metrics
        self.last_seen_monotonic = last_seen_monotonic


class _FakeService:
    def __init__(self):
        self.published = []
        self.connected = True
        self._snapshot = None

    def set_snapshot(self, metrics, last_seen_monotonic):
        self._snapshot = _FakeSnapshot(dict(metrics), last_seen_monotonic)

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(
            self._snapshot, 60.0, now_monotonic=now_monotonic or 0.0
        )

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _cloud_pro2(topic_family=FAMILY_LEGACY_JSON, **kwargs):
    return ZendureMqttDeviceClient(
        "INV-CLOUD",
        _FakeService(),
        device_id="DEVICE_ID",
        topic_family=topic_family,
        source="zendure_cloud_mqtt",
        broker_ref="zendure_cloud",
        product_key="PRODUCT_KEY",
        hardware_profile="solarflow_800_pro_2",
        max_power=800,
        **kwargs,
    )


def _last_properties(dev):
    topic, payload = dev._service.published[-1]
    return topic, json.loads(payload)["properties"]


# --- primary cause: outputLimit-only payload cannot leave an inactive mode ---


def test_zensdk_discharge_publishes_atomic_mode_and_power_properties():
    """A ZenSDK discharge must carry the full source-backed property set.

    Zendure-HA (device.py, ZendureZenSdk.discharge) writes smartMode/acMode/
    outputLimit/inputLimit in one atomic properties write; a device left in
    smartMode=0/acMode=1 ignores a bare outputLimit.
    """

    dev = _cloud_pro2()
    dev._service.set_snapshot(INACTIVE_MODE_METRICS, last_seen_monotonic=0.0)
    assert dev.write_output_limit(300) is True
    _topic, properties = _last_properties(dev)
    assert properties == {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 300,
        "inputLimit": 0,
    }


def test_zensdk_idle_publishes_atomic_zero_output_properties():
    dev = _cloud_pro2()
    assert dev.write_output_limit(0) is True
    _topic, properties = _last_properties(dev)
    assert properties == {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 0,
        "inputLimit": 0,
    }


# --- addressing: writes must use the iot/… topic even on the alt family ------


def test_zensdk_alt_family_write_topic_uses_iot_prefix():
    """Cloud capture: devices report on ``/PK/DEV/…`` but accept commands on
    ``iot/PK/DEV/…`` (a getAll published to iot/ was answered). The write topic
    must therefore keep the iot/ prefix even when telemetry arrived on the
    leading-slash family — the reference implementation always writes to iot/.
    """

    dev = _cloud_pro2(topic_family=FAMILY_LEGACY_JSON_ALT)
    assert dev.write_output_limit(300) is True
    topic, _properties = _last_properties(dev)
    assert topic == "iot/PRODUCT_KEY/DEVICE_ID/properties/write"


def test_profile_backed_stored_write_topic_cannot_override_canonical_topic():
    """A stored ``mqtt.write_topic`` must never redirect a known-profile command.

    A stale leading-slash report topic left in config (the exact misconfiguration
    that publishes the atomic command where the device never applies it) must be
    ignored: a pinned model always publishes to ``iot/PK/DEV/properties/write``.
    """

    dev = _cloud_pro2(write_topic="/PRODUCT_KEY/DEVICE_ID/properties/report")
    assert dev.write_output_limit(300) is True
    topic, properties = _last_properties(dev)
    assert topic == "iot/PRODUCT_KEY/DEVICE_ID/properties/write"
    assert properties == {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 300,
        "inputLimit": 0,
    }


def test_profile_backed_arbitrary_write_topic_is_ignored_for_property_write():
    """The state/property write path also ignores a stored write_topic override."""

    dev = _cloud_pro2(write_topic="iot/OTHER/OTHER/properties/write")
    assert bool(dev.write_properties({"acMode": 2}, reason="runtime_intent")) is True
    topic, properties = _last_properties(dev)
    assert topic == "iot/PRODUCT_KEY/DEVICE_ID/properties/write"
    assert properties == {"acMode": 2}


def test_describe_exposes_canonical_effective_topic_and_flags_obsolete_override():
    dev = _cloud_pro2(write_topic="/PRODUCT_KEY/DEVICE_ID/properties/report")
    described = dev.describe()
    assert described["effective_write_topic"] == "iot/PRODUCT_KEY/DEVICE_ID/properties/write"
    assert described["effective_write_topic_source"] == "canonical_profile"
    assert described["write_topic_obsolete"] is True


# --- the symptom stays honest: unapplied write times out, never confirms -----


def test_unapplied_write_is_never_confirmed_and_times_out_honestly():
    dev = _cloud_pro2(confirmation_timeout_seconds=30.0)
    dev._service.set_snapshot(INACTIVE_MODE_METRICS, last_seen_monotonic=0.0)
    result = dev.dispatch_output_limit(300)
    assert result.published is True
    record = dev._active_command
    assert record.state == "published"

    # The device keeps reporting the unchanged inactive state after the publish:
    # that telemetry must not confirm the command.
    dev._service.set_snapshot(
        INACTIVE_MODE_METRICS,
        last_seen_monotonic=record.published_monotonic + 1.0,
    )
    dev.fetch()
    assert record.state == "published"

    # With no matching telemetry the command must reach an honest
    # confirmation_timed_out — never telemetry_confirmed, never a fake success.
    dev.describe(now_monotonic=record.published_monotonic + 31.0)
    assert record.state == "confirmation_timed_out"
    assert dev._active_command is None


# --- wrong-transport reconciliation is structurally impossible ---------------


def test_generic_http_reconciliation_path_cannot_write_an_mqtt_device():
    """``ems.clients.zendure_write`` requires ``dev.session`` (local HTTP). An
    MQTT device intentionally has none, so routing a reconciliation write for it
    through the generic HTTP path must fail loudly — never publish, never fall
    back — proving the controller needs a transport-neutral capability instead.
    """

    from ems.clients import zendure_write

    dev = _cloud_pro2()
    assert not hasattr(dev, "session")
    with pytest.raises(AttributeError):
        zendure_write(dev, "acMode", {"acMode": 2}, "write_ac_mode_intent_error")
    assert dev._service.published == []


def test_mqtt_device_exposes_transport_native_property_writes():
    """The transport-neutral capability: an MQTT ZenSDK device writes properties
    by publishing to its own properties/write topic, no HTTP session involved.
    """

    dev = _cloud_pro2()
    result = dev.write_properties({"acMode": 2}, reason="runtime_intent")
    assert bool(result) is True
    topic, properties = _last_properties(dev)
    assert topic == "iot/PRODUCT_KEY/DEVICE_ID/properties/write"
    assert properties == {"acMode": 2}


# --- QoS metadata must survive to the paho client ----------------------------


class _FakePahoClient:
    def __init__(self):
        self.publish_calls = []
        self.subscriptions = []
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.on_publish = None
        self.next_mid = 0

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
        from types import SimpleNamespace

        self.publish_calls.append((topic, payload, qos, retain))
        self.next_mid += 1
        return SimpleNamespace(rc=0, mid=self.next_mid)


def _live_control_service():
    from ems.zendure_mqtt.control import (
        ZendureMqttControlClient,
        ZendureMqttControlService,
    )
    from ems.zendure_mqtt.service import ZendureMqttRuntimeConfig

    fake = _FakePahoClient()
    runtime = ZendureMqttRuntimeConfig(enabled=True, host="broker.local")
    service = ZendureMqttControlService(
        runtime,
        read_client_factory=lambda _cfg: ZendureMqttControlClient(
            _cfg, client_factory=lambda _c: fake
        ),
    )
    service.start()
    return service, fake


def test_control_publish_reaches_paho_with_qos1_and_no_retain():
    """A control command must reach the paho client as QoS 1, retain False.

    The builder already carries qos/retain on ``MqttPublishMessage``; dropping
    them at the service boundary silently downgraded every control publish to
    QoS 0 (fire-and-forget, no delivery evidence).
    """

    service, fake = _live_control_service()
    dev = ZendureMqttDeviceClient(
        "INV-CLOUD",
        service,
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="zendure_cloud_mqtt",
        product_key="PRODUCT_KEY",
        hardware_profile="solarflow_800_pro_2",
        max_power=800,
    )
    assert dev.write_output_limit(300) is True
    topic, _payload, qos, retain = fake.publish_calls[-1]
    assert topic == "iot/PRODUCT_KEY/DEVICE_ID/properties/write"
    assert qos == 1
    assert retain is False


def test_service_publish_message_forwards_builder_metadata():
    from ems.zendure_mqtt.write_protocols import MqttPublishMessage

    service, fake = _live_control_service()
    message = MqttPublishMessage(
        topic="iot/PK/DEV/properties/write",
        payload=b"{}",
        qos=1,
        retain=False,
    )
    submission = service.publish_message(message)
    assert bool(submission) is True
    assert fake.publish_calls[-1] == ("iot/PK/DEV/properties/write", b"{}", 1, False)


# --- a local rc==0 is not broker delivery ------------------------------------


def test_local_publish_acceptance_is_not_broker_delivery():
    """paho ``rc == 0`` only means the client queued the message locally. With
    no PUBACK observed the command must expose broker delivery as pending —
    never as delivered, and never as device success.
    """

    service, fake = _live_control_service()
    dev = ZendureMqttDeviceClient(
        "INV-CLOUD",
        service,
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="zendure_cloud_mqtt",
        product_key="PRODUCT_KEY",
        hardware_profile="solarflow_800_pro_2",
        max_power=800,
    )
    assert dev.write_output_limit(300) is True
    record = dev._active_command
    assert record.state == "published"
    assert record.snapshot()["broker_delivery"] == "pending"
