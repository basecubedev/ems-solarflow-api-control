# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured outcome of a device power-write dispatch.

``write_output_limit()`` returns a bare boolean that cannot distinguish a target
that was *published* from one that was only *queued* behind an in-flight command
or *coalesced* into the active one. The controller needs that distinction to log
honestly (a queued target is not a published one) without threading transport
checks through the control loop, so a device may expose ``dispatch_output_limit``
returning a :class:`WriteDispatchResult`. :func:`dispatch_device_write` normalizes
any device — MQTT (structured) or HTTP (boolean) — to one result type.
"""

from dataclasses import dataclass
from enum import Enum


class WriteDispatchStatus(Enum):
    """What actually happened to a power-write request at dispatch time."""

    PUBLISHED = "published"
    COALESCED_ACTIVE = "coalesced_active"
    QUEUED_LATEST = "queued_latest"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    FAILED = "failed"
    WITHHELD = "withheld"


# Statuses accepted, or withheld by the write gate (the controller's legacy truthy return).
_ACCEPTED = frozenset(
    {
        WriteDispatchStatus.PUBLISHED,
        WriteDispatchStatus.COALESCED_ACTIVE,
        WriteDispatchStatus.QUEUED_LATEST,
        WriteDispatchStatus.WITHHELD,
    }
)


@dataclass(frozen=True)
class WriteDispatchResult:
    """Immutable, secret-free outcome of one power-write dispatch."""

    status: WriteDispatchStatus
    target_w: int | None = None
    message_id: int | None = None
    command_state: str | None = None
    reason: str | None = None
    correlation_id: str | None = None

    def __bool__(self) -> bool:
        # Backward-compatible truthiness: a dispatch that accepted the request
        # (published/coalesced/queued) is True; a rejected/failed one is False.
        return self.status in _ACCEPTED

    @property
    def published(self) -> bool:
        return self.status is WriteDispatchStatus.PUBLISHED


def published(
    target_w, *, message_id=None, command_state=None, correlation_id=None
) -> WriteDispatchResult:
    return WriteDispatchResult(
        WriteDispatchStatus.PUBLISHED,
        target_w=target_w,
        message_id=message_id,
        command_state=command_state,
        correlation_id=correlation_id,
    )


def coalesced(
    target_w, *, message_id=None, command_state=None, correlation_id=None
) -> WriteDispatchResult:
    return WriteDispatchResult(
        WriteDispatchStatus.COALESCED_ACTIVE,
        target_w=target_w,
        message_id=message_id,
        command_state=command_state,
        reason="coalesced_active_target",
        correlation_id=correlation_id,
    )


def queued(
    target_w, *, command_state=None, correlation_id=None
) -> WriteDispatchResult:
    return WriteDispatchResult(
        WriteDispatchStatus.QUEUED_LATEST,
        target_w=target_w,
        command_state=command_state,
        reason="queued_behind_active",
        correlation_id=correlation_id,
    )


def superseded(target_w, *, correlation_id, reason) -> WriteDispatchResult:
    return WriteDispatchResult(
        WriteDispatchStatus.SUPERSEDED,
        target_w=target_w,
        command_state="superseded",
        reason=reason,
        correlation_id=correlation_id,
    )


def rejected(
    target_w, *, reason, command_state="rejected", correlation_id=None
) -> WriteDispatchResult:
    return WriteDispatchResult(
        WriteDispatchStatus.REJECTED,
        target_w=target_w,
        command_state=command_state,
        reason=reason,
        correlation_id=correlation_id,
    )


def failed(
    target_w, *, message_id=None, reason="publish_failed", correlation_id=None
) -> WriteDispatchResult:
    return WriteDispatchResult(
        WriteDispatchStatus.FAILED,
        target_w=target_w,
        message_id=message_id,
        command_state="rejected",
        reason=reason,
        correlation_id=correlation_id,
    )


def withheld(target_w, *, reason="write_gate_closed") -> WriteDispatchResult:
    """A write the controller's gate kept from the transport: nothing was sent."""

    return WriteDispatchResult(
        WriteDispatchStatus.WITHHELD,
        target_w=target_w,
        reason=reason,
    )


def normalize_bool_dispatch(ok, *, target_w) -> WriteDispatchResult:
    """Adapt a legacy boolean write result to a structured dispatch result.

    A device with no structured dispatch (e.g. the HTTP client) applies the write
    synchronously, so a success is a publish and a failure is a failed dispatch.
    """

    if ok:
        return published(target_w)
    return failed(target_w)


def dispatch_device_write(device, value, charge_exit=None) -> WriteDispatchResult:
    """Dispatch a power write to any device, returning one structured result.

    Uses the device's ``dispatch_output_limit`` when present (MQTT control
    devices), otherwise falls back to the boolean ``write_output_limit`` and
    normalizes it — keeping transport-specific handling out of the controller.
    ``charge_exit`` names the shape of an exit from a charge the controller
    decided on; it reaches only a device that dispatches structurally.
    """

    dispatch = getattr(device, "dispatch_output_limit", None)
    if callable(dispatch):
        result = (
            dispatch(value)
            if charge_exit is None
            else dispatch(value, charge_exit=charge_exit)
        )
        if isinstance(result, WriteDispatchResult):
            return result
        return normalize_bool_dispatch(result, target_w=value)
    ok = device.write_output_limit(value)
    return normalize_bool_dispatch(ok, target_w=value)


__all__ = [
    "WriteDispatchStatus",
    "WriteDispatchResult",
    "published",
    "coalesced",
    "queued",
    "superseded",
    "rejected",
    "failed",
    "withheld",
    "normalize_bool_dispatch",
    "dispatch_device_write",
]
