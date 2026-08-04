# SPDX-License-Identifier: AGPL-3.0-or-later
"""Queued MQTT writes emit their eventual, correlated dispatch outcome."""

import logging

import pytest

from ems import config as cfg
from ems.controller import EMSController
from ems.mqtt_control.dispatch import WriteDispatchStatus
from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class _Service:
    connected = True

    def __init__(self):
        self.published = []
        self.fail_next = False

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        if self.fail_next:
            self.fail_next = False
            return False
        return True

    def snapshot_status(self, *_args, **_kwargs):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(None, 60.0, now_monotonic=0.0)


def _device():
    return ZendureMqttDeviceClient(
        "WR",
        _Service(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile="hyper_2000",
        max_power=2000,
    )


def _failed_reply(record):
    return {
        "messageId": record.message_id,
        "deviceId": record.device_id,
        "function": "deviceAutomation",
        "output": "error",
        "success": 0,
    }


def test_queued_target_emits_correlated_published_result_when_flushed():
    dev = _device()
    observed = []
    dev.set_dispatch_observer(observed.append)
    dev.dispatch_output_limit(500)
    active = dev._active_command

    queued = dev.dispatch_output_limit(300)
    dev.handle_reply(_failed_reply(active))

    assert queued.status is WriteDispatchStatus.QUEUED_LATEST
    assert queued.correlation_id
    assert [(event.status, event.target_w) for event in observed] == [
        (WriteDispatchStatus.PUBLISHED, 300)
    ]
    assert observed[0].correlation_id == queued.correlation_id
    assert observed[0].message_id == dev._active_command.message_id


def test_replaced_pending_target_emits_superseded_before_latest_queue():
    dev = _device()
    observed = []
    dev.set_dispatch_observer(observed.append)
    dev.dispatch_output_limit(500)

    first = dev.dispatch_output_limit(450)
    latest = dev.dispatch_output_limit(425)

    assert latest.correlation_id != first.correlation_id
    assert len(observed) == 1
    assert observed[0].status is WriteDispatchStatus.SUPERSEDED
    assert observed[0].target_w == 450
    assert observed[0].correlation_id == first.correlation_id


def test_pending_target_rejected_during_flush_is_observable():
    dev = _device()
    observed = []
    dev.set_dispatch_observer(observed.append)
    dev.dispatch_output_limit(500)
    active = dev._active_command
    queued = dev.dispatch_output_limit(300)
    dev.max_power = 250

    dev.handle_reply(_failed_reply(active))

    assert observed[-1].status is WriteDispatchStatus.REJECTED
    assert observed[-1].reason == "target_above_maximum"
    assert observed[-1].correlation_id == queued.correlation_id


def test_pending_target_publish_failure_is_observable():
    dev = _device()
    observed = []
    dev.set_dispatch_observer(observed.append)
    dev.dispatch_output_limit(500)
    active = dev._active_command
    queued = dev.dispatch_output_limit(300)
    dev._service.fail_next = True

    dev.handle_reply(_failed_reply(active))

    assert observed[-1].status is WriteDispatchStatus.FAILED
    assert observed[-1].correlation_id == queued.correlation_id


def test_controller_logs_queued_then_later_published(monkeypatch, caplog):
    gate = cfg.WriteGateDecision(
        allowed=True,
        transport="mqtt_local",
        gate_name="allow_mqtt_local_control_writes",
        gate_enabled=True,
        blocked_by=(),
    )
    monkeypatch.setattr(cfg, "resolve_device_write_gate", lambda _dev: gate)
    dev = _device()
    controller = EMSController(devices=[dev], shelly=None, sleep_enabled=False)
    caplog.set_level(logging.DEBUG)

    controller.set_output_limit(dev, 500)
    active = dev._active_command
    controller.set_output_limit(dev, 300)
    dev.handle_reply(_failed_reply(active))

    queued = [
        record
        for record in caplog.records
        if "event=write_output_limit_queued" in record.getMessage()
    ]
    published = [
        record
        for record in caplog.records
        if "event=write_output_limit_published" in record.getMessage()
        and "target_w=300" in record.getMessage()
    ]
    assert queued and published
    queued_id = queued[0].getMessage().split("correlation_id=", 1)[1].split()[0]
    published_id = published[0].getMessage().split("correlation_id=", 1)[1].split()[0]
    assert queued_id == published_id
