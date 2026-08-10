# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a real builder run produced, and how a release proves it came from one.

A signed OS release says which rpi-image-gen revision built it. Nothing made
that true: the wrapper wrote the pinned revision out of the lock for whatever
archive it was handed, so an artefact assembled by any means could be signed as
if the pinned generator had produced it.

A build authority closes that. One completed builder run writes exactly one of
these, into its own output directory, naming the source form and revision it
built from, the hash of the source tree it built from, and the SHA-256 of the
artefacts it produced. Production signing then verifies the artefact in front of
it against that record and refuses anything else.

A manually supplied artefact stays supported — a development bench needs it —
but only as development: it never receives production provenance and it is never
signed.

Nothing here reads a request. Every path comes from a build operator's command
line, and the object is a plain file rather than a service.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 2 binds the project's own tree. Schema 1 named only the generator's, so an
# image built from a working tree with local appliance changes claimed the clean
# revision it was branched from.
# 3 binds the builder itself: the base image it booted, its kernel, and the
# versions of the tools that assembled the filesystem. Two identical source
# trees built on different builders are not the same supply chain, and a
# release that cannot say which builder produced it cannot be diagnosed later.
SCHEMA_VERSION = 3
AUTHORITY_NAME = "build-authority.json"
ENVIRONMENT_NAME = "builder-environment.json"

FILE_MODE = 0o644

SOURCE_FORMS = ("git", "tarball")

PROFILES = ("rpi4", "rpi5")

# A build id names a directory (``build-<id>``) and travels through release
# scripts as an argument. Path separators, whitespace and control characters
# are refused so it can only ever name one directory beside its siblings.
BUILD_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")

MISMATCH = "build_authority_mismatch"
INCOMPLETE = "build_authority_incomplete"
UNSUPPORTED = "build_authority_unsupported"
UNREADABLE = "build_authority_unreadable"
INVALID_IDENTIFIER = "build_identifier_invalid"


def validate_profile(profile):
    """The hardware profiles this project builds, and nothing else."""

    if profile not in PROFILES:
        raise BuildAuthorityError(
            INVALID_IDENTIFIER,
            f"{profile!r} is not a supported profile ({', '.join(PROFILES)})",
        )
    return profile


def validate_build_id(build_id):
    if not isinstance(build_id, str) or not BUILD_ID_PATTERN.match(build_id):
        raise BuildAuthorityError(
            INVALID_IDENTIFIER,
            f"{build_id!r} is not a usable build id: "
            "letters, digits, dot, dash and underscore, at most 96 characters",
        )
    return build_id


class BuildAuthorityError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def file_sha256(path, *, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def canonical_hash(payload):
    """One hash over the authority object, independent of key order."""

    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class Builder:
    source_form: str = ""
    revision: str = ""
    source_tree_sha256: str = ""

    def to_dict(self):
        return {
            "source_form": self.source_form,
            "revision": self.revision,
            "source_tree_sha256": self.source_tree_sha256,
        }


@dataclass(frozen=True)
class Project:
    """This repository, as the full revision and a hash of its tracked tree.

    A short revision is a prefix, and a revision alone says nothing about the
    files a build actually read. Both, or the build has no project provenance.
    """

    revision: str = ""
    tree_sha256: str = ""

    def to_dict(self):
        return {"revision": self.revision, "tree_sha256": self.tree_sha256}


@dataclass(frozen=True)
class Artefact:
    path: str = ""
    sha256: str = ""

    def to_dict(self):
        return {"path": self.path, "sha256": self.sha256}


ENVIRONMENT_SCHEMA_VERSION = 1

# What has to be known about a builder before its output may be signed. Each is
# either an identity of the machine the image was assembled on, or a version of
# a tool that decided what went into the filesystem.
ENVIRONMENT_REQUIRED = (
    "base_image_lock_id",
    "base_image_sha512",
    "os_release",
    "kernel",
    "architecture",
    "python_version",
    "podman_version",
    "mmdebstrap_version",
    "binfmt_handler",
    "dependency_manifest_sha256",
    "captured_at",
)


@dataclass(frozen=True)
class BuilderEnvironment:
    """The machine that assembled the image, bounded and hashable.

    Deliberately not the process environment: no variables, no credentials, no
    cloud-init data. Only the identities a later supply-chain question needs —
    which base image booted, which kernel ran, which versions of podman,
    mmdebstrap and the aarch64 binfmt handler produced the root filesystem.
    """

    base_image_lock_id: str = ""
    base_image_sha512: str = ""
    os_release: str = ""
    kernel: str = ""
    architecture: str = ""
    python_version: str = ""
    podman_version: str = ""
    mmdebstrap_version: str = ""
    qemu_version: str = ""
    binfmt_handler: str = ""
    dependency_manifest_sha256: str = ""
    critical_packages: tuple = ()
    captured_at: str = ""
    schema_version: int = ENVIRONMENT_SCHEMA_VERSION

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "base_image_lock_id": self.base_image_lock_id,
            "base_image_sha512": self.base_image_sha512,
            "os_release": self.os_release,
            "kernel": self.kernel,
            "architecture": self.architecture,
            "python_version": self.python_version,
            "podman_version": self.podman_version,
            "mmdebstrap_version": self.mmdebstrap_version,
            "qemu_version": self.qemu_version,
            "binfmt_handler": self.binfmt_handler,
            "dependency_manifest_sha256": self.dependency_manifest_sha256,
            "critical_packages": list(self.critical_packages),
            "captured_at": self.captured_at,
        }

    @property
    def canonical_hash(self):
        return canonical_hash(self.to_dict())

    @property
    def recorded(self):
        return any(getattr(self, name) for name in ENVIRONMENT_REQUIRED)

    def missing(self):
        return tuple(name for name in ENVIRONMENT_REQUIRED if not getattr(self, name))


def parse_environment(payload):
    if not isinstance(payload, dict):
        raise BuildAuthorityError(UNREADABLE, "the builder environment is not an object")
    version = payload.get("schema_version", ENVIRONMENT_SCHEMA_VERSION)
    if version != ENVIRONMENT_SCHEMA_VERSION:
        raise BuildAuthorityError(
            UNSUPPORTED,
            f"builder environment schema {version!r} is not schema "
            f"{ENVIRONMENT_SCHEMA_VERSION}",
        )
    packages = payload.get("critical_packages") or []
    if not isinstance(packages, list):
        raise BuildAuthorityError(UNREADABLE, "critical_packages is not a list")
    return BuilderEnvironment(
        base_image_lock_id=str(payload.get("base_image_lock_id") or ""),
        base_image_sha512=str(payload.get("base_image_sha512") or ""),
        os_release=str(payload.get("os_release") or ""),
        kernel=str(payload.get("kernel") or ""),
        architecture=str(payload.get("architecture") or ""),
        python_version=str(payload.get("python_version") or ""),
        podman_version=str(payload.get("podman_version") or ""),
        mmdebstrap_version=str(payload.get("mmdebstrap_version") or ""),
        qemu_version=str(payload.get("qemu_version") or ""),
        binfmt_handler=str(payload.get("binfmt_handler") or ""),
        dependency_manifest_sha256=str(payload.get("dependency_manifest_sha256") or ""),
        critical_packages=tuple(str(item) for item in packages),
        captured_at=str(payload.get("captured_at") or ""),
    )


def read_environment(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise BuildAuthorityError(UNREADABLE, f"{path} could not be read: {exc}")
    except ValueError:
        raise BuildAuthorityError(UNREADABLE, f"{path} is not valid JSON")
    return parse_environment(payload)


@dataclass(frozen=True)
class BuildAuthority:
    builder: Builder = field(default_factory=Builder)
    project: Project = field(default_factory=Project)
    profile: str = ""
    build_id: str = ""
    image: Artefact = field(default_factory=Artefact)
    update: Artefact = field(default_factory=Artefact)
    package_sha256: str = ""
    completed: bool = False
    environment: BuilderEnvironment = field(default_factory=BuilderEnvironment)
    schema_version: int = SCHEMA_VERSION

    @property
    def project_revision(self):
        return self.project.revision

    @property
    def builder_environment_sha256(self):
        return self.environment.canonical_hash

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "builder": self.builder.to_dict(),
            "builder_environment": self.environment.to_dict(),
            "builder_environment_sha256": self.builder_environment_sha256,
            "project": self.project.to_dict(),
            "profile": self.profile,
            "build_id": self.build_id,
            "image": self.image.to_dict(),
            "update": self.update.to_dict(),
            "package_sha256": self.package_sha256,
            "completed": self.completed,
        }

    @property
    def canonical_hash(self):
        return canonical_hash(self.to_dict())


def parse(payload):
    if not isinstance(payload, dict):
        raise BuildAuthorityError(UNREADABLE, "the build authority is not an object")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise BuildAuthorityError(
            UNSUPPORTED, f"build authority schema {version!r} is not schema {SCHEMA_VERSION}"
        )
    builder = payload.get("builder") or {}
    if not isinstance(builder, dict):
        raise BuildAuthorityError(UNREADABLE, "the builder description is not an object")
    project = payload.get("project") or {}
    if not isinstance(project, dict):
        raise BuildAuthorityError(UNREADABLE, "the project description is not an object")
    environment = parse_environment(payload.get("builder_environment") or {})
    recorded_hash = str(payload.get("builder_environment_sha256") or "")
    if recorded_hash and recorded_hash != environment.canonical_hash:
        raise BuildAuthorityError(
            MISMATCH,
            "the recorded builder environment hash does not describe the recorded "
            "builder environment",
        )
    return BuildAuthority(
        builder=Builder(
            source_form=str(builder.get("source_form") or ""),
            revision=str(builder.get("revision") or ""),
            source_tree_sha256=str(builder.get("source_tree_sha256") or ""),
        ),
        project=Project(
            revision=str(project.get("revision") or ""),
            tree_sha256=str(project.get("tree_sha256") or ""),
        ),
        profile=str(payload.get("profile") or ""),
        build_id=str(payload.get("build_id") or ""),
        image=_artefact(payload.get("image")),
        update=_artefact(payload.get("update")),
        package_sha256=str(payload.get("package_sha256") or ""),
        completed=bool(payload.get("completed")),
        environment=environment,
    )


def _artefact(payload):
    if not isinstance(payload, dict):
        return Artefact()
    return Artefact(
        path=str(payload.get("path") or ""), sha256=str(payload.get("sha256") or "")
    )


def read(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise BuildAuthorityError(UNREADABLE, f"{path} could not be read: {exc}")
    except ValueError:
        raise BuildAuthorityError(UNREADABLE, f"{path} is not valid JSON")
    return parse(payload)


def write(directory, authority):
    target = Path(directory) / AUTHORITY_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(authority.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(target, FILE_MODE)
    return target


def prepare_output(root, *, build_id):
    """One build, one fresh directory. Stale artefacts cannot be inherited.

    A reused directory is how yesterday's ``update.tar.zst`` ends up beside
    today's metadata, which would let a release be signed for an artefact no
    completed build produced.
    """

    validate_build_id(build_id)
    target = Path(root) / f"build-{build_id}"
    if target.exists():
        raise BuildAuthorityError(
            MISMATCH, f"{target} already exists; a build id names exactly one build"
        )
    target.mkdir(parents=True)
    return target


def _artefact_problems(label, declared, path):
    if not declared.sha256:
        return [f"{INCOMPLETE}: the build authority records no {label} hash"]
    try:
        observed = file_sha256(path)
    except OSError as exc:
        return [f"{MISMATCH}: the {label} could not be read: {exc}"]
    if observed != declared.sha256:
        return [
            f"{MISMATCH}: {path} hashes to {observed}, the build authority records "
            f"{declared.sha256}"
        ]
    return []


def verify_update(authority, path, *, profile="", revision="", build_id="",
                  require_environment=False):
    """Is this artefact the one that completed build produced? Say why not."""

    problems = list(_common_problems(authority, profile=profile, revision=revision,
                                     build_id=build_id,
                                     require_environment=require_environment))
    problems.extend(_artefact_problems("update artefact", authority.update, path))
    return tuple(problems)


def verify_image(authority, path, *, profile="", revision="", build_id="",
                 require_environment=False):
    problems = list(_common_problems(authority, profile=profile, revision=revision,
                                     build_id=build_id,
                                     require_environment=require_environment))
    problems.extend(_artefact_problems("image", authority.image, path))
    return tuple(problems)


def _common_problems(authority, *, profile, revision, build_id, require_environment=False):
    problems = []
    if require_environment:
        missing = authority.environment.missing()
        if missing:
            problems.append(
                f"{INCOMPLETE}: the build authority records no builder environment "
                f"({', '.join(missing)})"
            )
    if not authority.completed:
        problems.append(f"{INCOMPLETE}: the build did not complete")
    if authority.builder.source_form not in SOURCE_FORMS:
        problems.append(
            f"{INCOMPLETE}: {authority.builder.source_form!r} is not a proven source form"
        )
    if not authority.builder.source_tree_sha256:
        problems.append(f"{INCOMPLETE}: the build authority records no source tree hash")
    # The project's own tree, to the same standard as the generator's. A full
    # revision, because a short one names a prefix and not a commit.
    if len(authority.project.revision) != 40:
        problems.append(
            f"{INCOMPLETE}: the build authority records no full project revision"
        )
    if not authority.project.tree_sha256:
        problems.append(f"{INCOMPLETE}: the build authority records no project tree hash")
    if authority.profile not in PROFILES:
        problems.append(
            f"{INVALID_IDENTIFIER}: {authority.profile!r} is not a supported profile"
        )
    if not BUILD_ID_PATTERN.match(authority.build_id or ""):
        problems.append(
            f"{INVALID_IDENTIFIER}: {authority.build_id!r} is not a usable build id"
        )
    if profile and authority.profile != profile:
        problems.append(
            f"{MISMATCH}: the build authority names profile {authority.profile!r}, "
            f"this release is {profile!r}"
        )
    if revision and authority.builder.revision != revision:
        problems.append(
            f"{MISMATCH}: the build authority names generator revision "
            f"{authority.builder.revision!r}, the lock pins {revision!r}"
        )
    if build_id and authority.build_id != build_id:
        problems.append(
            f"{MISMATCH}: the build authority names build {authority.build_id!r}, "
            f"this release is {build_id!r}"
        )
    return problems
