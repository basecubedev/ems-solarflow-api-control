# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether a release may be trusted, and on whose authority.

Readiness was derived from ``Path(f"{attestation}.asc").is_file()``. That is a
statement about a filename. It is true of a signature made with any key in the
world, over any bytes, including bytes that are no longer the ones being read —
so an unsigned rehearsal, a release signed by a developer's throwaway key and a
production release were indistinguishable, and every one of them could reach
``physical_ready=true``.

Trust here is a chain with one anchor, and the anchor is outside the document
being checked:

    trusted keyring + trusted fingerprint   (release policy, from the operator)
        -> the attestation signature verifies, by a key the policy names
        -> the attestation's own digests re-hash against the files on disk
        -> the source bundle really is the tracked tree at the certified commit
        -> the checkout being certified is still the checkout that was built

A document may never supply its own trust anchor, so nothing below reads a key,
a keyring or a fingerprint out of the attestation, the kit or the reports.

Read-only. ``gpg`` runs through the allowlisted command runner with a fixed
argv; no path here comes from a request.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from appliance import commands, artifact_trust

UNSIGNED = "release_attestation_unsigned"
UNVERIFIED = "release_attestation_signature_invalid"
UNTRUSTED = "release_attestation_signer_untrusted"
NO_POLICY = "release_trust_policy_missing"
SOURCE_MISMATCH = "release_source_binding_mismatch"
STALE = "release_stale"


def file_sha256(path, *, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _normalise_fingerprint(value):
    return str(value or "").replace(" ", "").upper()


@dataclass(frozen=True)
class TrustPolicy:
    """The keyring and the keys a release is allowed to be signed with.

    Both come from the operator running the verification — a command line, or
    the release configuration it reads. A keyring alone is not a policy: it can
    hold more keys than a release may be signed with, and "gpg said good" does
    not say which key it was good for.
    """

    keyring: str = ""
    fingerprints: tuple = ()

    @classmethod
    def of(cls, keyring, fingerprints=()):
        return cls(
            keyring=str(keyring or ""),
            fingerprints=tuple(
                _normalise_fingerprint(item) for item in fingerprints if str(item).strip()
            ),
        )

    @property
    def configured(self):
        return bool(self.keyring) and bool(self.fingerprints)

    def unmet(self):
        if not self.keyring:
            return "no trusted keyring was named"
        if not Path(self.keyring).is_file():
            return f"the trusted keyring {self.keyring} does not exist"
        if not self.fingerprints:
            return "no trusted signer fingerprint was named"
        return ""

    def to_dict(self):
        return {"keyring_sha256": self._keyring_digest(), "fingerprints": list(self.fingerprints)}

    def _keyring_digest(self):
        try:
            return file_sha256(self.keyring)
        except OSError:
            return ""


@dataclass(frozen=True)
class SignatureVerdict:
    """What a detached signature proved, as four separate answers.

    "Present" and "verified" and "made by a trusted key" fail in different ways
    and a caller that collapsed them could not say which one it was missing.
    """

    present: bool = False
    verified: bool = False
    trusted: bool = False
    fingerprints: tuple = ()
    signature_sha256: str = ""
    document_sha256: str = ""
    code: str = ""
    detail: str = ""

    @property
    def ok(self):
        return self.present and self.verified and self.trusted

    def to_dict(self):
        return {
            "present": self.present,
            "verified": self.verified,
            "trusted": self.trusted,
            "fingerprints": list(self.fingerprints),
            "signature_sha256": self.signature_sha256,
            "document_sha256": self.document_sha256,
            "code": self.code,
            "detail": self.detail,
        }


def verify_signature(document, policy, *, signature=None, runner=None):
    """Verify a detached signature over ``document`` against ``policy``.

    The same verifier a running appliance uses on an OS release manifest, so a
    release and the appliance that installs it agree on what a valid signature
    is. Nothing is re-implemented here.
    """

    document = Path(document)
    signature = Path(signature) if signature else Path(f"{document}.asc")
    document_digest = file_sha256(document) if document.is_file() else ""

    if not signature.is_file():
        return SignatureVerdict(
            document_sha256=document_digest,
            code=UNSIGNED,
            detail=f"{signature.name} is missing; an unsigned release is not a release",
        )
    signature_digest = file_sha256(signature)

    unmet = policy.unmet()
    if unmet:
        return SignatureVerdict(
            present=True,
            signature_sha256=signature_digest,
            document_sha256=document_digest,
            code=NO_POLICY,
            detail=unmet,
        )

    verifier = artifact_trust.SignatureVerifier(
        runner or commands.CommandRunner(),
        keyring=policy.keyring,
        fingerprints=policy.fingerprints,
    )
    if not verifier.available:
        return SignatureVerdict(
            present=True,
            signature_sha256=signature_digest,
            document_sha256=document_digest,
            code=UNVERIFIED,
            detail="gpg is not installed, so the signature could not be verified",
        )

    observed = verifier.fingerprints_of(document, signature)
    if not observed:
        return SignatureVerdict(
            present=True,
            signature_sha256=signature_digest,
            document_sha256=document_digest,
            code=UNVERIFIED,
            detail="gpg reported no valid signature over the attestation",
        )
    try:
        verifier.verify(document, signature)
    except artifact_trust.ReleaseError as error:
        return SignatureVerdict(
            present=True,
            verified=True,
            fingerprints=observed,
            signature_sha256=signature_digest,
            document_sha256=document_digest,
            code=UNTRUSTED if error.code == "release_signature_untrusted" else UNVERIFIED,
            detail=error.message,
        )
    return SignatureVerdict(
        present=True,
        verified=True,
        trusted=True,
        fingerprints=observed,
        signature_sha256=signature_digest,
        document_sha256=document_digest,
        detail=f"signed by {observed[0]}",
    )


@dataclass(frozen=True)
class SourceBinding:
    """Whether the attestation, the authority, the bundle and the tree are one."""

    bundle_sha256: str = ""
    authority_sha256: str = ""
    parity_sha256: str = ""
    revision: str = ""
    tree_sha256: str = ""
    tracked_objects: int = 0
    symlinks: int = 0
    problems: tuple = ()

    @property
    def ok(self):
        return not self.problems

    def to_dict(self):
        return {
            "ok": self.ok,
            "bundle_sha256": self.bundle_sha256,
            "authority_sha256": self.authority_sha256,
            "parity_sha256": self.parity_sha256,
            "revision": self.revision,
            "tree_sha256": self.tree_sha256,
            "tracked_objects": self.tracked_objects,
            "symlinks": self.symlinks,
            "problems": list(self.problems),
        }


def _parity_problems(payload):
    """A parity report is only evidence if it compared something and found nothing."""

    problems = []
    if not payload.get("ok"):
        problems.append(f"{SOURCE_MISMATCH}: the source bundle is not the tracked tree")
    if not int(payload.get("compared") or 0):
        problems.append(f"{SOURCE_MISMATCH}: the parity report compared no tracked object")
    for name in ("missing", "mismatched", "unexpected", "unsafe", "duplicate"):
        count = len(payload.get(name) or ())
        if count:
            problems.append(f"{SOURCE_MISMATCH}: the bundle has {count} {name} object(s)")
    return problems


def verify_source_binding(attestation, *, authority=None, bundle=None, parity=None):
    """Re-hash the source chain the attestation names, end to end.

    The attestation records what the source bundle was. Reading that record
    back proves nothing about the archive: the digests below are recomputed
    from the authority document, the bundle and the parity report on disk, and
    the parity report is the only thing that ever compared the archive's
    contents against ``git ls-tree``.
    """

    declared = dict(attestation.source_bundle or {})
    problems = []

    for name, path, key in (
        ("source bundle", bundle, "bundle_sha256"),
        ("source authority", authority, "authority_sha256"),
        ("source parity report", parity, "parity_sha256"),
    ):
        expected = str(declared.get(key) or "")
        if not expected:
            problems.append(f"{SOURCE_MISMATCH}: the attestation records no {key}")
            continue
        if not path or not Path(path).is_file():
            problems.append(f"{SOURCE_MISMATCH}: the {name} is missing")
            continue
        observed = file_sha256(path)
        if observed != expected:
            problems.append(
                f"{SOURCE_MISMATCH}: the {name} hashes to {observed}, the attestation "
                f"records {expected}"
            )

    parity_payload = {}
    if parity and Path(parity).is_file():
        try:
            parity_payload = json.loads(Path(parity).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append(f"{SOURCE_MISMATCH}: the source parity report could not be read")
        else:
            problems.extend(_parity_problems(parity_payload))

    authority_payload = {}
    if authority and Path(authority).is_file():
        try:
            authority_payload = json.loads(Path(authority).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append(f"{SOURCE_MISMATCH}: the source authority could not be read")

    project = dict(attestation.project or {})
    recorded = authority_payload.get("project") or {}
    for key in ("revision", "tree_sha256"):
        if recorded and str(recorded.get(key) or "") != str(project.get(key) or ""):
            problems.append(
                f"{SOURCE_MISMATCH}: the source authority names {key} "
                f"{str(recorded.get(key) or 'nothing')[:12]}, the attestation names "
                f"{str(project.get(key) or 'nothing')[:12]}"
            )

    return SourceBinding(
        bundle_sha256=str(declared.get("bundle_sha256") or ""),
        authority_sha256=str(declared.get("authority_sha256") or ""),
        parity_sha256=str(declared.get("parity_sha256") or ""),
        revision=str(project.get("revision") or ""),
        tree_sha256=str(project.get("tree_sha256") or ""),
        tracked_objects=int(parity_payload.get("compared") or declared.get("tracked_objects") or 0),
        symlinks=int(parity_payload.get("symlinks") or declared.get("symlinks") or 0),
        problems=tuple(problems),
    )


@dataclass(frozen=True)
class Freshness:
    """Is the checkout that would ship still the checkout that was certified?"""

    certified_revision: str = ""
    certified_tree: str = ""
    current_revision: str = ""
    current_tree: str = ""
    stale: bool = True
    detail: str = ""

    def to_dict(self):
        return {
            "certified_revision": self.certified_revision,
            "certified_tree": self.certified_tree,
            "current_revision": self.current_revision,
            "current_tree": self.current_tree,
            "stale": self.stale,
            "detail": self.detail,
        }


def freshness(attestation, *, root):
    """Compare the certified revision and tree against the checkout right now.

    Through ``assert_clean``, so an edited tracked file counts. The tree hash is
    taken from git objects and an uncommitted edit leaves it untouched, which is
    exactly the shape of "a release nobody rebuilt": the source that would ship
    is no longer the source that was built.
    """

    from appliance import project_source

    project = dict(attestation.project or {})
    certified_revision = str(project.get("revision") or "")
    certified_tree = str(project.get("tree_sha256") or "")

    try:
        current = project_source.assert_clean(root)
    except project_source.ProjectSourceError as error:
        return Freshness(
            certified_revision=certified_revision,
            certified_tree=certified_tree,
            detail=f"{STALE}: {error.code}: {error.message}",
        )
    current_revision, current_tree = current.revision, current.tree_sha256

    if not certified_revision or not certified_tree:
        return Freshness(
            certified_revision=certified_revision,
            certified_tree=certified_tree,
            current_revision=current_revision,
            current_tree=current_tree,
            detail=f"{STALE}: the attestation certifies no revision and tree",
        )
    if certified_revision != current_revision:
        return Freshness(
            certified_revision=certified_revision,
            certified_tree=certified_tree,
            current_revision=current_revision,
            current_tree=current_tree,
            detail=(
                f"{STALE}: the release certifies {certified_revision[:12]}, the checkout "
                f"is {current_revision[:12]}"
            ),
        )
    if certified_tree != current_tree:
        return Freshness(
            certified_revision=certified_revision,
            certified_tree=certified_tree,
            current_revision=current_revision,
            current_tree=current_tree,
            detail=f"{STALE}: a tracked file changed after the release was built",
        )
    return Freshness(
        certified_revision=certified_revision,
        certified_tree=certified_tree,
        current_revision=current_revision,
        current_tree=current_tree,
        stale=False,
        detail="the checkout is the certified revision and tree",
    )


# Every one of these has been false while a release still reported ready. The
# list is the readiness rule: a new invariant is added here, not to a caller.
READINESS_INVARIANTS = (
    "production_gate_pass",
    "attestation_result_pass",
    "attestation_signature_present",
    "attestation_signature_verified",
    "trusted_signer",
    "attestation_artefacts_rehashed",
    "source_bundle_verified",
    "all_profiles_verified",
    "all_mandatory_inspections_pass",
    "runtime_required_gates_pass",
    "release_not_stale",
    # Builder approval was enforced in exactly one place -- the finalizer's
    # pre-signature refusal -- so a result assembled by any other route reached
    # physical_ready without the lock ever being consulted. The hardware kit
    # checks the environment for completeness, which asks whether the fields are
    # filled in, not whether release policy approves the machine they describe.
    "builder_environment_approved",
)

KIT_READINESS_INVARIANTS = READINESS_INVARIANTS + ("hardware_kit_verified",)


@dataclass(frozen=True)
class Readiness:
    """physical_ready, and the named invariant that is not true if it is false."""

    invariants: dict = field(default_factory=dict)
    required: tuple = READINESS_INVARIANTS

    @property
    def unmet(self):
        return tuple(name for name in self.required if not self.invariants.get(name))

    @property
    def ready(self):
        return not self.unmet

    def to_dict(self):
        return {
            "physical_ready": self.ready,
            "invariants": {name: bool(self.invariants.get(name)) for name in self.required},
            "unmet": list(self.unmet),
        }


def readiness(invariants, *, required=READINESS_INVARIANTS):
    return Readiness(invariants=dict(invariants), required=tuple(required))
