# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve and verify one strictly-paired Admin/EMS *system build*.

A managed installation selects exactly one system build (e.g. ``v0.8.0``,
``v0.8.0-RC1``, ``latest`` as a bootstrap, or an immutable
``dev-<branch>-<sha>-<run>-<attempt>`` tag). That single tag names *two* images — the Admin
console image and the EMS controller image — which are only the same system
build when their OCI build-identity labels agree:

    org.opencontainers.image.revision   (git revision)
    de.basecubedev.ems.build_id         (paired build id)
    de.basecubedev.ems.channel          (release channel)

and, for stable/RC builds, ``de.basecubedev.ems.release_tag``.

The browser only ever sends a *tag*. Both image repositories are fixed
server-side (:data:`admin.admin_update.ADMIN_IMAGE_REPO` /
:data:`~admin.admin_update.EMS_IMAGE_REPO`), so a tag can never smuggle an image
ref, registry or digest. This module only *resolves and validates*: it never
edits compose or starts containers (that is the alignment service's job). No
Setup resource is fetched here — resolution must succeed first.
"""

import re
import threading
from dataclasses import dataclass
from enum import Enum
from time import monotonic

from admin.admin_update import ADMIN_IMAGE_REPO, EMS_IMAGE_REPO
from admin.image_identity import ImageIdentity, identify_image
from admin.releases import TAG_PATTERN
from admin.system_build_id import parse_system_build_id, validate_system_build_id

# Release channels a system build can belong to.
CHANNEL_STABLE = "stable"
CHANNEL_RC = "rc"
CHANNEL_LATEST = "latest"
CHANNEL_DEV = "development"
CHANNEL_UNKNOWN = "unknown"

# Channels whose ``release_tag`` label must equal the requested tag.
_RELEASE_TAG_CHANNELS = frozenset({CHANNEL_STABLE, CHANNEL_RC})

# An immutable development tag ends with ``-<shortsha>-<runid>-<attempt>``.
# Run attempts are part of the install identity because GitHub retries retain a
# run id while rebuilding/pushing new artifacts.
_IMMUTABLE_DEV_SUFFIX = re.compile(r"-[0-9a-f]{7,40}-[1-9]\d*-[1-9]\d*$")
_RC_MARKER = re.compile(r"-[rR][cC]")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

# The stable code + actionable copy for a registry pull-rate-limit while
# resolving a System Build. The concrete Docker failure detail is redacted before
# it reaches the browser; only this actionable message is surfaced.
SYSTEM_BUILD_RATE_LIMIT_CODE = "system_build_registry_rate_limited"
SYSTEM_BUILD_RATE_LIMIT_MESSAGE = (
    "GitHub Container Registry rate limit reached.\n\n"
    "No installation changes were made. Wait before retrying, or authenticate "
    "Docker with a GitHub account to increase the available request quota."
)
# Registry throttle signals recognised on a raw pull exception. The typed
# ``image_pull_rate_limited`` code from ``admin.deployment.DockerCli`` is the
# primary signal; the text markers catch a non-DockerCli pull double.
_RATE_LIMIT_MARKERS = (
    "toomanyrequests",
    "too many requests",
    "rate limit exceeded",
    "pull rate limit",
    "reached your pull rate limit",
    "denied due to rate limit",
)


def _is_registry_rate_limit(exc) -> bool:
    """True when a pull exception signals a registry pull-rate-limit (429)."""

    if getattr(exc, "code", None) == "image_pull_rate_limited":
        return True
    text = f"{getattr(exc, 'message', '') or ''} {exc}".lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _is_digest_pinned(image_ref: str) -> bool:
    """True when a ref pins an immutable content digest (``repo@sha256:...``)."""

    return "@sha256:" in str(image_ref or "")


# An OCI content digest: ``algorithm:encoded`` (e.g. ``sha256:<hex>``). The
# encoded part is deliberately not required to be 64-hex so opaque test digests
# and future algorithms are accepted, while whitespace, ``@``, ``/`` and other
# reference-breaking characters are still rejected.
_OCI_DIGEST_RE = re.compile(r"[a-z0-9]+(?:[.+_-][a-z0-9]+)*:[A-Za-z0-9=_-]+")


class DigestReferenceError(ValueError):
    """A digest-pinned image reference could not be safely constructed."""


def _repository_of(image_ref: str) -> str:
    """Return an image ref's repository, dropping any tag or digest.

    A registry ``host:port`` is preserved: only a tag colon in the final path
    segment (the image name) is stripped, never the host port.
    """

    ref = str(image_ref or "").strip()
    base = ref.split("@", 1)[0]  # drop an existing digest
    head, _, name = base.rpartition("/")
    name = name.split(":", 1)[0]  # strip a :tag from the image name only
    if not name:
        raise DigestReferenceError(f"malformed image reference: {image_ref!r}")
    return f"{head}/{name}" if head else name


def digest_pinned_ref(image_ref, digest, *, require_repo=None) -> str:
    """Combine an image repository with a verified content digest.

    Returns ``repository@<digest>``: any tag is stripped, a registry host port is
    preserved and the repository path is kept. An already digest-pinned input is
    accepted only when its digest equals ``digest`` — a conflicting digest fails
    closed. When ``require_repo`` is given the repository must equal it, so a
    caller that requires the official repository can never persist a different
    one. This is the single shared builder so no caller splits on every colon.
    """

    text = str(digest or "").strip()
    if not _OCI_DIGEST_RE.fullmatch(text):
        raise DigestReferenceError(f"malformed or missing digest: {digest!r}")
    ref = str(image_ref or "").strip()
    if not ref:
        raise DigestReferenceError("an image reference is required")
    if "@" in ref:
        existing = ref.split("@", 1)[1].strip()
        if existing != text:
            raise DigestReferenceError(
                "the image reference is already pinned to a different digest"
            )
    repository = _repository_of(ref)
    if require_repo is not None and repository != require_repo:
        raise DigestReferenceError(
            f"unexpected image repository {repository!r}; expected {require_repo!r}"
        )
    return f"{repository}@{text}"


class SystemBuildError(Exception):
    """A requested system build could not be resolved into a verified pair.

    ``code`` is a stable machine string surfaced to the UI/tests:
    ``system_build_invalid_tag``, ``system_build_dev_floating``,
    ``system_build_admin_unavailable``, ``system_build_ems_unavailable``,
    ``system_build_mismatch``, ``system_build_registry_rate_limited``.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SystemBuild:
    """An immutable, verified Admin/EMS system-build pair.

    Carries no credentials, registry tokens or host paths — only the two fixed
    official image refs, their resolved digests, and the shared build identity.
    """

    requested_tag: str
    canonical_tag: str
    channel: str
    revision: str
    build_id: str
    admin_image: str
    admin_digest: str
    ems_image: str
    ems_digest: str
    release_tag: str | None = None
    build_serial: int | None = None

    def as_dict(self) -> dict:
        return {
            "requested_tag": self.requested_tag,
            "canonical_tag": self.canonical_tag,
            "channel": self.channel,
            "revision": self.revision,
            "build_id": self.build_id,
            "admin_image": self.admin_image,
            "admin_digest": self.admin_digest,
            "ems_image": self.ems_image,
            "ems_digest": self.ems_digest,
            "release_tag": self.release_tag,
        }


def classify_channel(tag: str) -> str:
    """Classify a *validated* tag into its release channel."""

    text = str(tag or "").strip()
    if text == "latest":
        return CHANNEL_LATEST
    if text == "local" or text.startswith("local-"):
        return CHANNEL_DEV
    if text.startswith("dev-") or text.startswith("dev_"):
        return CHANNEL_DEV
    if _RC_MARKER.search(text):
        return CHANNEL_RC
    if re.match(r"^v?\d+\.\d+", text):
        return CHANNEL_STABLE
    return CHANNEL_UNKNOWN


def is_immutable_dev_tag(tag: str) -> bool:
    """True when a development tag pins ``-<sha>-<run>-<attempt>``."""

    return bool(_IMMUTABLE_DEV_SUFFIX.search(str(tag or "").strip()))


def is_development_build_tag(tag: str) -> bool:
    """True when a tag names a development build (needs explicit acknowledgement)."""

    return classify_channel(tag) == CHANNEL_DEV


# --- compatibility mode ---------------------------------------------------
#
# A resolved System Build belongs to one compatibility mode, decided purely by
# its build-id kind. Modern paired builds get strict Admin+EMS pairing and
# embedded-resource verification. A legacy release (a pre-contract CI build id)
# and a local checkout are deliberate compatibility exceptions.

COMPAT_MODERN_PAIRED = "modern_paired"
COMPAT_LEGACY_RELEASE = "legacy_release"
COMPAT_LOCAL = "local"


def _build_field(build, name):
    if isinstance(build, dict):
        return build.get(name)
    return getattr(build, name, None)


def system_build_compatibility(build) -> str:
    """Classify a resolved build (object or dict) into its compatibility mode.

    An unrecognized build id resolves to the strict modern mode, so a
    compatibility exception is never granted by default.
    """

    try:
        parsed = parse_system_build_id(_build_field(build, "build_id"))
    except (TypeError, ValueError):
        return COMPAT_MODERN_PAIRED
    if parsed.is_local:
        return COMPAT_LOCAL
    if parsed.is_legacy:
        return COMPAT_LEGACY_RELEASE
    return COMPAT_MODERN_PAIRED


def system_build_keeps_current_admin(build) -> bool:
    """True when the running modern Admin must stay the orchestration layer.

    A legacy release predates the embedded-resource bundle and the modern
    transition/resume protocol, so replacing the running Admin with the
    historical Admin image would break the workflow. The historical EMS image
    still remains the install target and the resources come from the exact
    historical tag/revision.
    """

    return system_build_compatibility(build) == COMPAT_LEGACY_RELEASE


# --- resource strategy ----------------------------------------------------
#
# A compatibility mode also decides *where* a build's Setup resources come from.
# Modern paired builds (and a local checkout) carry a verified embedded bundle
# inside the running Admin image. A legacy release predates that bundle, so its
# resources must be fetched from the exact historical release archive instead —
# never the running Admin's embedded copy and never ``main``.


class BuildResourceStrategy(str, Enum):
    """Where a System Build's Setup resources are sourced from."""

    EMBEDDED = "embedded"
    RELEASE_ARCHIVE = "release_archive"


_COMPAT_RESOURCE_STRATEGY = {
    COMPAT_MODERN_PAIRED: BuildResourceStrategy.EMBEDDED,
    COMPAT_LOCAL: BuildResourceStrategy.EMBEDDED,
    COMPAT_LEGACY_RELEASE: BuildResourceStrategy.RELEASE_ARCHIVE,
}


def resource_strategy_for_compatibility(mode: str) -> str:
    """Map a compatibility mode to its resource strategy, or fail closed.

    An unmapped mode raises rather than defaulting, so a new compatibility mode
    can never silently reuse the embedded strategy.
    """

    try:
        return _COMPAT_RESOURCE_STRATEGY[mode].value
    except KeyError as exc:
        raise SystemBuildError(
            "system_build_resource_strategy_unknown",
            f"no resource strategy is defined for compatibility mode {mode!r}",
        ) from exc


def system_build_resource_strategy(build) -> str:
    """Return the :class:`BuildResourceStrategy` value for a resolved build."""

    return resource_strategy_for_compatibility(system_build_compatibility(build))


def validate_system_build_tag(requested_tag: str) -> str:
    """Return the trimmed tag or raise ``SystemBuildError('system_build_invalid_tag')``.

    Reuses the strict release :data:`~admin.releases.TAG_PATTERN`, so any tag
    carrying a registry/repo/digest (``ghcr.io/...``, ``repo/image``, ``x:y``),
    path traversal, whitespace or shell metacharacter is rejected before any
    image is touched — the browser can never inject an image ref through a tag.
    """

    tag = str(requested_tag or "").strip()
    if not tag or not TAG_PATTERN.fullmatch(tag):
        raise SystemBuildError(
            "system_build_invalid_tag", f"unsupported system build tag: {requested_tag!r}"
        )
    return tag


class SystemBuildResolver:
    """Resolve a requested tag into a verified :class:`SystemBuild` pair.

    Injectable ``docker`` exposes ``pull(ref)`` and ``inspect_image(ref)`` (the
    real :class:`admin.deployment.DockerCli` or a test double). ``allow_floating_dev``
    keeps the strict default (floating dev aliases are rejected) but lets a caller
    opt into an explicit-confirmation flow later.
    """

    def __init__(self, *, docker, admin_repo=ADMIN_IMAGE_REPO, ems_repo=EMS_IMAGE_REPO,
                 allow_floating_dev=False, development_build_source=None):
        self._docker = docker
        self._admin_repo = admin_repo
        self._ems_repo = ems_repo
        self._allow_floating_dev = allow_floating_dev
        self._development_build_source = development_build_source

    def resolve(self, requested_tag: str) -> SystemBuild:
        tag = validate_system_build_tag(requested_tag)
        channel = classify_channel(tag)
        repository_local = tag == "local"
        if (
            channel == CHANNEL_DEV
            and not repository_local
            and not is_immutable_dev_tag(tag)
            and not self._allow_floating_dev
        ):
            raise SystemBuildError(
                "system_build_dev_floating",
                "floating development aliases are not canonical install targets; "
                "use the immutable dev-<branch>-<sha>-<run>-<attempt> tag",
            )

        admin_ref = f"{self._admin_repo}:{tag}"
        ems_ref = f"{self._ems_repo}:{tag}"
        descriptor = self._development_descriptor(tag, channel, admin_ref, ems_ref)
        admin_pull_ref = (
            f'{self._admin_repo}@{descriptor["admin_digest"]}'
            if descriptor is not None
            else admin_ref
        )
        ems_pull_ref = (
            f'{self._ems_repo}@{descriptor["ems_digest"]}'
            if descriptor is not None
            else ems_ref
        )
        admin_identity = self._pull_and_identify(
            admin_pull_ref, "admin", repository_local=repository_local
        )
        ems_identity = self._pull_and_identify(
            ems_pull_ref, "ems", repository_local=repository_local
        )
        self._validate_pair(tag, channel, admin_identity, ems_identity)
        if descriptor is not None:
            self._validate_development_descriptor(
                descriptor, admin_identity, ems_identity
            )

        return SystemBuild(
            requested_tag=str(requested_tag).strip(),
            canonical_tag=tag,
            channel=admin_identity.channel or channel,
            revision=admin_identity.revision,
            build_id=admin_identity.build_id,
            admin_image=admin_ref,
            admin_digest=admin_identity.digest,
            ems_image=ems_ref,
            ems_digest=ems_identity.digest,
            release_tag=admin_identity.release_tag,
            build_serial=admin_identity.build_serial,
        )

    def _development_descriptor(self, tag, channel, admin_ref, ems_ref):
        source = self._development_build_source
        if channel != CHANNEL_DEV or tag == "local" or source is None:
            return None
        try:
            descriptor = source(tag)
        except Exception as exc:
            raise SystemBuildError(
                "system_build_mismatch",
                "the Development catalogue entry could not be verified",
            ) from exc
        if not isinstance(descriptor, dict):
            raise SystemBuildError(
                "system_build_mismatch",
                "the Development build is not an installable catalogue entry",
            )
        valid = all(
            (
                descriptor.get("tag") == tag,
                descriptor.get("channel") == CHANNEL_DEV,
                descriptor.get("installable") is True,
                descriptor.get("build_id") == tag,
                descriptor.get("admin_image") == admin_ref,
                descriptor.get("ems_image") == ems_ref,
                bool(_DIGEST_PATTERN.fullmatch(str(descriptor.get("admin_digest") or ""))),
                bool(_DIGEST_PATTERN.fullmatch(str(descriptor.get("ems_digest") or ""))),
            )
        )
        if not valid:
            raise SystemBuildError(
                "system_build_mismatch",
                "the Development catalogue entry does not match the selected build",
            )
        return descriptor

    @staticmethod
    def _validate_development_descriptor(descriptor, admin, ems):
        expected = (
            ("admin digest", admin.digest, descriptor["admin_digest"]),
            ("EMS digest", ems.digest, descriptor["ems_digest"]),
            ("revision", admin.revision, descriptor.get("revision")),
            ("build ID", admin.build_id, descriptor.get("build_id")),
        )
        for name, actual, wanted in expected:
            if actual != wanted:
                raise SystemBuildError(
                    "system_build_mismatch",
                    f"the pulled Development {name} differs from the catalogue entry",
                )

    def _pull_and_identify(self, image_ref, role, *, repository_local=False) -> ImageIdentity:
        code = f"system_build_{role}_unavailable"
        reused = None
        if not repository_local:
            # An exact digest-pinned image already present locally is reused as-is:
            # a content digest cannot move, so its identity is proven by inspection
            # without contacting the registry. A matching *tag* alone is never
            # proof and always resolves through a pull.
            if _is_digest_pinned(image_ref):
                candidate = identify_image(self._docker, image_ref)
                if candidate.digest is not None:
                    reused = candidate
            if reused is None:
                try:
                    self._docker.pull(image_ref)
                except Exception as exc:
                    if _is_registry_rate_limit(exc):
                        raise SystemBuildError(
                            SYSTEM_BUILD_RATE_LIMIT_CODE,
                            SYSTEM_BUILD_RATE_LIMIT_MESSAGE,
                        ) from exc
                    raise SystemBuildError(
                        code, f"the {role} image could not be pulled: {image_ref} ({exc})"
                    ) from exc
        identity = reused or identify_image(self._docker, image_ref)
        if identity.digest is None:
            action = "found locally" if repository_local else "inspected"
            raise SystemBuildError(
                code, f"the {role} image could not be {action}: {image_ref}"
            )
        return identity

    def _validate_pair(self, tag, channel, admin: ImageIdentity, ems: ImageIdentity) -> None:
        def _mismatch(message):
            raise SystemBuildError("system_build_mismatch", message)

        # Required identity metadata must be present on both images.
        for role, identity in (("admin", admin), ("ems", ems)):
            if not identity.version_label:
                _mismatch(f"{role} image is missing its version label")
            if not identity.revision:
                _mismatch(f"{role} image is missing its revision label")
            if not identity.build_id:
                _mismatch(f"{role} image is missing its build_id label")
            if not identity.channel:
                _mismatch(f"{role} image is missing its channel label")
            try:
                validate_system_build_id(identity.build_id)
            except ValueError as exc:
                _mismatch(f"{role} image has an invalid build_id label: {exc}")

        # A locally-built image (local build id) published under the rolling
        # ``latest`` tag is a mis-tag, not a registry latest build. Surface an
        # actionable rebuild error rather than silently reinterpreting it.
        if tag == "latest" and any(
            parse_system_build_id(identity.build_id).is_local
            for identity in (admin, ems)
        ):
            raise SystemBuildError(
                "system_build_local_mistagged_latest",
                "This local image uses the rolling latest tag. Rebuild it with "
                "deploy/admin/start-admin-setup.sh to register it as a local "
                "System Build.",
            )

        # The pair identity: revision, build_id and channel must all agree.
        if admin.revision != ems.revision:
            _mismatch("admin and ems image revisions differ")
        if admin.build_id != ems.build_id:
            _mismatch("admin and ems image build ids differ")
        if admin.channel != ems.channel:
            _mismatch("admin and ems image channels differ")
        if admin.version_label != ems.version_label:
            _mismatch("admin and ems image version labels differ")
        if admin.version_label != tag:
            _mismatch("image version label does not match the requested build tag")
        if channel != CHANNEL_UNKNOWN and admin.channel != channel:
            _mismatch(
                f"image channel {admin.channel!r} does not match requested {channel!r} build"
            )

        # A canonical development tag is itself the immutable build identity.
        # Binding both labels to the requested tag detects a moved/overwritten
        # tag even when the two repositories were overwritten together.
        if tag == "local":
            expected_local_id = f"local-{admin.revision[:7]}"
            if admin.build_id not in {
                expected_local_id,
                f"{expected_local_id}-dirty",
            }:
                _mismatch("local build_id does not match the image revision")
            if admin.release_tag != tag or ems.release_tag != tag:
                _mismatch("local release_tag does not match the local System Build")
        elif channel == CHANNEL_DEV:
            if admin.build_id != tag:
                _mismatch("development build_id does not match the requested tag")
            if admin.release_tag != tag or ems.release_tag != tag:
                _mismatch(
                    "development release_tag does not match the requested canonical tag"
                )

        # Stable/RC builds must also match the expected release tag on both.
        if channel in _RELEASE_TAG_CHANNELS or channel == CHANNEL_LATEST:
            if admin.release_tag != tag or ems.release_tag != tag:
                _mismatch(
                    f"release_tag label does not match the requested build tag {tag}"
                )


# --- verified-resolution reuse --------------------------------------------
#
# Resolving a tag pulls and verifies two images; that is the one expensive,
# registry-touching step. Selecting/browsing builds must never trigger it, and
# once an explicit verification has resolved a tag every later action in the same
# operation (Continue, Update Admin Server, a re-render) must reuse the pinned
# result instead of pulling again. The cache is keyed by the requested tag and
# holds the fully-verified, digest-pinned pair, so a reused entry is exactly the
# build the user verified — never a different build and never a silently-moved
# tag. Concurrent resolutions of the same tag coalesce into one pull.


class CachingBuildResolver:
    """Resolve verified System Build pairs once and reuse the pinned result.

    Wraps a :class:`SystemBuildResolver` (or any object exposing ``resolve``). A
    successful resolution is cached by requested tag together with a bounded TTL;
    a second resolution of the same tag returns the identical verified
    :class:`SystemBuild` without pulling. Selecting a different tag uses a
    different key. Concurrent resolutions of one tag serialize on a per-tag lock
    so exactly one underlying pull runs; different tags resolve in parallel. The
    wrapped resolver's other attributes are exposed transparently.
    """

    DEFAULT_TTL_SECONDS = 1800

    def __init__(self, resolver, *, ttl_seconds=DEFAULT_TTL_SECONDS, clock=None):
        self._resolver = resolver
        self._ttl = ttl_seconds
        self._clock = clock or monotonic
        self._master = threading.Lock()
        self._entries = {}
        self._locks = {}

    def resolve(self, requested_tag):
        tag = str(requested_tag or "")
        cached = self._cached(tag)
        if cached is not None:
            return cached
        with self._tag_lock(tag):
            cached = self._cached(tag)
            if cached is not None:
                return cached
            build = self._resolver.resolve(requested_tag)
            with self._master:
                self._entries[tag] = (build, self._clock() + self._ttl)
            return build

    def invalidate(self, requested_tag=None):
        """Drop one tag's verified entry, or all entries when ``None``."""

        with self._master:
            if requested_tag is None:
                self._entries.clear()
            else:
                self._entries.pop(str(requested_tag or ""), None)

    def _cached(self, tag):
        with self._master:
            entry = self._entries.get(tag)
            if entry is None:
                return None
            build, deadline = entry
            if self._clock() >= deadline:
                self._entries.pop(tag, None)
                return None
            return build

    def _tag_lock(self, tag):
        with self._master:
            lock = self._locks.get(tag)
            if lock is None:
                lock = threading.Lock()
                self._locks[tag] = lock
            return lock

    def __getattr__(self, name):
        # Only reached for attributes this wrapper does not define. Private names
        # (including its own fields before __init__ runs) never forward, so this
        # can never recurse on ``_resolver`` and never shadows resolve().
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._resolver, name)


# --- alignment decision ---------------------------------------------------
#
# Alignment has two independent dimensions: whether the *running* Admin content
# matches the selected build, and whether the *persistent* Compose image
# reference points at the canonical selected build. Equal digests with a stale
# persistent tag (``admin:latest`` currently == ``v0.8.0``) is NOT fully aligned:
# the next pull/recreate could silently move Admin to another version.

ALIGN_ALIGNED = "aligned"
ALIGN_RETAG_REQUIRED = "retag_required"
ALIGN_ADMIN_RECREATE_REQUIRED = "admin_recreate_required"
ALIGN_ADMIN_UPDATE_REQUIRED = "admin_update_required"
ALIGN_SYSTEM_BUILD_MISMATCH = "system_build_mismatch"

# Decisions that require the user to update/re-tag Admin before continuing.
ALIGNMENT_UPDATE_DECISIONS = frozenset(
    {ALIGN_RETAG_REQUIRED, ALIGN_ADMIN_RECREATE_REQUIRED, ALIGN_ADMIN_UPDATE_REQUIRED}
)


@dataclass(frozen=True)
class AlignmentDecision:
    """What must happen to align Admin with the selected system build."""

    decision: str
    runtime_aligned: bool
    persistent_aligned: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "decision": self.decision,
            "runtime_aligned": self.runtime_aligned,
            "persistent_aligned": self.persistent_aligned,
            "reason": self.reason,
        }


def _persistent_ref_is_canonical(persistent_ref, target: SystemBuild) -> bool:
    """True when the persisted Compose image ref pins the canonical build.

    Accepts a full image ref (``ghcr.io/...admin:v0.8.0``) or a bare tag
    (``v0.8.0``). A repo, when present, must be the official Admin repo.
    """

    text = str(persistent_ref or "").strip()
    if not text:
        return False
    last_segment = text.rsplit("/", 1)[-1]
    if ":" in last_segment:
        repo, _, tag = text.rpartition(":")
        if repo != target.admin_image.rpartition(":")[0]:
            return False
    else:
        tag = text  # bare tag
    return tag == target.canonical_tag


def decide_alignment(running_admin: ImageIdentity, target: SystemBuild, *,
                     persistent_ref, local_target: ImageIdentity | None = None) -> AlignmentDecision:
    """Decide how to align the running Admin with ``target``.

    ``running_admin`` is the currently-running Admin identity; ``persistent_ref``
    is the image ref/tag written in the Admin compose/env; ``local_target`` is the
    locally-present target image identity when known (lets a same-digest local
    image be applied by recreate instead of a full update).
    """

    running = running_admin or ImageIdentity()
    persistent_aligned = _persistent_ref_is_canonical(persistent_ref, target)
    runtime_aligned = bool(
        running.digest and target.admin_digest and running.digest == target.admin_digest
    )

    if runtime_aligned:
        if persistent_aligned:
            return AlignmentDecision(
                ALIGN_ALIGNED, True, True, "running content and persistent ref match the build"
            )
        return AlignmentDecision(
            ALIGN_RETAG_REQUIRED, True, False,
            "running content matches but the persistent compose ref is stale",
        )

    # Runtime content differs from the target.
    if (
        local_target is not None
        and local_target.digest == target.admin_digest
        and persistent_aligned
    ):
        return AlignmentDecision(
            ALIGN_ADMIN_RECREATE_REQUIRED, False, True,
            "target image is present locally; recreate the Admin container",
        )
    return AlignmentDecision(
        ALIGN_ADMIN_UPDATE_REQUIRED, False, persistent_aligned,
        "the running Admin must be updated to the selected build",
    )


# --- upgrade direction ----------------------------------------------------
#
# Guided Upgrade only ever moves forward. The forward check compares the
# running EMS identity against the resolved target System Build identity — no
# second ``ReleaseManager.verify_upgrade_target`` decision. Development targets
# cannot be SemVer-ordered and are testing builds gated by explicit
# acknowledgement, so they are allowed rather than blocked.


@dataclass(frozen=True)
class UpgradeDirection:
    """Whether moving the running EMS build to a target is forward-safe."""

    allowed: bool
    state: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "state": self.state, "reason": self.reason}


def decide_upgrade_direction(running_ems, target: SystemBuild) -> UpgradeDirection:
    """Assess moving the running EMS build to ``target`` using identity only.

    ``running_ems`` is the running EMS :class:`ImageIdentity` (all-``None`` when
    it cannot be inspected). Only the resolved :class:`SystemBuild` identity and
    the running EMS identity settle the verdict; nothing is pulled or re-listed.

    The target identity carries the resolved build serial. Without it the
    serial fallback in :func:`assess_upgrade` cannot fire, so every move from a
    running build whose tag is not SemVer-comparable — ``latest`` above all —
    ends as ``identity_unknown`` and is refused. The release listing already
    supplies the serial for the same decision; both paths must agree.
    """

    from admin.image_identity import assess_upgrade
    from admin.releases import BLOCKING_UPGRADE_REASONS, _version

    if isinstance(running_ems, dict):
        running_ems = ImageIdentity(
            image_ref=running_ems.get("image_ref"),
            digest=running_ems.get("digest"),
            version_label=running_ems.get("version_label"),
            revision=running_ems.get("revision"),
            channel=running_ems.get("channel"),
            build_serial=running_ems.get("build_serial"),
            build_id=running_ems.get("build_id"),
            release_tag=running_ems.get("release_tag"),
        )
    running = running_ems or ImageIdentity()
    target_identity = ImageIdentity(
        image_ref=target.ems_image,
        digest=target.ems_digest,
        version_label=target.canonical_tag,
        revision=target.revision,
        channel=target.channel,
        build_serial=target.build_serial,
        build_id=target.build_id,
        release_tag=target.release_tag or target.canonical_tag,
    )
    running_tag = running.release_tag or running.version_label
    assessment = assess_upgrade(
        running,
        target_identity,
        current_version=_version(running_tag) if running_tag else None,
        target_version=_version(target.canonical_tag),
        allow_unverified=(target.channel == CHANNEL_DEV),
        target_rolling=(target.channel == CHANNEL_LATEST),
    )
    return UpgradeDirection(
        allowed=not assessment.blocked,
        state=assessment.state,
        reason=assessment.warning
        or BLOCKING_UPGRADE_REASONS.get(assessment.state, ""),
    )
