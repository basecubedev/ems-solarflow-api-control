# SPDX-License-Identifier: AGPL-3.0-or-later
"""One machine-readable record of what a release actually proved.

The hardware process used to trust report files. A reader looked for
``RESULT: PASS`` in text, which is a claim anyone can write, in a file nobody
bound to the artefacts it was about. A stale report from a previous build, a
report for another profile, and a report with the right words and the wrong
image were all indistinguishable from the real thing.

An attestation is the alternative: the exact digests of the artefacts, of the
inspection reports that examined them, of the source bundle they were built
from and of the package that went into them, in one document. Verification is
re-hashing, not reading. What a kit trusts is a hash it can recompute.

Nothing here signs anything. The finalizer signs the attestation the same way it
signs a manifest, with a key this module never sees — and nothing here names a
key, a keyring or a fingerprint either. A document that carried its own trust
anchor would be self-certifying, so an attestation that names one is refused
rather than ignored: a later reader cannot pick up a field that cannot parse.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2

UNREADABLE = "release_attestation_unreadable"
UNSUPPORTED = "release_attestation_unsupported"
MISMATCH = "release_attestation_mismatch"
SELF_TRUSTED = "release_attestation_self_trusted"

# Trust comes from release policy, never from the document being verified.
TRUST_FIELDS = (
    "keyring",
    "public_key",
    "signing_key",
    "trust",
    "trusted_fingerprint",
    "trusted_fingerprints",
    "trusted_keys",
)

PASS = "pass"
FAIL = "fail"

# Every artefact a profile's entry names, and whether a release may be cut
# without it. A signature is absent from an unsigned rehearsal; nothing else is.
PROFILE_ARTEFACTS = (
    ("image", True),
    ("update", True),
    ("build_authority", True),
    ("manifest", True),
    ("signature", False),
)

PROFILE_REPORTS = (
    ("image_inspection", True),
    ("update_inspection", True),
    ("sparse_crosscheck", True),
    ("release_gate", True),
)


class AttestationError(Exception):
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


def canonical_hash(payload):
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ProfileAttestation:
    """What one profile's release is, as digests rather than as prose."""

    profile: str = ""
    build_id: str = ""
    artefacts: dict = field(default_factory=dict)
    reports: dict = field(default_factory=dict)
    result: str = FAIL

    def to_dict(self):
        return {
            "profile": self.profile,
            "build_id": self.build_id,
            "artefacts": dict(sorted(self.artefacts.items())),
            "reports": dict(sorted(self.reports.items())),
            "result": self.result,
        }


@dataclass(frozen=True)
class ReleaseAttestation:
    project: dict = field(default_factory=dict)
    source_bundle: dict = field(default_factory=dict)
    package: dict = field(default_factory=dict)
    builder: dict = field(default_factory=dict)
    profiles: tuple = ()
    runtime_gates: dict = field(default_factory=dict)
    release_gate: dict = field(default_factory=dict)
    minimum_media_bytes: int = 0
    created_at: str = ""
    result: str = FAIL
    schema_version: int = SCHEMA_VERSION

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "project": dict(sorted(self.project.items())),
            "source_bundle": dict(sorted(self.source_bundle.items())),
            "package": dict(sorted(self.package.items())),
            "builder": dict(sorted(self.builder.items())),
            "profiles": [profile.to_dict() for profile in self.profiles],
            "runtime_gates": dict(sorted(self.runtime_gates.items())),
            "release_gate": dict(sorted(self.release_gate.items())),
            "minimum_media_bytes": int(self.minimum_media_bytes),
            "result": self.result,
        }

    @property
    def canonical_hash(self):
        return canonical_hash(self.to_dict())

    def profile(self, name):
        for entry in self.profiles:
            if entry.profile == name:
                return entry
        return None


def _named_paths(dist, prefix):
    return {
        "image": dist / f"{prefix}.img",
        "update": dist / f"{prefix}.update.tar.zst",
        "build_authority": dist / f"{prefix}.build-authority.json",
        "manifest": dist / f"{prefix}.manifest.json",
        "signature": dist / f"{prefix}.manifest.json.asc",
    }


def _report_paths(reports, profile, gate_report):
    return {
        "image_inspection": Path(reports) / f"image-inspection-{profile}.json",
        "update_inspection": Path(reports) / f"update-inspection-{profile}.json",
        "sparse_crosscheck": Path(reports) / f"sparse-crosscheck-{profile}.json",
        "release_gate": Path(gate_report),
    }


def describe_profile(profile, *, dist, prefix, reports, build_id, gate_report):
    """Hash everything this profile's release consists of."""

    artefacts, missing = {}, []
    for name, required in PROFILE_ARTEFACTS:
        path = _named_paths(Path(dist), prefix)[name]
        if path.is_file():
            artefacts[name] = file_sha256(path)
        elif required:
            missing.append(name)

    recorded = {}
    for name, required in PROFILE_REPORTS:
        path = _report_paths(reports, profile, gate_report)[name]
        if path.is_file():
            recorded[name] = file_sha256(path)
        elif required:
            missing.append(name)

    return ProfileAttestation(
        profile=profile,
        build_id=build_id,
        artefacts=artefacts,
        reports=recorded,
        result=FAIL if missing else PASS,
    )


def build(
    *,
    project,
    source,
    package,
    builder,
    profiles,
    runtime_gates=None,
    release_gate=None,
    minimum_media_bytes=0,
    created_at=None,
):
    """One attestation from the inputs a finalization already verified."""

    stamp = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = tuple(profiles)
    return ReleaseAttestation(
        project=dict(project),
        source_bundle=dict(source),
        package=dict(package),
        builder=dict(builder),
        profiles=entries,
        runtime_gates=dict(runtime_gates or {}),
        release_gate=dict(release_gate or {}),
        minimum_media_bytes=int(minimum_media_bytes or 0),
        created_at=stamp,
        result=PASS if entries and all(entry.result == PASS for entry in entries) else FAIL,
    )


def parse(payload):
    if not isinstance(payload, dict):
        raise AttestationError(UNREADABLE, "the release attestation is not an object")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise AttestationError(
            UNSUPPORTED, f"release attestation schema {version!r} is not schema {SCHEMA_VERSION}"
        )
    supplied = sorted(field for field in TRUST_FIELDS if field in payload)
    if supplied:
        raise AttestationError(
            SELF_TRUSTED,
            f"the release attestation supplies its own trust anchor ({', '.join(supplied)}); "
            "trust comes from release policy, not from the document being verified",
        )
    raw = payload.get("profiles")
    if not isinstance(raw, list) or not raw:
        raise AttestationError(UNREADABLE, "the release attestation names no profile")
    profiles = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise AttestationError(UNREADABLE, "a profile entry is not an object")
        artefacts = entry.get("artefacts")
        reports = entry.get("reports")
        if not isinstance(artefacts, dict) or not isinstance(reports, dict):
            raise AttestationError(
                UNREADABLE, f"the {entry.get('profile')!r} entry names no artefacts and reports"
            )
        profiles.append(
            ProfileAttestation(
                profile=str(entry.get("profile") or ""),
                build_id=str(entry.get("build_id") or ""),
                artefacts={str(k): str(v) for k, v in artefacts.items()},
                reports={str(k): str(v) for k, v in reports.items()},
                result=str(entry.get("result") or FAIL),
            )
        )
    return ReleaseAttestation(
        project=dict(payload.get("project") or {}),
        source_bundle=dict(payload.get("source_bundle") or {}),
        package=dict(payload.get("package") or {}),
        builder=dict(payload.get("builder") or {}),
        profiles=tuple(profiles),
        runtime_gates=dict(payload.get("runtime_gates") or {}),
        release_gate=dict(payload.get("release_gate") or {}),
        minimum_media_bytes=int(payload.get("minimum_media_bytes") or 0),
        created_at=str(payload.get("created_at") or ""),
        result=str(payload.get("result") or FAIL),
    )


def read(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise AttestationError(UNREADABLE, f"{path} could not be read: {exc}")
    except ValueError:
        raise AttestationError(UNREADABLE, f"{path} is not valid JSON")
    return parse(payload)


def write(path, attestation):
    target = Path(path)
    target.write_text(
        json.dumps(attestation.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return target


def _location(value, profile):
    """One directory, or one per profile.

    A hardware kit files each build's artefacts under its own profile
    directory, so the same attestation has to be verifiable against a flat
    build output and against a kit without either side rewriting it.
    """

    if isinstance(value, dict):
        return Path(value[profile]) if profile in value else None
    return Path(value)


def verify(attestation, *, dist, reports, prefixes, gate_report):
    """Re-hash every file the attestation names. Nothing here reads a verdict.

    ``prefixes`` maps a profile to the artefact basename its release uses, so a
    verifier never discovers what to check from a directory listing: a stale
    artefact from another build is exactly what a glob would find.
    """

    problems = []
    if attestation.result != PASS:
        problems.append(f"{MISMATCH}: the attestation itself records {attestation.result!r}")

    for entry in attestation.profiles:
        prefix = prefixes.get(entry.profile)
        if not prefix:
            problems.append(f"{MISMATCH}: nothing names the artefacts for {entry.profile!r}")
            continue
        artefact_root = _location(dist, entry.profile)
        report_root = _location(reports, entry.profile)
        if artefact_root is None or report_root is None:
            problems.append(f"{MISMATCH}: nothing holds the artefacts for {entry.profile!r}")
            continue
        if entry.result != PASS:
            problems.append(f"{entry.profile}: the attestation records {entry.result!r}")

        for name, required in PROFILE_ARTEFACTS:
            declared = entry.artefacts.get(name)
            path = _named_paths(artefact_root, prefix)[name]
            if declared is None:
                if required:
                    problems.append(f"{entry.profile}: the attestation names no {name}")
                continue
            problems.extend(_compare(entry.profile, name, path, declared))

        for name, required in PROFILE_REPORTS:
            declared = entry.reports.get(name)
            path = _report_paths(report_root, entry.profile, gate_report)[name]
            if declared is None:
                if required:
                    problems.append(f"{entry.profile}: the attestation names no {name} report")
                continue
            problems.extend(_compare(entry.profile, name, path, declared))

    return tuple(problems)


def _compare(profile, name, path, declared):
    if not path.is_file():
        return [f"{MISMATCH}: {profile} {name} is missing at {path}"]
    observed = file_sha256(path)
    if observed != declared:
        return [
            f"{MISMATCH}: {profile} {name} hashes to {observed}, the attestation "
            f"records {declared}"
        ]
    return []
