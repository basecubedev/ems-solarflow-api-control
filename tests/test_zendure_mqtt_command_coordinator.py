# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded one-command-in-flight coordinator with a single pending target.

Exactly one command may be published and unresolved per physical device. A
repeat of the in-flight target coalesces (no republish); a *changed* target does
not publish a second command — it is stored as the single ``pending_latest_target``
and published once, after the active command reaches a terminal state
(rejection, acknowledgement completion or timeout). There is never an unbounded
queue, and an out-of-order reply for the previous command can never touch the
next one.
"""


import pytest

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
        self.metric_monotonic = {
            key: last_seen_monotonic for key in metrics
        }


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


def test_changed_target_does_not_create_a_second_in_flight_publish():
    dev = _device()
    assert dev.write_output_limit(500) is True
    active = dev._active_command
    # A changed target while one command is in flight publishes nothing more.
    assert dev.write_output_limit(300) is True
    assert len(dev._service.published) == 1
    assert dev._active_command is active
    assert dev._active_command.target_w == 500
    assert dev._pending_target == 300


def test_multiple_changed_targets_collapse_to_the_latest():
    dev = _device()
    dev.write_output_limit(500)
    dev.write_output_limit(300)
    dev.write_output_limit(250)
    dev.write_output_limit(275)
    assert len(dev._service.published) == 1
    assert dev._pending_target == 275


def test_repeat_of_active_target_clears_a_stale_pending():
    dev = _device()
    dev.write_output_limit(500)
    dev.write_output_limit(300)
    assert dev._pending_target == 300
    # The controller now wants the in-flight target again: the stale pending goes.
    dev.write_output_limit(500)
    assert dev._pending_target is None
    assert len(dev._service.published) == 1


def test_pending_target_publishes_after_rejection():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.write_output_limit(300)
    assert len(dev._service.published) == 1
    # A correlated failure reply rejects the active command → pending flushes once.
    dev.handle_reply(_reply(rec, success=0, output="error"))
    assert len(dev._service.published) == 2
    assert dev._active_command.target_w == 300
    assert dev._pending_target is None


def test_pending_target_publishes_after_acknowledgement_completion():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.write_output_limit(300)
    dev.handle_reply(_reply(rec))
    assert rec.state == "acknowledged"
    # Acknowledged command completes via fresh matching telemetry → slot released.
    dev._service.set_snapshot(
        {"outputLimit": 500}, last_seen_monotonic=rec.published_monotonic + 1.0
    )
    dev.fetch()
    assert rec.state == "telemetry_confirmed"
    assert len(dev._service.published) == 2
    assert dev._active_command.target_w == 300


def test_pending_target_publishes_after_acknowledgement_timeout():
    dev = _device(command_ack_timeout_seconds=5.0)
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.write_output_limit(300)
    assert len(dev._service.published) == 1
    # No reply before the deadline → active times out → pending flushes.
    dev.describe(now_monotonic=rec.created_monotonic + 6.0)
    assert rec.state == "timed_out"
    assert len(dev._service.published) == 2
    assert dev._active_command.target_w == 300


def test_old_reply_cannot_acknowledge_the_next_command():
    dev = _device(command_ack_timeout_seconds=5.0)
    dev.write_output_limit(500)
    old = dev._active_command
    dev.write_output_limit(300)
    # Active (500) times out → pending 300 publishes as a new command.
    dev.describe(now_monotonic=old.created_monotonic + 6.0)
    new = dev._active_command
    assert new is not old
    assert new.target_w == 300
    # A late reply for the OLD command must not acknowledge the NEW command.
    assert dev.handle_reply(_reply(old)) is False
    assert new.state == "published"


def test_pending_target_is_a_single_slot_not_a_queue():
    # Sub-margin reductions never preempt, so they exercise the single-slot
    # pending collapse purely (safety preemption is covered separately).
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    for target in (480, 460, 440, 420):
        dev.write_output_limit(target)
    assert dev._pending_target == 420
    dev.handle_reply(_reply(rec, success=0, output="error"))
    # Only the latest pending target is published; the intermediates are dropped.
    assert len(dev._service.published) == 2
    assert dev._active_command.target_w == 420
    assert dev._pending_target is None
