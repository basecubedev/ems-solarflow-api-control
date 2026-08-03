# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live readiness is separate from static eligibility.

``control_supported`` reflects hardware/transport eligibility only.
``control_ready`` additionally requires the live conditions: the write gate
enabled, the broker connected, valid write identifiers, fresh telemetry, and no
unresolved command conflict. ``control_ready`` can never be true while the broker
is disconnected. Diagnostics also expose ``broker_connected``,
``telemetry_fresh``, ``active_command``, ``pending_target`` and
``confirmation_deadline``.
"""

import pytest

from ems import config as cfg
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class _FakeSnapshot:
    def __init__(self, metrics, last_seen_monotonic):
        self.metrics = metrics
        self.last_seen_monotonic = last_seen_monotonic


class _FakeService:
    def __init__(self, *, connected=False):
        self.connected = connected
        self._snapshot = None

    def set_snapshot(self, metrics, last_seen_monotonic):
        self._snapshot = _FakeSnapshot(metrics, last_seen_monotonic)

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(self._snapshot, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        return True


def _device(service, **kwargs):
    return ZendureMqttDeviceClient(
        "WR",
        service,
        device_id="DEV",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile="hyper_2000",
        max_power=2000,
        **kwargs,
    )


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(cfg, "ALLOW_MQTT_LOCAL_CONTROL_WRITES", True)


def test_control_ready_false_when_broker_disconnected(gate_on):
    service = _FakeService(connected=False)
    service.set_snapshot({"outputLimit": 100}, last_seen_monotonic=100.0)
    d = _device(service).describe(now_monotonic=100.0)
    assert d["control_supported"] is True
    assert d["broker_connected"] is False
    assert d["control_ready"] is False


def test_control_ready_true_when_all_live_conditions_met(gate_on):
    service = _FakeService(connected=True)
    service.set_snapshot({"outputLimit": 100}, last_seen_monotonic=100.0)
    d = _device(service).describe(now_monotonic=100.0)
    assert d["control_supported"] is True
    assert d["broker_connected"] is True
    assert d["telemetry_fresh"] is True
    assert d["control_ready"] is True


def test_control_ready_false_when_telemetry_stale(gate_on):
    service = _FakeService(connected=True)
    # Snapshot older than the 60s stale window.
    service.set_snapshot({"outputLimit": 100}, last_seen_monotonic=0.0)
    d = _device(service).describe(now_monotonic=1000.0)
    assert d["control_supported"] is True
    assert d["telemetry_fresh"] is False
    assert d["control_ready"] is False


def test_control_ready_false_when_gate_disabled():
    # Gate defaults off in an unconfigured process: supported but not ready.
    service = _FakeService(connected=True)
    service.set_snapshot({"outputLimit": 100}, last_seen_monotonic=100.0)
    d = _device(service).describe(now_monotonic=100.0)
    assert d["control_supported"] is True
    assert d["write_gate_enabled"] is False
    assert d["control_ready"] is False


def test_describe_exposes_command_and_confirmation_fields():
    service = _FakeService(connected=True)
    dev = _device(service)
    d = dev.describe(now_monotonic=0.0)
    assert d["active_command"] is None
    assert d["pending_target"] is None
    assert d["confirmation_deadline"] is None

    dev.write_output_limit(500)
    rec = dev._active_command
    dev.write_output_limit(300)
    d = dev.describe(now_monotonic=rec.published_monotonic + 0.1)
    assert d["active_command"] is not None
    assert d["active_command"]["target_power_w"] == 500
    assert d["pending_target"] == 300


def test_confirmation_deadline_exposed_after_acknowledgement():
    service = _FakeService(connected=True)
    dev = _device(service, confirmation_timeout_seconds=30.0)
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.handle_reply(
        {
            "messageId": rec.message_id,
            "deviceId": rec.device_id,
            "output": "success",
            "success": 1,
        }
    )
    d = dev.describe(now_monotonic=rec.acknowledged_monotonic + 0.1)
    assert d["confirmation_deadline"] == pytest.approx(rec.acknowledged_monotonic + 30.0)
