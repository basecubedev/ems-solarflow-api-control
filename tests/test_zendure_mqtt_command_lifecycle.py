# SPDX-License-Identifier: AGPL-3.0-or-later
"""Device-client command lifecycle: records, replies, timeout, confirmation.

Every hardware write creates a correlated CommandRecord. A broker publish alone
is only ``published``; only a correlated success reply is ``acknowledged``; only
fresh, target-compatible telemetry newer than the command is
``telemetry_confirmed``; a wrong/stale/duplicate/post-timeout reply can never
confirm. Exactly one command is in flight per device.
"""

import json

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
        self._stale_after = 60.0
        self.connected = True

    def set_snapshot(self, metrics, last_seen_monotonic):
        self._snapshot = _FakeSnapshot(metrics, last_seen_monotonic)

    def snapshot_status(self, device_id, *, now_monotonic=None):
        from ems.zendure_mqtt.service import classify_snapshot

        return classify_snapshot(
            self._snapshot, self._stale_after, now_monotonic=now_monotonic or 0.0
        )

    def publish_output_limit(self, topic, payload):
        self.published.append((topic, payload))
        return True


def _device(hardware_profile="hyper_2000", **kwargs):
    return ZendureMqttDeviceClient(
        "WR",
        _FakeService(),
        device_id="DEVICE_ID",
        topic_family=FAMILY_LEGACY_JSON,
        source="local_mqtt",
        product_key="PK",
        hardware_profile=hardware_profile,
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


# --- Phase 9: command records ------------------------------------------------


def test_write_creates_published_command_record():
    dev = _device()
    assert dev.write_output_limit(500) is True
    rec = dev._last_command
    assert rec is not None
    assert rec.state == "published"
    assert rec.target_w == 500
    assert rec.operation == "discharge"
    assert rec.topic == "iot/PK/DEVICE_ID/function/invoke"
    assert rec.message_id > 0


def test_only_one_in_flight_command_same_target_is_coalesced():
    dev = _device()
    assert dev.write_output_limit(500) is True
    first = dev._active_command
    # A repeat of the in-flight target does not republish.
    assert dev.write_output_limit(500) is True
    assert len(dev._service.published) == 1
    assert dev._active_command is first


def test_changed_target_is_coalesced_into_a_single_pending_target():
    dev = _device()
    dev.write_output_limit(500)
    first = dev._active_command
    # A changed target does not supersede/republish; it becomes the pending target.
    dev.write_output_limit(300)
    assert len(dev._service.published) == 1
    assert dev._active_command is first
    assert dev._pending_target == 300


# --- Phase 10: replies -------------------------------------------------------


def test_live_reply_acknowledges_matching_command():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    assert dev.handle_reply(json.dumps(_reply(rec)).encode()) is True
    assert rec.state == "acknowledged"


def test_live_failure_reply_rejects_command():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    assert dev.handle_reply(_reply(rec, success=0, output="error")) is True
    assert rec.state == "rejected"
    assert rec.response_code == "error"


def test_wrong_device_reply_is_ignored():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    bad = _reply(rec)
    bad["deviceId"] = "OTHER"
    assert dev.handle_reply(bad) is False
    assert rec.state == "published"


def test_wrong_message_id_reply_is_ignored():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    bad = _reply(rec)
    bad["messageId"] = rec.message_id + 999
    assert dev.handle_reply(bad) is False
    assert rec.state == "published"


def test_duplicate_reply_after_ack_is_ignored():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.handle_reply(_reply(rec))
    # After ack the command is no longer active; a duplicate cannot flip it.
    assert dev.handle_reply(_reply(rec, success=0, output="error")) is False
    assert rec.state == "acknowledged"


# --- Phase 11: timeout -------------------------------------------------------


def test_command_times_out_without_reply():
    dev = _device(command_ack_timeout_seconds=5.0)
    dev.write_output_limit(500)
    rec = dev._last_command
    created = rec.created_monotonic
    # describe drives the timeout clock forward past the deadline.
    dev.describe(now_monotonic=created + 6.0)
    assert rec.state == "timed_out"
    assert dev._active_command is None
    assert dev.describe(now_monotonic=created + 7.0)["last_command"]["state"] == "timed_out"


def test_reply_after_timeout_is_ignored():
    dev = _device(command_ack_timeout_seconds=5.0)
    dev.write_output_limit(500)
    rec = dev._last_command
    dev.describe(now_monotonic=rec.created_monotonic + 6.0)
    assert dev.handle_reply(_reply(rec)) is False
    assert rec.state == "timed_out"


# --- Phase 12: telemetry confirmation ---------------------------------------


def test_fresh_telemetry_confirms_acknowledged_command():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.handle_reply(_reply(rec))
    assert rec.state == "acknowledged"
    # Fresh telemetry, newer than the command, matching the target -> confirmed.
    published_at = rec.published_monotonic
    dev._service.set_snapshot({"outputLimit": 500}, last_seen_monotonic=published_at + 1.0)
    dev.fetch()
    assert rec.state == "telemetry_confirmed"


def test_old_telemetry_does_not_confirm():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.handle_reply(_reply(rec))
    # Retained telemetry from before the command must not confirm it.
    dev._service.set_snapshot(
        {"outputLimit": 500}, last_seen_monotonic=rec.published_monotonic - 5.0
    )
    dev.fetch()
    assert rec.state == "acknowledged"


def test_out_of_tolerance_telemetry_does_not_confirm():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.handle_reply(_reply(rec))
    dev._service.set_snapshot(
        {"outputLimit": 50}, last_seen_monotonic=rec.published_monotonic + 1.0
    )
    dev.fetch()
    assert rec.state == "acknowledged"


# --- Phase 13: structured diagnostics ---------------------------------------


def test_describe_exposes_structured_last_command():
    dev = _device()
    dev.write_output_limit(500)
    rec = dev._active_command
    dev.handle_reply(_reply(rec))
    described = dev.describe(now_monotonic=rec.published_monotonic + 0.5)
    last = described["last_command"]
    assert last["message_id"] == rec.message_id
    assert last["operation"] == "discharge"
    assert last["target_power_w"] == 500
    assert last["state"] == "acknowledged"
    assert described["hardware_profile"] == "hyper_2000"
    assert described["hardware_generation"] == "hub_hyper_legacy"
    assert described["command_ack_timeout_seconds"] > 0
