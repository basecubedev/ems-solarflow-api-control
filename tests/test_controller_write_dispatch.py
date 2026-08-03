# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2: the controller logs write dispatches under honest, distinct events.

A published target, a queued target and a coalesced target are different facts.
The controller must never claim ``write_output_limit_published`` for a target it
only queued behind an in-flight command. Non-MQTT (boolean) devices normalize to
published/failed through the shared dispatch adapter.
"""

import logging
import types

import pytest

from ems import config as cfg
from ems.controller import EMSController
from ems.mqtt_control import dispatch
from ems.mqtt_control.dispatch import WriteDispatchStatus

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def _allowed_gate():
    return cfg.WriteGateDecision(
        allowed=True,
        transport="mqtt_local",
        gate_name="allow_mqtt_local_control_writes",
        gate_enabled=True,
        blocked_by=(),
    )


def _controller(monkeypatch):
    monkeypatch.setattr(cfg, "resolve_device_write_gate", lambda dev: _allowed_gate())
    return EMSController(devices=[], shelly=None, sleep_enabled=False)


def _events(caplog, event):
    return [r for r in caplog.records if f"event={event}" in r.getMessage()]


class _DispatchDevice:
    """A control device whose dispatch outcome is fixed for the test."""

    name = "WR"
    control_gate = "mqtt_local"

    def __init__(self, result):
        self._result = result
        self.dispatched = []

    def dispatch_output_limit(self, value):
        self.dispatched.append(value)
        return self._result


def test_published_logs_published_event(monkeypatch, caplog):
    controller = _controller(monkeypatch)
    dev = _DispatchDevice(
        dispatch.published(600, message_id=7, command_state="published")
    )
    caplog.set_level(logging.DEBUG)

    controller.set_output_limit(dev, 600)

    assert _events(caplog, "write_output_limit_published")
    assert not _events(caplog, "write_output_limit_queued")
    assert not _events(caplog, "write_output_limit_coalesced")


def test_queued_logs_queued_not_published(monkeypatch, caplog):
    controller = _controller(monkeypatch)
    dev = _DispatchDevice(dispatch.queued(500, command_state="published"))
    caplog.set_level(logging.DEBUG)

    controller.set_output_limit(dev, 500)

    assert _events(caplog, "write_output_limit_queued")
    # The controller must NOT claim a queued target was published.
    assert not _events(caplog, "write_output_limit_published")


def test_coalesced_logs_coalesced_event(monkeypatch, caplog):
    controller = _controller(monkeypatch)
    dev = _DispatchDevice(
        dispatch.coalesced(600, message_id=7, command_state="published")
    )
    caplog.set_level(logging.DEBUG)

    controller.set_output_limit(dev, 600)

    assert _events(caplog, "write_output_limit_coalesced")
    assert not _events(caplog, "write_output_limit_published")


def test_rejected_logs_rejected_event(monkeypatch, caplog):
    controller = _controller(monkeypatch)
    dev = _DispatchDevice(dispatch.rejected(-500, reason="charge_target_unsupported"))
    caplog.set_level(logging.DEBUG)

    controller.set_output_limit(dev, -500)

    rejected = _events(caplog, "write_output_limit_rejected")
    assert rejected
    assert "reason=charge_target_unsupported" in rejected[0].getMessage()
    assert not _events(caplog, "write_output_limit_published")


def test_failed_dispatch_logs_failed_not_rejected(monkeypatch, caplog):
    # A transport failure is distinct from a validation rejection and is already
    # surfaced by the transport/health layer, so the controller must not raise a
    # second WARNING mislabeled as a rejection.
    controller = _controller(monkeypatch)
    dev = _DispatchDevice(dispatch.failed(500))
    caplog.set_level(logging.DEBUG)

    controller.set_output_limit(dev, 500)

    assert _events(caplog, "write_output_limit_failed")
    assert not _events(caplog, "write_output_limit_rejected")
    assert not _events(caplog, "write_output_limit_published")


def test_boolean_false_device_normalizes_to_failed(monkeypatch, caplog):
    controller = _controller(monkeypatch)
    dev = types.SimpleNamespace(
        name="HTTP",
        control_gate="mqtt_local",
        write_output_limit=lambda value: False,
    )
    caplog.set_level(logging.DEBUG)

    controller.set_output_limit(dev, 400)

    assert _events(caplog, "write_output_limit_failed")
    assert not _events(caplog, "write_output_limit_published")


def test_boolean_device_normalizes_to_published(monkeypatch, caplog):
    controller = _controller(monkeypatch)
    # A non-MQTT device with only a boolean write_output_limit publishes.
    dev = types.SimpleNamespace(
        name="HTTP",
        control_gate="mqtt_local",
        write_output_limit=lambda value: True,
    )
    caplog.set_level(logging.DEBUG)

    controller.set_output_limit(dev, 400)

    assert _events(caplog, "write_output_limit_published")


def test_blocked_gate_still_logs_dry_run(monkeypatch, caplog):
    monkeypatch.setattr(
        cfg,
        "resolve_device_write_gate",
        lambda dev: cfg.WriteGateDecision(
            allowed=False,
            transport="mqtt_local",
            gate_name="allow_mqtt_local_control_writes",
            gate_enabled=False,
            blocked_by=("allow_mqtt_local_control_writes",),
        ),
    )
    controller = EMSController(devices=[], shelly=None, sleep_enabled=False)
    dev = _DispatchDevice(dispatch.published(600))
    caplog.set_level(logging.DEBUG)

    controller.set_output_limit(dev, 600)

    assert _events(caplog, "dry_run_output_limit")
    assert not dev.dispatched
    assert not _events(caplog, "write_output_limit_published")


def test_dispatch_adapter_normalizes_boolean_false_to_failed():
    dev = types.SimpleNamespace(write_output_limit=lambda value: False)
    result = dispatch.dispatch_device_write(dev, 100)
    assert result.status is WriteDispatchStatus.FAILED
    assert bool(result) is False
