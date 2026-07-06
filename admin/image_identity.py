# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parse EMS Docker image build-identity labels into a small reusable model.

CI stamps build-identity labels onto every published image (see
``.github/workflows/docker-publish.yml``): the OCI ``version``/``revision``
labels plus ``de.basecubedev.ems.*`` channel/build/release fields. Admin reads
these so a later release-selection step can compare a running ``latest`` image
against stable/rc tags by identity — build serial, channel, revision — rather
than by tag name alone (``latest`` is a channel, not a version).

This module is pure parsing. It never shells out and never raises for missing
or malformed labels: unknown fields come back as ``None`` and a non-numeric
``build_serial`` is treated as unknown. Docker inspection itself lives in
``admin.deployment.DockerCli.inspect_image``; ``identify_image`` glues the two
together for callers that just want an ``ImageIdentity``.
"""

from dataclasses import dataclass, field


LABEL_VERSION = "org.opencontainers.image.version"
LABEL_REVISION = "org.opencontainers.image.revision"
LABEL_CHANNEL = "de.basecubedev.ems.channel"
LABEL_BUILD_SERIAL = "de.basecubedev.ems.build_serial"
LABEL_BUILD_ID = "de.basecubedev.ems.build_id"
LABEL_RELEASE_TAG = "de.basecubedev.ems.release_tag"


@dataclass(frozen=True)
class ImageIdentity:
    """Build identity of one Docker image, as read from its labels/digest.

    Every field is optional: an image without EMS build labels (or an image
    that could not be inspected at all) yields an all-``None`` identity rather
    than a missing value. ``build_serial`` is an ``int`` only when the label
    parsed cleanly, else ``None``.
    """

    image_ref: str | None = None
    digest: str | None = None
    version_label: str | None = None
    revision: str | None = None
    channel: str | None = None
    build_serial: int | None = None
    build_id: str | None = None
    release_tag: str | None = None
    labels: dict = field(default_factory=dict)

    def as_dict(self):
        """Return a plain JSON-serializable view (labels copied defensively)."""

        return {
            "image_ref": self.image_ref,
            "digest": self.digest,
            "version_label": self.version_label,
            "revision": self.revision,
            "channel": self.channel,
            "build_serial": self.build_serial,
            "build_id": self.build_id,
            "release_tag": self.release_tag,
            "labels": dict(self.labels),
        }


def _clean(value):
    """Return a stripped non-empty string, or ``None``."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _coerce_labels(labels):
    """Return a ``{str: str}`` label map, dropping non-string keys/None values."""

    safe = {}
    if isinstance(labels, dict):
        for key, value in labels.items():
            if not isinstance(key, str) or value is None:
                continue
            safe[key] = value if isinstance(value, str) else str(value)
    return safe


def _parse_build_serial(value):
    """Parse the monotonic build serial as ``int``; unknown/bad values → ``None``."""

    text = _clean(value)
    if text is None:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def parse_labels(labels, image_ref=None, digest=None):
    """Build an :class:`ImageIdentity` from a Docker label mapping.

    Missing or malformed labels become ``None`` fields — never a traceback. The
    normalized label map is preserved on the result for callers that want the
    raw values.
    """

    safe_labels = _coerce_labels(labels)
    return ImageIdentity(
        image_ref=_clean(image_ref),
        digest=_clean(digest),
        version_label=_clean(safe_labels.get(LABEL_VERSION)),
        revision=_clean(safe_labels.get(LABEL_REVISION)),
        channel=_clean(safe_labels.get(LABEL_CHANNEL)),
        build_serial=_parse_build_serial(safe_labels.get(LABEL_BUILD_SERIAL)),
        build_id=_clean(safe_labels.get(LABEL_BUILD_ID)),
        release_tag=_clean(safe_labels.get(LABEL_RELEASE_TAG)),
        labels=safe_labels,
    )


def from_inspect(result):
    """Build an :class:`ImageIdentity` from a ``DockerCli.inspect_image`` result.

    ``result`` is the sanitized dict that inspection returns (or ``None`` when
    Docker/the image was unavailable); either way this yields an
    ``ImageIdentity`` and never raises.
    """

    if not isinstance(result, dict):
        return ImageIdentity()
    return parse_labels(
        result.get("labels"),
        image_ref=result.get("image_ref"),
        digest=result.get("digest"),
    )


def identify_image(docker, image_ref):
    """Inspect one image via ``docker`` and return its :class:`ImageIdentity`.

    ``docker`` is any object exposing ``inspect_image(image_ref) -> dict | None``
    (the real ``DockerCli`` or a test double). Works equally for a running
    container's image ref or a compose-declared image ref. Returns an
    identity carrying just ``image_ref`` when the image cannot be inspected, so
    callers never have to branch on ``None``.
    """

    inspect = getattr(docker, "inspect_image", None)
    result = inspect(image_ref) if callable(inspect) else None
    if result is None:
        return ImageIdentity(image_ref=_clean(image_ref))
    return from_inspect(result)


# --- upgrade assessment --------------------------------------------------
#
# Release selection decides whether moving from the running EMS build to a
# target image is a real upgrade. ``latest`` is a channel, not a version, so
# SemVer tags settle it only when both sides carry comparable tags; when one
# side is ``latest`` the monotonic build serial breaks the tie, and the image
# digest short-circuits the whole thing when both sides are literally the same
# build.

UPGRADE_AVAILABLE = "upgrade_available"
ALREADY_CURRENT = "already_current"
OLDER_THAN_RUNNING_BUILD = "older_than_running_build"
DOWNGRADE_BLOCKED = "downgrade_blocked"
IDENTITY_UNKNOWN = "identity_unknown"

# ``basis`` value for an upgrade allowed only by the legacy test override: build
# identity was missing and SemVer could not settle the move, so the verdict is
# unproven and carries a warning.
LEGACY_UNVERIFIED = "legacy_unverified"

# Short, user-facing copy for the two legacy-metadata paths (kept in sync with
# the wording in ``admin/releases.py`` and the setup UI).
LEGACY_SEMVER_WARNING = (
    "Legacy image metadata missing. Upgrade check uses SemVer fallback."
)
LEGACY_UNVERIFIED_WARNING = (
    "Legacy image metadata missing. This upgrade cannot be fully verified and "
    "is allowed only by admin test override."
)

# States that Guided Upgrade must refuse: they are either a real downgrade or a
# comparison it cannot prove is an upgrade.
BLOCKING_UPGRADE_STATES = frozenset(
    {OLDER_THAN_RUNNING_BUILD, DOWNGRADE_BLOCKED, IDENTITY_UNKNOWN}
)


@dataclass(frozen=True)
class UpgradeAssessment:
    """Verdict on moving from a running build to a target image.

    ``state`` is one of the module-level ``*_`` constants. ``basis`` records
    which signal decided it (``digest``/``semver``/``build_serial``/
    ``legacy_unverified``/``none``) for diagnostics and tests. ``warning`` is a
    short user-facing note when the move was allowed on legacy metadata (a
    SemVer fallback, or the unverified test override).
    """

    state: str
    basis: str = "none"
    warning: str | None = None

    @property
    def is_upgrade(self):
        return self.state == UPGRADE_AVAILABLE

    @property
    def is_noop(self):
        return self.state == ALREADY_CURRENT

    @property
    def blocked(self):
        return self.state in BLOCKING_UPGRADE_STATES


def assess_upgrade(
    current,
    target,
    *,
    current_version=None,
    target_version=None,
    allow_unverified=False,
    target_rolling=False,
):
    """Classify a move from ``current`` to ``target`` build identity.

    ``current``/``target`` are :class:`ImageIdentity` values (all-``None`` when
    an image could not be inspected). ``current_version``/``target_version`` are
    the comparable SemVer keys of the running and target release tags, or
    ``None`` when a side is ``latest`` / not a version tag. The policy, in
    order:

    1. Same digest -> :data:`ALREADY_CURRENT` (no real upgrade needed).
    2. ``target_rolling`` (the target is the ``latest`` channel) -> a different
       image is always a forward :data:`UPGRADE_AVAILABLE` (basis ``channel``).
       ``latest`` is a rolling channel, not a fixed version, so selecting it
       tracks the newest main build regardless of any build-serial ordering; it
       must never be blocked as older-than-running or already-current (the same
       image is caught by step 1).
    3. Both sides carry comparable SemVer -> target must be ``>=`` current; a
       lower target is :data:`DOWNGRADE_BLOCKED` regardless of build serial.
       When neither side carries a build serial this is the *legacy* SemVer
       fallback and the upgrade verdict carries :data:`LEGACY_SEMVER_WARNING`.
    4. SemVer is not comparable (a ``latest`` side) but both build serials are
       known -> a higher target serial is an upgrade, otherwise
       :data:`OLDER_THAN_RUNNING_BUILD`.
    5. Nothing can prove an upgrade -> :data:`IDENTITY_UNKNOWN`, unless
       ``allow_unverified`` is set (the ``ADMIN_ALLOW_LEGACY_UNVERIFIED_UPGRADES``
       test override), in which case the move is allowed as
       :data:`LEGACY_UNVERIFIED` with :data:`LEGACY_UNVERIFIED_WARNING`. The
       override never turns a SemVer-proven downgrade (step 3) into an upgrade.
    """

    current = current or ImageIdentity()
    target = target or ImageIdentity()

    if current.digest and target.digest and current.digest == target.digest:
        return UpgradeAssessment(ALREADY_CURRENT, "digest")

    if target_rolling:
        return UpgradeAssessment(UPGRADE_AVAILABLE, "channel")

    if current_version is not None and target_version is not None:
        legacy = current.build_serial is None and target.build_serial is None
        if target_version > current_version:
            return UpgradeAssessment(
                UPGRADE_AVAILABLE,
                "semver",
                warning=LEGACY_SEMVER_WARNING if legacy else None,
            )
        if target_version == current_version:
            return UpgradeAssessment(ALREADY_CURRENT, "semver")
        return UpgradeAssessment(DOWNGRADE_BLOCKED, "semver")

    if current.build_serial is not None and target.build_serial is not None:
        if target.build_serial > current.build_serial:
            return UpgradeAssessment(UPGRADE_AVAILABLE, "build_serial")
        if target.build_serial == current.build_serial:
            return UpgradeAssessment(ALREADY_CURRENT, "build_serial")
        return UpgradeAssessment(OLDER_THAN_RUNNING_BUILD, "build_serial")

    if allow_unverified:
        return UpgradeAssessment(
            UPGRADE_AVAILABLE, LEGACY_UNVERIFIED, warning=LEGACY_UNVERIFIED_WARNING
        )
    return UpgradeAssessment(IDENTITY_UNKNOWN, "none")
