# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-memory communication health tracking for grid-meter and device I/O.

Tracks read/write success, failure, latency, and staleness so intermittent
communication problems (for example repeated Shelly read timeouts) become
visible in diagnostics without changing control behavior. State is in-memory
only and resets on restart; the snapshot shape is intentionally export-friendly
so it can later feed InfluxDB or the dashboard.
"""

from __future__ import annotations

import time
from collections import deque

DEFAULT_LATENCY_SAMPLES = 32
DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_STALE_AFTER_S = 60.0


def redact_error(error, max_len=200):
    """Return a short, single-line, secret-free description of an error."""

    if error is None:
        return None

    if isinstance(error, BaseException):
        text = f"{error.__class__.__name__}: {error}"
    else:
        text = str(error)

    text = " ".join(text.split())
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def percentile(samples, fraction):
    if not samples:
        return None
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


class CommHealth:
    """Runtime health for one communication endpoint (a read or a write)."""

    def __init__(self, name, kind="read", max_samples=DEFAULT_LATENCY_SAMPLES):
        self.name = name
        self.kind = kind
        self.success_count = 0
        self.failure_count = 0
        self.consecutive_failures = 0
        self.last_success_monotonic = None
        self.last_failure_monotonic = None
        self.last_latency_ms = None
        self.last_error = None
        self.last_field = None
        self.stale_used = False
        self._latency_samples = deque(maxlen=max_samples)

    def record_success(self, latency_ms=None, field=None):
        self.success_count += 1
        self.consecutive_failures = 0
        self.last_success_monotonic = time.monotonic()
        self.stale_used = False
        self.last_error = None
        if latency_ms is not None:
            self.last_latency_ms = float(latency_ms)
            self._latency_samples.append(float(latency_ms))
        if field is not None:
            self.last_field = field

    def record_failure(self, error=None, latency_ms=None, field=None, stale_used=False):
        self.failure_count += 1
        self.consecutive_failures += 1
        self.last_failure_monotonic = time.monotonic()
        self.last_error = redact_error(error)
        self.stale_used = bool(stale_used)
        if latency_ms is not None:
            self.last_latency_ms = float(latency_ms)
        if field is not None:
            self.last_field = field

    @property
    def attempted(self):
        return bool(self.success_count or self.failure_count)

    def age_seconds(self, now=None):
        """Seconds since the last successful read/write, or None."""

        if self.last_success_monotonic is None:
            return None
        now = now if now is not None else time.monotonic()
        return max(0.0, now - self.last_success_monotonic)

    def failure_age_seconds(self, now=None):
        if self.last_failure_monotonic is None:
            return None
        now = now if now is not None else time.monotonic()
        return max(0.0, now - self.last_failure_monotonic)

    def latency_summary(self):
        samples = list(self._latency_samples)
        if not samples:
            return {
                "last": self.last_latency_ms,
                "avg": None,
                "p50": None,
                "p95": None,
                "max": None,
                "count": 0,
            }
        return {
            "last": self.last_latency_ms,
            "avg": round(sum(samples) / len(samples), 1),
            "p50": round(percentile(samples, 0.5), 1),
            "p95": round(percentile(samples, 0.95), 1),
            "max": round(max(samples), 1),
            "count": len(samples),
        }

    def classify(
        self,
        stale_after_s=DEFAULT_STALE_AFTER_S,
        fail_threshold=DEFAULT_FAIL_THRESHOLD,
        now=None,
    ):
        if not self.attempted:
            return "unknown"
        if self.last_success_monotonic is None:
            return "failed"
        if self.consecutive_failures >= fail_threshold:
            return "failed"
        age = self.age_seconds(now)
        if stale_after_s and age is not None and age > stale_after_s:
            return "failed"
        if self.consecutive_failures > 0 or self.stale_used:
            return "degraded"
        return "ok"

    def snapshot(
        self,
        stale_after_s=DEFAULT_STALE_AFTER_S,
        fail_threshold=DEFAULT_FAIL_THRESHOLD,
        now=None,
    ):
        age = self.age_seconds(now)
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.classify(stale_after_s, fail_threshold, now),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "consecutive_failures": self.consecutive_failures,
            "last_success_age_s": None if age is None else round(age, 1),
            "last_failure_age_s": (
                None
                if self.failure_age_seconds(now) is None
                else round(self.failure_age_seconds(now), 1)
            ),
            "value_age_s": None if age is None else round(age, 1),
            "last_latency_ms": (
                None if self.last_latency_ms is None else round(self.last_latency_ms, 1)
            ),
            "latency": self.latency_summary(),
            "last_error": self.last_error,
            "last_field": self.last_field,
            "stale_used": self.stale_used,
        }


def format_age(seconds):
    if seconds is None:
        return "never"
    return f"{int(round(seconds))}s ago"


def _format_latency(value):
    if value is None:
        return "n/a"
    return f"{int(round(value))} ms"


def render_grid_meter_health(snapshot):
    """Render the compact grid-meter health block as text lines."""

    return [
        "Grid meter health:",
        f"  provider: {snapshot.get('provider', snapshot.get('name', 'unknown'))}",
        f"  status: {snapshot['status']}",
        f"  last success: {format_age(snapshot['last_success_age_s'])}",
        f"  consecutive errors: {snapshot['consecutive_failures']}",
        f"  last latency: {_format_latency(snapshot['last_latency_ms'])}",
        f"  stale value used: {'yes' if snapshot['stale_used'] else 'no'}",
    ]


def render_device_health(devices):
    """Render the compact per-device read/write health block as text lines.

    ``devices`` is a list of {"name", "read": snapshot, "write": snapshot}.
    """

    lines = ["Device health:"]
    if not devices:
        lines.append("  (no devices)")
        return lines

    for entry in devices:
        lines.append(f"  {entry['name']}:")
        read = entry.get("read")
        if read and read["status"] != "unknown":
            lines.append(
                "    read: "
                f"{read['status']}, last success {format_age(read['last_success_age_s'])}, "
                f"consecutive errors {read['consecutive_failures']}, "
                f"last latency {_format_latency(read['last_latency_ms'])}"
            )
        else:
            lines.append("    read: not attempted")

        write = entry.get("write")
        if write and write["status"] != "unknown":
            lines.append(
                "    write: "
                f"{write['status']}, last success {format_age(write['last_success_age_s'])}, "
                f"consecutive errors {write['consecutive_failures']}"
            )
        else:
            lines.append("    write: not attempted")

    return lines
