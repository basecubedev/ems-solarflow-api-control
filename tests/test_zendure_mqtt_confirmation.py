# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic post-acknowledgement completion.

An acknowledged command must never occupy the single active slot forever. Each
write profile carries a telemetry-confirmation policy. When reliable confirmation
is available, an acknowledged command reaches ``telemetry_confirmed`` (fresh
matching telemetry) or ``confirmation_timed_out`` (no telemetry before the
deadline). When no reliable confirmation is available, an acknowledged command
completes immediately as ``completed_unconfirmed``. Every path releases the slot.
"""

import pytest

from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

pytestmark = pytest.mark.simulation


class _FakeSnapshot:
    def __init__(self, metrics, last_seen_monotonic):
        self.metrics = metrics
        self.last_seen_monotonic = last_seen_monotonic


class _FakeService:
    def __init__(self):
        self.published = []
        self._snapshot = None
        self.connected = True

    def set_snapshot(self, metrics, last_seen_monotonic):
        self._snapshot = _FakeSnapshot(metrics, last_seen_monotonic)

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(self._snapshot, 60.0, now_monotonic=now_monotonic or 0.0)

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _device(**kwargs):
    return ZendureMqttDeviceClient(
        "WR",
        _FakeService(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile="hyper_2000",
        max_power=2000,
        **kwargs,
    )


def _reply(record, *, success=1, output="success"):
    return {
        "messageId": record.message_id,
        "deviceId": record.device_id,
        "function": "deviceAutomation",
        "output": output,
        "success": success,
    }


# --- policy ------------------------------------------------------------------


def test_confirmation_policy_resolved_from_write_profile():
    from ems.mqtt_control.confirmation import resolve_confirmation_policy
    from ems.mqtt_control.zendure_profiles import (
        WRITE_PROFILE_LEGACY_OBJECT,
        WRITE_PROFILE_TELEMETRY_ONLY,
    )

    supported = resolve_confirmation_policy(WRITE_PROFILE_LEGACY_OBJECT)
    assert supported.telemetry_confirmation_supported is True
    assert supported.confirmation_metric == "outputLimit"
    assert supported.confirmation_tolerance_w > 0
    assert supported.confirmation_timeout_seconds > 0

    # An unwritable / unknown profile carries no reliable confirmation.
    none_policy = resolve_confirmation_policy(WRITE_PROFILE_TELEMETRY_ONLY)
    assert none_policy.telemetry_confirmation_supported is False


# --- reliable confirmation available -----------------------------------------


def test_acknowledged_confirms_from_fresh_telemetry_and_releases_slot():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.handle_reply(_reply(rec))
    assert rec.state == "acknowledged"
    dev._service.set_snapshot(
        {"outputLimit": 500}, last_seen_monotonic=rec.published_monotonic + 1.0
    )
    dev.fetch()
    assert rec.state == "telemetry_confirmed"
    assert dev._active_command is None


def test_acknowledged_without_telemetry_times_out_confirmation_and_releases_slot():
    dev = _device(command_ack_timeout_seconds=100.0, confirmation_timeout_seconds=5.0)
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.handle_reply(_reply(rec))
    assert rec.state == "acknowledged"
    # No matching telemetry before the confirmation deadline.
    dev.describe(now_monotonic=rec.acknowledged_monotonic + 6.0)
    assert rec.state == "confirmation_timed_out"
    assert dev._active_command is None


# --- no reliable confirmation available --------------------------------------


def test_no_reliable_confirmation_completes_unconfirmed_and_releases_slot():
    dev = _device(telemetry_confirmation_supported=False)
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.handle_reply(_reply(rec))
    # Acknowledged with no confirmation policy → completed immediately, slot free.
    assert rec.state == "completed_unconfirmed"
    assert dev._active_command is None


# --- the core guarantee ------------------------------------------------------


def test_acknowledged_never_occupies_the_slot_forever():
    dev = _device(command_ack_timeout_seconds=100.0, confirmation_timeout_seconds=5.0)
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.handle_reply(_reply(rec))
    # A changed target is pending behind the acknowledged command.
    dev.write_output_limit(300)
    assert dev._pending_target == 300
    # Even with no telemetry ever, the acknowledged command is bounded and the
    # pending target eventually publishes.
    dev.describe(now_monotonic=rec.acknowledged_monotonic + 6.0)
    assert rec.is_terminal
    assert len(dev._service.published) == 2
    assert dev._active_command.target_w == 300


def test_completed_unconfirmed_is_terminal():
    from ems.mqtt_control.command_state import (
        STATE_COMPLETED_UNCONFIRMED,
        STATE_CONFIRMATION_TIMED_OUT,
        CommandRecord,
    )

    rec = CommandRecord(
        message_id=1,
        device_id="D",
        operation="discharge",
        target_w=100,
        created_monotonic=0.0,
        state=STATE_COMPLETED_UNCONFIRMED,
    )
    assert rec.is_terminal
    assert not rec.is_active
    rec.state = STATE_CONFIRMATION_TIMED_OUT
    assert rec.is_terminal
