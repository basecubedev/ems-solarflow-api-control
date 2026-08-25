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

from appliance.sparse import ENCODING_ANDROID_SPARSE, ENCODING_RAW
from appliance.version import version_key

# Format 2 added the sparse authority: a member carries its encoded and its
# expanded identity separately. A format 1 manifest cannot be upgraded here —
# the expanded digest it never carried cannot be inferred.
MANIFEST_FORMAT_VERSION = 2
MANIFEST_DIAGNOSTIC_FORMATS = (1,)
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
    "device_layer",
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

# rpi-image-gen's own member names. One boot payload and one system payload,
# because image-rota builds one bit-for-bit identical slot pair. Anything else
# is an artifact this appliance does not know how to write.
MEMBER_BOOT = "boot"
MEMBER_SYSTEM = "system"
REQUIRED_MEMBERS = (MEMBER_BOOT, MEMBER_SYSTEM)

# A manifest must never carry key material. These are refused outright rather
# than ignored, so a manifest that tried cannot be quietly accepted.
FORBIDDEN_MANIFEST_KEYS = ("private_key", "signing_key", "secret", "passphrase", "token")

SUPPORTED_ENCODINGS = (ENCODING_ANDROID_SPARSE, ENCODING_RAW)


class ReleaseError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ArtifactMember:
    """One update payload, in both the identities it has.

    ``encoded`` is what the archive carries and what extraction verifies.
    ``expanded`` is what the partition receives and what the read-back proves.
    They are never the same value for a sparse member, and a single ambiguous
    digest is what let a container be written as if it were a filesystem.
    """

    name: str
    encoded_digest: str
    expanded_digest: str
    expanded_size: int
    role: str
    encoding: str = ""
    filesystem: str = ""
    slot: str = ""

    @property
    def digest(self):
        return self.encoded_digest

    @property
    def sparse(self):
        return self.encoding == ENCODING_ANDROID_SPARSE

    def to_dict(self):
        return {
            "name": self.name,
            "role": self.role,
            "encoding": self.encoding,
            "encoded_sha256": self.encoded_digest,
            "expanded_sha256": self.expanded_digest,
            "expanded_size": self.expanded_size,
            "filesystem": self.filesystem,
            "slot": self.slot,
        }


@dataclass(frozen=True)
class OsRelease:
    """One signed, compatible operating-system artifact."""

    release_id: str
    release_version: str
    build_id: str
    created_at: str
    architecture: str
    device_layer: str
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
    # What the manager inside this image writes, and the oldest it can read.
    # Optional: a manifest built before this existed declares neither, and is
    # judged on the two legacy numbers alone -- which is enough to refuse it as
    # a step back, and not enough to prove it safe as one.
    state_implements: dict = field(default_factory=dict)
    state_reads: dict = field(default_factory=dict)

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
            "device_layer": self.device_layer,
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


def _member(name, entry):
    """One member of a format 2 manifest, with both identities present.

    Nothing is defaulted. An absent expanded digest is not an unencoded
    member — it is a manifest that never described what a partition should
    end up holding, and inferring one would defeat the whole chain.
    """

    if not isinstance(entry, dict):
        raise ReleaseError("release_manifest_invalid", f"member {name} is not an object")
    encoding = str(entry.get("encoding") or "")
    if encoding not in SUPPORTED_ENCODINGS:
        raise ReleaseError(
            "release_manifest_invalid",
            f"member {name} declares encoding {encoding!r}; this appliance writes "
            + ", ".join(SUPPORTED_ENCODINGS),
        )
    try:
        expanded_size = int(entry["expanded_size"])
    except (KeyError, TypeError, ValueError):
        raise ReleaseError(
            "release_manifest_invalid", f"member {name} declares no expanded size"
        )
    if expanded_size <= 0:
        raise ReleaseError(
            "release_manifest_invalid", f"the expanded size of {name} must be positive"
        )
    encoded = _digest(entry.get("encoded_sha256"), label=f"the encoded digest of {name}")
    expanded = _digest(entry.get("expanded_sha256"), label=f"the expanded digest of {name}")
    if encoding == ENCODING_RAW and encoded != expanded:
        raise ReleaseError(
            "release_manifest_invalid",
            f"member {name} is unencoded, so its two digests must be the same value",
        )
    return ArtifactMember(
        name=name,
        encoded_digest=encoded,
        expanded_digest=expanded,
        expanded_size=expanded_size,
        role=str(entry.get("role") or ""),
        encoding=encoding,
        filesystem=str(entry.get("filesystem") or ""),
        slot=str(entry.get("slot") or ""),
    )


def _state_schemas(block):
    """What the image declares it writes, and the oldest it can read.

    Absent is allowed: manifests predate this block. Present but malformed is
    not — the manifest is signed, so a broken block is a broken build, and
    guessing at it would be guessing at what a downgrade is allowed to cross.
    """

    if block is None:
        return {}, {}
    if not isinstance(block, dict):
        raise ReleaseError("release_manifest_invalid", "state_schemas is not an object")

    parsed = {}
    for key in ("implements", "reads"):
        values = block.get(key)
        if not isinstance(values, dict) or not values:
            raise ReleaseError(
                "release_manifest_invalid", f"state_schemas.{key} names no schemas"
            )
        clean = {}
        for name, value in values.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ReleaseError(
                    "release_manifest_invalid",
                    f"state_schemas.{key}.{name} is not a schema number",
                )
            clean[str(name)] = int(value)
        parsed[key] = clean

    undeclared = sorted(set(parsed["reads"]) - set(parsed["implements"]))
    if undeclared:
        raise ReleaseError(
            "release_manifest_invalid",
            f"state_schemas.reads names {', '.join(undeclared)}, which it does not implement",
        )
    ahead = sorted(
        name for name, value in parsed["reads"].items() if value > parsed["implements"][name]
    )
    if ahead:
        raise ReleaseError(
            "release_manifest_invalid",
            f"state_schemas.reads is ahead of implements for {', '.join(ahead)}",
        )
    return parsed["implements"], parsed["reads"]


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
    if payload["format_version"] in MANIFEST_DIAGNOSTIC_FORMATS:
        raise ReleaseError(
            "release_manifest_unsupported",
            f"manifest format {payload['format_version']} predates the sparse authority; "
            "its expanded digests were never recorded and cannot be inferred",
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
    members = {name: _member(name, entry) for name, entry in raw_members.items()}

    implements, reads = _state_schemas(payload.get("state_schemas"))

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
        device_layer=str(payload["device_layer"]),
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
        state_implements=implements,
        state_reads=reads,
    )


# --- signature authority -----------------------------------------------------


class SignatureVerifier:
    """Detached-signature verification against a root-owned keyring.

    The keyring path comes from the host configuration only. ``gpgv`` is invoked
    through the allowlisted command runner with a fixed argv; there is no shell
    and no caller-supplied option.
    """

    def __init__(self, runner, *, keyring, fingerprints=()):
        self.runner = runner
        self.keyring = str(keyring or "")
        # A trust policy, not a key list: a keyring can hold more keys than a
        # release is allowed to be signed with, and "gpg said good" does not
        # say which key it was good for.
        self.fingerprints = tuple(
            str(item).replace(" ", "").upper() for item in fingerprints if str(item).strip()
        )

    @property
    def available(self):
        return bool(self.keyring) and self.runner is not None and self.runner.available("gpgv")

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
        if self.runner is None or not self.runner.available("gpgv"):
            raise ReleaseError(
                "release_verification_unavailable", "gpgv is not installed on this appliance"
            )
        result = self.runner.run(
            "gpgv",
            [
                "--status-fd",
                "1",
                "--keyring",
                self.keyring,
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
        unusable = unusable_signature(result.stdout)
        if unusable is not None:
            code, reason = unusable
            raise ReleaseError(
                code, f"the release manifest signature is not usable: {reason}"
            )
        observed = valid_signature_fingerprints(result.stdout)
        if not observed:
            raise ReleaseError(
                "release_signature_invalid",
                "gpg reported no valid signature over the release manifest",
            )
        if self.fingerprints and not set(observed) & set(self.fingerprints):
            raise ReleaseError(
                "release_signature_untrusted",
                f"the release manifest was signed by {observed[0]}, which is not a trusted "
                "release key",
            )
        return True

    def fingerprints_of(self, manifest_path, signature_path):
        """The keys a usable signature was made with, or ().

        A revoked or expired key still produces VALIDSIG, so the same refusal
        `verify` applies is applied here: a caller that only asks this question
        must not be handed a fingerprint it would then treat as trusted.
        """

        if not self.available or not Path(self.keyring).is_file():
            return ()
        result = self.runner.run(
            "gpgv",
            [
                "--status-fd",
                "1",
                "--keyring",
                self.keyring,
                str(signature_path),
                str(manifest_path),
            ],
            timeout=60,
        )
        if not result.ok:
            return ()
        if unusable_signature(result.stdout) is not None:
            return ()
        return valid_signature_fingerprints(result.stdout)


# gpgv reports these *alongside* VALIDSIG and still exits 0, so a verifier that
# reads only the exit status and VALIDSIG accepts a signature made with a key
# that was revoked or has expired.
UNUSABLE_SIGNATURE_MARKERS = {
    "EXPKEYSIG": ("release_signature_key_expired", "the signing key has expired"),
    "REVKEYSIG": ("release_signature_key_revoked", "the signing key was revoked"),
    "EXPSIG": ("release_signature_expired", "the signature has expired"),
    "ERRSIG": ("release_signature_invalid", "the signature could not be checked"),
}


def unusable_signature(status_output):
    """The first reason gpg gave for not trusting an otherwise valid signature."""

    for line in (status_output or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "[GNUPG:]" and parts[1] in UNUSABLE_SIGNATURE_MARKERS:
            return UNUSABLE_SIGNATURE_MARKERS[parts[1]]
    return None


def valid_signature_fingerprints(status_output):
    """The primary-key fingerprints gpg reported as VALIDSIG."""

    fingerprints = []
    for line in (status_output or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "[GNUPG:]" and parts[1] == "VALIDSIG":
            # Field 12 of VALIDSIG is the primary key fingerprint; field 3 is
            # the fingerprint of the signing (sub)key.
            primary = parts[11] if len(parts) >= 12 else parts[2]
            fingerprints.append(primary.upper())
    return tuple(fingerprints)


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
        # By version, not by the text of it: a string sort puts 0.9.0 above
        # 0.10.0 and a release candidate level with its own final release.
        releases.sort(
            key=lambda item: (version_key(item.release_version), item.build_id), reverse=True
        )
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




def compatibility_problems(
    release,
    *,
    layout,
    board,
    appliance_version,
    state_schemas=None,
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
    if not board:
        problems.append(
            {
                "code": "hardware_not_supported",
                "message": (
                    "this board could not be identified from its device tree, so no OS "
                    "artifact can be proven to be built for it"
                ),
            }
        )
    elif not release.compatible_hardware:
        # An empty list used to fall through the whole branch, so a manifest
        # that named no board at all passed the board check that exists to keep
        # a Pi 4 artifact off a Pi 5.
        problems.append(
            {
                "code": "artifact_hardware_unspecified",
                "message": (
                    "the artifact manifest names no compatible hardware, so it cannot be "
                    "proven to be built for this board"
                ),
            }
        )
    else:
        if not any(str(entry) == board for entry in release.compatible_hardware):
            problems.append(
                {
                    "code": "artifact_hardware_incompatible",
                    "message": (
                        f"the artifact is built for {', '.join(release.compatible_hardware)}; "
                        f"this appliance is a {board}"
                    ),
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
    problems.extend(state_schema_problems(release, recorded=state_schemas))
    if version_key(appliance_version) < version_key(release.minimum_appliance_manager_version):
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


LEGACY_STATE_AXES = {
    "persistent_paths": "persistent_schema_version",
    "slot_layout": "slot_schema_version",
}


def state_schema_problems(release, *, recorded):
    """Whether the code in this artifact can read the state already on the disk.

    ``recorded`` is what the persistent partition says it was last written by —
    the one schema record in this system that does not come out of an image, and
    therefore the only one that can answer the question at all. Every other
    number is stamped into a slot and compared against a constant compiled into
    that same slot.

    Undecidable is refused. An appliance that cannot say what its state is
    formatted as cannot be told that some artifact is safe for it.
    """

    if recorded is None:
        return [
            {
                "code": "state_schemas_unrecorded",
                "message": (
                    "this appliance has no record of what its persistent state is formatted "
                    "as, so no artifact can be proven able to read it; run "
                    "'ems-appliance ab verify-persistence' and try again"
                ),
            }
        ]

    recorded = dict(recorded)
    if not release.state_implements:
        # A manifest from before the declaration existed. The two legacy numbers
        # are enough to refuse a step back, and not enough to prove one safe:
        # they say nothing about the record formats stored on the shared paths.
        problems = []
        for axis, attribute in LEGACY_STATE_AXES.items():
            declared = getattr(release, attribute)
            if recorded.get(axis, 0) > declared:
                problems.append(
                    {
                        "code": "artifact_state_schema_too_old",
                        "message": (
                            f"this appliance's state is written at {axis} schema "
                            f"{recorded[axis]}; the artifact's manager implements {declared} "
                            "and could not read it"
                        ),
                    }
                )
        return problems

    problems = []
    for axis in sorted(recorded):
        if axis not in release.state_implements:
            problems.append(
                {
                    "code": "artifact_state_schema_undeclared",
                    "message": (
                        f"this appliance holds {axis} state; the artifact does not say "
                        "whether its manager can read that format"
                    ),
                }
            )
            continue
        implements = release.state_implements[axis]
        if recorded[axis] > implements:
            problems.append(
                {
                    "code": "artifact_state_schema_too_old",
                    "message": (
                        f"this appliance's state is written at {axis} schema {recorded[axis]}; "
                        f"the artifact's manager implements {implements} and could not read it"
                    ),
                }
            )
            continue
        floor = release.state_reads.get(axis, 1)
        if recorded[axis] < floor:
            problems.append(
                {
                    "code": "artifact_state_schema_unreadable",
                    "message": (
                        f"the artifact's manager reads {axis} schema {floor} or newer; this "
                        f"appliance's state is written at {recorded[axis]}"
                    ),
                }
            )
    # An axis the artifact declares and this partition has no state for is not a
    # problem: there is nothing of that format here to be incompatible with.
    return problems


def downgrade_problem(release, *, current_version, rollback=False):
    """A normal update never goes backwards; only a recorded rollback may."""

    if rollback:
        return None
    if not current_version:
        return None
    if version_key(release.release_version) >= version_key(current_version):
        return None
    return {
        "code": "artifact_older_than_current",
        "message": (
            f"{release.release_version} is older than the running {current_version}; a downgrade "
            "is only available as a rollback to the recorded previous known-good slot"
        ),
    }
