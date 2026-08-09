# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which operating-system artifacts this appliance is allowed to install.

The browser may name a release. It may not name a URL, a path, a device, a
repository, a key or a checksum — every one of those comes from the root-owned
host configuration or from a signed manifest, because a request that could
supply any of them could supply an image of its own choosing.

The trust chain is short on purpose:

    root-owned keyring
      → detached signature over the release manifest
        → manifest carries the archive's SHA-256
          → archive carries the boot and root image digests
            → each image is verified again after it is written

A development override exists so an unsigned artifact can be exercised on a
bench, but only from the root CLI, only when the host configuration allows it,
and never in a way a release gate can read as a pass.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_FORMAT_VERSION = 1
MAX_MANIFEST_BYTES = 256 * 1024
MAX_INDEX_BYTES = 1024 * 1024

VERIFIED_SIGNATURE = "signature"
VERIFIED_DEVELOPMENT = "development_override"
VERIFIED_NONE = "unverified"

RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

REQUIRED_MANIFEST_FIELDS = (
    "format_version",
    "release_version",
    "build_id",
    "created_at",
    "architecture",
    "compatible_hardware",
    "os_release",
    "project_revision",
    "appliance_manager_version",
    "minimum_appliance_manager_version",
    "layout_id",
    "slot_schema_version",
    "persistent_schema_version",
    "archive",
    "members",
)

# Exactly what an update archive may contain. Anything else is an artifact this
# appliance does not know how to write, not an artifact it should try to write.
#
# These are rpi-image-gen's own names, produced by image-rota's post-image.sh:
# one boot payload and one system payload, both android-sparse. There is no
# per-slot boot image because upstream builds one bit-for-bit identical slot
# pair and selects the root filesystem through /dev/disk/by-slot at boot.
MEMBER_BOOT = "boot"
MEMBER_SYSTEM = "system"
REQUIRED_MEMBERS = (MEMBER_BOOT, MEMBER_SYSTEM)

# A manifest must never carry key material. These are refused outright rather
# than ignored, so a manifest that tried cannot be quietly accepted.
FORBIDDEN_MANIFEST_KEYS = ("private_key", "signing_key", "secret", "passphrase", "token")


class ReleaseError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ArtifactMember:
    name: str
    digest: str
    role: str
    slot: str = ""

    def to_dict(self):
        return {"name": self.name, "digest": self.digest, "role": self.role, "slot": self.slot}


@dataclass(frozen=True)
class OsRelease:
    """One signed, compatible operating-system artifact."""

    release_id: str
    release_version: str
    build_id: str
    created_at: str
    architecture: str
    compatible_hardware: tuple
    os_release: str
    project_revision: str
    appliance_manager_version: str
    minimum_appliance_manager_version: str
    layout_id: str
    slot_schema_version: int
    persistent_schema_version: int
    archive_name: str
    archive_digest: str
    archive_size: int
    members: dict = field(default_factory=dict)
    rpi_image_gen_revision: str = ""
    verified: str = VERIFIED_NONE

    @property
    def signed(self):
        return self.verified == VERIFIED_SIGNATURE

    def member(self, name):
        try:
            return self.members[name]
        except KeyError:
            raise ReleaseError("artifact_member_missing", f"the artifact has no {name}")

    def boot_member(self):
        """The one boot payload. Both slots receive identical boot filesystems."""

        for entry in self.members.values():
            if entry.role == "boot":
                return entry
        raise ReleaseError("artifact_member_missing", "the artifact has no boot image")

    def root_member(self):
        for entry in self.members.values():
            if entry.role == "root":
                return entry
        raise ReleaseError("artifact_member_missing", "the artifact has no system image")

    def to_dict(self):
        return {
            "release_id": self.release_id,
            "release_version": self.release_version,
            "build_id": self.build_id,
            "created_at": self.created_at,
            "architecture": self.architecture,
            "compatible_hardware": list(self.compatible_hardware),
            "os_release": self.os_release,
            "project_revision": self.project_revision,
            "rpi_image_gen_revision": self.rpi_image_gen_revision,
            "appliance_manager_version": self.appliance_manager_version,
            "minimum_appliance_manager_version": self.minimum_appliance_manager_version,
            "layout_id": self.layout_id,
            "slot_schema_version": self.slot_schema_version,
            "persistent_schema_version": self.persistent_schema_version,
            "archive_name": self.archive_name,
            "archive_digest": self.archive_digest,
            "archive_size": self.archive_size,
            "members": {name: entry.to_dict() for name, entry in sorted(self.members.items())},
            "verified": self.verified,
            "signed": self.signed,
        }


def validate_release_id(value):
    text = str(value or "").strip()
    if not RELEASE_ID.match(text):
        raise ReleaseError("invalid_release_id", "the release id is not a valid identifier")
    return text


def _digest(value, *, label):
    text = str(value or "").strip().lower()
    if not DIGEST.match(text):
        raise ReleaseError("artifact_digest_invalid", f"{label} is not a sha256 digest")
    return text


def parse_manifest(payload, *, release_id="", verified=VERIFIED_NONE):
    """Turn manifest JSON into an ``OsRelease`` or refuse it."""

    if not isinstance(payload, dict):
        raise ReleaseError("release_manifest_invalid", "the release manifest is not an object")
    for key in FORBIDDEN_MANIFEST_KEYS:
        if key in payload:
            raise ReleaseError(
                "release_manifest_invalid", f"a release manifest must not carry {key!r}"
            )
    missing = [name for name in REQUIRED_MANIFEST_FIELDS if name not in payload]
    if missing:
        raise ReleaseError(
            "release_manifest_invalid", f"the release manifest is missing {', '.join(missing)}"
        )
    if payload["format_version"] != MANIFEST_FORMAT_VERSION:
        raise ReleaseError(
            "release_manifest_unsupported",
            f"manifest format {payload['format_version']!r} is not format {MANIFEST_FORMAT_VERSION}",
        )

    archive = payload["archive"]
    if not isinstance(archive, dict):
        raise ReleaseError("release_manifest_invalid", "the archive description is not an object")

    raw_members = payload["members"]
    if not isinstance(raw_members, dict):
        raise ReleaseError("release_manifest_invalid", "the member list is not an object")
    if set(raw_members) != set(REQUIRED_MEMBERS):
        raise ReleaseError(
            "release_manifest_invalid",
            f"an update artifact holds exactly {', '.join(REQUIRED_MEMBERS)}",
        )
    members = {}
    for name, entry in raw_members.items():
        if not isinstance(entry, dict):
            raise ReleaseError("release_manifest_invalid", f"member {name} is not an object")
        members[name] = ArtifactMember(
            name=name,
            digest=_digest(entry.get("digest"), label=f"the digest of {name}"),
            role=str(entry.get("role") or ""),
            slot=str(entry.get("slot") or ""),
        )

    try:
        size = int(archive.get("size_bytes") or 0)
    except (TypeError, ValueError):
        raise ReleaseError("release_manifest_invalid", "the archive size is not a number")
    if size <= 0:
        raise ReleaseError("release_manifest_invalid", "the archive size must be positive")

    return OsRelease(
        release_id=validate_release_id(release_id or payload["build_id"]),
        release_version=str(payload["release_version"]),
        build_id=str(payload["build_id"]),
        created_at=str(payload["created_at"]),
        architecture=str(payload["architecture"]),
        compatible_hardware=tuple(str(item) for item in payload["compatible_hardware"]),
        os_release=str(payload["os_release"]),
        project_revision=str(payload["project_revision"]),
        rpi_image_gen_revision=str(payload.get("rpi_image_gen_revision") or ""),
        appliance_manager_version=str(payload["appliance_manager_version"]),
        minimum_appliance_manager_version=str(payload["minimum_appliance_manager_version"]),
        layout_id=str(payload["layout_id"]),
        slot_schema_version=int(payload["slot_schema_version"]),
        persistent_schema_version=int(payload["persistent_schema_version"]),
        archive_name=str(archive.get("name") or ""),
        archive_digest=_digest(archive.get("digest"), label="the archive digest"),
        archive_size=size,
        members=members,
        verified=verified,
    )


# --- signature authority -----------------------------------------------------


class SignatureVerifier:
    """Detached-signature verification against a root-owned keyring.

    The keyring path comes from the host configuration only. ``gpg`` is invoked
    through the allowlisted command runner with a fixed argv; there is no shell
    and no caller-supplied option.
    """

    def __init__(self, runner, *, keyring):
        self.runner = runner
        self.keyring = str(keyring or "")

    @property
    def available(self):
        return bool(self.keyring) and self.runner is not None and self.runner.available("gpg")

    def verify(self, manifest_path, signature_path):
        if not self.keyring:
            raise ReleaseError(
                "release_keyring_missing",
                "no OS release keyring is configured; an unsigned artifact is refused",
            )
        if not Path(self.keyring).is_file():
            raise ReleaseError(
                "release_keyring_missing", f"the OS release keyring {self.keyring} does not exist"
            )
        if self.runner is None or not self.runner.available("gpg"):
            raise ReleaseError(
                "release_verification_unavailable", "gpg is not installed on this appliance"
            )
        result = self.runner.run(
            "gpg",
            [
                "--batch",
                "--no-default-keyring",
                "--keyring",
                self.keyring,
                "--verify",
                str(signature_path),
                str(manifest_path),
            ],
            timeout=60,
        )
        if not result.ok:
            raise ReleaseError(
                "release_signature_invalid",
                "the release manifest signature could not be verified against the appliance keyring",
            )
        return True


def file_digest(path, *, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


# --- the release source ------------------------------------------------------


@dataclass(frozen=True)
class ReleaseSource:
    """Where artifacts come from. Root-owned configuration, never a request."""

    index_path: str = ""
    directory: str = ""
    keyring: str = ""
    allow_unsigned: bool = False

    @property
    def configured(self):
        return bool(self.index_path or self.directory)


class OsReleaseCatalogue:
    """Resolve a release id to a verified ``OsRelease``.

    Verification happens here, once, before anything downstream sees a release.
    A caller that receives an ``OsRelease`` may rely on it being signed unless
    ``verified`` says otherwise, and the staging path refuses everything that is
    not ``VERIFIED_SIGNATURE`` unless the development override was used.
    """

    def __init__(self, source, *, runner=None, verifier=None):
        self.source = source
        self.runner = runner
        self.verifier = verifier or SignatureVerifier(runner, keyring=source.keyring)

    # --- discovery ---------------------------------------------------------

    def _manifest_paths(self):
        directory = Path(self.source.directory) if self.source.directory else None
        if directory is None or not directory.is_dir():
            return []
        return sorted(directory.glob("*.manifest.json"))

    def _load(self, path, *, verified_override=None):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReleaseError("release_manifest_unreadable", f"{path.name} could not be read: {exc}")
        if len(raw.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise ReleaseError("release_manifest_invalid", f"{path.name} is implausibly large")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise ReleaseError("release_manifest_invalid", f"{path.name} is not valid JSON")

        verified = verified_override
        if verified is None:
            verified = self._verify(path)
        release_id = path.name[: -len(".manifest.json")]
        return parse_manifest(payload, release_id=release_id, verified=verified)

    def _verify(self, manifest_path):
        signature = manifest_path.with_suffix(manifest_path.suffix + ".asc")
        if not signature.exists():
            signature = Path(str(manifest_path) + ".sig")
        if signature.exists():
            self.verifier.verify(manifest_path, signature)
            return VERIFIED_SIGNATURE
        if self.source.allow_unsigned:
            # Reachable only from the root CLI on a host whose configuration
            # enables it. It is a distinct value, never VERIFIED_SIGNATURE, so
            # no consumer can mistake it for a release-gate pass.
            return VERIFIED_DEVELOPMENT
        raise ReleaseError(
            "release_signature_missing",
            f"{manifest_path.name} has no detached signature; an unsigned OS artifact is refused",
        )

    def available(self):
        """Every release this appliance can see, with its verification state."""

        releases = []
        for path in self._manifest_paths():
            try:
                releases.append(self._load(path))
            except ReleaseError:
                continue
        releases.sort(key=lambda item: (item.release_version, item.build_id), reverse=True)
        return releases

    def get(self, release_id):
        wanted = validate_release_id(release_id)
        for path in self._manifest_paths():
            if path.name[: -len(".manifest.json")] == wanted:
                return self._load(path)
        raise ReleaseError("unknown_release", f"{wanted} is not an available OS release")

    def archive_path(self, release):
        """The artifact file belonging to a release, derived — never requested."""

        directory = Path(self.source.directory or "")
        name = release.archive_name or f"{release.release_id}.tar.zst"
        candidate = directory / Path(name).name
        if not candidate.is_file():
            raise ReleaseError(
                "artifact_missing", f"{candidate.name} is not present in the release directory"
            )
        return candidate

    def verify_archive(self, release, path):
        observed = file_digest(path)
        if observed != release.archive_digest:
            raise ReleaseError(
                "artifact_digest_mismatch",
                f"the artifact hashes to {observed}, the manifest declares {release.archive_digest}",
            )
        return observed


# --- compatibility -----------------------------------------------------------


def _version_key(text):
    parts = []
    for chunk in str(text).lstrip("v").split("-", 1)[0].split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def compatibility_problems(
    release,
    *,
    layout,
    board,
    appliance_version,
    persistent_schema_version,
    current_build_id="",
    repair=False,
    rollback=False,
):
    """Every reason this artifact may not be installed on this appliance.

    An empty list is the only thing that authorises a write. Each entry is a
    stable code plus a message an operator can act on.
    """

    problems = []
    if release.architecture != "arm64":
        problems.append(
            {
                "code": "artifact_architecture_unsupported",
                "message": f"the artifact is {release.architecture}, this appliance is arm64",
            }
        )
    if release.compatible_hardware and board:
        if not any(str(entry) == board for entry in release.compatible_hardware):
            problems.append(
                {
                    "code": "artifact_hardware_incompatible",
                    "message": f"the artifact does not list {board} as compatible hardware",
                }
            )
    if layout is not None and release.layout_id != layout.layout_id:
        problems.append(
            {
                "code": "artifact_layout_unknown",
                "message": (
                    f"the artifact was built for layout {release.layout_id}, this appliance "
                    f"is {layout.layout_id}"
                ),
            }
        )
    if layout is not None and release.slot_schema_version != layout.slot_schema_version:
        problems.append(
            {
                "code": "artifact_slot_schema_unknown",
                "message": (
                    f"the artifact declares slot schema {release.slot_schema_version}, this "
                    f"appliance implements {layout.slot_schema_version}"
                ),
            }
        )
    if release.persistent_schema_version > persistent_schema_version:
        problems.append(
            {
                "code": "artifact_persistent_schema_too_new",
                "message": (
                    f"the artifact needs persistent schema {release.persistent_schema_version}, "
                    f"this appliance implements {persistent_schema_version}"
                ),
            }
        )
    if _version_key(appliance_version) < _version_key(release.minimum_appliance_manager_version):
        problems.append(
            {
                "code": "appliance_manager_too_old",
                "message": (
                    f"the artifact needs Appliance Manager "
                    f"{release.minimum_appliance_manager_version} or newer; this host runs "
                    f"{appliance_version}"
                ),
            }
        )
    if current_build_id and release.build_id == current_build_id and not repair:
        problems.append(
            {
                "code": "artifact_already_active",
                "message": (
                    f"build {release.build_id} is the build this appliance is running; "
                    "use an explicit repair to write it again"
                ),
            }
        )
    return problems


def downgrade_problem(release, *, current_version, rollback=False):
    """A normal update never goes backwards; only a recorded rollback may."""

    if rollback:
        return None
    if not current_version:
        return None
    if _version_key(release.release_version) >= _version_key(current_version):
        return None
    return {
        "code": "artifact_older_than_current",
        "message": (
            f"{release.release_version} is older than the running {current_version}; a downgrade "
            "is only available as a rollback to the recorded previous known-good slot"
        ),
    }
