# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command-result state machine: publish is not confirmation.

Uses the sanitized ``device_automation_*_reply`` fixtures. A publish only reaches
``published``; only a correlated success reply reaches ``acknowledged``; only
observed target-compatible telemetry reaches ``telemetry_confirmed``. Wrong-id,
wrong-device, stale and duplicate replies are ignored.
"""

import json
from pathlib import Path

import pytest

from ems.mqtt_control.command_state import (
    STATE_ACKNOWLEDGED,
    STATE_PUBLISHED,
    STATE_QUEUED,
    STATE_REJECTED,
    STATE_TELEMETRY_CONFIRMED,
    STATE_TIMED_OUT,
    CommandRecord,
    apply_reply,
    apply_timeout,
    confirm_from_telemetry,
    mark_publish_failed,
    mark_published,
)

pytestmark = pytest.mark.simulation

_FIXTURES = Path(__file__).parent / "fixtures" / "zendure_mqtt"


def _reply(name, **overrides):
    reply = json.loads((_FIXTURES / name).read_text())
    reply.update(overrides)
    return reply


def _record(message_id=1, device_id="DEVICE_ID", target_w=500):
    return CommandRecord(
        message_id=message_id,
        device_id=device_id,
        operation="discharge",
        target_w=target_w,
        created_monotonic=100.0,
    )


def test_publish_only_reaches_published_not_confirmed():
    rec = _record()
    assert rec.state == STATE_QUEUED
    mark_published(rec)
    assert rec.state == STATE_PUBLISHED
    assert rec.acknowledged is False
    assert rec.confirmed is False


def test_publish_failure_is_a_rejection():
    rec = _record()
    mark_publish_failed(rec)
    assert rec.state == STATE_REJECTED
    assert rec.response_code == "publish_failed"


def test_success_reply_acknowledges():
    rec = _record()
    mark_published(rec)
    assert apply_reply(rec, _reply("device_automation_success_reply.json")) is True
    assert rec.state == STATE_ACKNOWLEDGED


def test_failure_reply_rejects_with_device_response():
    rec = _record()
    mark_published(rec)
    assert apply_reply(rec, _reply("device_automation_failure_reply.json")) is True
    assert rec.state == STATE_REJECTED
    assert rec.response_code == "error"


def test_wrong_message_id_reply_is_ignored():
    rec = _record(message_id=1)
    mark_published(rec)
    assert apply_reply(rec, _reply("device_automation_success_reply.json", messageId=999)) is False
    assert rec.state == STATE_PUBLISHED


def test_wrong_device_reply_is_ignored():
    rec = _record(device_id="DEVICE_ID")
    mark_published(rec)
    assert apply_reply(rec, _reply("device_automation_success_reply.json", deviceId="OTHER")) is False
    assert rec.state == STATE_PUBLISHED


def test_duplicate_reply_after_ack_is_ignored():
    rec = _record()
    mark_published(rec)
    apply_reply(rec, _reply("device_automation_success_reply.json"))
    assert rec.state == STATE_ACKNOWLEDGED
    # A duplicate/late failure reply must not flip an acknowledged command.
    assert apply_reply(rec, _reply("device_automation_failure_reply.json")) is False
    assert rec.state == STATE_ACKNOWLEDGED


def test_stale_reply_after_timeout_is_ignored():
    rec = _record()
    mark_published(rec)
    assert apply_timeout(rec, now_monotonic=110.0, timeout_s=5) is True
    assert rec.state == STATE_TIMED_OUT
    assert apply_reply(rec, _reply("device_automation_success_reply.json")) is False
    assert rec.state == STATE_TIMED_OUT


def test_no_reply_before_timeout_times_out():
    rec = _record()
    mark_published(rec)
    assert apply_timeout(rec, now_monotonic=101.0, timeout_s=5) is False
    assert rec.state == STATE_PUBLISHED
    assert apply_timeout(rec, now_monotonic=106.0, timeout_s=5) is True
    assert rec.state == STATE_TIMED_OUT


def test_telemetry_confirmation_requires_matching_output():
    rec = _record(target_w=500)
    mark_published(rec)
    apply_reply(rec, _reply("device_automation_success_reply.json"))
    assert rec.state == STATE_ACKNOWLEDGED
    # No usable telemetry field -> stays acknowledged, never confirmed.
    assert confirm_from_telemetry(rec, None) is False
    assert rec.state == STATE_ACKNOWLEDGED
    # A mismatching output does not confirm.
    assert confirm_from_telemetry(rec, 0) is False
    assert rec.state == STATE_ACKNOWLEDGED
    # Observed output compatible with the target confirms.
    assert confirm_from_telemetry(rec, 495) is True
    assert rec.state == STATE_TELEMETRY_CONFIRMED


def test_confirmation_never_happens_without_acknowledgement():
    rec = _record(target_w=500)
    mark_published(rec)
    # Cannot jump published -> confirmed on telemetry alone.
    assert confirm_from_telemetry(rec, 500) is False
    assert rec.state == STATE_PUBLISHED
