# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve the readable installed EMS release identity from authoritative state.

A running EMS container is the active baseline. When its immutable image identity
cannot be read, the installed release is unknown and no persisted Compose or
Known-Good record may be presented as the running one. Only when no EMS container
is running (absent, stopped, Docker unavailable) does resolution fall back to the
digest-pinned Compose image's OCI labels, a digest-matching Known-Good record, or
a legacy concrete Compose tag. A prepared or downloaded release is never the
installed baseline, and a release tag is never derived from the digest text.

This is the single shared installed-image identity helper; both
:class:`admin.releases.ReleaseManager` and the Maintenance Overview use it (via
:func:`release_tag_from_labels` and :func:`running_image_ref`) so they cannot
disagree about the installed release.
"""

from dataclasses import dataclass

from admin.container_names import DEFAULT_EMS_CONTAINER
from admin.image_identity import (
    LABEL_RELEASE_TAG,
    LABEL_VERSION,
    identify_image,
)

SOURCE_RUNNING_CONTAINER = "running_container"
SOURCE_RUNNING_CONTAINER_UNKNOWN = "running_container_unknown"
SOURCE_COMPOSE_IMAGE = "compose_image"
SOURCE_KNOWN_GOOD = "known_good"
SOURCE_LEGACY_COMPOSE = "legacy_compose"
SOURCE_UNKNOWN = "unknown"

PROBE_ABSENT = "absent"
PROBE_STOPPED = "stopped"
PROBE_RUNNING_IDENTIFIED = "running_identified"
PROBE_RUNNING_UNIDENTIFIED = "running_unidentified"
PROBE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class InstalledReleaseIdentity:
    """The installed EMS release, and which authoritative source proved it."""

    tag: str | None
    image_ref: str | None
    digest: str | None
    build_id: str | None
    revision: str | None
    source: str


@dataclass(frozen=True)
class RunningReleaseProbe:
    """Running-container presence, kept separate from identity success."""

    running: bool
    identity: InstalledReleaseIdentity | None
    status: str


_UNKNOWN = InstalledReleaseIdentity(None, None, None, None, None, SOURCE_UNKNOWN)
_UNKNOWN_RUNNING = InstalledReleaseIdentity(
    None, None, None, None, None, SOURCE_RUNNING_CONTAINER_UNKNOWN
)


def release_tag_from_labels(labels):
    """Return the readable release from OCI labels: release_tag, then version.

    Returns the raw label string (or ``None``); it is intentionally not narrowed
    to a concrete tag here so the Maintenance Overview can still show ``latest``.
    """

    if not isinstance(labels, dict):
        return None
    tag = labels.get(LABEL_RELEASE_TAG) or labels.get(LABEL_VERSION)
    text = str(tag).strip() if isinstance(tag, str) else ""
    return text or None


def _concrete_tag(tag):
    from admin.releases import TAG_PATTERN

    text = str(tag or "").strip()
    if not text or text.lower() == "latest":
        return None
    return text if TAG_PATTERN.fullmatch(text) else None


def _compose_digest(compose_ref):
    ref = str(compose_ref or "")
    if "@sha256:" not in ref:
        return None
    return ref.split("@", 1)[1].strip() or None


def _concrete_ref_tag(image_ref):
    ref = str(image_ref or "").strip()
    if not ref or "@sha256:" in ref:
        return None
    last = ref.rsplit("/", 1)[-1]
    return _concrete_tag(ref.rsplit(":", 1)[1]) if ":" in last else None


def _running_image_ref(docker, container, container_name):
    get_id = getattr(docker, "inspect_container_image_id", None)
    if callable(get_id):
        try:
            image_id = get_id(container_name)
        except Exception:
            image_id = None
        if image_id:
            return str(image_id).strip() or None
    return str(container.get("image") or "").strip() or None


def running_image_ref(docker, container_name=DEFAULT_EMS_CONTAINER):
    """Immutable image ref of the RUNNING EMS container, or ``None``.

    Prefers ``inspect_container_image_id`` so a tag moved after the container
    started cannot change the perceived running image; falls back to the mutable
    ``docker ps`` image string only when no immutable id is available.
    """

    if docker is None:
        return None
    inspect_container = getattr(docker, "inspect_container", None)
    if not callable(inspect_container):
        return None
    try:
        container = inspect_container(container_name)
    except Exception:
        return None
    if not container or str(container.get("status") or "").lower() != "running":
        return None
    return _running_image_ref(docker, container, container_name)


def probe_running_release(docker, container_name=DEFAULT_EMS_CONTAINER):
    """Classify the running EMS container's presence and identity."""

    inspect_container = getattr(docker, "inspect_container", None)
    if not callable(inspect_container):
        return RunningReleaseProbe(False, None, PROBE_UNAVAILABLE)
    try:
        container = inspect_container(container_name)
    except Exception:
        return RunningReleaseProbe(False, None, PROBE_UNAVAILABLE)
    if not container:
        return RunningReleaseProbe(False, None, PROBE_ABSENT)
    if str(container.get("status") or "").lower() != "running":
        return RunningReleaseProbe(False, None, PROBE_STOPPED)
    image_ref = _running_image_ref(docker, container, container_name)
    if not image_ref:
        return RunningReleaseProbe(True, None, PROBE_RUNNING_UNIDENTIFIED)
    identity = identify_image(docker, image_ref)
    tag = (
        _concrete_tag(identity.release_tag)
        or _concrete_tag(identity.version_label)
        or _concrete_ref_tag(container.get("image"))
    )
    if tag is None:
        return RunningReleaseProbe(True, None, PROBE_RUNNING_UNIDENTIFIED)
    return RunningReleaseProbe(
        True,
        InstalledReleaseIdentity(
            tag=tag, image_ref=image_ref, digest=identity.digest,
            build_id=identity.build_id, revision=identity.revision,
            source=SOURCE_RUNNING_CONTAINER,
        ),
        PROBE_RUNNING_IDENTIFIED,
    )


def _compose_image_release(docker, compose_ref):
    ref = str(compose_ref or "").strip()
    if "@sha256:" not in ref:
        return None
    identity = identify_image(docker, ref)
    if identity.digest is None:
        return None
    tag = _concrete_tag(identity.release_tag) or _concrete_tag(identity.version_label)
    if tag is None:
        return None
    return InstalledReleaseIdentity(
        tag=tag, image_ref=ref, digest=identity.digest,
        build_id=identity.build_id, revision=identity.revision,
        source=SOURCE_COMPOSE_IMAGE,
    )


def _known_good_release(compose_ref, known_good):
    if not isinstance(known_good, dict):
        return None
    compose_digest = _compose_digest(compose_ref)
    if compose_digest is None or known_good.get("ems_digest") != compose_digest:
        return None
    tag = _concrete_tag(known_good.get("system_tag"))
    if tag is None:
        return None
    return InstalledReleaseIdentity(
        tag=tag, image_ref=str(compose_ref), digest=compose_digest,
        build_id=known_good.get("build_id"), revision=known_good.get("revision"),
        source=SOURCE_KNOWN_GOOD,
    )


def _legacy_compose_tag(compose_ref):
    tag = _concrete_ref_tag(compose_ref)
    if tag is None:
        return None
    return InstalledReleaseIdentity(
        tag=tag, image_ref=str(compose_ref).strip(), digest=None, build_id=None,
        revision=None, source=SOURCE_LEGACY_COMPOSE,
    )


def resolve_installed_release(*, docker, compose_ref=None, known_good=None,
                              container_name=DEFAULT_EMS_CONTAINER):
    """Return the :class:`InstalledReleaseIdentity` by source-of-truth order.

    A running EMS container is authoritative: an identified one returns its
    release; an unidentifiable one returns ``running_container_unknown`` and
    stops — Compose and Known-Good must not stand in for the actual running bits.
    Only when no container is running (absent, stopped, or Docker unavailable)
    does resolution fall back to Compose image labels, a digest-matched
    Known-Good record, then a legacy concrete Compose tag.
    """

    if docker is not None:
        probe = probe_running_release(docker, container_name)
        if probe.status == PROBE_RUNNING_IDENTIFIED:
            return probe.identity
        if probe.status == PROBE_RUNNING_UNIDENTIFIED:
            return _UNKNOWN_RUNNING
        compose = _compose_image_release(docker, compose_ref)
        if compose is not None:
            return compose
    known = _known_good_release(compose_ref, known_good)
    if known is not None:
        return known
    legacy = _legacy_compose_tag(compose_ref)
    if legacy is not None:
        return legacy
    return _UNKNOWN
