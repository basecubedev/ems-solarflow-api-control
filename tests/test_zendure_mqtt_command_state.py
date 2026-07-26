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


# --- shared property comparator + per-property freshness ---------------------


def test_property_matches_classifies_watt_vs_enum():
    from ems.mqtt_control.command_state import property_matches

    # Watt-like: within tolerance passes, enum off-by-one within tolerance fails.
    assert property_matches("outputLimit", 297, 300, watt_tolerance=25) is True
    assert property_matches("inputLimit", 30, 0, watt_tolerance=25) is False
    assert property_matches("acMode", 2, 2, watt_tolerance=25) is True
    assert property_matches("acMode", 1, 2, watt_tolerance=25) is False
    assert property_matches("smartMode", 0, 1, watt_tolerance=25) is False
    # A non-numeric/boolean observation never matches.
    assert property_matches("acMode", True, 1, watt_tolerance=25) is False
    assert property_matches("outputLimit", None, 300, watt_tolerance=25) is False


def test_metric_is_fresh_uses_per_metric_timestamp_over_snapshot():
    from ems.mqtt_control.command_state import metric_is_fresh

    published = 100.0
    # A stale per-metric time wins over a newer snapshot-wide time.
    assert metric_is_fresh(
        "acMode",
        published_monotonic=published,
        telemetry_monotonic=200.0,
        metric_monotonic={"acMode": 90.0},
    ) is False
    # A fresh per-metric time confirms.
    assert metric_is_fresh(
        "acMode",
        published_monotonic=published,
        telemetry_monotonic=90.0,
        metric_monotonic={"acMode": 105.0},
    ) is True
    # No per-metric timestamp falls back to the snapshot time.
    assert metric_is_fresh(
        "acMode",
        published_monotonic=published,
        telemetry_monotonic=105.0,
        metric_monotonic={},
    ) is True
    # No publish time to compare against: treated as fresh (single-metric parity).
    assert metric_is_fresh(
        "acMode",
        published_monotonic=None,
        telemetry_monotonic=None,
        metric_monotonic=None,
    ) is True


def _expected_record(target_w=300):
    rec = _record(target_w=target_w)
    rec.expected_properties = {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": target_w,
        "inputLimit": 0,
    }
    mark_published(rec, now_monotonic=100.0)
    return rec


def test_confirm_from_expected_properties_requires_fresh_required_property():
    from ems.mqtt_control.command_state import confirm_from_expected_properties

    rec = _expected_record()
    metrics = {"smartMode": 1, "acMode": 2, "outputLimit": 300, "inputLimit": 0}
    # acMode matches but is stale -> no confirmation.
    times = {k: 101.0 for k in metrics}
    times["acMode"] = 90.0
    assert (
        confirm_from_expected_properties(
            rec,
            metrics,
            now_monotonic=101.0,
            telemetry_monotonic=101.0,
            metric_monotonic=times,
            allow_from_published=True,
        )
        is False
    )
    assert rec.state == STATE_PUBLISHED

    # All fresh -> confirms.
    times["acMode"] = 101.0
    assert (
        confirm_from_expected_properties(
            rec,
            metrics,
            now_monotonic=101.0,
            telemetry_monotonic=101.0,
            metric_monotonic=times,
            allow_from_published=True,
        )
        is True
    )
    assert rec.state == STATE_TELEMETRY_CONFIRMED


def test_confirm_from_expected_properties_absent_required_fails_absent_optional_ok():
    from ems.mqtt_control.command_state import confirm_from_expected_properties

    # Absent required acMode -> fail.
    rec = _expected_record()
    assert (
        confirm_from_expected_properties(
            rec,
            {"smartMode": 1, "outputLimit": 300, "inputLimit": 0},
            now_monotonic=101.0,
            telemetry_monotonic=101.0,
            metric_monotonic={"smartMode": 101.0, "outputLimit": 101.0, "inputLimit": 101.0},
            allow_from_published=True,
        )
        is False
    )

    # Absent optional smartMode -> confirms on the required properties alone.
    rec = _expected_record()
    metrics = {"acMode": 2, "outputLimit": 300, "inputLimit": 0}
    times = {k: 101.0 for k in metrics}
    assert (
        confirm_from_expected_properties(
            rec, metrics, now_monotonic=101.0, telemetry_monotonic=101.0,
            metric_monotonic=times, allow_from_published=True,
        )
        is True
    )


def test_confirm_from_expected_properties_present_optional_stale_fails():
    from ems.mqtt_control.command_state import confirm_from_expected_properties

    rec = _expected_record()
    metrics = {"smartMode": 1, "acMode": 2, "outputLimit": 300, "inputLimit": 0}
    times = {k: 101.0 for k in metrics}
    # A present-but-stale optional smartMode is NOT the same as an absent one.
    times["smartMode"] = 90.0
    assert (
        confirm_from_expected_properties(
            rec, metrics, now_monotonic=101.0, telemetry_monotonic=101.0,
            metric_monotonic=times, allow_from_published=True,
        )
        is False
    )
