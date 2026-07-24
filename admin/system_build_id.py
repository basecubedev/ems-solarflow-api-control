# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authoritative parsing/validation for Admin/EMS System Build identifiers.

A build id is one of a small set of strictly-shaped, Docker-tag-safe kinds. The
modern kinds are the paired-build contract (a version/latest/dev/local build id
that binds the two images of one System Build). ``LEGACY_CI`` is the *original*
CI build id (``<GITHUB_RUN_ID>-<GITHUB_RUN_ATTEMPT>``, e.g. ``123456789-1``)
stamped on images published before that contract existed; it is recognized so a
historical release can still be identity-verified, but it is deliberately never
equated with a modern build id (see ``docs/technical/admin-architecture.md``).

One central parser (:func:`parse_system_build_id`) owns every pattern so no
module has to carry its own legacy regex.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


MAX_SYSTEM_BUILD_ID_LENGTH = 128

_HEX_REVISION = r"[0-9a-f]{7,40}"
_POSITIVE_INTEGER = r"[1-9][0-9]*"
_STABLE_OR_RC = re.compile(
    r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-RC[0-9]+)?-"
    r"[a-z0-9][a-z0-9.-]*$"
)
_LATEST = re.compile(r"^latest-[a-z0-9][a-z0-9.-]*$")
_DEVELOPMENT = re.compile(
    rf"^dev-(?:[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)-"
    rf"{_HEX_REVISION}-{_POSITIVE_INTEGER}-{_POSITIVE_INTEGER}$"
)
_LOCAL = re.compile(rf"^local-{_HEX_REVISION}(?:-dirty)?$")
# The pre-contract CI build id: a positive run id and a positive run attempt.
# ``[1-9][0-9]*`` on both sides rejects a zero or leading-zero run id/attempt.
_LEGACY_CI = re.compile(rf"^{_POSITIVE_INTEGER}-{_POSITIVE_INTEGER}$")


class SystemBuildIdKind(Enum):
    """The recognized kinds of System Build identifier."""

    MODERN_RELEASE = "modern_release"
    MODERN_LATEST = "modern_latest"
    DEVELOPMENT = "development"
    LOCAL = "local"
    LEGACY_CI = "legacy_ci"


_MODERN_KINDS = frozenset(
    {SystemBuildIdKind.MODERN_RELEASE, SystemBuildIdKind.MODERN_LATEST}
)

# Order matters only for readability: the kinds are mutually exclusive by shape.
_KIND_PATTERNS = (
    (SystemBuildIdKind.MODERN_RELEASE, _STABLE_OR_RC),
    (SystemBuildIdKind.MODERN_LATEST, _LATEST),
    (SystemBuildIdKind.DEVELOPMENT, _DEVELOPMENT),
    (SystemBuildIdKind.LOCAL, _LOCAL),
    (SystemBuildIdKind.LEGACY_CI, _LEGACY_CI),
)


@dataclass(frozen=True)
class ParsedSystemBuildId:
    """A recognized build id together with its :class:`SystemBuildIdKind`."""

    value: str
    kind: SystemBuildIdKind

    @property
    def is_legacy(self) -> bool:
        return self.kind is SystemBuildIdKind.LEGACY_CI

    @property
    def is_local(self) -> bool:
        return self.kind is SystemBuildIdKind.LOCAL

    @property
    def is_modern(self) -> bool:
        return self.kind in _MODERN_KINDS


def parse_system_build_id(value: str) -> ParsedSystemBuildId:
    """Parse a build id into its kind, or raise :class:`ValueError`.

    Build ids are deliberately Docker-tag-safe and bounded. They are never
    stripped or case-folded: persisted/label metadata must already be canonical,
    otherwise two layers could silently assign different meaning to one build.
    The only uppercase spelling allowed is the conventional ``RC`` marker in a
    release-candidate version.
    """

    if not isinstance(value, str) or not value:
        raise ValueError("system build id is required")
    if value != value.strip():
        raise ValueError("system build id must not contain surrounding whitespace")
    if len(value) > MAX_SYSTEM_BUILD_ID_LENGTH:
        raise ValueError(
            f"system build id exceeds {MAX_SYSTEM_BUILD_ID_LENGTH} characters"
        )
    for kind, pattern in _KIND_PATTERNS:
        if pattern.fullmatch(value):
            return ParsedSystemBuildId(value=value, kind=kind)
    raise ValueError("system build id has an unsupported format")


def validate_system_build_id(value: str) -> str:
    """Return an unchanged, format-valid build id, or raise :class:`ValueError`.

    A thin wrapper over :func:`parse_system_build_id` that keeps the historical
    "return the value or raise" contract for callers that only need the guard.
    """

    return parse_system_build_id(value).value
