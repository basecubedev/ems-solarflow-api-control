# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a production finalization has to prove about the things handed to it.

Three artefacts arrive at the signing environment: a build authority, a
canonical source bundle and the package the image was built from. Each of them
already validates on its own. That is not the same as their describing one
release, and every gap below was reachable with three individually valid
artefacts:

- a build from commit A signed alongside a source bundle from commit B, because
  nothing ever compared the two;
- ``package_sha256`` recorded by the builder and never checked against a package
  again, so the digest was a note rather than a claim;
- a structurally perfect build authority from a builder image nobody approved,
  because the authority was only ever checked against itself.

So this module answers one question per artefact and one about the set: is this
the same project, the same package and an approved builder? Read-only, and no
path here comes from a request.
"""

import fnmatch
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from appliance import project_source

SCHEMA_VERSION = 1

UNREADABLE = "release_input_unreadable"
UNSUPPORTED = "release_input_unsupported"
SOURCE_BUILD_MISMATCH = "source_build_authority_mismatch"
PACKAGE_MISMATCH = "package_authority_mismatch"
BUILDER_UNTRUSTED = "builder_environment_untrusted"

# The builder reports the kernel's name for the machine; the base-image lock
# names the Debian architecture the image was published for. They are the same
# fact under two vocabularies.
ARCHITECTURE_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


class ReleaseInputError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def file_sha256(path, *, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _normalise_digest(value):
    """``sha256:...`` and a bare hex digest are the same claim."""

    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text if text.startswith("sha256:") else f"sha256:{text}"


# --- the canonical source bundle --------------------------------------------


@dataclass(frozen=True)
class SourceBundleAuthority:
    """A bundle, and the project revision and tree it was archived from.

    The tree hash is ``project_source.tree_sha256`` — the same canonical
    representation the build authority records — so the two are comparable
    without either side converting anything.
    """

    bundle_sha256: str = ""
    revision: str = ""
    tree_sha256: str = ""
    tracked_objects: int = 0
    symlinks: int = 0
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "bundle_sha256": self.bundle_sha256,
            "project": {"revision": self.revision, "tree_sha256": self.tree_sha256},
            "tracked_objects": self.tracked_objects,
            "symlinks": self.symlinks,
            "created_at": self.created_at,
        }


# The two verdicts a release kit is judged by. They live here so the kit
# builder, the kit verifier and the release result all read a report the
# same way, instead of one of them believing a manifest's summary of it.
def inspection_passed(path):
    """The verdict, and the mandatory checks it was derived from.

    A report that classifies its findings is read through that classification:
    a named optional oracle that did not run is not the same defect as a
    mandatory check nobody executed. A report from before the classification
    existed is held to the stricter rule — no skipped check at all.
    """

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "the inspection report could not be read"
    result = str(payload.get("result") or "")
    counts = payload.get("counts") or {}
    if result != "pass":
        return False, f"the inspection reports {result or 'nothing'}"
    if counts.get("fail"):
        return False, f"{counts['fail']} failed check(s)"
    skipped = payload.get("mandatory_not_run")
    if skipped is None:
        if counts.get("not_run"):
            return False, f"{counts['not_run']} check(s) never ran"
        return True, "pass"
    if skipped:
        return False, f"mandatory check(s) never ran: {', '.join(skipped)}"
    return True, "pass"


def gate_passed(path):
    """The gate report's own verdict.

    Only meaningful once the attestation has proven *which* file this is: the
    words RESULT: PASS are a claim anyone can write, and a stale report from a
    previous build carries them just as convincingly.
    """

    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "the release gate report could not be read"
    if "RESULT: PASS" not in text:
        last = [line for line in text.splitlines() if line.startswith("RESULT:")]
        return False, last[-1] if last else "the report carries no verdict"
    return True, "RESULT: PASS"


def describe_source_bundle(bundle, *, root, ref="HEAD", created_at=None):
    """Record what this bundle is, from the object tree it was archived from.

    Deliberately not conditional on a clean working directory. A bundle comes
    out of ``git archive``, which reads objects, so its contents are exactly the
    revision whatever the working tree happens to hold — and a description that
    refused to state that would leave a review archive with no provenance at
    all. Requiring the *build* to come from a clean tree is a separate rule,
    enforced where a build happens.
    """

    from appliance import source_bundle

    revision = project_source.revision(root, ref)
    entries = source_bundle.tracked_entries(root, ref=revision)
    stamp = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SourceBundleAuthority(
        bundle_sha256=file_sha256(bundle),
        revision=revision,
        tree_sha256=project_source.tree_sha256(root, revision),
        tracked_objects=len(entries),
        symlinks=sum(1 for entry in entries if entry.kind == source_bundle.SYMLINK),
        created_at=stamp,
    )


def parse_source_bundle_authority(payload):
    if not isinstance(payload, dict):
        raise ReleaseInputError(UNREADABLE, "the source bundle authority is not an object")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ReleaseInputError(
            UNSUPPORTED,
            f"source bundle authority schema {version!r} is not schema {SCHEMA_VERSION}",
        )
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ReleaseInputError(UNREADABLE, "the source bundle authority names no project")
    return SourceBundleAuthority(
        bundle_sha256=_normalise_digest(payload.get("bundle_sha256")),
        revision=str(project.get("revision") or ""),
        tree_sha256=str(project.get("tree_sha256") or ""),
        tracked_objects=int(payload.get("tracked_objects") or 0),
        symlinks=int(payload.get("symlinks") or 0),
        created_at=str(payload.get("created_at") or ""),
    )


def read_source_bundle_authority(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseInputError(UNREADABLE, f"{path} could not be read: {exc}")
    except ValueError:
        raise ReleaseInputError(UNREADABLE, f"{path} is not valid JSON")
    return parse_source_bundle_authority(payload)


def write_source_bundle_authority(path, authority):
    target = Path(path)
    target.write_text(
        json.dumps(authority.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def verify_source_bundle(authority, bundle):
    """Is this the archive the authority describes?"""

    problems = []
    if len(authority.revision) != 40:
        problems.append(f"{UNREADABLE}: the source authority records no full project revision")
    if not authority.tree_sha256:
        problems.append(f"{UNREADABLE}: the source authority records no project tree hash")
    if not authority.bundle_sha256:
        problems.append(f"{UNREADABLE}: the source authority records no bundle hash")
        return tuple(problems)
    try:
        observed = file_sha256(bundle)
    except OSError as exc:
        problems.append(f"{UNREADABLE}: {bundle} could not be read: {exc}")
        return tuple(problems)
    if observed != authority.bundle_sha256:
        problems.append(
            f"{SOURCE_BUILD_MISMATCH}: {bundle} hashes to {observed}, the source "
            f"authority records {authority.bundle_sha256}"
        )
    return tuple(problems)


def verify_source_matches_build(build, source):
    """One project, or two artefacts that merely both validate.

    Both halves, because a revision alone names a commit and says nothing about
    the files an archive carries, and a tree hash alone does not say which
    commit produced them.
    """

    problems = []
    if build.project.revision != source.revision:
        problems.append(
            f"{SOURCE_BUILD_MISMATCH}: the build was made from project "
            f"{build.project.revision[:12] or 'nothing'}, the source bundle is "
            f"{source.revision[:12] or 'nothing'}"
        )
    if build.project.tree_sha256 != source.tree_sha256:
        problems.append(
            f"{SOURCE_BUILD_MISMATCH}: the build read project tree "
            f"{build.project.tree_sha256 or 'nothing'}, the source bundle carries "
            f"{source.tree_sha256 or 'nothing'}"
        )
    return tuple(problems)


# --- the package the image was built from ------------------------------------

AR_MAGIC = b"!<arch>\n"
AR_HEADER = 60
CONTROL_MEMBERS = ("control.tar.gz", "control.tar.xz", "control.tar.bz2", "control.tar")


@dataclass(frozen=True)
class PackageRecord:
    """A Debian package, as its own control stanza describes it."""

    name: str = ""
    version: str = ""
    architecture: str = ""
    sha256: str = ""
    fields: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "name": self.name,
            "version": self.version,
            "architecture": self.architecture,
            "sha256": self.sha256,
        }


def _ar_members(payload):
    """Every member of an ``ar`` archive, as ``(name, bytes)``."""

    if payload[: len(AR_MAGIC)] != AR_MAGIC:
        raise ReleaseInputError(UNREADABLE, "the package is not an ar archive")
    offset = len(AR_MAGIC)
    while offset + AR_HEADER <= len(payload):
        header = payload[offset : offset + AR_HEADER]
        if header[58:60] != b"\x60\n":
            raise ReleaseInputError(UNREADABLE, "the package has a damaged member header")
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii", "replace").strip())
        except ValueError:
            raise ReleaseInputError(UNREADABLE, f"the member {name!r} declares no usable size")
        start = offset + AR_HEADER
        if start + size > len(payload):
            raise ReleaseInputError(UNREADABLE, f"the member {name!r} runs past the archive")
        yield name, payload[start : start + size]
        offset = start + size + (size % 2)


def read_package(path):
    """The control stanza and digest of a ``.deb``, without needing dpkg.

    A finalizer holds a signing key and builds nothing; requiring it to have
    Debian packaging tools installed to check a digest would be a reason to skip
    the check on the machines where it matters most.
    """

    target = Path(path)
    try:
        payload = target.read_bytes()
    except OSError as exc:
        raise ReleaseInputError(UNREADABLE, f"{target} could not be read: {exc}")

    control = None
    for name, member in _ar_members(payload):
        if name in CONTROL_MEMBERS:
            control = (name, member)
            break
    if control is None:
        raise ReleaseInputError(
            UNSUPPORTED,
            f"{target} carries no control member this reader understands "
            f"({', '.join(CONTROL_MEMBERS)})",
        )

    name, member = control
    try:
        with tarfile.open(fileobj=io.BytesIO(member), mode="r:*") as handle:
            entry = None
            for candidate in ("./control", "control"):
                try:
                    entry = handle.extractfile(candidate)
                except KeyError:
                    continue
                if entry is not None:
                    break
            if entry is None:
                raise ReleaseInputError(UNREADABLE, f"{name} carries no control file")
            stanza = entry.read().decode("utf-8", "replace")
    except tarfile.TarError as exc:
        raise ReleaseInputError(UNREADABLE, f"{name} could not be read: {exc}")

    fields = {}
    for line in stanza.splitlines():
        if line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()

    return PackageRecord(
        name=fields.get("Package", ""),
        version=fields.get("Version", ""),
        architecture=fields.get("Architecture", ""),
        sha256=file_sha256(target),
        fields=fields,
    )


def verify_package(build, path, *, name="", version="", architecture=""):
    """Is this the package that build installed, and is it the right one?

    The digest answers the first question and only the first: a package whose
    bytes match a recorded hash can still be the wrong name, the wrong version
    or the wrong architecture if the recorded hash itself was never the one this
    release is about.
    """

    problems = []
    declared = _normalise_digest(build.package_sha256)
    if not declared:
        problems.append(f"{UNREADABLE}: the build authority records no package hash")
    try:
        record = read_package(path)
    except ReleaseInputError as error:
        return (*problems, f"{error.code}: {error.message}")

    if declared and record.sha256 != declared:
        problems.append(
            f"{PACKAGE_MISMATCH}: {path} hashes to {record.sha256}, the build "
            f"authority records {declared}"
        )
    if name and record.name != name:
        problems.append(
            f"{PACKAGE_MISMATCH}: the package is {record.name!r}, this release is {name!r}"
        )
    if version and record.version != version:
        problems.append(
            f"{PACKAGE_MISMATCH}: the package is version {record.version!r}, this "
            f"release is {version!r}"
        )
    if architecture and record.architecture != architecture:
        problems.append(
            f"{PACKAGE_MISMATCH}: the package is for {record.architecture!r}, this "
            f"release is {architecture!r}"
        )
    return tuple(problems)


# --- the builder that assembled the image ------------------------------------


def approved_builders(lock):
    """The base images release policy allows a builder to have booted from."""

    try:
        payload = json.loads(Path(lock).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseInputError(UNREADABLE, f"{lock} could not be read: {exc}")
    except ValueError:
        raise ReleaseInputError(UNREADABLE, f"{lock} is not valid JSON")
    images = payload.get("images")
    if not isinstance(images, dict):
        raise ReleaseInputError(UNREADABLE, f"{lock} declares no images")
    return images


def verify_builder_environment(build, *, lock, role="builder"):
    """Was this image assembled on a builder release policy approved?

    A build authority that is internally consistent proves only that whoever
    produced it filled every field in. The base image a builder booted from is
    the supply chain: it decides which mmdebstrap, which podman and which
    aarch64 interpreter wrote the root filesystem.
    """

    environment = build.environment
    problems = []
    try:
        images = approved_builders(lock)
    except ReleaseInputError as error:
        return (f"{error.code}: {error.message}",)

    entry = images.get(role)
    if not isinstance(entry, dict):
        return (f"{BUILDER_UNTRUSTED}: release policy approves no {role!r} builder image",)

    expected_id = f"{role}:{entry.get('filename') or ''}"
    if environment.base_image_lock_id != expected_id:
        problems.append(
            f"{BUILDER_UNTRUSTED}: the build names builder image "
            f"{environment.base_image_lock_id or 'nothing'}, release policy approves "
            f"{expected_id}"
        )
    expected_digest = str(entry.get("sha512") or "").strip().lower()
    if not expected_digest:
        problems.append(f"{BUILDER_UNTRUSTED}: release policy records no digest for {role!r}")
    elif environment.base_image_sha512.strip().lower() != expected_digest:
        problems.append(
            f"{BUILDER_UNTRUSTED}: the builder image digest is not the approved one"
        )

    distribution = str(entry.get("distribution") or "").lower()
    release_version = str(entry.get("release_version") or "")
    observed = environment.os_release.lower()
    if distribution and distribution not in observed:
        problems.append(
            f"{BUILDER_UNTRUSTED}: the builder ran {environment.os_release or 'nothing'}, "
            f"release policy approves {distribution} {release_version}"
        )
    elif release_version and release_version not in observed:
        problems.append(
            f"{BUILDER_UNTRUSTED}: the builder ran {environment.os_release!r}, release "
            f"policy approves {distribution} {release_version}"
        )

    # The one field a builder somewhere else cannot reproduce by accident. Every
    # other recorded fact is either copied from this lock or true of any Debian
    # host; the running kernel belongs to the image that booted, and the
    # genericcloud flavour belongs to the pinned artefact and to nothing else.
    # A row with no pattern is an unreadable row rather than an exemption --
    # otherwise deleting one line from the lock deletes the check.
    kernel_pattern = str(entry.get("kernel_pattern") or "")
    if not kernel_pattern:
        problems.append(
            f"{BUILDER_UNTRUSTED}: release policy states no expected kernel for {role!r}, "
            "so the builder cannot be told from any other machine"
        )
    elif not fnmatch.fnmatch(environment.kernel, kernel_pattern):
        problems.append(
            f"{BUILDER_UNTRUSTED}: the builder ran {environment.kernel or 'no reported kernel'}, "
            f"release policy approves {kernel_pattern}"
        )

    # Required but never compared, with a truthy default: the capture script
    # writes the literal "none" when it finds no handler, which satisfied the
    # completeness check while meaning the opposite of what it records.
    if environment.binfmt_handler.strip().lower() == "none":
        problems.append(
            f"{BUILDER_UNTRUSTED}: the builder registered no aarch64 binfmt handler, so "
            "nothing it produced for the target architecture was executed as recorded"
        )

    declared_architecture = str(entry.get("architecture") or "").lower()
    expected_architecture = ARCHITECTURE_ALIASES.get(declared_architecture)
    observed_architecture = ARCHITECTURE_ALIASES.get(environment.architecture.lower())
    if declared_architecture and not expected_architecture:
        # An architecture nobody enumerated used to make the comparison vanish,
        # so a policy row naming one approved a builder of any architecture at
        # all. A row this code cannot read is an unreadable row, not a waiver.
        problems.append(
            f"{BUILDER_UNTRUSTED}: release policy approves architecture "
            f"{entry.get('architecture')!r}, which this appliance cannot interpret"
        )
    elif expected_architecture and observed_architecture != expected_architecture:
        problems.append(
            f"{BUILDER_UNTRUSTED}: the builder is {environment.architecture or 'unknown'}, "
            f"release policy approves {entry.get('architecture')}"
        )
    return tuple(problems)
