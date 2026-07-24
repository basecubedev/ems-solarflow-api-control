# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release discovery and setup-resource caching for the Admin wizard."""

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from admin.container_names import resolve_ems_container_name
from admin.image_identity import (
    ALREADY_CURRENT,
    DOWNGRADE_BLOCKED,
    IDENTITY_UNKNOWN,
    OLDER_THAN_RUNNING_BUILD,
    ImageIdentity,
    assess_upgrade,
    identify_image,
)


REPO = "basecubedev/ems-solarflow-api-control"
DOCKER_IMAGE_REPOSITORY = f"ghcr.io/{REPO}"
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=50"
# EMS image reference in a compose file; group 1 is the tag (may be ``latest``)
# or, when the ref is digest-pinned, the ``sha256:...`` digest. A digest ref has
# no detectable release tag, so identity comes from the image's OCI labels.
COMPOSE_IMAGE_RE = re.compile(
    rf"ghcr\.io/{re.escape(REPO)}[@:]([^\s\"'{{}}]+)",
    re.IGNORECASE,
)
# User-facing copy for the two identity-based blocks (kept short for the UI).
OLDER_THAN_RUNNING_BUILD_REASON = (
    "Target is older than the running EMS build. Guided Upgrade only supports "
    "upgrades. Use Backup/Restore for recovery flows."
)
IDENTITY_UNKNOWN_REASON = (
    "Guided Upgrade cannot verify this release is newer than the running EMS "
    "build. Use Backup/Restore for recovery flows."
)
# Shown while the target image is not local yet: selection stays allowed and the
# real check runs (pulling the image) during prepare / Guided Upgrade.
IDENTITY_UNVERIFIED_REASON = (
    "Guided Upgrade verifies this target's build identity (pulling the image if "
    "needed) before making any changes."
)
ALREADY_CURRENT_REASON = "Already running this EMS build."
# Test/development escape hatch: allow a legacy target with no build-identity
# labels through the ``identity_unknown`` gate (e.g. a running ``latest`` whose
# build cannot be compared to a pre-labels stable release). Never relaxes a
# SemVer-proven downgrade — see ``assess_upgrade(allow_unverified=...)``.
LEGACY_UNVERIFIED_ENV = "ADMIN_ALLOW_LEGACY_UNVERIFIED_UPGRADES"
TAG_PATTERN = re.compile(r"^v?[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")
# The git revision embedded in an immutable dev tag's ``-<sha>-<run>-<attempt>``.
_DEV_TAG_REVISION_PATTERN = re.compile(r"-([0-9a-f]{7,40})-[1-9][0-9]*-[1-9][0-9]*$")
# A standalone git revision (short or full) used to pin a historical rolling
# ``latest`` image's resources to its exact build.
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
_FULL_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VERSION_PATTERN = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
MIN_ADMIN_VERSION = (0, 6, 0, (1,))
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_EXTRACTED_BYTES = 30 * 1024 * 1024

RESOURCE_PATHS = {
    "config_template": "config.template.json",
    "compose_example": "docker-compose.example.yml",
    "install_linux": "install-docker.sh",
    "install_windows": "install-docker.ps1",
    "deploy_docker_dir": "deploy/docker",
}
OPTIONAL_FILES = {"docs/install-docker.md", "docs/docker.md"}
CONFIG_TEMPLATE_NAME = "config.template.json"
# Release archives may ship the template at the canonical config/ path or, for
# older releases, at the archive root. Both are flattened to CONFIG_TEMPLATE_NAME
# in the cache so downstream cache checks stay stable.
ARCHIVE_TEMPLATE_PATHS = ("config/config.template.json", CONFIG_TEMPLATE_NAME)
REQUIRED_FILES = {
    CONFIG_TEMPLATE_NAME,
    "docker-compose.example.yml",
    "install-docker.sh",
    "install-docker.ps1",
}
SOURCE_ARCHIVE_NAME = ".source-archive"


class ReleaseError(Exception):
    """Expected release operation failure suitable for an API response."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def legacy_release_resource_ref(*, tag, channel, revision):
    """Return the exact git ref a legacy release's resources load from.

    A versioned legacy release (``v0.7.0``) loads from its exact tag; a
    historical rolling ``latest`` image loads from its exact OCI git revision,
    never the moving ``main`` branch. Combining an old image revision with the
    current ``main`` config template is refused by pinning here. Returns a ref
    usable both for the git-tree resource check and for the archive URL.
    """

    tag = str(tag or "").strip()
    if channel == "latest" or tag == "latest":
        revision = str(revision or "").strip()
        if not _REVISION_PATTERN.fullmatch(revision):
            raise ReleaseError(
                "A historical latest image needs a pinned git revision for its "
                "resources.",
                422,
            )
        return revision
    if not tag:
        raise ReleaseError("A legacy release needs a tag for its resources.", 422)
    return tag


def legacy_release_resource_url(*, tag, channel, revision):
    """codeload archive URL for a legacy release, pinned to tag or revision."""

    ref = legacy_release_resource_ref(tag=tag, channel=channel, revision=revision)
    if _REVISION_PATTERN.fullmatch(ref):
        return f"https://codeload.github.com/{REPO}/zip/{ref}"
    return f"https://codeload.github.com/{REPO}/zip/refs/tags/{ref}"


def default_admin_data_dir():
    configured = os.environ.get("EMS_ADMIN_DATA_DIR")
    if configured:
        return Path(configured)
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "data" / "admin"


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version(tag):
    match = VERSION_PATTERN.fullmatch(str(tag))
    if not match:
        return None
    major, minor, patch = (int(part) for part in match.groups()[:3])
    prerelease = match.group(4)
    if prerelease is None:
        prerelease_key = (1,)
    else:
        prerelease_key = (0,) + tuple(
            (0, int(part)) if part.isdigit() else (1, part.lower())
            for part in prerelease.split(".")
        )
    return major, minor, patch, prerelease_key


def _version_text(tag):
    return str(tag)[1:] if str(tag).startswith("v") else str(tag)


def _development_tag_revision(tag):
    """Return the git revision embedded in an immutable dev tag, or ``""``."""

    match = _DEV_TAG_REVISION_PATTERN.search(str(tag or ""))
    return match.group(1) if match else ""


def _is_admin_version(tag):
    return tag == "latest" or bool(
        _version(tag) and _version(tag) >= MIN_ADMIN_VERSION
    )


def _is_release_candidate(tag, github_prerelease=False):
    parsed = _version(tag)
    return bool(github_prerelease or (parsed and parsed[3][0] == 0))


def _downgrade_baseline(active, prepared):
    # A concrete installed release is the authoritative downgrade baseline; a
    # prepared (downloaded, not installed) release never raises it above the
    # installed one. Only with no concrete installed release (fresh install or a
    # rolling ``latest``) does the prepared download gate re-preparing an older one.
    if active and _version(active):
        return active
    concrete = [tag for tag in (active, prepared) if _version(tag)]
    if concrete:
        return max(concrete, key=_version)
    return active or prepared


def _has_build_identity(identity):
    """True when an image identity carries a comparable build signal."""

    return identity.build_serial is not None or bool(identity.digest)


def _legacy_unverified_override():
    """True when the legacy-unverified test override env var is truthy."""

    return os.environ.get(LEGACY_UNVERIFIED_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _safe_tag(tag):
    value = str(tag or "").strip()
    if not TAG_PATTERN.fullmatch(value):
        raise ReleaseError("Invalid release tag.")
    return value


def _relative_archive_path(name):
    path = PurePosixPath(str(name).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ReleaseError("Release archive contains an unsafe path.")
    parts = [part for part in path.parts if part not in ("", ".")]
    if len(parts) < 2:
        return None
    relative = PurePosixPath(*parts[1:])
    if relative.is_absolute() or ".." in relative.parts:
        raise ReleaseError("Release archive contains an unsafe path.")
    return relative


def _is_whitelisted(path):
    value = path.as_posix()
    return (
        value in REQUIRED_FILES
        or value in OPTIONAL_FILES
        or value in ARCHIVE_TEMPLATE_PATHS
        or value.startswith("deploy/docker/")
    )


def _cache_relative(relative):
    if relative.as_posix() in ARCHIVE_TEMPLATE_PATHS:
        return PurePosixPath(CONFIG_TEMPLATE_NAME)
    return relative


class ReleaseManager:
    def __init__(
        self,
        data_dir=None,
        project_dir=None,
        urlopen=None,
        resource_checker=None,
        revision_resolver=None,
        docker=None,
        development_source=None,
        known_good=None,
    ):
        explicit_data_dir = data_dir is not None
        self.data_dir = Path(data_dir) if explicit_data_dir else default_admin_data_dir()
        self.releases_dir = self.data_dir / "releases"
        self.state_dir = self.data_dir / "state"
        # An explicitly isolated data directory (tests and offline tooling) must
        # not accidentally treat the developer checkout's compose image as its
        # active deployment. Production construction omits ``data_dir`` and
        # continues to inspect the real project root.
        self.project_dir = (
            Path(project_dir)
            if project_dir is not None
            else self.data_dir.parent
            if explicit_data_dir
            else Path(__file__).resolve().parent.parent
        )
        self._urlopen = urlopen or urllib.request.urlopen
        self._resource_checker = resource_checker or self._check_remote_resources
        self._revision_resolver = revision_resolver or self._resolve_remote_revision
        # Optional callable that returns candidate development-build descriptors
        # (from a registry/CI index). ``None`` keeps the catalogue release-only.
        self._development_source = development_source
        # Read-only Docker inspector (``inspect_container``/``inspect_image``) used
        # to read build identity. ``None`` disables identity checks entirely, so
        # release selection falls back to SemVer/tag reasoning only.
        self._docker = docker
        # Known-good store (``.current()`` → the installed baseline record) used
        # only as a digest-matched fallback for the installed release tag when a
        # digest-pinned Compose image is not locally inspectable. Lazily defaults
        # to the state directory's store.
        self._known_good = known_good
        self._identity_cache = {}
        self._known_downloads = {}
        self._resource_checks = {}
        self._prepare_lock = threading.Lock()

    def list_releases(self, *, for_upgrade=True):
        """Return the release catalogue.

        ``for_upgrade`` (the default) applies the upgrade-only safety gate: the
        running EMS build identity is inspected and proven downgrades / older
        builds are marked non-selectable. Guided **Setup** (a fresh install) has
        no running build to protect, so it calls with ``for_upgrade=False`` and
        every supported release with available Docker resources stays selectable.
        """

        cached = self._cached_manifests()
        warnings = []
        remote = []
        remote_available = True
        try:
            remote = self._fetch_release_metadata()
        except (OSError, ValueError, urllib.error.URLError) as exc:
            remote_available = False
            warnings.append(f"GitHub releases are unavailable: {exc}")

        by_tag = {item["tag"]: item for item in remote}
        by_tag["latest"] = {
            "tag": "latest",
            "name": "latest",
            "published_at": None,
            "github_prerelease": False,
            "kind": "tag",
        }
        if remote_available:
            self._known_downloads["latest"] = (
                f"https://codeload.github.com/{REPO}/zip/refs/heads/main"
            )
        from admin.system_build import classify_channel

        for tag, manifest in cached.items():
            # Development builds come only from the live catalogue; never
            # resurrect a pruned one from a stale local cache as a ghost entry.
            if classify_channel(tag) == "development":
                continue
            item = by_tag.setdefault(
                tag,
                {
                    "tag": tag,
                    "name": tag,
                    "published_at": manifest.get("downloaded_at"),
                    "github_prerelease": False,
                    "kind": "tag" if tag == "latest" else "release",
                },
            )
            item["prepared"] = True

        active = self.detect_active_release()
        prepared = self._selected_release(cached)
        baseline = _downgrade_baseline(active, prepared)
        # A fresh install has no running build to compare against: skip the
        # (Docker-touching) identity read and let every supported release stand.
        running = self._running_identity() if for_upgrade else ImageIdentity()
        running_known = _has_build_identity(running) if for_upgrade else False
        for item in by_tag.values():
            tag = item["tag"]
            item.setdefault("prepared", False)
            item["active"] = tag == active
            item["version"] = None if tag == "latest" else (
                _version_text(tag) if _version(tag) else None
            )
            item["kind"] = item.get("kind", "release")
            rc = _is_release_candidate(tag, item.pop("github_prerelease", False))
            eligible = _is_admin_version(tag)
            resources = (
                True
                if tag in cached
                else self._resource_status(
                    "main" if tag == "latest" else tag,
                    check_remote=remote_available,
                )
                if eligible
                else False
            )
            # Setup offers every supported release; only the upgrade flow applies
            # the downgrade / build-identity gate against the running build.
            downgrade = for_upgrade and self._is_downgrade(baseline, tag)
            # SemVer downgrade stays authoritative; the build-identity verdict
            # only adds blocks when the running build is actually known.
            assessment = (
                self._assess_upgrade(tag, running, running_known, baseline)
                if for_upgrade and not downgrade
                else None
            )
            state = (
                DOWNGRADE_BLOCKED if downgrade
                else assessment.state if assessment
                else None
            )
            warning = assessment.warning if assessment else None
            item["upgrade_state"] = state
            item["upgrade_warning"] = warning
            # Only a proven-older target blocks selection. An unverifiable target
            # (image not local yet) stays selectable; prepare / Guided Upgrade
            # pull and verify it before any change. ``latest`` is a rolling
            # channel and is never blocked as older/already-current here.
            identity_block = (
                running_known and state == OLDER_THAN_RUNNING_BUILD and tag != "latest"
            )
            identity_noop = (
                running_known and state == ALREADY_CURRENT and tag != "latest"
            )
            item["channel"] = (
                "latest"
                if tag == "latest"
                else "legacy"
                if _version(tag) and not eligible
                else "rc"
                if rc
                else "stable"
                if _version(tag)
                else "unknown"
            )
            item["stable"] = item["channel"] in ("stable", "legacy") and not rc
            item["prerelease"] = rc
            item["admin_supported"] = eligible
            item["docker_supported"] = bool(eligible and resources is True)
            item["selectable"] = bool(
                item["admin_supported"]
                and item["docker_supported"]
                and not downgrade
                and not identity_block
                and not identity_noop
            )
            if not eligible:
                item["reason"] = (
                    "Admin Setup supports Docker releases from v0.6.0 onward"
                    if _version(tag)
                    else "This tag is not supported by Admin Setup"
                )
            elif resources is False:
                item["reason"] = "Docker setup resources are missing for this release."
            elif resources is None:
                item["reason"] = "Docker setup resources could not be verified."
            elif downgrade:
                item["reason"] = (
                    "Downgrades are not supported by the setup assistant. "
                    "Use Backup/Restore flow instead."
                )
            elif identity_block:
                item["reason"] = OLDER_THAN_RUNNING_BUILD_REASON
            elif identity_noop:
                item["reason"] = ALREADY_CURRENT_REASON
            elif running_known and state == IDENTITY_UNKNOWN:
                item["reason"] = IDENTITY_UNVERIFIED_REASON
            elif warning:
                item["reason"] = warning
            elif tag == "latest":
                item["reason"] = (
                    "Rolling Docker channel, not a stable release"
                    + (
                        "; switching from a stable release requires extra caution"
                        if _version(baseline)
                        else ""
                    )
                )
            elif rc:
                item["reason"] = "Release candidate, not a stable release"
            else:
                item["reason"] = None

        if baseline and not _version(baseline) and baseline != "latest":
            warnings.append(
                "The current release cannot be compared safely; no downgrade claim "
                "was made."
            )
        # Pre-v0.6.0 versioned releases (channel "legacy") are unsupported and
        # never selectable; hide them from Setup and Guided Upgrade entirely.
        releases = sorted(
            (item for item in by_tag.values() if item["channel"] != "legacy"),
            key=self._release_sort_key,
        )
        # Development builds are a distinct, always-last group in the same
        # catalogue (never mixed into the versioned sort above).
        releases.extend(self._development_release_items())
        # A valid locally-built pair is offered as its own explicit Experimental
        # entry — never merged into or presented as the rolling ``latest`` tag.
        local_item = self._local_release_item()
        if local_item is not None:
            releases.append(local_item)
        stable = next(
            (
                item["tag"]
                for item in releases
                if item["stable"] and item["selectable"] and item["channel"] == "stable"
            ),
            None,
        )
        default_release = stable or next(
            (item["tag"] for item in releases if item["tag"] == "latest" and item["selectable"]),
            None,
        )
        return {
            "active_release": active,
            "prepared_release": prepared,
            "latest": "latest",
            "latest_stable": stable,
            "default_release": default_release,
            "releases": releases,
            "warnings": warnings,
        }

    def prepare(self, tag, *, revision=None):
        tag = _safe_tag(tag)
        try:
            with self._prepare_lock:
                self._ensure_data_directories()
                return self._prepare_locked(tag, revision=revision)
        except ReleaseError:
            raise
        except OSError as exc:
            raise self._data_directory_error() from exc

    def _prepare_locked(self, tag, *, revision=None):
        cached = self._cached_manifests()
        active = self.detect_active_release()
        prepared = self._selected_release(cached)
        baseline = _downgrade_baseline(active, prepared)
        if self._is_downgrade(baseline, tag):
            raise ReleaseError(
                "Downgrades are not supported by the setup assistant. "
                "Use Backup/Restore flow instead.",
                status=409,
            )
        # Block preparing a release the running build identity proves is not an
        # upgrade (older serial) or cannot be proven newer at all. Only enforced
        # when the running build is actually known — never on a fresh install.
        running = self._running_identity()
        if _has_build_identity(running):
            state = self._assess_upgrade(
                tag, running, True, baseline, pull=self._pull_callable()
            ).state
            if state == OLDER_THAN_RUNNING_BUILD:
                raise ReleaseError(OLDER_THAN_RUNNING_BUILD_REASON, status=409)
            if state == IDENTITY_UNKNOWN:
                raise ReleaseError(IDENTITY_UNKNOWN_REASON, status=409)
        if not _is_admin_version(tag):
            raise ReleaseError(
                "Admin Setup supports Docker releases from v0.6.0 onward.", 400
            )

        manifest = cached.get(tag)
        if revision:
            return self._prepare_revision_bound(
                tag, revision=revision, manifest=manifest
            )
        if manifest and self._cache_complete(tag, manifest):
            manifest = self._normalized_manifest(tag, manifest)
            self._write_json(self.releases_dir / tag / "manifest.json", manifest)
            self._write_selected(tag)
            return self._ready_payload(tag, manifest, reused=True)

        if tag not in self._known_downloads:
            try:
                self._fetch_release_metadata()
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise ReleaseError(
                    f"Could not validate release {tag}: {exc}", 503
                ) from exc
        archive_url = self._known_downloads.get(tag)
        if not archive_url:
            raise ReleaseError(f"Unknown EMS release tag: {tag}", 404)
        ref = "main" if tag == "latest" else tag
        if self._resource_status(ref, check_remote=True) is not True:
            raise ReleaseError(
                "Docker setup resources are missing or could not be verified "
                "for this release.",
                422,
            )

        archive = self._download(archive_url)
        manifest = self._extract(
            tag,
            archive,
            identity={
                "identity_verification": "unverified",
                "resource_ref": (
                    "refs/heads/main" if tag == "latest" else f"refs/tags/{tag}"
                ),
                "resolved_revision": None,
                "expected_ems_revision": None,
            },
        )
        self._write_selected(tag)
        return self._ready_payload(tag, manifest, reused=False)

    def _prepare_revision_bound(self, tag, *, revision, manifest):
        """Prepare legacy resources bound to the selected EMS OCI revision.

        A live tag resolution detects moved tags. When GitHub is unavailable, a
        fully verified cache for the same tag and EMS revision remains usable;
        any incomplete, tampered or differently-bound cache fails closed.
        """

        reported_revision = str(revision or "").strip().lower()
        if not _REVISION_PATTERN.fullmatch(reported_revision):
            raise ReleaseError(
                "The selected legacy EMS image has an invalid OCI revision.", 422
            )
        resource_ref = (
            reported_revision if tag == "latest" else f"refs/tags/{tag}"
        )
        resolved_revision = None
        resolution_error = None
        try:
            if tag == "latest" and _FULL_REVISION_PATTERN.fullmatch(
                reported_revision
            ):
                resolved_revision = reported_revision
            else:
                resolved_revision = self._revision_resolver(resource_ref)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            resolution_error = exc

        if resolved_revision is not None:
            resolved_revision = str(resolved_revision).strip().lower()
            if not _FULL_REVISION_PATTERN.fullmatch(resolved_revision):
                raise ReleaseError(
                    "GitHub returned an invalid legacy resource revision.", 502
                )
            if not resolved_revision.startswith(reported_revision):
                raise ReleaseError(
                    "Legacy release resource revision does not match the selected "
                    "EMS image revision.",
                    409,
                )
            if self._verified_cache_matches(
                tag,
                manifest,
                resource_ref=resource_ref,
                resolved_revision=resolved_revision,
                reported_revision=reported_revision,
            ):
                manifest = self._normalized_manifest(tag, manifest)
                self._write_json(
                    self.releases_dir / tag / "manifest.json", manifest
                )
                self._write_selected(tag)
                return self._ready_payload(tag, manifest, reused=True)
        elif self._verified_cache_matches(
            tag,
            manifest,
            resource_ref=resource_ref,
            resolved_revision=None,
            reported_revision=reported_revision,
        ):
            manifest = self._normalized_manifest(tag, manifest)
            self._write_selected(tag)
            return self._ready_payload(tag, manifest, reused=True)
        else:
            raise ReleaseError(
                f"Could not resolve the exact legacy release revision: "
                f"{resolution_error or 'revision unavailable'}",
                503,
            )

        if self._resource_status(resolved_revision, check_remote=True) is not True:
            raise ReleaseError(
                "Docker setup resources are missing or could not be verified "
                "for the selected EMS image revision.",
                422,
            )
        archive_url = f"https://codeload.github.com/{REPO}/zip/{resolved_revision}"
        archive = self._download(archive_url)
        manifest = self._extract(
            tag,
            archive,
            identity={
                "identity_verification": "verified",
                "resource_ref": resource_ref,
                "resolved_revision": resolved_revision,
                # A matching short image label is expanded only after the exact
                # tag commit has been resolved and compared.
                "expected_ems_revision": resolved_revision,
                "reported_ems_revision": reported_revision,
            },
        )
        self._write_selected(tag)
        return self._ready_payload(tag, manifest, reused=False)

    def _verified_cache_matches(
        self,
        tag,
        manifest,
        *,
        resource_ref,
        resolved_revision,
        reported_revision,
    ):
        if not manifest or not self._cache_complete(tag, manifest):
            return False
        cached_revision = manifest.get("resolved_revision")
        expected_revision = manifest.get("expected_ems_revision")
        if not (
            manifest.get("identity_verification") == "verified"
            and manifest.get("resource_ref") == resource_ref
            and _FULL_REVISION_PATTERN.fullmatch(str(cached_revision or ""))
            and cached_revision == expected_revision
            and cached_revision.startswith(reported_revision)
        ):
            return False
        return resolved_revision is None or cached_revision == resolved_revision

    def config_template(self):
        tag = self._selected_release_tag()
        if not tag:
            raise ReleaseError("No release resources prepared yet.", 404)
        template_path = self.releases_dir / tag / "config.template.json"
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ReleaseError(
                f"Prepared release {tag} is missing config.template.json.", 500
            ) from exc
        except (OSError, ValueError) as exc:
            raise ReleaseError(
                f"Prepared release {tag} has an invalid config.template.json.", 500
            ) from exc
        if not isinstance(template, dict):
            raise ReleaseError(
                f"Prepared release {tag} has an invalid config.template.json.", 500
            )
        return {
            "tag": tag,
            "template": template,
            "source": str(template_path),
            "docker_image": self._docker_image(tag),
        }

    def prepared_release(self):
        """Return the currently prepared+cached release tag, or ``None``."""

        return self._selected_release(self._cached_manifests())

    def detect_active_release(self):
        """Return the installed release tag, or ``None`` when it is not concrete.

        Resolves the readable installed release through the shared source-of-truth
        order (running container OCI labels → digest-pinned Compose image labels →
        digest-matched known-good → legacy concrete Compose tag). A digest-pinned
        Compose ref no longer loses the installed tag, and a prepared release is
        never treated as installed. ``latest`` stays non-concrete.
        """

        from admin.installed_release import resolve_installed_release

        installed = resolve_installed_release(
            docker=self._docker,
            compose_ref=self._compose_image_ref(),
            known_good=self._current_known_good(),
            container_name=self._ems_container_name(),
        )
        return installed.tag

    def _ems_container_name(self):
        return resolve_ems_container_name(
            compose_text=self._compose_text(), env=os.environ
        )

    def _compose_text(self):
        for name in ("docker-compose.yml", "compose.yml"):
            try:
                return (self.project_dir / name).read_text(encoding="utf-8")
            except OSError:
                continue
        return ""

    def _current_known_good(self):
        store = self._known_good
        if store is None:
            from admin.known_good import KnownGoodStore

            store = self._known_good = KnownGoodStore(self.state_dir)
        try:
            return store.current()
        except Exception:  # a read-only view must never fail on a bad record
            return None

    def _compose_image_match(self):
        for name in ("docker-compose.yml", "compose.yml"):
            path = self.project_dir / name
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            match = COMPOSE_IMAGE_RE.search(text)
            if match:
                return match
        return None

    def _compose_image_ref(self):
        """Full EMS image ref declared in the compose file (incl. ``latest``)."""

        match = self._compose_image_match()
        return match.group(0) if match else None

    def _running_identity(self):
        """Build identity of the running EMS system, or an all-``None`` identity.

        Prefers the running container's immutable image id (its actual bits, via
        the shared probe) so a moved tag cannot alter it, and falls back to the
        compose-declared image ref when no container is running. Returns an
        all-``None`` :class:`ImageIdentity` when Docker is unavailable or nothing
        can be inspected, which disables identity-based blocking.
        """

        if self._docker is None:
            return ImageIdentity()
        from admin.installed_release import running_image_ref

        ref = running_image_ref(self._docker, self._ems_container_name())
        if not ref:
            ref = self._compose_image_ref()
        if ref:
            return self._identify(ref)
        return ImageIdentity()

    def _identify(self, image_ref):
        ref = str(image_ref or "").strip()
        if not ref:
            return ImageIdentity()
        if ref not in self._identity_cache:
            self._identity_cache[ref] = identify_image(self._docker, ref)
        return self._identity_cache[ref]

    def _target_identity(self, tag, pull=None):
        if self._docker is None:
            return ImageIdentity()
        return self._resolve_target_identity(self._docker_image(tag), pull)

    def _resolve_target_identity(self, target_image, pull):
        """Inspect the target image, pulling once via ``pull`` when not local.

        ``pull`` is an optional ``callable(image_ref)`` (a not-local target has
        no build identity to compare, so pulling resolves it). Pull/inspect
        failures degrade to the last known identity rather than raising.
        """

        identity = self._identify(target_image)
        if pull is None or _has_build_identity(identity):
            return identity
        try:
            pull(target_image)
        except Exception:  # a pull failure just leaves the target unverifiable
            return identity
        self._identity_cache.pop(target_image, None)
        return self._identify(target_image)

    def _pull_callable(self):
        """The injected Docker's image pull, or ``None`` when it cannot pull."""

        pull = getattr(self._docker, "pull", None)
        return pull if callable(pull) else None

    def _assess_upgrade(self, tag, running, running_known, baseline, *, pull=None):
        """Assess moving from the running build to ``tag`` (see ``assess_upgrade``).

        The target image is only inspected when SemVer cannot settle the move (a
        ``latest`` side) and the running build is known, so a normal
        stable->stable listing never shells out to Docker per release. When
        ``pull`` is given, a not-yet-local target is pulled once so its build
        serial/digest can settle the decision instead of defaulting to unknown.

        The ``ADMIN_ALLOW_LEGACY_UNVERIFIED_UPGRADES`` test override is applied
        only for a supported concrete release target — never ``latest`` and never
        a SemVer-proven downgrade — so unlabeled legacy ``v0.6.x`` images stay
        testable while normal safety is unchanged.
        """

        target_version = None if tag == "latest" else _version(tag)
        current_version = _version(baseline) if baseline else None
        need_serial = running_known and (
            current_version is None or target_version is None
        )
        target = self._target_identity(tag, pull) if need_serial else ImageIdentity()
        allow_unverified = (
            target_version is not None
            and _is_admin_version(tag)
            and _legacy_unverified_override()
        )
        return assess_upgrade(
            running,
            target,
            current_version=current_version,
            target_version=target_version,
            allow_unverified=allow_unverified,
            target_rolling=tag == "latest",
        )

    def verify_upgrade_target(self, tag, *, pull=None):
        """Assess the move from the running EMS build to ``tag`` for Guided Upgrade.

        Unlike the release listing this compares against the *running* release
        only (never the prepared target) and, when ``pull`` is given, pulls a
        not-yet-local target so its build identity settles the decision. Returns
        an :class:`~admin.image_identity.UpgradeAssessment`; the caller blocks on
        anything that is not a proven upgrade before making changes.
        """

        tag = _safe_tag(tag)
        running = self._running_identity()
        running_known = _has_build_identity(running)
        active = self.detect_active_release()
        return self._assess_upgrade(tag, running, running_known, active, pull=pull)

    def _fetch_release_metadata(self):
        request = urllib.request.Request(
            GITHUB_RELEASES_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ems-solarflow-admin",
            },
        )
        with self._urlopen(request, timeout=10) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("GitHub response is too large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("GitHub returned an invalid release list")

        releases = []
        for entry in payload:
            if not isinstance(entry, dict) or entry.get("draft"):
                continue
            try:
                tag = _safe_tag(entry.get("tag_name"))
            except ReleaseError:
                continue
            prerelease = bool(entry.get("prerelease"))
            self._known_downloads[tag] = (
                f"https://codeload.github.com/{REPO}/zip/refs/tags/{tag}"
            )
            releases.append(
                {
                    "tag": tag,
                    "name": str(entry.get("name") or tag),
                    "published_at": entry.get("published_at"),
                    "github_prerelease": prerelease,
                    "kind": "release",
                }
            )
        self._known_downloads["latest"] = (
            f"https://codeload.github.com/{REPO}/zip/refs/heads/main"
        )
        return releases

    def _resource_status(self, ref, check_remote):
        if ref in self._resource_checks and self._resource_checks[ref] is not None:
            return self._resource_checks[ref]
        if not check_remote:
            return None
        try:
            status = bool(self._resource_checker(ref))
        except (OSError, ValueError, urllib.error.URLError):
            status = None
        self._resource_checks[ref] = status
        return status

    def _check_remote_resources(self, ref):
        url = (
            f"https://api.github.com/repos/{REPO}/git/trees/"
            f"{quote(ref, safe='')}?recursive=1"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ems-solarflow-admin",
            },
        )
        with self._urlopen(request, timeout=10) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("GitHub tree response is too large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            raise ValueError("GitHub returned an invalid resource tree")
        paths = {
            item.get("path")
            for item in payload["tree"]
            if isinstance(item, dict) and item.get("type") == "blob"
        }
        resource_files = REQUIRED_FILES - {CONFIG_TEMPLATE_NAME}
        has_template = any(path in paths for path in ARCHIVE_TEMPLATE_PATHS)
        return (
            resource_files.issubset(paths)
            and has_template
            and any(str(path).startswith("deploy/docker/") for path in paths)
        )

    def _resolve_remote_revision(self, ref):
        """Resolve a tag/ref to the full commit SHA through GitHub's commit API."""

        url = (
            f"https://api.github.com/repos/{REPO}/commits/"
            f"{quote(str(ref), safe='')}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ems-solarflow-admin",
            },
        )
        with self._urlopen(request, timeout=10) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("GitHub commit response is too large")
        payload = json.loads(raw.decode("utf-8"))
        revision = payload.get("sha") if isinstance(payload, dict) else None
        if not isinstance(revision, str) or not _FULL_REVISION_PATTERN.fullmatch(
            revision.lower()
        ):
            raise ValueError("GitHub returned an invalid commit revision")
        return revision.lower()

    def _development_release_items(self):
        """Return normalized, installable development-build catalogue items.

        Only immutable canonical ``dev-<branch>-<sha>-<run>-<attempt>`` tags whose
        Admin/EMS pair is complete (``installable``) are surfaced; floating
        aliases, incomplete pairs and failed builds are dropped. Reuses the
        canonical dev-tag rules from ``system_build`` (imported lazily to avoid a
        module import cycle). A failing source never breaks the catalogue.
        """

        source = self._development_source
        if source is None:
            return []
        from admin.system_build import classify_channel, is_immutable_dev_tag

        try:
            raw = list(source())
        except Exception:
            return []

        items = []
        seen = set()
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            tag = str(entry.get("tag") or "").strip()
            if not tag or not TAG_PATTERN.fullmatch(tag):
                continue
            if classify_channel(tag) != "development" or not is_immutable_dev_tag(tag):
                continue
            if entry.get("installable") is not True or tag in seen:
                continue
            seen.add(tag)
            revision = str(entry.get("revision") or "") or _development_tag_revision(tag)
            display_name = str(entry.get("display_name") or "").strip() or tag
            items.append(
                {
                    "tag": tag,
                    "name": display_name,
                    "display_name": display_name,
                    "published_at": entry.get("created_at"),
                    "created_at": entry.get("created_at"),
                    "revision": revision or None,
                    "revision_short": revision[:7] if revision else None,
                    "build_id": entry.get("build_id") or tag,
                    "run_id": entry.get("run_id"),
                    "run_attempt": entry.get("run_attempt"),
                    "admin_image": entry.get("admin_image"),
                    "admin_digest": entry.get("admin_digest"),
                    "ems_image": entry.get("ems_image"),
                    "ems_digest": entry.get("ems_digest"),
                    "kind": "development",
                    "channel": "development",
                    "version": None,
                    "stable": False,
                    "prerelease": False,
                    "prepared": False,
                    "active": False,
                    "admin_supported": True,
                    "docker_supported": True,
                    "installable": True,
                    "selectable": True,
                    "upgrade_state": None,
                    "upgrade_warning": None,
                    "reason": None,
                }
            )
        items.sort(key=lambda it: (it.get("created_at") or "", it["tag"]), reverse=True)
        return items

    def development_build(self, tag):
        """Return the complete installable catalogue descriptor for ``tag``."""

        source = self._development_source
        if source is None:
            return None
        try:
            entries = source()
        except Exception:
            return None
        for entry in entries:
            if (
                isinstance(entry, dict)
                and entry.get("tag") == tag
                and entry.get("installable") is True
            ):
                return dict(entry)
        return None

    def _local_release_item(self):
        """Discover a valid locally-built System Build, offered under Experimental.

        Returns a release item for the local Admin+EMS pair the official local
        launcher builds and tags ``:local``, or ``None`` when no valid local
        pair is present. Resolution inspects only local images and never pulls
        from a registry.
        """

        if self._docker is None:
            return None
        from admin.system_build import SystemBuildResolver

        try:
            build = SystemBuildResolver(docker=self._docker).resolve("local")
        except Exception:
            # Best-effort discovery: an absent local pair or a Docker daemon that
            # raises on inspection must never break the release catalogue.
            return None
        dirty = build.build_id.endswith("-dirty")
        return {
            "tag": "local",
            "name": "local · current checkout",
            "display_name": "local · current checkout",
            "published_at": None,
            "created_at": None,
            "revision": build.revision,
            "revision_short": build.revision[:7],
            "build_id": build.build_id,
            "dirty": dirty,
            "kind": "development",
            "channel": "development",
            "version": None,
            "stable": False,
            "prerelease": False,
            "prepared": False,
            "active": False,
            "admin_supported": True,
            "docker_supported": True,
            "installable": True,
            "selectable": True,
            "upgrade_state": None,
            "upgrade_warning": None,
            "reason": (
                "Local development build from the current checkout"
                + (" with uncommitted changes" if dirty else "")
            ),
        }

    @staticmethod
    def _release_sort_key(item):
        channel_order = {
            "latest": 0,
            "stable": 1,
            "rc": 2,
            "development": 3,
            "legacy": 4,
            "unknown": 5,
        }
        parsed = _version(item["tag"])
        core = parsed[:3] if parsed else (0, 0, 0)
        return (
            channel_order.get(item["channel"], 5),
            tuple(-part for part in core),
            item["tag"],
        )

    def _download(self, url):
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/octet-stream", "User-Agent": "ems-solarflow-admin"},
        )
        try:
            with self._urlopen(request, timeout=30) as response:
                chunks = []
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ReleaseError(
                            "Release archive exceeds the download size limit.", 413
                        )
                    chunks.append(chunk)
        except ReleaseError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise ReleaseError(f"Could not download release archive: {exc}", 502) from exc
        return b"".join(chunks)

    def _extract(self, tag, archive, *, identity=None):
        self.releases_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{tag}-", dir=self.releases_dir))
        try:
            extracted = self._extract_archive(archive, staging)
            missing = sorted(REQUIRED_FILES - extracted)
            if missing or not any(path.startswith("deploy/docker/") for path in extracted):
                detail = ", ".join(missing or ["deploy/docker/*"])
                raise ReleaseError(f"Release archive is missing required resources: {detail}")
            try:
                template = json.loads(
                    (staging / "config.template.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReleaseError("Release config.template.json is invalid.") from exc
            if not isinstance(template, dict):
                raise ReleaseError("Release config.template.json is invalid.")

            (staging / SOURCE_ARCHIVE_NAME).write_bytes(archive)
            resource_hashes = {
                relative: _sha256_file(staging / relative)
                for relative in sorted(extracted)
            }
            manifest = self._normalized_manifest(
                tag,
                {
                    "tag": tag,
                    "source": "github",
                    "downloaded_at": _utc_now(),
                    "repo": REPO,
                    "resources": dict(RESOURCE_PATHS),
                    "archive_sha256": hashlib.sha256(archive).hexdigest(),
                    "resource_hashes": resource_hashes,
                    **(identity or {}),
                },
            )
            self._write_json(staging / "manifest.json", manifest)
            target = self.releases_dir / tag
            if target.exists():
                shutil.rmtree(target)
            staging.replace(target)
            return manifest
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _extract_archive(self, archive, destination):
        if zipfile.is_zipfile(io.BytesIO(archive)):
            return self._extract_zip(archive, destination)
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as handle:
                return self._extract_tar(handle, destination)
        except tarfile.TarError as exc:
            raise ReleaseError("Downloaded release is not a supported archive.") from exc

    def _extract_zip(self, archive, destination):
        extracted = set()
        total = 0
        with zipfile.ZipFile(io.BytesIO(archive)) as handle:
            for info in handle.infolist():
                relative = _relative_archive_path(info.filename)
                if relative is None or info.is_dir() or not _is_whitelisted(relative):
                    continue
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ReleaseError("Release archive contains a symbolic link.")
                total = self._checked_total(total, info.file_size)
                cache_relative = _cache_relative(relative)
                with handle.open(info) as source:
                    self._write_resource(
                        destination, cache_relative, source.read(MAX_FILE_BYTES + 1)
                    )
                extracted.add(cache_relative.as_posix())
        return extracted

    def _extract_tar(self, handle, destination):
        extracted = set()
        total = 0
        for member in handle.getmembers():
            relative = _relative_archive_path(member.name)
            if relative is None or member.isdir() or not _is_whitelisted(relative):
                continue
            if not member.isfile():
                raise ReleaseError("Release archive contains an unsupported resource type.")
            total = self._checked_total(total, member.size)
            source = handle.extractfile(member)
            if source is None:
                raise ReleaseError("Release archive contains an unreadable resource.")
            cache_relative = _cache_relative(relative)
            self._write_resource(
                destination, cache_relative, source.read(MAX_FILE_BYTES + 1)
            )
            extracted.add(cache_relative.as_posix())
        return extracted

    @staticmethod
    def _checked_total(total, size):
        if size < 0 or size > MAX_FILE_BYTES:
            raise ReleaseError("A release resource exceeds the file size limit.", 413)
        total += size
        if total > MAX_EXTRACTED_BYTES:
            raise ReleaseError("Release resources exceed the extraction size limit.", 413)
        return total

    @staticmethod
    def _write_resource(destination, relative, content):
        if len(content) > MAX_FILE_BYTES:
            raise ReleaseError("A release resource exceeds the file size limit.", 413)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def _cached_manifests(self):
        manifests = {}
        try:
            directories = list(self.releases_dir.iterdir())
        except OSError:
            return manifests
        for directory in directories:
            if not directory.is_dir() or not TAG_PATTERN.fullmatch(directory.name):
                continue
            try:
                manifest = json.loads(
                    (directory / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if (
                isinstance(manifest, dict)
                and manifest.get("tag") == directory.name
                and self._cache_complete(directory.name, manifest)
            ):
                manifests[directory.name] = manifest
        return manifests

    def _cache_complete(self, tag, manifest):
        root = self.releases_dir / tag
        if manifest.get("repo") != REPO:
            return False
        if not all((root / path).is_file() for path in REQUIRED_FILES) or not any(
            path.is_file() for path in (root / "deploy" / "docker").glob("**/*")
        ):
            return False
        try:
            template = json.loads(
                (root / "config.template.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        if not isinstance(template, dict):
            return False

        # New manifests bind both the source archive and every extracted file.
        # Older caches remain readable only as explicitly unverified legacy
        # compatibility data; they can never satisfy revision-bound reuse.
        verification = manifest.get("identity_verification")
        if verification not in {"verified", "unverified"}:
            return True
        archive_hash = manifest.get("archive_sha256")
        archive_path = root / SOURCE_ARCHIVE_NAME
        try:
            archive_valid = (
                isinstance(archive_hash, str)
                and re.fullmatch(r"[0-9a-f]{64}", archive_hash)
                and archive_path.is_file()
                and not archive_path.is_symlink()
                and _sha256_file(archive_path) == archive_hash
            )
        except OSError:
            archive_valid = False
        if not archive_valid:
            return False
        resource_hashes = manifest.get("resource_hashes")
        if not isinstance(resource_hashes, dict):
            return False
        if not REQUIRED_FILES.issubset(resource_hashes) or not any(
            str(path).startswith("deploy/docker/") for path in resource_hashes
        ):
            return False
        for relative, expected_hash in resource_hashes.items():
            path = PurePosixPath(str(relative))
            if (
                path.is_absolute()
                or ".." in path.parts
                or not isinstance(expected_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            ):
                return False
            resource = root.joinpath(*path.parts)
            if not resource.is_file() or resource.is_symlink():
                return False
            try:
                actual_hash = _sha256_file(resource)
            except OSError:
                return False
            if actual_hash != expected_hash:
                return False
        if verification == "verified":
            resolved = manifest.get("resolved_revision")
            expected = manifest.get("expected_ems_revision")
            if not (
                _FULL_REVISION_PATTERN.fullmatch(str(resolved or ""))
                and resolved == expected
                and isinstance(manifest.get("resource_ref"), str)
            ):
                return False
        return True

    def _selected_release_tag(self):
        try:
            selected = json.loads(
                (self.state_dir / "selected-release.json").read_text(encoding="utf-8")
            )
            tag = selected.get("tag")
        except (OSError, ValueError, AttributeError):
            return None
        return tag if isinstance(tag, str) and TAG_PATTERN.fullmatch(tag) else None

    def _selected_release(self, cached):
        selected = self._selected_release_tag()
        if selected in cached:
            return selected
        if not cached:
            return None
        return max(
            cached,
            key=lambda tag: cached[tag].get("downloaded_at") or "",
        )

    def _write_selected(self, tag):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(
            self.state_dir / "selected-release.json",
            {"tag": tag, "selected_at": _utc_now()},
        )

    def _ensure_data_directories(self):
        self.releases_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(dir=self.data_dir, prefix=".write-check-"):
                pass
        except OSError as exc:
            raise self._data_directory_error() from exc

    def _data_directory_error(self):
        return ReleaseError(
            f"Admin data directory is not writable: {self.data_dir}. "
            "Check the Docker volume mount for ./data/admin:/data.",
            500,
        )

    @staticmethod
    def _write_json(path, payload):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    def _normalized_manifest(self, tag, manifest):
        normalized = dict(manifest)
        normalized.update(
            {
                "tag": tag,
                "release": tag,
                "repo": REPO,
                "config_template": RESOURCE_PATHS["config_template"],
                "docker_compose_template": RESOURCE_PATHS["compose_example"],
                "docker_image": self._docker_image(tag),
            }
        )
        normalized["prepared_at"] = (
            normalized.get("prepared_at")
            or normalized.get("downloaded_at")
            or _utc_now()
        )
        normalized["resources"] = {
            **normalized.get("resources", {}),
            **dict(RESOURCE_PATHS),
        }
        return normalized

    @staticmethod
    def _is_downgrade(active, selected):
        active_version = _version(active)
        selected_version = _version(selected)
        return bool(active_version and selected_version and selected_version < active_version)

    def _ready_payload(self, tag, manifest, reused):
        root = self.releases_dir / tag
        config_template_loaded = self._valid_cached_template(root)
        identity_verified = manifest.get("identity_verification") == "verified"
        warnings = []
        if not identity_verified:
            warnings.append(
                "Legacy resource identity is unverified because the selected EMS "
                "image revision was unavailable; only an explicit compatibility "
                "workflow may use these resources."
            )
        return {
            "status": "ready",
            "tag": tag,
            "manifest": manifest,
            "config_template_loaded": config_template_loaded,
            "docker_image": self._docker_image(tag),
            "resources": {
                "config_template_available": config_template_loaded,
                "config_template_loaded": config_template_loaded,
                "docker_install_available": (
                    (root / "install-docker.sh").is_file()
                    and (root / "install-docker.ps1").is_file()
                ),
                "compose_example_available": (
                    root / "docker-compose.example.yml"
                ).is_file(),
                "deploy_docker_available": any(
                    path.is_file() for path in (root / "deploy" / "docker").glob("**/*")
                ),
            },
            "reused": reused,
            "legacy_identity_verified": identity_verified,
            "warnings": warnings,
        }

    @staticmethod
    def _valid_cached_template(root):
        try:
            template = json.loads(
                (root / "config.template.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        return isinstance(template, dict)

    @staticmethod
    def _docker_image(tag):
        return f"{DOCKER_IMAGE_REPOSITORY}:{tag}"
