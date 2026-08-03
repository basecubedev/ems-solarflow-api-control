# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from ems.health import (
    CommHealth,
    percentile,
    redact_error,
    render_device_health,
    render_grid_meter_health,
)

pytestmark = [
    pytest.mark.contract,
]


def test_unattempted_health_is_unknown():
    health = CommHealth("WR1", kind="read")
    assert health.classify() == "unknown"
    snapshot = health.snapshot()
    assert snapshot["status"] == "unknown"
    assert snapshot["last_success_age_s"] is None
    assert snapshot["last_latency_ms"] is None


def test_success_records_latency_and_clears_consecutive_failures():
    health = CommHealth("grid", kind="read")
    health.record_failure(error=TimeoutError("boom"), latency_ms=3000)
    health.record_failure(error=TimeoutError("boom"), latency_ms=3000)
    assert health.consecutive_failures == 2

    health.record_success(latency_ms=42)
    assert health.consecutive_failures == 0
    assert health.success_count == 1
    assert health.last_latency_ms == 42
    assert health.classify() == "ok"


def test_consecutive_failures_classify_as_failed():
    health = CommHealth("grid", kind="read")
    for _ in range(3):
        health.record_failure(error=ConnectionError("down"))
    assert health.classify(fail_threshold=3) == "failed"
    snapshot = health.snapshot(fail_threshold=3)
    assert snapshot["consecutive_failures"] == 3
    assert snapshot["last_error"].startswith("ConnectionError")


def test_single_failure_after_success_is_degraded():
    health = CommHealth("grid", kind="read")
    health.record_success(latency_ms=10)
    health.record_failure(error=TimeoutError("late"), stale_used=True)
    assert health.classify(fail_threshold=3) == "degraded"
    assert health.snapshot()["stale_used"] is True


def test_stale_value_after_unsafe_age_is_failed():
    health = CommHealth("grid", kind="read")
    health.record_success(latency_ms=10)
    # Force an old success timestamp.
    health.last_success_monotonic -= 120
    assert health.classify(stale_after_s=60) == "failed"


def test_redact_error_is_single_line_and_bounded():
    text = redact_error(ValueError("line1\nline2 " + "x" * 500))
    assert "\n" not in text
    assert len(text) <= 200


def test_percentile_handles_small_samples():
    assert percentile([], 0.5) is None
    assert percentile([5], 0.95) == 5
    assert percentile([10, 20, 30], 0.5) == 20
    assert percentile([10, 20, 30, 40], 1.0) == 40


def test_render_grid_meter_health_block():
    health = CommHealth("Shelly", kind="read")
    health.record_success(latency_ms=42)
    snapshot = health.snapshot()
    snapshot["provider"] = "Shelly"
    lines = render_grid_meter_health(snapshot)
    text = "\n".join(lines)
    assert "Grid meter health:" in text
    assert "provider: Shelly" in text
    assert "status: ok" in text
    assert "stale value used: no" in text


def test_render_device_health_block_marks_not_attempted_write():
    read = CommHealth("WR1", kind="read")
    read.record_success(latency_ms=180)
    devices = [{"name": "WR1", "read": read.snapshot(), "write": None}]
    lines = render_device_health(devices)
    text = "\n".join(lines)
    assert "WR1:" in text
    assert "read: ok" in text
    assert "write: not attempted" in text
