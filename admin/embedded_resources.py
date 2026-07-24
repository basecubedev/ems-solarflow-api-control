# SPDX-License-Identifier: AGPL-3.0-or-later
"""Embed and verify Setup resources shipped inside the Admin image.

The Admin image bundles ``/app/release-resources/`` with the Setup resources plus
two generated descriptors:

    system-build.json     -- the complete paired-build identity this Admin image
                             belongs to (cross-checked against OCI labels).
    resource-manifest.json -- a sha256 for every embedded resource file.

:class:`EmbeddedReleaseResources` verifies the bundle against the *running* Admin
build and every file hash, then imports it into the existing
:class:`admin.releases.ReleaseManager` cache (``<data>/releases/<tag>/``) so
``config_template()``/``prepared_release()``/DeploymentService/GuidedUpgrade keep
working with no GitHub access after Admin has aligned to the selected build.

A cache directory is trusted only when its ``manifest.json`` matches the complete
paired-build identity, resource format and file hashes — never merely because a
tag-named directory exists. This module never trusts a tag directory by name.

:func:`write_release_resources` is the build-time generator (invoked from the
Admin image build / CI) that produces a bundle this service can verify.
"""

import hashlib
import json
import re
import shutil
from pathlib import Path

from admin.admin_update import ADMIN_IMAGE_REPO, EMS_IMAGE_REPO
from admin.releases import REPO, RESOURCE_PATHS, TAG_PATTERN
from admin.system_build import CHANNEL_UNKNOWN, classify_channel
from admin.system_build_id import validate_system_build_id

# The Admin image bakes the bundle here; overridable for tests/tools.
DEFAULT_RESOURCES_DIR = "/app/release-resources"

SYSTEM_BUILD_FILE = "system-build.json"
RESOURCE_MANIFEST_FILE = "resource-manifest.json"
RESOURCE_FORMAT_VERSION = 1

# The Setup resource files (relative to the bundle root) plus the deploy/docker
# tree. These mirror ``admin.releases.RESOURCE_PATHS`` / ``REQUIRED_FILES``.
REQUIRED_RESOURCE_FILES = (
    RESOURCE_PATHS["config_template"],      # config.template.json
    RESOURCE_PATHS["compose_example"],      # docker-compose.example.yml
    RESOURCE_PATHS["install_linux"],        # install-docker.sh
    RESOURCE_PATHS["install_windows"],      # install-docker.ps1
)
DEPLOY_DOCKER_DIR = RESOURCE_PATHS["deploy_docker_dir"]  # deploy/docker
_DEPLOY_DOCKER_PREFIX = DEPLOY_DOCKER_DIR + "/"

_INVALID = "system_build_resources_invalid"
_FULL_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")

# This is the complete, non-recursive identity shared by the Admin descriptor,
# resource manifest, expected running build, and imported cache manifest.  Keep
# it centralized so adding a field cannot accidentally leave one persistence
# layer bound only to a weaker subset.
SYSTEM_BUILD_IDENTITY_FIELDS = (
    "system_tag",
    "channel",
    "revision",
    "build_id",
    "release_tag",
    "admin_image",
    "ems_image",
)


class EmbeddedResourcesError(Exception):
    """Embedded resources are missing, tampered, or bound to a different build.

    Always carries ``code == "system_build_resources_invalid"`` so callers can
    surface one stable error regardless of which check failed.
    """

    def __init__(self, message, code=_INVALID):
        super().__init__(message)
        self.code = code
        self.message = message


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _iter_resource_files(root: Path):
    """Yield the required files plus every file under ``deploy/docker`` (posix rel)."""

    yield from REQUIRED_RESOURCE_FILES
    deploy = root / DEPLOY_DOCKER_DIR
    if deploy.is_dir():
        for path in sorted(deploy.rglob("*")):
            if path.is_file():
                yield path.relative_to(root).as_posix()


def _is_safe_relative(rel: str) -> bool:
    """True when ``rel`` is a normal in-tree relative path (no traversal/abs)."""

    if not rel or rel.startswith("/") or "\\" in rel:
        return False
    parts = Path(rel).parts
    return ".." not in parts and not Path(rel).is_absolute()


def _has_deployment_resources(files) -> bool:
    return isinstance(files, dict) and any(
        isinstance(relative, str)
        and relative.startswith(_DEPLOY_DOCKER_PREFIX)
        and relative != _DEPLOY_DOCKER_PREFIX
        for relative in files
    )


def _validate_identity(payload: dict, *, source: str) -> dict:
    """Return a complete, validated system-build identity from ``payload``."""

    identity = {}
    for key in SYSTEM_BUILD_IDENTITY_FIELDS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise EmbeddedResourcesError(f"{source} missing {key}")
        identity[key] = value

    for key in ("system_tag", "release_tag"):
        if not TAG_PATTERN.fullmatch(identity[key]):
            raise EmbeddedResourcesError(f"{source} has an invalid {key}")
    try:
        validate_system_build_id(identity["build_id"])
    except ValueError as exc:
        raise EmbeddedResourcesError(
            f"{source} has an invalid build_id: {exc}"
        ) from exc
    if not _FULL_REVISION_RE.fullmatch(identity["revision"]):
        raise EmbeddedResourcesError(f"{source} has an invalid revision")
    if identity["revision"][:7] not in identity["build_id"]:
        raise EmbeddedResourcesError(f"{source} build_id mismatches revision")
    expected_channel = classify_channel(identity["system_tag"])
    if expected_channel == CHANNEL_UNKNOWN or identity["channel"] != expected_channel:
        raise EmbeddedResourcesError(f"{source} channel mismatches system_tag")
    if identity["release_tag"] != identity["system_tag"]:
        raise EmbeddedResourcesError(f"{source} release_tag mismatches system_tag")
    expected_images = {
        "admin_image": f"{ADMIN_IMAGE_REPO}:{identity['system_tag']}",
        "ems_image": f"{EMS_IMAGE_REPO}:{identity['system_tag']}",
    }
    for field, expected in expected_images.items():
        if identity[field] != expected:
            raise EmbeddedResourcesError(f"{source} has an invalid {field}")
    return identity


def _expected_identity(running_build) -> dict:
    """Normalize the public ``running_build=`` API to the complete identity."""

    running = running_build if isinstance(running_build, dict) else {}
    system_tag = running.get("system_tag")
    canonical_tag = running.get("canonical_tag")
    if system_tag is not None and canonical_tag is not None and system_tag != canonical_tag:
        raise EmbeddedResourcesError(
            "running build system_tag mismatches canonical_tag"
        )

    payload = dict(running)
    payload["system_tag"] = canonical_tag if canonical_tag is not None else system_tag
    return _validate_identity(payload, source="running build")


class EmbeddedReleaseResources:
    """Verify the embedded bundle and import it into the ReleaseManager cache."""

    def __init__(self, *, release_manager, resources_dir=DEFAULT_RESOURCES_DIR):
        self._manager = release_manager
        self._root = Path(resources_dir)

    # --- loading / verification -----------------------------------------

    def _read_json(self, name):
        path = self._root / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise EmbeddedResourcesError(f"embedded {name} is missing") from exc
        except (OSError, ValueError) as exc:
            raise EmbeddedResourcesError(f"embedded {name} is unreadable") from exc
        if not isinstance(data, dict):
            raise EmbeddedResourcesError(f"embedded {name} is not a JSON object")
        return data

    def load_system_build(self) -> dict:
        data = self._read_json(SYSTEM_BUILD_FILE)
        if data.get("format_version") != RESOURCE_FORMAT_VERSION:
            raise EmbeddedResourcesError("embedded system-build.json has an unknown format")
        _validate_identity(data, source="embedded system-build.json")
        return data

    def load_manifest(self) -> dict:
        data = self._read_json(RESOURCE_MANIFEST_FILE)
        if data.get("format_version") != RESOURCE_FORMAT_VERSION:
            raise EmbeddedResourcesError("embedded resource-manifest.json has an unknown format")
        files = data.get("files")
        if not isinstance(files, dict) or not files:
            raise EmbeddedResourcesError("embedded resource-manifest.json has no files")
        if not _has_deployment_resources(files):
            raise EmbeddedResourcesError(
                "embedded resource-manifest.json has no deploy/docker resources"
            )
        _validate_identity(data, source="embedded resource-manifest.json")
        return data

    def verify(self, *, running_build) -> dict:
        """Verify the bundle against the running build + every file hash.

        Returns the parsed ``system-build.json`` on success; raises
        :class:`EmbeddedResourcesError` (``system_build_resources_invalid``)
        otherwise. No file is copied here — verification is side-effect-free.
        """

        system_build = self.load_system_build()
        manifest = self.load_manifest()

        descriptor_identity = _validate_identity(
            system_build, source="embedded system-build.json"
        )
        manifest_identity = _validate_identity(
            manifest, source="embedded resource-manifest.json"
        )
        expected_identity = _expected_identity(running_build)

        # Both embedded descriptors and the expected running pair must agree on
        # every identity component.  Equal revision/build_id alone is not enough
        # to distinguish a moved tag or a different image pair.
        if manifest_identity != descriptor_identity:
            raise EmbeddedResourcesError(
                "resource manifest identity mismatches system-build.json"
            )
        if descriptor_identity != expected_identity:
            raise EmbeddedResourcesError(
                "embedded resources are from a different system build"
            )

        # Every required resource file must be listed, present and hash-matched.
        listed = manifest["files"]
        for rel in REQUIRED_RESOURCE_FILES:
            if rel not in listed:
                raise EmbeddedResourcesError(f"embedded resource {rel} is not in the manifest")
        for rel, expected in listed.items():
            if not _is_safe_relative(rel):
                raise EmbeddedResourcesError(f"embedded resource path is unsafe: {rel!r}")
            path = self._root.joinpath(*rel.split("/"))
            # Reject symlink escapes: the resolved path must stay in the bundle.
            root_resolved = self._root.resolve()
            try:
                resolved = path.resolve()
            except OSError as exc:
                raise EmbeddedResourcesError(f"embedded resource {rel} is unreadable") from exc
            if root_resolved not in resolved.parents and resolved != root_resolved:
                raise EmbeddedResourcesError(f"embedded resource escapes the bundle: {rel}")
            if path.is_symlink():
                raise EmbeddedResourcesError(f"embedded resource is a symlink: {rel}")
            if not path.is_file():
                raise EmbeddedResourcesError(f"embedded resource {rel} is missing")
            if sha256_file(path) != expected:
                raise EmbeddedResourcesError(f"embedded resource {rel} failed hash verification")
        return system_build

    # --- import into the ReleaseManager cache ---------------------------

    def import_into_cache(self, *, running_build) -> str:
        """Verify and copy the bundle into ``<data>/releases/<tag>/``; return the tag.

        Writes ``manifest.json`` last so a partially-copied cache is never seen as
        complete, and records the complete identity plus hashes so the cache is
        bound to this system build (not just its tag directory name).
        """

        system_build = self.verify(running_build=running_build)
        tag = system_build["system_tag"]
        manifest = self.load_manifest()

        releases_dir = Path(self._manager.releases_dir)
        state_dir = Path(self._manager.state_dir)
        target = releases_dir / tag
        # Fresh import: drop any stale cache directory for this tag first.
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)

        for rel in manifest["files"]:
            src = self._root.joinpath(*rel.split("/"))
            dst = target.joinpath(*rel.split("/"))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

        cache_manifest = {
            "tag": tag,
            "release": tag,
            "repo": REPO,
            **{field: system_build[field] for field in SYSTEM_BUILD_IDENTITY_FIELDS},
            "resource_format": RESOURCE_FORMAT_VERSION,
            "resource_hashes": dict(manifest["files"]),
            "source": "embedded",
            "config_template": RESOURCE_PATHS["config_template"],
            "docker_compose_template": RESOURCE_PATHS["compose_example"],
            "resources": dict(RESOURCE_PATHS),
        }
        # Written last: the cache is only "complete" once this exists.
        state_dir.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text(
            json.dumps(cache_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (state_dir / "selected-release.json").write_text(
            json.dumps({"tag": tag, "source": "embedded"}), encoding="utf-8"
        )
        return tag

    # --- cache identity binding -----------------------------------------

    def is_cache_valid_for_build(self, manifest, *, running_build) -> bool:
        """True only when a cached manifest matches this system build and files.

        Rejects a tag-only stale cache (wrong paired identity — e.g. an older
        ``latest`` or another dev run) and a partially-imported cache (a required
        file missing or hash-mismatched).
        """

        if not isinstance(manifest, dict):
            return False
        if manifest.get("repo") != REPO:
            return False
        if manifest.get("resource_format") != RESOURCE_FORMAT_VERSION:
            return False
        try:
            cached_identity = _validate_identity(manifest, source="cache manifest")
            expected_identity = _expected_identity(running_build)
        except EmbeddedResourcesError:
            return False
        if cached_identity != expected_identity:
            return False
        hashes = manifest.get("resource_hashes")
        tag = manifest.get("tag")
        if (
            not isinstance(hashes, dict)
            or not hashes
            or not _has_deployment_resources(hashes)
            or tag != cached_identity["system_tag"]
        ):
            return False
        root = Path(self._manager.releases_dir) / tag
        for rel in REQUIRED_RESOURCE_FILES:
            if rel not in hashes:
                return False
        for rel, expected in hashes.items():
            path = root.joinpath(*rel.split("/"))
            if not path.is_file() or sha256_file(path) != expected:
                return False
        return True


class ReleaseArchiveResources:
    """Prepare a legacy release's Setup resources from its exact historical tag.

    A legacy release predates the embedded bundle, so its config template, Docker
    Compose resources and deployment metadata are fetched (or reused from cache)
    for the exact selected tag/revision through the existing
    :class:`admin.releases.ReleaseManager` download/verify/cache path. It never
    substitutes ``main``-branch resources and never touches the embedded bundle.
    """

    def __init__(self, *, release_manager):
        self._manager = release_manager

    def import_into_cache(self, *, running_build) -> str:
        """Prepare the exact release resources; return the prepared tag.

        Raises :class:`admin.releases.ReleaseError` if the exact release cannot be
        resolved, downloaded or verified — the caller marks the transition failed
        before any config/deployment mutation.
        """

        build = running_build if isinstance(running_build, dict) else {}
        tag = build.get("canonical_tag") or build.get("system_tag")
        revision = build.get("revision")
        if not isinstance(tag, str) or not tag.strip():
            raise EmbeddedResourcesError(
                "legacy release resource preparation requires a release tag"
            )
        if not isinstance(revision, str) or not revision.strip():
            raise EmbeddedResourcesError(
                "legacy release EMS image revision is unavailable; resource "
                "identity is unverified and cannot be used by the install workflow"
            )
        # ReleaseManager.prepare pins a historical ``latest`` image to its exact
        # revision and a versioned release to its tag, verifies the resources
        # exist, then extracts them into the normal Admin release cache.
        self._manager.prepare(tag.strip(), revision=revision)
        return tag.strip()


# --- build-time generator -------------------------------------------------


def build_resource_manifest(
    resources_dir: Path,
    *,
    system_tag: str,
    channel: str,
    revision: str,
    build_id: str,
    release_tag: str,
    admin_image: str,
    ems_image: str,
) -> dict:
    """Compute the resource manifest (sha256 of every embedded file)."""

    resources_dir = Path(resources_dir)
    files = {
        rel: sha256_file(resources_dir.joinpath(*rel.split("/")))
        for rel in _iter_resource_files(resources_dir)
    }
    if not _has_deployment_resources(files):
        raise EmbeddedResourcesError(
            "generated release resources have no deploy/docker files"
        )
    manifest = {
        "format_version": RESOURCE_FORMAT_VERSION,
        "system_tag": system_tag,
        "channel": channel,
        "revision": revision,
        "build_id": build_id,
        "release_tag": release_tag,
        "admin_image": admin_image,
        "ems_image": ems_image,
        "files": files,
    }
    _validate_identity(manifest, source="generated resource manifest")
    return manifest


def build_system_build_json(
    *, system_tag, channel, revision, build_id, release_tag, admin_image, ems_image
) -> dict:
    descriptor = {
        "format_version": RESOURCE_FORMAT_VERSION,
        "system_tag": system_tag,
        "channel": channel,
        "revision": revision,
        "build_id": build_id,
        "release_tag": release_tag,
        "admin_image": admin_image,
        "ems_image": ems_image,
    }
    _validate_identity(descriptor, source="generated system build")
    return descriptor


def write_release_resources(output_dir, *, source_root, system_tag, channel, revision,
                            build_id, release_tag, admin_image, ems_image):
    """Assemble ``output_dir`` from ``source_root`` and write both descriptors.

    Copies the required Setup files and the ``deploy/docker`` tree, then writes
    ``resource-manifest.json`` and ``system-build.json``. Used at image build time
    so the Admin image and its embedded manifests share one set of build args.
    """

    output_dir = Path(output_dir)
    source_root = Path(source_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    for rel in REQUIRED_RESOURCE_FILES:
        src = source_root.joinpath(*rel.split("/"))
        dst = output_dir.joinpath(*rel.split("/"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    deploy_src = source_root / DEPLOY_DOCKER_DIR
    if deploy_src.is_dir():
        for path in sorted(deploy_src.rglob("*")):
            if path.is_file():
                rel = path.relative_to(source_root)
                dst = output_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dst)

    manifest = build_resource_manifest(
        output_dir,
        system_tag=system_tag,
        channel=channel,
        revision=revision,
        build_id=build_id,
        release_tag=release_tag,
        admin_image=admin_image,
        ems_image=ems_image,
    )
    (output_dir / RESOURCE_MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    system_build = build_system_build_json(
        system_tag=system_tag, channel=channel, revision=revision, build_id=build_id,
        release_tag=release_tag, admin_image=admin_image, ems_image=ems_image,
    )
    (output_dir / SYSTEM_BUILD_FILE).write_text(
        json.dumps(system_build, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return output_dir
