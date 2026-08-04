# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one owner of keep/replace/add/block decisions for a device connection.

Setup and Maintenance present different workflows but must never disagree about
whether a candidate connection may take over an existing logical device. Both
call :func:`plan_connection_change`; no endpoint re-derives "is this the same
device?" or "may this replace that?" locally.

The planner *composes* two canonical answers, it never recomputes them:

physical identity
    ``ems.device_identity`` resolves and compares the observations.

output-control capability
    the caller passes the already-resolved verdicts
    (``ems.zendure_mqtt.capability`` / ``ems.mqtt_control.power_capability``),
    because capability depends on model, write route and broker source — none of
    which belong here.

Capability policy matches the rest of the project: a candidate that cannot write
is *downgraded to telemetry-only* with an explicit confirmation, not refused,
and the "at least one enabled output-control inverter" invariant stays with
Maintenance apply validation. ``control_required=True`` lets a caller that would
lose its last write path ask for the fail-closed answer instead.
"""

from dataclasses import dataclass, field

from ems.device_identity import (
    STATUS_AMBIGUOUS,
    STATUS_CONFLICT,
    STATUS_UNRESOLVED,
    compare_physical_identity,
    connection_coordinates,
    opaque_connection_id,
    resolve_physical_identity,
)

INTENT_SWITCH_CONNECTION = "switch_connection"
INTENT_ADD_DEVICE = "add_device"
INTENT_REVIEW = "review"

ACTION_KEEP_CURRENT = "keep_current"
ACTION_USE_CANDIDATE = "use_candidate"
ACTION_REPLACE_WITH_CONFIRMATION = "replace_with_confirmation"
ACTION_ADD_AS_NEW_DEVICE = "add_as_new_device"
ACTION_BLOCK_IDENTITY_CONFLICT = "block_identity_conflict"
ACTION_BLOCK_CAPABILITY_LOSS = "block_capability_loss"
ACTION_BLOCK_UNRESOLVED_IDENTITY = "block_unresolved_identity"

CONTROL_PRESERVED = "preserved"
CONTROL_GAINED = "gained"
CONTROL_LOST = "lost"
CONTROL_UNKNOWN = "unknown"
CONTROL_NOT_REQUIRED = "not_required"

_BLOCKING_ACTIONS = frozenset(
    {
        ACTION_BLOCK_IDENTITY_CONFLICT,
        ACTION_BLOCK_CAPABILITY_LOSS,
        ACTION_BLOCK_UNRESOLVED_IDENTITY,
    }
)


@dataclass(frozen=True)
class ConnectionPlan:
    """One authoritative decision about a candidate connection.

    Everything here is browser-safe: ids are keyed tokens, ``reason`` is a
    stable lowercase code, and no raw serial, host or route segment appears.
    """

    action: str
    same_physical_device: bool = False
    identity_status: str = STATUS_UNRESOLVED
    identity_conflict: bool = False
    replacement_allowed: bool = False
    confirmation_required: bool = False
    control_continuity: str = CONTROL_UNKNOWN
    reason: str = ""
    current_connection_id: str | None = None
    candidate_connection_id: str | None = None
    physical_device_id: str | None = None
    notes: tuple = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        return self.action in _BLOCKING_ACTIONS

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "same_physical_device": self.same_physical_device,
            "identity_status": self.identity_status,
            "identity_conflict": self.identity_conflict,
            "replacement_allowed": self.replacement_allowed,
            "confirmation_required": self.confirmation_required,
            "control_continuity": self.control_continuity,
            "reason": self.reason,
            "current_connection_id": self.current_connection_id,
            "candidate_connection_id": self.candidate_connection_id,
            "physical_device_id": self.physical_device_id,
            "notes": list(self.notes),
        }


def _connection_id(item, *, key, broker_sources):
    if item is None or key is None:
        return None
    coordinates = connection_coordinates(item, broker_sources=broker_sources)
    return opaque_connection_id(coordinates, key) if coordinates is not None else None


def _control_continuity(current_supported, candidate_supported):
    if current_supported is None or candidate_supported is None:
        return CONTROL_UNKNOWN
    if not current_supported:
        return CONTROL_GAINED if candidate_supported else CONTROL_NOT_REQUIRED
    return CONTROL_PRESERVED if candidate_supported else CONTROL_LOST


def plan_connection_change(
    *,
    current_device=None,
    candidate=None,
    intent=INTENT_SWITCH_CONNECTION,
    identity_token_key=None,
    broker_sources=None,
    current_control_supported=None,
    candidate_control_supported=None,
    candidate_control_block_reason=None,
    control_required=False,
    operator_confirmed=False,
):
    """Decide how ``candidate`` may relate to ``current_device``.

    ``current_device`` is the configured entry (or ``None`` in Setup adoption),
    ``candidate`` the discovered observation or proposal. The two
    ``*_control_supported`` flags are the canonical capability verdicts; ``None``
    means "not resolved", which never silently counts as capable.
    """

    current_connection_id = _connection_id(
        current_device, key=identity_token_key, broker_sources=broker_sources
    )
    candidate_connection_id = _connection_id(
        candidate, key=identity_token_key, broker_sources=broker_sources
    )
    candidate_identity = resolve_physical_identity(
        candidate, broker_sources=broker_sources, token_key=identity_token_key
    )
    continuity = _control_continuity(
        current_control_supported, candidate_control_supported
    )
    notes = tuple(
        note for note in (candidate_control_block_reason,) if isinstance(note, str) and note
    )

    if candidate is None:
        return ConnectionPlan(
            action=ACTION_KEEP_CURRENT,
            reason="no_candidate_connection",
            current_connection_id=current_connection_id,
            control_continuity=CONTROL_PRESERVED
            if current_control_supported
            else CONTROL_NOT_REQUIRED,
        )

    # Nothing to replace: this is an addition, and only the candidate's own
    # identity matters.
    if current_device is None:
        return ConnectionPlan(
            action=ACTION_ADD_AS_NEW_DEVICE,
            identity_status=candidate_identity.status,
            replacement_allowed=False,
            confirmation_required=False,
            control_continuity=CONTROL_GAINED
            if candidate_control_supported
            else CONTROL_NOT_REQUIRED,
            reason="no_configured_device",
            candidate_connection_id=candidate_connection_id,
            physical_device_id=candidate_identity.public_identity_id,
            notes=notes,
        )

    current_identity = resolve_physical_identity(
        current_device, broker_sources=broker_sources, token_key=identity_token_key
    )
    comparison = compare_physical_identity(current_identity, candidate_identity)

    if comparison.status == STATUS_CONFLICT:
        return ConnectionPlan(
            action=ACTION_BLOCK_IDENTITY_CONFLICT,
            identity_status=STATUS_CONFLICT,
            identity_conflict=True,
            control_continuity=continuity,
            reason=comparison.reason,
            current_connection_id=current_connection_id,
            candidate_connection_id=candidate_connection_id,
            notes=notes,
        )

    if comparison.status == STATUS_AMBIGUOUS and not comparison.same_physical_device:
        # One device anchor, two known write addresses and no serial to settle
        # it: replacing would bind an unproven route.
        return ConnectionPlan(
            action=ACTION_BLOCK_IDENTITY_CONFLICT,
            identity_status=STATUS_AMBIGUOUS,
            control_continuity=continuity,
            reason=comparison.reason,
            current_connection_id=current_connection_id,
            candidate_connection_id=candidate_connection_id,
            notes=notes,
        )

    if not comparison.same_physical_device:
        # Never silently re-home a configured device on evidence that does not
        # prove it is the same hardware. Adding it separately stays available.
        if intent == INTENT_ADD_DEVICE:
            return ConnectionPlan(
                action=ACTION_ADD_AS_NEW_DEVICE,
                identity_status=candidate_identity.status,
                control_continuity=CONTROL_GAINED
                if candidate_control_supported
                else CONTROL_NOT_REQUIRED,
                reason="candidate_is_a_separate_device",
                current_connection_id=current_connection_id,
                candidate_connection_id=candidate_connection_id,
                physical_device_id=candidate_identity.public_identity_id,
                notes=notes,
            )
        return ConnectionPlan(
            action=ACTION_BLOCK_UNRESOLVED_IDENTITY,
            identity_status=comparison.status,
            control_continuity=continuity,
            reason=comparison.reason,
            current_connection_id=current_connection_id,
            candidate_connection_id=candidate_connection_id,
            notes=notes,
        )

    physical_device_id = (
        current_identity.public_identity_id or candidate_identity.public_identity_id
    )

    if current_connection_id is not None and current_connection_id == candidate_connection_id:
        return ConnectionPlan(
            action=ACTION_KEEP_CURRENT,
            same_physical_device=True,
            identity_status=comparison.status,
            control_continuity=continuity,
            reason="candidate_is_the_current_connection",
            current_connection_id=current_connection_id,
            candidate_connection_id=candidate_connection_id,
            physical_device_id=physical_device_id,
            notes=notes,
        )

    if continuity == CONTROL_LOST and control_required:
        return ConnectionPlan(
            action=ACTION_BLOCK_CAPABILITY_LOSS,
            same_physical_device=True,
            identity_status=comparison.status,
            control_continuity=CONTROL_LOST,
            reason="candidate_cannot_write_output_limit",
            current_connection_id=current_connection_id,
            candidate_connection_id=candidate_connection_id,
            physical_device_id=physical_device_id,
            notes=notes,
        )

    # Same physical device, no contradiction: a replacement may be proposed. It
    # still needs an explicit confirmation whenever control would be lost, the
    # candidate's capability is unresolved, or the same inverter answers on two
    # precise routes so the write address is ambiguous.
    needs_confirmation = (
        continuity in (CONTROL_LOST, CONTROL_UNKNOWN) or comparison.route_ambiguous
    )
    if needs_confirmation and not operator_confirmed:
        return ConnectionPlan(
            action=ACTION_REPLACE_WITH_CONFIRMATION,
            same_physical_device=True,
            identity_status=comparison.status,
            replacement_allowed=True,
            confirmation_required=True,
            control_continuity=continuity,
            reason="ambiguous_mqtt_write_route"
            if comparison.route_ambiguous
            else "control_continuity_needs_confirmation",
            current_connection_id=current_connection_id,
            candidate_connection_id=candidate_connection_id,
            physical_device_id=physical_device_id,
            notes=notes,
        )
    return ConnectionPlan(
        action=ACTION_USE_CANDIDATE,
        same_physical_device=True,
        identity_status=comparison.status,
        replacement_allowed=True,
        confirmation_required=False,
        control_continuity=continuity,
        reason=comparison.reason,
        current_connection_id=current_connection_id,
        candidate_connection_id=candidate_connection_id,
        physical_device_id=physical_device_id,
        notes=notes,
    )


__all__ = [
    "ACTION_ADD_AS_NEW_DEVICE",
    "ACTION_BLOCK_CAPABILITY_LOSS",
    "ACTION_BLOCK_IDENTITY_CONFLICT",
    "ACTION_BLOCK_UNRESOLVED_IDENTITY",
    "ACTION_KEEP_CURRENT",
    "ACTION_REPLACE_WITH_CONFIRMATION",
    "ACTION_USE_CANDIDATE",
    "CONTROL_GAINED",
    "CONTROL_LOST",
    "CONTROL_NOT_REQUIRED",
    "CONTROL_PRESERVED",
    "CONTROL_UNKNOWN",
    "ConnectionPlan",
    "INTENT_ADD_DEVICE",
    "INTENT_REVIEW",
    "INTENT_SWITCH_CONNECTION",
    "plan_connection_change",
]
