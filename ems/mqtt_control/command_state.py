# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit command-result state machine for Zendure power writes.

A broker ``publish`` succeeding is *not* device acceptance. A command moves
through explicit states — ``queued`` -> ``published`` -> (``acknowledged`` ->
``telemetry_confirmed``) | ``rejected`` | ``timed_out`` — and only a correlated
device reply may report acceptance. Replies are correlated by ``messageId`` +
``deviceId``; a wrong-id, wrong-device, stale or duplicate reply is ignored and
can never confirm a command. ``telemetry_confirmed`` is reached only when a real
target-compatible telemetry value is observed, never from a publish alone.
"""

from collections.abc import Mapping
from dataclasses import dataclass

STATE_QUEUED = "queued"
STATE_PUBLISHED = "published"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_TELEMETRY_CONFIRMED = "telemetry_confirmed"
STATE_COMPLETED_UNCONFIRMED = "completed_unconfirmed"
STATE_CONFIRMATION_TIMED_OUT = "confirmation_timed_out"
STATE_REJECTED = "rejected"
STATE_TIMED_OUT = "timed_out"
STATE_SUPERSEDED = "superseded"

# Every post-acknowledgement completion is terminal, so an acknowledged command
# can never occupy the single active slot forever. ``superseded`` is terminal
# too: a safety preemption retires the old command out of the active slot and it
# can never confirm the replacement.
_TERMINAL_STATES = frozenset(
    {
        STATE_TELEMETRY_CONFIRMED,
        STATE_COMPLETED_UNCONFIRMED,
        STATE_CONFIRMATION_TIMED_OUT,
        STATE_REJECTED,
        STATE_TIMED_OUT,
        STATE_SUPERSEDED,
    }
)

# Default watts tolerance for telemetry confirmation of an output target.
_CONFIRM_TOLERANCE_W = 25


@dataclass
class CommandRecord:
    """One in-flight power command, its correlation identity and its timeline."""

    message_id: int
    device_id: str
    operation: str
    target_w: int
    created_monotonic: float
    state: str = STATE_QUEUED
    response_code: str | None = None
    response_message: str | None = None
    # Correlation + provenance for diagnostics. device_key is the MQTT routing
    # identity (usually equal to device_id); topic is where the command was sent.
    device_key: str | None = None
    topic: str | None = None
    # Monotonic timeline stamps for each lifecycle transition (None until reached).
    published_monotonic: float | None = None
    acknowledged_monotonic: float | None = None
    confirmed_monotonic: float | None = None
    timeout_monotonic: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        """A command still awaiting a terminal outcome (may accept a reply)."""

        return self.state in (STATE_QUEUED, STATE_PUBLISHED, STATE_ACKNOWLEDGED)

    @property
    def acknowledged(self) -> bool:
        return self.state in (STATE_ACKNOWLEDGED, STATE_TELEMETRY_CONFIRMED)

    @property
    def confirmed(self) -> bool:
        return self.state == STATE_TELEMETRY_CONFIRMED

    def snapshot(self) -> dict:
        """Structured, secret-free view of this command for diagnostics."""

        return {
            "message_id": self.message_id,
            "device_id": self.device_id,
            "device_key": self.device_key,
            "operation": self.operation,
            "target_power_w": self.target_w,
            "topic": self.topic,
            "state": self.state,
            "response_code": self.response_code,
            "response_message": self.response_message,
        }


def reply_matches(record: CommandRecord, reply) -> bool:
    """True only when a reply correlates to this record by messageId + deviceId."""

    if not isinstance(reply, Mapping):
        return False
    return (
        reply.get("messageId") == record.message_id
        and reply.get("deviceId") == record.device_id
    )


def mark_published(record: CommandRecord, *, now_monotonic=None) -> CommandRecord:
    """Record a successful broker publish. This is transport-level only."""

    if record.state == STATE_QUEUED:
        record.state = STATE_PUBLISHED
        record.published_monotonic = now_monotonic
    return record


def mark_publish_failed(record: CommandRecord) -> CommandRecord:
    """A failed broker publish is a rejection: the command never left the host."""

    if record.state == STATE_QUEUED:
        record.state = STATE_REJECTED
        record.response_code = "publish_failed"
    return record


def mark_superseded(record: CommandRecord, *, now_monotonic=None) -> bool:
    """Retire an in-flight command that a safety preemption has replaced.

    The command leaves the active slot as terminal ``superseded`` so a late reply
    or late telemetry for it can never be mistaken for a result of the newer,
    safer command that replaced it.
    """

    if record.is_active:
        record.state = STATE_SUPERSEDED
        record.timeout_monotonic = now_monotonic
        return True
    return False


def apply_reply(record: CommandRecord, reply, *, now_monotonic=None) -> bool:
    """Apply a device reply; ignore wrong/stale/duplicate. Return whether applied.

    Only a ``published`` command accepts a reply. A reply that arrives for an
    already-acknowledged/terminal command (duplicate/stale) or that does not
    correlate (wrong messageId/device) is ignored and changes nothing.
    """

    if record.state != STATE_PUBLISHED:
        return False
    if not reply_matches(record, reply):
        return False
    success = reply.get("success")
    output = str(reply.get("output") or "").strip().lower()
    if success == 1 or output == "success":
        record.state = STATE_ACKNOWLEDGED
        record.acknowledged_monotonic = now_monotonic
        record.response_code = str(reply.get("output") or reply.get("code") or "success")
        return True
    record.state = STATE_REJECTED
    record.response_code = str(reply.get("output") or reply.get("code") or "error")
    message = reply.get("message") or reply.get("output")
    record.response_message = str(message) if message is not None else None
    return True


def apply_timeout(record: CommandRecord, *, now_monotonic, timeout_s) -> bool:
    """Time out a published command with no reply within ``timeout_s``."""

    if (
        record.state == STATE_PUBLISHED
        and (now_monotonic - record.created_monotonic) >= timeout_s
    ):
        record.state = STATE_TIMED_OUT
        record.timeout_monotonic = now_monotonic
        return True
    return False


def apply_confirmation_timeout(
    record: CommandRecord, *, now_monotonic, timeout_s, from_published=False
) -> bool:
    """Time out a command still awaiting telemetry confirmation.

    The confirmation deadline is measured from the acknowledgement for an
    acknowledged command, and from the publish for a no-ack command that supports
    telemetry confirmation (``from_published``). Either way the command reaches
    ``confirmation_timed_out`` (terminal), releasing the active slot and exposing
    the uncertainty — control is never blocked forever, and this state is never
    conflated with a missing acknowledgement (``timed_out``).
    """

    if (
        record.state == STATE_ACKNOWLEDGED
        and record.acknowledged_monotonic is not None
        and (now_monotonic - record.acknowledged_monotonic) >= timeout_s
    ):
        record.state = STATE_CONFIRMATION_TIMED_OUT
        record.timeout_monotonic = now_monotonic
        return True
    if (
        from_published
        and record.state == STATE_PUBLISHED
        and record.published_monotonic is not None
        and (now_monotonic - record.published_monotonic) >= timeout_s
    ):
        record.state = STATE_CONFIRMATION_TIMED_OUT
        record.timeout_monotonic = now_monotonic
        return True
    return False


def complete_unconfirmed(record: CommandRecord, *, now_monotonic=None) -> bool:
    """Complete a command that has no reliable telemetry confirmation available.

    When a write profile cannot be confirmed from telemetry, the strongest honest
    signal is the acknowledgement (ack-capable profile) or the successful publish
    (no-ack profile). The command is marked ``completed_unconfirmed`` (terminal)
    rather than falsely ``telemetry_confirmed`` or left occupying the active slot
    until a meaningless timeout.
    """

    if record.state in (STATE_ACKNOWLEDGED, STATE_PUBLISHED):
        record.state = STATE_COMPLETED_UNCONFIRMED
        record.confirmed_monotonic = now_monotonic
        return True
    return False


def confirm_from_telemetry(
    record: CommandRecord,
    observed_output_w,
    *,
    tolerance_w: int = _CONFIRM_TOLERANCE_W,
    now_monotonic=None,
    telemetry_monotonic=None,
    allow_from_published: bool = False,
) -> bool:
    """Promote a command to telemetry_confirmed, when confirmable.

    An acknowledged command is always eligible. A still-``published`` command is
    eligible only when ``allow_from_published`` is set — the caller passes this
    exclusively for a no-ack write profile that has no acknowledgement to wait for
    but whose commanded output is observable in telemetry. Acknowledgement
    correlation therefore stays strict for profiles that do have a reply contract:
    their published commands are never confirmed from telemetry alone.

    Confirmation additionally requires an actual observed output value compatible
    with the command's target AND telemetry newer than the command (retained/stale
    telemetry from before the publish can never confirm it). Where no usable
    telemetry field exists (``observed_output_w`` is ``None``), the honest current
    state is retained — a command is never reported ``confirmed`` on a publish or
    ack alone.
    """

    eligible = record.state == STATE_ACKNOWLEDGED or (
        allow_from_published and record.state == STATE_PUBLISHED
    )
    if not eligible:
        return False
    if not isinstance(observed_output_w, (int, float)) or isinstance(
        observed_output_w, bool
    ):
        return False
    # Telemetry that predates the command (retained/stale) cannot confirm it.
    if (
        telemetry_monotonic is not None
        and record.published_monotonic is not None
        and telemetry_monotonic < record.published_monotonic
    ):
        return False
    if abs(float(observed_output_w) - float(record.target_w)) <= tolerance_w:
        record.state = STATE_TELEMETRY_CONFIRMED
        record.confirmed_monotonic = now_monotonic
        return True
    return False


__all__ = [
    "STATE_QUEUED",
    "STATE_PUBLISHED",
    "STATE_ACKNOWLEDGED",
    "STATE_TELEMETRY_CONFIRMED",
    "STATE_COMPLETED_UNCONFIRMED",
    "STATE_CONFIRMATION_TIMED_OUT",
    "STATE_REJECTED",
    "STATE_TIMED_OUT",
    "STATE_SUPERSEDED",
    "CommandRecord",
    "reply_matches",
    "mark_published",
    "mark_publish_failed",
    "mark_superseded",
    "apply_reply",
    "apply_timeout",
    "apply_confirmation_timeout",
    "complete_unconfirmed",
    "confirm_from_telemetry",
]
