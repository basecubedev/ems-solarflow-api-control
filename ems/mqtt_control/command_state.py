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
import math
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
    correlation_id: str | None = None
    # Telemetry values proving the command applied; None -> single-metric path.
    expected_properties: dict | None = None
    # Monotonic timeline stamps for each lifecycle transition (None until reached).
    published_monotonic: float | None = None
    acknowledged_monotonic: float | None = None
    confirmed_monotonic: float | None = None
    timeout_monotonic: float | None = None
    # rc==0 is only submission; "delivered" requires an observed PUBACK.
    publish_mid: int | None = None
    publish_delivery_token: object | None = None
    broker_delivery: str | None = None
    delivered_monotonic: float | None = None
    confirmation_block_reason: str | None = None
    confirmation_evidence: tuple = ()

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
            "correlation_id": self.correlation_id,
            "target_power_w": self.target_w,
            "topic": self.topic,
            "state": self.state,
            "response_code": self.response_code,
            "response_message": self.response_message,
            "broker_delivery": self.broker_delivery,
            "confirmation_block_reason": self.confirmation_block_reason,
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
    metric_key: str = "outputLimit",
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
        record.confirmation_block_reason = f"{metric_key}: missing_or_invalid"
        return False
    freshness = metric_confirmation_freshness(
        command_published_monotonic=record.published_monotonic,
        metric_observed_monotonic=telemetry_monotonic,
        snapshot_observed_monotonic=None,
        metric_was_in_snapshot=False,
    )
    if not freshness.fresh:
        record.confirmation_block_reason = f"{metric_key}: {freshness.reason}"
        return False
    if abs(float(observed_output_w) - float(record.target_w)) <= tolerance_w:
        record.state = STATE_TELEMETRY_CONFIRMED
        record.confirmed_monotonic = now_monotonic
        record.confirmation_block_reason = None
        return True
    record.confirmation_block_reason = f"{metric_key}: mismatch"
    return False


# Watt-like properties compare within a tolerance; every other expected
# property is a mode/enum and must match exactly.
WATT_PROPERTY_KEYS = frozenset({"outputLimit", "inputLimit"})

# acMode and outputLimit are required; optional fields are checked when reported.
OPTIONAL_EXPECTED_KEYS = frozenset({"smartMode", "inputLimit"})

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_MISSING_COMMAND_TIME = "missing_command_time"
FRESHNESS_MISSING_METRIC_TIME = "missing_metric_time"
FRESHNESS_UNTRUSTED_SNAPSHOT = "untrusted_snapshot"
FRESHNESS_NOT_OBSERVED = "not_observed"


@dataclass(frozen=True)
class FreshnessResult:
    """Factual timestamp-provenance verdict for one telemetry property."""

    reason: str
    observed_monotonic: float | None = None

    @property
    def fresh(self) -> bool:
        return self.reason == FRESHNESS_FRESH


@dataclass(frozen=True)
class PropertyConfirmation:
    """Matching and provenance evidence for one expected command property."""

    key: str
    expected: object
    observed: object | None
    required: bool
    matches: bool
    freshness: str
    confirmed: bool


def _observed_number(metrics, key):
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def exact_state_number(value) -> int | None:
    """Normalize a mode/enum/state value to an exact int, or ``None`` if it is not one.

    ``int`` is accepted (except ``bool``); a ``float`` is accepted only when it is
    finite and integer-valued (``2.0`` -> ``2``). A fractional, ``NaN`` or infinite
    float, a boolean, a string, or any other type is rejected so it can never be
    mistaken for a valid mode.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return None
    return None


def property_matches(key, observed, expected, *, watt_tolerance: int) -> bool:
    """Whether an observed value satisfies an expected one for its property type.

    Watt-like properties (``outputLimit``/``inputLimit``) match within
    ``watt_tolerance``; every other property is a mode/enum compared as exact
    finite integers (see ``exact_state_number``). A non-numeric, boolean or
    fractional observation never matches a mode.
    """

    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return False
    if isinstance(expected, bool) or not isinstance(expected, (int, float)):
        return False
    if key in WATT_PROPERTY_KEYS:
        if not math.isfinite(float(observed)) or not math.isfinite(float(expected)):
            return False
        return abs(float(observed) - float(expected)) <= watt_tolerance
    observed_mode = exact_state_number(observed)
    expected_mode = exact_state_number(expected)
    if observed_mode is None or expected_mode is None:
        return False
    return observed_mode == expected_mode


def _usable_monotonic(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def metric_confirmation_freshness(
    *,
    command_published_monotonic,
    metric_observed_monotonic,
    snapshot_observed_monotonic,
    metric_was_in_snapshot,
) -> FreshnessResult:
    """Return fail-closed time provenance for one confirmation property."""

    command_time = _usable_monotonic(command_published_monotonic)
    if command_time is None:
        return FreshnessResult(FRESHNESS_MISSING_COMMAND_TIME)
    observed_time = _usable_monotonic(metric_observed_monotonic)
    if observed_time is None and metric_was_in_snapshot:
        observed_time = _usable_monotonic(snapshot_observed_monotonic)
    if observed_time is None:
        snapshot_time = _usable_monotonic(snapshot_observed_monotonic)
        reason = (
            FRESHNESS_UNTRUSTED_SNAPSHOT
            if snapshot_time is not None
            else FRESHNESS_MISSING_METRIC_TIME
        )
        return FreshnessResult(reason)
    if observed_time < command_time:
        return FreshnessResult(FRESHNESS_STALE, observed_time)
    return FreshnessResult(FRESHNESS_FRESH, observed_time)


def metric_is_fresh(
    key,
    *,
    published_monotonic,
    telemetry_monotonic,
    metric_monotonic,
    metric_was_in_snapshot=False,
) -> bool:
    """Whether telemetry for ``key`` is at least as new as the command's publish.

    A per-property report time is authoritative. Snapshot time is usable only
    when the parser explicitly says this property was observed in that snapshot;
    missing or ambiguous provenance fails closed.
    """

    if isinstance(metric_monotonic, Mapping) and key in metric_monotonic:
        metric_reference = metric_monotonic.get(key)
    else:
        metric_reference = None
    return metric_confirmation_freshness(
        command_published_monotonic=published_monotonic,
        metric_observed_monotonic=metric_reference,
        snapshot_observed_monotonic=telemetry_monotonic,
        metric_was_in_snapshot=metric_was_in_snapshot,
    ).fresh


def evaluate_expected_properties_confirmation(
    record: CommandRecord,
    metrics,
    *,
    tolerance_w: int,
    telemetry_monotonic,
    metric_monotonic,
    snapshot_observed_keys=None,
) -> tuple[PropertyConfirmation, ...]:
    """Evaluate property matching and time provenance without changing state."""

    expected = record.expected_properties
    if not isinstance(expected, Mapping) or not expected:
        return ()
    if not isinstance(metrics, Mapping):
        return ()
    observed_keys = (
        set(snapshot_observed_keys)
        if isinstance(snapshot_observed_keys, (set, frozenset, list, tuple))
        else set()
    )
    evidence = []
    for key, target in expected.items():
        required = key not in OPTIONAL_EXPECTED_KEYS
        observed = metrics.get(key)
        numeric_observed = _observed_number(metrics, key)
        if numeric_observed is None:
            evidence.append(
                PropertyConfirmation(
                    key=key,
                    expected=target,
                    observed=observed,
                    required=required,
                    matches=False,
                    freshness=FRESHNESS_NOT_OBSERVED,
                    confirmed=not required and key not in metrics,
                )
            )
            continue
        matches = property_matches(
            key, numeric_observed, target, watt_tolerance=tolerance_w
        )
        metric_time = (
            metric_monotonic.get(key)
            if isinstance(metric_monotonic, Mapping) and key in metric_monotonic
            else None
        )
        freshness = metric_confirmation_freshness(
            command_published_monotonic=record.published_monotonic,
            metric_observed_monotonic=metric_time,
            snapshot_observed_monotonic=telemetry_monotonic,
            metric_was_in_snapshot=key in observed_keys,
        )
        evidence.append(
            PropertyConfirmation(
                key=key,
                expected=target,
                observed=observed,
                required=required,
                matches=matches,
                freshness=freshness.reason,
                confirmed=matches and freshness.fresh,
            )
        )
    return tuple(evidence)


def _property_failure_reason(evidence):
    for item in evidence:
        if item.confirmed:
            continue
        if item.freshness == FRESHNESS_NOT_OBSERVED:
            return f"{item.key}: missing_or_invalid"
        if not item.matches:
            return f"{item.key}: mismatch"
        return f"{item.key}: {item.freshness}"
    return None


def confirm_from_expected_properties(
    record: CommandRecord,
    metrics,
    *,
    tolerance_w: int = _CONFIRM_TOLERANCE_W,
    now_monotonic=None,
    telemetry_monotonic=None,
    metric_monotonic=None,
    snapshot_observed_keys=None,
    allow_from_published: bool = False,
) -> bool:
    """Confirm a command only when telemetry proves it *effective*.

    Every expected property that participates in confirmation must both match
    (watt properties within ``tolerance_w``, mode properties exactly) AND be
    fresh — newer than or equal to the command publish, judged per property from
    its own report time. ``acMode`` and the commanded ``outputLimit`` are
    required; ``smartMode``/``inputLimit`` are verified only when telemetry
    exposes them, but a *present* optional property must still match and be
    fresh. A stale ``acMode``/``smartMode``/``inputLimit`` — a merged snapshot's
    cached value, or a superseded command's late echo — can never confirm.
    """

    eligible = record.state == STATE_ACKNOWLEDGED or (
        allow_from_published and record.state == STATE_PUBLISHED
    )
    if not eligible:
        return False
    evidence = evaluate_expected_properties_confirmation(
        record,
        metrics,
        tolerance_w=tolerance_w,
        telemetry_monotonic=telemetry_monotonic,
        metric_monotonic=metric_monotonic,
        snapshot_observed_keys=snapshot_observed_keys,
    )
    record.confirmation_evidence = evidence
    if not evidence:
        record.confirmation_block_reason = "command: confirmation_unavailable"
        return False
    failure_reason = _property_failure_reason(evidence)
    if failure_reason is not None:
        record.confirmation_block_reason = failure_reason
        return False

    record.state = STATE_TELEMETRY_CONFIRMED
    record.confirmed_monotonic = now_monotonic
    record.confirmation_block_reason = None
    return True


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
    "FreshnessResult",
    "PropertyConfirmation",
    "reply_matches",
    "mark_published",
    "mark_publish_failed",
    "mark_superseded",
    "apply_reply",
    "apply_timeout",
    "apply_confirmation_timeout",
    "complete_unconfirmed",
    "confirm_from_telemetry",
    "confirm_from_expected_properties",
    "evaluate_expected_properties_confirmation",
    "exact_state_number",
    "property_matches",
    "metric_is_fresh",
    "metric_confirmation_freshness",
    "WATT_PROPERTY_KEYS",
    "OPTIONAL_EXPECTED_KEYS",
]
