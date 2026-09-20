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

from admin.system_build_id import development_line


LABEL_VERSION = "org.opencontainers.image.version"
LABEL_REVISION = "org.opencontainers.image.revision"
LABEL_CHANNEL = "de.basecubedev.ems.channel"
LABEL_CONTAINS_RELEASE = "de.basecubedev.ems.contains_release"

# The one channel whose images are numbered by a different workflow, so a build
# serial only orders builds that agree on this. ``admin.system_build`` imports
# it from here rather than keeping a second spelling.
CHANNEL_DEVELOPMENT = "development"
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
    contains_release: str | None = None
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
            "contains_release": self.contains_release,
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
        contains_release=_clean(safe_labels.get(LABEL_CONTAINS_RELEASE)),
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
# A lower target that stays inside the running release line: the same
# major and minor, an earlier patch. Permitted, and deliberately not in
# BLOCKING_UPGRADE_STATES -- see the note there.
ROLLBACK_AVAILABLE = "rollback_available"
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
#
# ROLLBACK_AVAILABLE is not among them. Undoing a bad patch is something an
# operator has to be able to do, and inside one release line the two builds read
# the same config schema and the same database: the store evolves by
# CREATE TABLE IF NOT EXISTS and ADD COLUMN, which an older build reads without
# noticing, and a config written under a schema this build does not know is
# refused by name in `ems.config` rather than misread. Leaving the line downward
# stays blocked, because that is where those two statements stop holding.
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


def release_line(version):
    """The ``(major, minor)`` a comparable version belongs to, or ``None``.

    Inside one line the two builds read the same config schema and the same
    database, so undoing a patch is a move an operator may make; the one-way
    migrations sit between lines. One definition, for the listing's downgrade
    guard and for every branch of the policy below.
    """

    return version[:2] if version else None


def same_release_line(a, b) -> bool:
    line_a = release_line(a)
    return line_a is not None and line_a == release_line(b)


def _development_line_of(identity):
    return development_line(identity.release_tag) or development_line(
        identity.build_id
    )


def _same_development_line(current, target) -> bool:
    """Whether two development builds come from the same branch.

    Two branches built past the same release declare the same line and are
    numbered by the same counter, so neither signal tells them apart; only the
    branch in the immutable tag does. A build that cannot name its branch is
    not on any line.
    """

    line = _development_line_of(current)
    return line is not None and line == _development_line_of(target)


def assess_upgrade(
    current,
    target,
    *,
    current_version=None,
    target_version=None,
    allow_unverified=False,
    target_rolling=False,
    target_contains_version=None,
    current_contains_version=None,
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
       lower target inside the same ``major.minor`` line is
       :data:`ROLLBACK_AVAILABLE` and any other lower target is
       :data:`DOWNGRADE_BLOCKED`, both regardless of build serial.
       When neither side carries a build serial this is the *legacy* SemVer
       fallback and the upgrade verdict carries :data:`LEGACY_SEMVER_WARNING`.
    4. Exactly one side is a development build, so the two build serials count
       runs of *different* workflows and are not an ordering. Only then, because
       within one counter the declared release is the same for both sides and
       distinguishes nothing. Each side then
       states the version it can -- its own SemVer, or the release it declares
       having been built on top of -- and those are read with the policy step 3
       uses: a lower target inside the running ``major.minor`` is
       :data:`ROLLBACK_AVAILABLE`, and anything lower across that boundary is
       :data:`DOWNGRADE_BLOCKED`. Equality is the one place the two kinds of
       target differ. A development build declaring the running release sits on
       top of it, so moving there is an upgrade; moving to the release itself
       drops those commits and is a rollback.
       The same policy reads a move from a rolling ``latest`` (or any running
       build without a version of its own) to a release, once the running
       image declares the release it was built past: a release inside that
       line is a rollback and stays selectable, one above it is an upgrade, and
       one in an older line is blocked. A build serial cannot say which line a
       target is in, only that it is older -- and "older" was refusing v0.8.4
       to a ``latest`` built from v0.8.4 plus a handful of commits.
    5. Both serials are known and count runs of the same workflow -> a higher
       target serial is an upgrade. This is what orders two development
       builds against each other, two rolling builds, and a running build that
       declares no release against anything. A lower serial is
       :data:`ROLLBACK_AVAILABLE` where both sides state the same
       ``major.minor`` -- going back to yesterday's experimental build is the
       same move as undoing a patch, and the serial says which is older, not
       whether the two share a line. For development builds the line is the
       branch as well: two branches built past the same release declare the
       same ``major.minor`` and count on the same counter, and only the
       immutable tag tells them apart, so a lower serial is a rollback only
       inside one branch, and a build that cannot name its branch is on no
       line. Anything else lower stays :data:`OLDER_THAN_RUNNING_BUILD`.
    6. Nothing can prove an upgrade -> :data:`IDENTITY_UNKNOWN`, unless
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
        if same_release_line(target_version, current_version):
            return UpgradeAssessment(ROLLBACK_AVAILABLE, "semver")
        return UpgradeAssessment(DOWNGRADE_BLOCKED, "semver")

    crosses_build_counters = (
        current.channel is not None
        and target.channel is not None
        and (current.channel == CHANNEL_DEVELOPMENT)
        != (target.channel == CHANNEL_DEVELOPMENT)
    )
    serials_order_these_two = (
        current.build_serial is not None
        and target.build_serial is not None
        and not crosses_build_counters
    )

    running_states = (
        current_version if current_version is not None else current_contains_version
    )
    target_states = (
        target_version if target_version is not None else target_contains_version
    )
    rolling_to_release = (
        current_version is None
        and current_contains_version is not None
        and target_version is not None
    )
    if (
        (crosses_build_counters or rolling_to_release)
        and running_states is not None
        and target_states is not None
    ):
        if target_states > running_states:
            return UpgradeAssessment(UPGRADE_AVAILABLE, "contains_release")
        if target_states == running_states:
            if target.channel == CHANNEL_DEVELOPMENT:
                return UpgradeAssessment(UPGRADE_AVAILABLE, "contains_release")
            return UpgradeAssessment(ROLLBACK_AVAILABLE, "contains_release")
        if same_release_line(target_states, running_states):
            return UpgradeAssessment(ROLLBACK_AVAILABLE, "contains_release")
        return UpgradeAssessment(DOWNGRADE_BLOCKED, "contains_release")

    if serials_order_these_two:
        if target.build_serial > current.build_serial:
            return UpgradeAssessment(UPGRADE_AVAILABLE, "build_serial")
        if target.build_serial == current.build_serial:
            return UpgradeAssessment(ALREADY_CURRENT, "build_serial")
        same_line = same_release_line(target_states, running_states)
        if same_line and CHANNEL_DEVELOPMENT in (current.channel, target.channel):
            same_line = _same_development_line(current, target)
        if same_line:
            return UpgradeAssessment(ROLLBACK_AVAILABLE, "build_serial")
        return UpgradeAssessment(OLDER_THAN_RUNNING_BUILD, "build_serial")

    if allow_unverified:
        return UpgradeAssessment(
            UPGRADE_AVAILABLE, LEGACY_UNVERIFIED, warning=LEGACY_UNVERIFIED_WARNING
        )
    return UpgradeAssessment(IDENTITY_UNKNOWN, "none")
