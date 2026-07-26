# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command-result state machine: publish is not confirmation.

Uses the sanitized ``device_automation_*_reply`` fixtures. A publish only reaches
``published``; only a correlated success reply reaches ``acknowledged``; only
observed target-compatible telemetry reaches ``telemetry_confirmed``. Wrong-id,
wrong-device, stale and duplicate replies are ignored.
"""

import json
import math
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
    mark_published(rec, now_monotonic=100.0)
    apply_reply(rec, _reply("device_automation_success_reply.json"))
    assert rec.state == STATE_ACKNOWLEDGED
    # No usable telemetry field -> stays acknowledged, never confirmed.
    assert confirm_from_telemetry(rec, None) is False
    assert rec.state == STATE_ACKNOWLEDGED
    # A mismatching output does not confirm.
    assert confirm_from_telemetry(rec, 0, telemetry_monotonic=101.0) is False
    assert rec.state == STATE_ACKNOWLEDGED
    # Observed output compatible with the target confirms.
    assert confirm_from_telemetry(rec, 495, telemetry_monotonic=101.0) is True
    assert rec.state == STATE_TELEMETRY_CONFIRMED


def test_confirmation_never_happens_without_acknowledgement():
    rec = _record(target_w=500)
    mark_published(rec)
    # Cannot jump published -> confirmed on telemetry alone.
    assert confirm_from_telemetry(rec, 500) is False
    assert rec.state == STATE_PUBLISHED


def test_single_metric_confirmation_requires_publish_and_observation_time():
    rec = _record(target_w=500)
    mark_published(rec)
    apply_reply(rec, _reply("device_automation_success_reply.json"))
    assert confirm_from_telemetry(rec, 500, telemetry_monotonic=None) is False
    assert rec.state == STATE_ACKNOWLEDGED

    rec = _record(target_w=500)
    mark_published(rec, now_monotonic=100.0)
    apply_reply(rec, _reply("device_automation_success_reply.json"))
    rec.published_monotonic = None
    assert confirm_from_telemetry(rec, 500, telemetry_monotonic=101.0) is False
    assert rec.state == STATE_ACKNOWLEDGED


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


def test_exact_state_number_normalizes_only_integer_valued_numbers():
    from ems.mqtt_control.command_state import exact_state_number

    assert exact_state_number(1) == 1
    assert exact_state_number(2) == 2
    assert exact_state_number(0) == 0
    assert exact_state_number(-1) == -1
    assert exact_state_number(1.0) == 1
    assert exact_state_number(2.0) == 2
    assert exact_state_number(1.1) is None
    assert exact_state_number(1.9) is None
    assert exact_state_number(2.9) is None
    assert exact_state_number(True) is None
    assert exact_state_number(False) is None
    assert exact_state_number("1") is None
    assert exact_state_number(None) is None
    assert exact_state_number(float("nan")) is None
    assert exact_state_number(float("inf")) is None
    assert exact_state_number(float("-inf")) is None


def test_property_matches_rejects_fractional_mode_values():
    from ems.mqtt_control.command_state import property_matches

    # Mode/enum properties must compare as exact finite integers: a fractional
    # observation is never an accepted enum value even when it truncates to the
    # target.
    assert property_matches("acMode", 1.9, 1, watt_tolerance=25) is False
    assert property_matches("acMode", 2.9, 2, watt_tolerance=25) is False
    assert property_matches("acMode", 1.1, 1, watt_tolerance=25) is False
    assert property_matches("smartMode", 0.9, 0, watt_tolerance=25) is False
    # An integer-valued float is accepted as its integer.
    assert property_matches("acMode", 1.0, 1, watt_tolerance=25) is True
    assert property_matches("acMode", 2.0, 2, watt_tolerance=25) is True
    # Booleans are never numeric modes; NaN/infinity never match.
    assert property_matches("acMode", True, 1, watt_tolerance=25) is False
    assert property_matches("smartMode", False, 0, watt_tolerance=25) is False
    assert property_matches("acMode", float("nan"), 1, watt_tolerance=25) is False
    assert property_matches("acMode", float("inf"), 2, watt_tolerance=25) is False
    # Watt properties keep their configured tolerance.
    assert property_matches("outputLimit", 297.4, 300, watt_tolerance=25) is True
    assert property_matches("outputLimit", 350, 300, watt_tolerance=25) is False


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
    # Snapshot time is trusted only when the property was in that snapshot.
    assert metric_is_fresh(
        "acMode",
        published_monotonic=published,
        telemetry_monotonic=105.0,
        metric_monotonic={},
        metric_was_in_snapshot=True,
    ) is True
    assert metric_is_fresh(
        "acMode",
        published_monotonic=published,
        telemetry_monotonic=105.0,
        metric_monotonic={},
    ) is False
    # Missing command time fails closed.
    assert metric_is_fresh(
        "acMode",
        published_monotonic=None,
        telemetry_monotonic=None,
        metric_monotonic=None,
    ) is False


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


def test_expected_property_confirmation_fails_closed_without_timestamps():
    from ems.mqtt_control.command_state import confirm_from_expected_properties

    rec = _expected_record()
    metrics = {"smartMode": 1, "acMode": 2, "outputLimit": 300, "inputLimit": 0}
    assert (
        confirm_from_expected_properties(
            rec,
            metrics,
            now_monotonic=101.0,
            telemetry_monotonic=None,
            metric_monotonic=None,
            allow_from_published=True,
        )
        is False
    )
    assert rec.state == STATE_PUBLISHED


def test_expected_property_confirmation_requires_command_publish_time():
    from ems.mqtt_control.command_state import confirm_from_expected_properties

    rec = _expected_record()
    rec.published_monotonic = None
    metrics = {"smartMode": 1, "acMode": 2, "outputLimit": 300, "inputLimit": 0}
    times = {key: 101.0 for key in metrics}
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


def test_cached_metric_cannot_inherit_unrelated_fresh_snapshot_time():
    from ems.mqtt_control.command_state import confirm_from_expected_properties

    rec = _expected_record()
    metrics = {"smartMode": 1, "acMode": 2, "outputLimit": 300, "inputLimit": 0}
    times = {"smartMode": 101.0, "outputLimit": 101.0, "inputLimit": 101.0}
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


def test_metric_confirmation_freshness_returns_structured_reasons():
    from ems.mqtt_control.command_state import metric_confirmation_freshness

    def reason(**overrides):
        values = {
            "command_published_monotonic": 100.0,
            "metric_observed_monotonic": 101.0,
            "snapshot_observed_monotonic": 102.0,
            "metric_was_in_snapshot": False,
        }
        values.update(overrides)
        return metric_confirmation_freshness(**values).reason

    assert reason(command_published_monotonic=None) == "missing_command_time"
    assert reason(command_published_monotonic=math.nan) == "missing_command_time"
    assert reason(command_published_monotonic=math.inf) == "missing_command_time"
    assert (
        reason(
            metric_observed_monotonic=None,
            snapshot_observed_monotonic=None,
        )
        == "missing_metric_time"
    )
    assert (
        reason(
            metric_observed_monotonic=math.nan,
            snapshot_observed_monotonic=None,
        )
        == "missing_metric_time"
    )
    assert (
        reason(
            metric_observed_monotonic=math.inf,
            snapshot_observed_monotonic=None,
        )
        == "missing_metric_time"
    )
    assert reason(metric_observed_monotonic=None) == "untrusted_snapshot"
    assert reason(metric_observed_monotonic=99.0) == "stale"
    assert reason() == "fresh"
    assert (
        reason(metric_observed_monotonic=None, metric_was_in_snapshot=True)
        == "fresh"
    )


def test_confirmation_snapshot_exposes_safe_provenance_block_reason():
    from ems.mqtt_control.command_state import confirm_from_expected_properties

    rec = _expected_record()
    metrics = {"smartMode": 1, "acMode": 2, "outputLimit": 300, "inputLimit": 0}
    assert (
        confirm_from_expected_properties(
            rec,
            metrics,
            telemetry_monotonic=None,
            metric_monotonic=None,
            allow_from_published=True,
        )
        is False
    )
    assert rec.confirmation_block_reason == "smartMode: missing_metric_time"
    assert rec.snapshot()["confirmation_block_reason"] == rec.confirmation_block_reason


def test_present_optional_property_without_timestamp_blocks_confirmation():
    from ems.mqtt_control.command_state import confirm_from_expected_properties

    rec = _expected_record()
    metrics = {"smartMode": 1, "acMode": 2, "outputLimit": 300, "inputLimit": 0}
    times = {"acMode": 101.0, "outputLimit": 101.0, "inputLimit": 101.0}
    assert (
        confirm_from_expected_properties(
            rec,
            metrics,
            telemetry_monotonic=101.0,
            metric_monotonic=times,
            allow_from_published=True,
        )
        is False
    )
    assert rec.confirmation_block_reason == "smartMode: untrusted_snapshot"


def test_explicitly_observed_snapshot_metric_may_use_snapshot_time():
    from ems.mqtt_control.command_state import confirm_from_expected_properties

    rec = _expected_record()
    metrics = {"acMode": 2, "outputLimit": 300}
    assert (
        confirm_from_expected_properties(
            rec,
            metrics,
            telemetry_monotonic=101.0,
            metric_monotonic={},
            snapshot_observed_keys={"acMode", "outputLimit"},
            allow_from_published=True,
        )
        is True
    )
