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

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]


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
        self.deliver_during_publish = False

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
        if self.deliver_during_publish and self.on_publish is not None:
            self.on_publish(self, None, self.next_mid)
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


def test_submission_delivery_token_history_is_bounded():
    service, fake = _service()
    message = SimpleNamespace(topic="topic", payload=b"{}", qos=1, retain=False)
    for _ in range(700):
        submission = service.publish_message(message)
        service._client._on_publish(fake, None, submission.mid)
    assert len(service._client._delivery_states) <= 512


def test_reused_mid_requires_puback_for_its_own_submission_token():
    service, fake = _service()
    message = SimpleNamespace(topic="topic", payload=b"{}", qos=1, retain=False)
    first = service.publish_message(message)
    service._client._on_publish(fake, None, first.mid)
    assert service.delivery_status(first.delivery_token) == "delivered"

    fake.next_mid = first.mid - 1
    second = service.publish_message(message)
    assert second.mid == first.mid
    assert second.delivery_token != first.delivery_token
    assert service.delivery_status(second.delivery_token) == "pending"

    service._client._on_publish(fake, None, second.mid)
    assert service.delivery_status(first.delivery_token) == "delivered"
    assert service.delivery_status(second.delivery_token) == "delivered"


def test_puback_racing_publish_return_is_attached_to_that_submission():
    service, fake = _service()
    fake.deliver_during_publish = True
    submission = service.publish_message(
        SimpleNamespace(topic="topic", payload=b"{}", qos=1, retain=False)
    )
    assert submission.accepted is True
    assert service.delivery_status(submission.delivery_token) == "delivered"


def test_disconnect_quarantines_reused_mid_instead_of_guessing_delivery():
    service, fake = _service()
    message = SimpleNamespace(topic="topic", payload=b"{}", qos=1, retain=False)
    first = service.publish_message(message)

    fake.on_disconnect(fake, None, 1)
    assert service.delivery_status(first.delivery_token) == "disconnected"
    fake.on_connect(fake, None, None, 0)

    fake.next_mid = first.mid - 1
    second = service.publish_message(message)
    assert second.mid == first.mid
    assert second.delivery_token.connection_generation != (
        first.delivery_token.connection_generation
    )
    assert service.delivery_status(second.delivery_token) == "expired"

    # A callback carries no connection generation. Once an unresolved publish
    # crossed a disconnect, this raw MID stays quarantined for the client lifetime:
    # callbacks can update neither the old nor the newer submission by guessing.
    service._client._on_publish(fake, None, second.mid)
    assert service.delivery_status(first.delivery_token) == "disconnected"
    assert service.delivery_status(second.delivery_token) == "expired"
    service._client._on_publish(fake, None, second.mid)
    assert service.delivery_status(second.delivery_token) == "expired"
    assert service.delivery_confirmed(second.mid) is False


def test_disconnect_between_publish_acceptance_and_token_registration_quarantines_mid():
    service, fake = _service()
    message = SimpleNamespace(topic="topic", payload=b"{}", qos=1, retain=False)
    normal_publish = fake.publish

    def publish_then_disconnect(*args, **kwargs):
        info = normal_publish(*args, **kwargs)
        fake.on_disconnect(fake, None, 1)
        return info

    fake.publish = publish_then_disconnect
    first = service.publish_message(message)

    assert service.delivery_status(first.delivery_token) == "disconnected"
    assert first.mid in service._client._quarantined_delivery_mids

    fake.publish = normal_publish
    fake.on_connect(fake, None, None, 0)
    fake.next_mid = first.mid - 1
    reused = service.publish_message(message)
    assert reused.mid == first.mid
    assert service.delivery_status(reused.delivery_token) == "expired"

    service._client._on_publish(fake, None, first.mid)
    assert service.delivery_status(first.delivery_token) == "disconnected"
    assert service.delivery_status(reused.delivery_token) == "expired"


def test_late_callback_after_tombstone_eviction_cannot_update_reused_mid(
    monkeypatch,
):
    import ems.zendure_mqtt.control as control_module

    monkeypatch.setattr(control_module, "_DELIVERY_TOKEN_HISTORY", 2)
    service, fake = _service()
    message = SimpleNamespace(topic="topic", payload=b"{}", qos=1, retain=False)
    first = service.publish_message(message)
    fake.on_disconnect(fake, None, 1)
    fake.on_connect(fake, None, None, 0)

    # Resolve enough later publishes to evict the disconnected token itself.
    for _ in range(3):
        submission = service.publish_message(message)
        service._client._on_publish(fake, None, submission.mid)
    assert first.delivery_token not in service._client._delivery_states
    assert service.delivery_status(first.delivery_token) == "expired"

    fake.next_mid = first.mid - 1
    reused = service.publish_message(message)
    assert reused.mid == first.mid
    assert service.delivery_status(reused.delivery_token) == "expired"

    # Even a callback arriving after the old tombstone is gone is ignored. It can
    # never be reinterpreted as delivery of the newer submission.
    service._client._on_publish(fake, None, reused.mid)
    assert service.delivery_status(reused.delivery_token) == "expired"
    assert reused.mid in service._client._quarantined_delivery_mids


def test_mid_quarantine_is_bounded_by_protocol_identifier_space(monkeypatch):
    import ems.zendure_mqtt.control as control_module

    monkeypatch.setattr(control_module, "_MAX_MQTT_PACKET_IDENTIFIER", 3)
    service, _fake = _service()
    client = service._client

    with client._delivery_lock:
        for mid in (-1, 0, 1, 2, 3, 4, 65_535, True, "2"):
            client._quarantine_delivery_mid_locked(mid)

    assert client._quarantined_delivery_mids == {1, 2, 3}


def test_two_devices_on_one_broker_keep_independent_delivery():
    service, fake = _service()
    dev_a = _device(service)
    dev_b = ZendureMqttDeviceClient(
        "INV2",
        service,
        device_id="DEV2",
        topic_family=FAMILY_LEGACY_JSON,
        source="zendure_cloud_mqtt",
        product_key="PK2",
        hardware_profile="solarflow_800_pro_2",
        max_power=2000,
    )
    dev_a.write_output_limit(300)
    dev_b.write_output_limit(400)
    rec_a, rec_b = dev_a._active_command, dev_b._active_command
    assert rec_a.publish_mid != rec_b.publish_mid
    # Deliver only device B's mid: A stays pending, B becomes delivered.
    service._client._on_publish(fake, None, rec_b.publish_mid)
    dev_a.describe(now_monotonic=rec_a.published_monotonic + 0.5)
    dev_b.describe(now_monotonic=rec_b.published_monotonic + 0.5)
    assert rec_a.broker_delivery == "pending"
    assert rec_b.broker_delivery == "delivered"


def test_reused_mid_on_shared_client_does_not_cross_update_devices():
    service, fake = _service()
    dev_a = _device(service)
    dev_b = ZendureMqttDeviceClient(
        "INV2",
        service,
        device_id="DEV2",
        topic_family=FAMILY_LEGACY_JSON,
        source="zendure_cloud_mqtt",
        product_key="PK2",
        hardware_profile="solarflow_800_pro_2",
        max_power=2000,
    )
    dev_a.write_output_limit(300)
    rec_a = dev_a._active_command
    service._client._on_publish(fake, None, rec_a.publish_mid)
    dev_a.describe(now_monotonic=rec_a.published_monotonic + 0.1)
    assert rec_a.broker_delivery == "delivered"

    fake.next_mid = rec_a.publish_mid - 1
    dev_b.write_output_limit(400)
    rec_b = dev_b._active_command
    assert rec_b.publish_mid == rec_a.publish_mid
    dev_b.describe(now_monotonic=rec_b.published_monotonic + 0.1)
    assert rec_b.broker_delivery == "pending"
    service._client._on_publish(fake, None, rec_b.publish_mid)
    dev_b.describe(now_monotonic=rec_b.published_monotonic + 0.2)
    assert rec_b.broker_delivery == "delivered"


def test_late_puback_after_command_supersession_updates_only_old_record():
    service, fake = _service()
    dev = _device(service)
    dev.write_output_limit(600)
    old = dev._active_command
    dev.write_output_limit(500)
    new = dev._active_command
    assert old.state == "superseded"

    service._client._on_publish(fake, None, old.publish_mid)
    dev.describe(now_monotonic=new.published_monotonic + 0.5)
    assert old.broker_delivery == "delivered"
    assert new.broker_delivery == "pending"


# --- broker delivery survives telemetry terminalization ----------------------


class _Snapshot:
    def __init__(self, metrics, last_seen_monotonic):
        self.metrics = dict(metrics)
        self.last_seen_monotonic = last_seen_monotonic
        self.metric_monotonic = {k: last_seen_monotonic for k in metrics}


class _DeliveryConfirmService:
    """Publishes with tracked mids AND serves a controllable telemetry snapshot."""

    def __init__(self):
        self.connected = True
        self._delivered = set()
        self._mid = 0
        self._snapshot = None

    def publish_message(self, message):
        self._mid += 1
        return PublishSubmission(True, self._mid)

    def delivery_confirmed(self, mid):
        return mid in self._delivered

    def deliver(self, mid):
        self._delivered.add(mid)

    def set_snapshot(self, metrics, last_seen_monotonic):
        self._snapshot = _Snapshot(metrics, last_seen_monotonic)

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(self._snapshot, 60.0, now_monotonic=now_monotonic or 0.0)


_APPLIED = {"outputLimit": 300, "acMode": 2, "smartMode": 1, "inputLimit": 0}


def _confirm_device():
    service = _DeliveryConfirmService()
    dev = ZendureMqttDeviceClient(
        "INV",
        service,
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="zendure_cloud_mqtt",
        product_key="PK",
        hardware_profile="solarflow_800_pro_2",
        max_power=2000,
        command_ack_timeout_seconds=5.0,
        confirmation_timeout_seconds=30.0,
    )
    return service, dev


def test_late_puback_after_telemetry_confirmation_reconciles_to_delivered():
    service, dev = _confirm_device()
    dev.write_output_limit(300)
    rec = dev._active_command
    assert rec.broker_delivery == "pending"
    mid = rec.publish_mid

    # Telemetry confirms first, before the broker PUBACK is observed.
    now = rec.published_monotonic
    service.set_snapshot(_APPLIED, now + 1.0)
    dev.fetch()
    assert rec.state == "telemetry_confirmed"
    assert dev._active_command is None
    assert rec.broker_delivery == "pending"

    # A later PUBACK still upgrades the retained terminal record to delivered.
    service.deliver(mid)
    dev.describe(now_monotonic=now + 2.0)
    assert rec.broker_delivery == "delivered"
    assert rec.delivered_monotonic is not None


def test_late_puback_updates_original_terminal_command_after_newer_publish():
    service, dev = _confirm_device()
    dev.write_output_limit(300)
    first = dev._active_command
    first_mid = first.publish_mid
    now = first.published_monotonic

    service.set_snapshot(_APPLIED, now + 1.0)
    dev.fetch()
    assert first.state == "telemetry_confirmed"
    assert first.broker_delivery == "pending"

    dev.write_output_limit(400)
    second = dev._active_command
    assert second is dev._last_command
    assert second.publish_mid != first_mid

    service.deliver(first_mid)
    dev.describe(now_monotonic=now + 2.0)
    assert first.broker_delivery == "delivered"
    assert second.broker_delivery == "pending"


def test_terminal_command_evidence_is_bounded_by_count_and_age():
    service, dev = _confirm_device()
    dev._command_evidence_max_records = 2
    records = []
    for target in (100, 200, 300):
        dev.write_output_limit(target)
        record = dev._active_command
        records.append(record)
        service.set_snapshot(dict(_APPLIED, outputLimit=target), record.published_monotonic + 0.1)
        dev.fetch()
    assert len(dev._command_evidence) == 2
    assert records[0] not in dev._command_evidence.values()

    dev._command_evidence_max_age_s = 1.0
    newest = records[-1]
    dev.describe(now_monotonic=newest.published_monotonic + 2.0)
    assert len(dev._command_evidence) == 0


def test_status_summarizes_retained_unresolved_delivery_evidence():
    service, dev = _confirm_device()
    dev.write_output_limit(300)
    first = dev._active_command
    service.set_snapshot(_APPLIED, first.published_monotonic + 0.1)
    dev.fetch()
    dev.write_output_limit(400)
    second = dev._active_command

    described = dev.describe(now_monotonic=second.published_monotonic + 0.5)
    assert described["recent_unresolved_delivery_count"] == 2
    assert described["oldest_unresolved_delivery_age_seconds"] >= 0.5
    assert described["last_command"]["target_power_w"] == 400


def test_older_terminal_delivery_times_out_after_newer_command_exists():
    service, dev = _confirm_device()
    dev.write_output_limit(300)
    first = dev._active_command
    service.set_snapshot(_APPLIED, first.published_monotonic + 0.1)
    dev.fetch()
    dev.write_output_limit(400)

    dev.describe(now_monotonic=first.published_monotonic + 6.0)
    assert first.state == "telemetry_confirmed"
    assert first.broker_delivery == "timeout"


def test_device_delivery_evidence_resolves_on_disconnect_without_changing_outcome():
    service, fake = _service()
    dev = _device(service)
    dev.write_output_limit(300)
    record = dev._active_command
    fake.on_disconnect(fake, None, 1)
    dev.describe(now_monotonic=record.published_monotonic + 0.5)
    assert record.broker_delivery == "disconnected"
    assert record.state == "published"


def test_puback_before_telemetry_confirmation_keeps_delivered():
    service, dev = _confirm_device()
    dev.write_output_limit(300)
    rec = dev._active_command
    now = rec.published_monotonic
    service.deliver(rec.publish_mid)
    dev.describe(now_monotonic=now + 0.5)
    assert rec.broker_delivery == "delivered"

    service.set_snapshot(_APPLIED, now + 1.0)
    dev.fetch()
    assert rec.state == "telemetry_confirmed"
    # Broker delivery evidence survives terminalization.
    assert rec.broker_delivery == "delivered"


def test_delivery_timeout_after_telemetry_confirmation_is_reported_honestly():
    service, dev = _confirm_device()
    dev.write_output_limit(300)
    rec = dev._active_command
    now = rec.published_monotonic
    service.set_snapshot(_APPLIED, now + 1.0)
    dev.fetch()
    assert rec.state == "telemetry_confirmed"
    assert rec.broker_delivery == "pending"
    # No PUBACK ever arrives: the retained record settles to timeout, and the
    # device evidence (telemetry_confirmed) is never downgraded.
    dev.describe(now_monotonic=now + 6.0)
    assert rec.broker_delivery == "timeout"
    assert rec.state == "telemetry_confirmed"


def test_describe_exposes_broker_delivery_and_confirmation_ages():
    service, dev = _confirm_device()
    dev.write_output_limit(300)
    rec = dev._active_command
    now = rec.published_monotonic
    service.deliver(rec.publish_mid)
    service.set_snapshot(_APPLIED, now + 1.0)
    dev.fetch()
    described = dev.describe(now_monotonic=now + 2.0)
    assert described["last_broker_delivery"] == "delivered"
    assert described["last_broker_delivery_age_seconds"] is not None
    assert described["last_telemetry_confirmation_age_seconds"] is not None
    assert described["last_local_submit_age_seconds"] == pytest.approx(2.0, abs=0.01)
