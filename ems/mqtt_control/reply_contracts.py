# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verified Zendure command reply contracts.

A reply contract names the request suffix a command is published on, the reply
suffix(es) the device answers on, the payload fields that correlate a reply to a
command, and whether an acknowledgement is possible at all. Contracts are
selected from the resolved power-write profile, never guessed from the topic
family.

Only the legacy ``function/invoke`` deviceAutomation protocol has a verified
reply contract here: the reference implementations answer on
``function/invoke/reply`` and correlate by ``messageId`` + ``deviceId``. The
ZenSDK ``properties/write`` shape has **no** verified reply contract — a
``properties/read/reply`` message answers a ``properties/read``, not a write — so
it carries ``supports_acknowledgement=False`` and subscribes to no reply topic.
An invented channel (``function/reply``, ``properties/read_reply``) is never a
contract and can never acknowledge a command.
"""

from dataclasses import dataclass

from ems.mqtt_control.zendure_profiles import (
    WRITE_PROFILE_LEGACY_HUB,
    WRITE_PROFILE_LEGACY_OBJECT,
)


@dataclass(frozen=True)
class CommandReplyContract:
    request_suffix: str
    reply_suffixes: tuple[str, ...]
    correlation_fields: tuple[str, ...]
    supports_acknowledgement: bool


# Verified: deviceAutomation function/invoke → function/invoke/reply.
INVOKE_REPLY_CONTRACT = CommandReplyContract(
    request_suffix="function/invoke",
    reply_suffixes=("function/invoke/reply",),
    correlation_fields=("messageId", "deviceId"),
    supports_acknowledgement=True,
)

# No verified reply contract: a properties/write is not acknowledged on any
# fixture- or hardware-verified topic, so it subscribes to nothing and can only
# reach a terminal state through timeout or telemetry confirmation.
NO_ACK_REPLY_CONTRACT = CommandReplyContract(
    request_suffix="properties/write",
    reply_suffixes=(),
    correlation_fields=("messageId", "deviceId"),
    supports_acknowledgement=False,
)

_INVOKE_WRITE_PROFILES = frozenset(
    {WRITE_PROFILE_LEGACY_HUB, WRITE_PROFILE_LEGACY_OBJECT}
)


def reply_contract_for_write_profile(write_profile) -> CommandReplyContract:
    """Select the verified reply contract for a resolved power-write profile."""

    if write_profile in _INVOKE_WRITE_PROFILES:
        return INVOKE_REPLY_CONTRACT
    return NO_ACK_REPLY_CONTRACT


__all__ = [
    "CommandReplyContract",
    "INVOKE_REPLY_CONTRACT",
    "NO_ACK_REPLY_CONTRACT",
    "reply_contract_for_write_profile",
]
