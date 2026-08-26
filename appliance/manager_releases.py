# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a published Appliance Manager package claims.

Reuses the OS release verifier and keyring rather than adding a second trust
anchor, and the same state-schema comparison, because installing an older
manager raises the same question an older image did.

Unlike the OS manifest, the state-schema declaration is required: no manager
package has shipped, so there is no history to be lenient towards.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from appliance import os_releases, persistent_state

MANIFEST_FORMAT_VERSION = 1

PACKAGE_NAME = "ems-appliance-manager"

REQUIRED_FIELDS = (
    "format_version",
    "package",
    "version",
    "architecture",
    "build_id",
    "created_at",
    "project_revision",
    "artifact",
)

VERIFIED_SIGNATURE = os_releases.VERIFIED_SIGNATURE
VERIFIED_NONE = os_releases.VERIFIED_NONE

# A build id names files: a staging copy while a revert is prepared, and the
# assets a release publishes. Path separators, whitespace and control
# characters are refused so it can only ever name one file beside its siblings.
# The same shape build_authority.BUILD_ID_PATTERN enforces on a build record.
BUILD_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")


class ManagerReleaseError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ManagerRelease:
    """One published package, as its signed manifest describes it."""

    release_id: str
    version: str
    architecture: str
    build_id: str
    created_at: str
    project_revision: str
    artifact_name: str
    artifact_digest: str
    artifact_size: int
    source_date_epoch: int = 0
    dpkg_deb: str = ""
    compression: str = ""
    state_implements: dict = field(default_factory=dict)
    state_reads: dict = field(default_factory=dict)
    verified: str = VERIFIED_NONE

    @property
    def signed(self):
        return self.verified == VERIFIED_SIGNATURE

    def to_dict(self):
        return {
            "release_id": self.release_id,
            "version": self.version,
            "architecture": self.architecture,
            "build_id": self.build_id,
            "created_at": self.created_at,
            "project_revision": self.project_revision,
            "artifact": {
                "name": self.artifact_name,
                "digest": self.artifact_digest,
                "size_bytes": self.artifact_size,
            },
            "reproducibility": {
                "source_date_epoch": self.source_date_epoch,
                "dpkg_deb": self.dpkg_deb,
                "compression": self.compression,
            },
            "state_schemas": {
                "implements": dict(self.state_implements),
                "reads": dict(self.state_reads),
            },
            "verified": self.verified,
        }


def _state_schemas(block):
    """What the manager in this package writes, and the oldest it can read.

    Required here, unlike the OS manifest where it had to stay optional for
    artefacts built before it existed. No manager package has ever been
    published, so there is no history to be lenient towards — and being lenient
    would mean accepting a package that cannot say whether it can read this
    appliance's state.
    """

    if not isinstance(block, dict):
        raise ManagerReleaseError(
            "manager_manifest_invalid", "state_schemas is missing or not an object"
        )
    parsed = {}
    for key in ("implements", "reads"):
        values = block.get(key)
        if not isinstance(values, dict) or not values:
            raise ManagerReleaseError(
                "manager_manifest_invalid", f"state_schemas.{key} names no schemas"
            )
        clean = {}
        for name, value in values.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ManagerReleaseError(
                    "manager_manifest_invalid",
                    f"state_schemas.{key}.{name} is not a schema number",
                )
            clean[str(name)] = int(value)
        parsed[key] = clean

    undeclared = sorted(set(parsed["reads"]) - set(parsed["implements"]))
    if undeclared:
        raise ManagerReleaseError(
            "manager_manifest_invalid",
            f"state_schemas.reads names {', '.join(undeclared)}, which it does not implement",
        )
    ahead = sorted(
        name for name, value in parsed["reads"].items() if value > parsed["implements"][name]
    )
    if ahead:
        raise ManagerReleaseError(
            "manager_manifest_invalid",
            f"state_schemas.reads is ahead of implements for {', '.join(ahead)}",
        )
    return parsed["implements"], parsed["reads"]


def parse_manifest(payload, *, release_id="", verified=VERIFIED_NONE):
    """A manifest, or a refusal naming the field that was wrong."""

    if not isinstance(payload, dict):
        raise ManagerReleaseError("manager_manifest_invalid", "the manifest is not an object")
    missing = [name for name in REQUIRED_FIELDS if name not in payload]
    if missing:
        raise ManagerReleaseError(
            "manager_manifest_invalid", f"the manifest omits {', '.join(missing)}"
        )
    if payload["format_version"] != MANIFEST_FORMAT_VERSION:
        raise ManagerReleaseError(
            "manager_manifest_unsupported",
            f"manifest format {payload['format_version']!r} is not format "
            f"{MANIFEST_FORMAT_VERSION}",
        )
    if str(payload["package"]) != PACKAGE_NAME:
        raise ManagerReleaseError(
            "manager_manifest_invalid",
            f"the manifest describes {payload['package']!r}, not {PACKAGE_NAME}",
        )

    version = str(payload["version"]).strip()
    if not version:
        raise ManagerReleaseError("manager_manifest_invalid", "the manifest names no version")
    if "-" in version:
        # dpkg reads a hyphen as a Debian revision and sorts it *above* the
        # release, while this project's comparator ranks it below. A version
        # both authorities disagree about cannot be ordered at all.
        raise ManagerReleaseError(
            "manager_manifest_invalid",
            f"version {version!r} uses a hyphen; a pre-release is spelled with ~",
        )

    build_id = str(payload["build_id"]).strip()
    if not BUILD_ID.match(build_id):
        raise ManagerReleaseError(
            "manager_manifest_invalid",
            f"build id {build_id!r} is not an identifier: letters, digits, dot, dash and "
            "underscore, at most 96 characters",
        )

    artifact = payload["artifact"]
    if not isinstance(artifact, dict):
        raise ManagerReleaseError("manager_manifest_invalid", "the artifact is not an object")
    try:
        size = int(artifact.get("size_bytes") or 0)
    except (TypeError, ValueError):
        raise ManagerReleaseError("manager_manifest_invalid", "the artifact size is not a number")
    if size <= 0:
        raise ManagerReleaseError("manager_manifest_invalid", "the artifact size must be positive")

    digest = str(artifact.get("digest") or "")
    if not os_releases.DIGEST.match(digest):
        raise ManagerReleaseError(
            "manager_manifest_invalid", f"the artifact digest {digest!r} is not a sha256 digest"
        )

    implements, reads = _state_schemas(payload.get("state_schemas"))
    reproducibility = payload.get("reproducibility")
    reproducibility = reproducibility if isinstance(reproducibility, dict) else {}

    return ManagerRelease(
        release_id=os_releases.validate_release_id(release_id or payload["build_id"]),
        version=version,
        architecture=str(payload["architecture"]),
        build_id=build_id,
        created_at=str(payload["created_at"]),
        project_revision=str(payload["project_revision"]),
        artifact_name=str(artifact.get("name") or ""),
        artifact_digest=digest,
        artifact_size=size,
        source_date_epoch=int(reproducibility.get("source_date_epoch") or 0),
        dpkg_deb=str(reproducibility.get("dpkg_deb") or ""),
        compression=str(reproducibility.get("compression") or ""),
        state_implements=implements,
        state_reads=reads,
        verified=verified,
    )


def read_manifest(path, *, release_id="", verified=VERIFIED_NONE):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManagerReleaseError("manager_manifest_missing", f"{path} does not exist")
    except (OSError, ValueError) as exc:
        raise ManagerReleaseError("manager_manifest_invalid", f"{path} could not be read: {exc}")
    return parse_manifest(payload, release_id=release_id, verified=verified)


def verify_artifact(release, path):
    """The bytes on disk are the bytes the signed manifest names."""

    observed = os_releases.file_digest(path)
    if observed != release.artifact_digest:
        raise ManagerReleaseError(
            "manager_artifact_corrupt",
            f"{Path(path).name} hashes to {observed}, the manifest names "
            f"{release.artifact_digest}",
        )
    size = Path(path).stat().st_size
    if size != release.artifact_size:
        raise ManagerReleaseError(
            "manager_artifact_corrupt",
            f"{Path(path).name} is {size} bytes, the manifest names {release.artifact_size}",
        )
    return True


def compatibility_problems(release, *, architecture, state_schemas):
    """Every reason this package may not be installed on this appliance.

    An empty list is the only thing that authorises an install. Going backwards
    is not among the reasons: an operator may deliberately install an older
    package, and refusing that would take away the recovery this whole path
    exists to provide. What is refused is a package whose manager could not read
    the state already on the disk — which is the question a version comparison
    was never able to answer.
    """

    problems = []
    if release.architecture != architecture:
        problems.append(
            {
                "code": "package_architecture_unsupported",
                "message": (
                    f"the package is {release.architecture}, this appliance is {architecture}"
                ),
            }
        )
    problems.extend(os_releases.state_schema_problems(release, recorded=state_schemas))
    return problems


def implemented_state_schemas():
    """What a manager built from this tree would declare."""

    return {
        "implements": persistent_state.implemented_schemas(),
        "reads": persistent_state.readable_floors(),
    }
