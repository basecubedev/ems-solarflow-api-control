#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify a hardware validation kit from the directory, not from the run that made it.

    scripts/appliance_verify_hardware_kit.py --kit DIR --keyring FILE
                                             [--trusted-fingerprint FPR]...
                                             [--project-root DIR] [--json FILE]

The assembling process knew which files it had just written and which digests it
had just computed, so its verdict was partly a memory of its own work. A kit is
carried to a bench on a stick, and what matters there is what the directory
holds — after a copy, a re-open and possibly a different machine.

So nothing here is taken from the assembling run. The manifest and the checksum
file are read back off disk, every file is re-hashed, every file present has to
be one the checksum file names, the attestation's signature is verified against
a keyring and a fingerprint the operator supplies, and every artefact the
attestation binds is re-hashed out of the kit's own profile directories.

Read-only: this opens files and runs gpg, and writes nothing into the kit.

Exit status: 0 the kit verifies, 1 it does not, 2 the command line is wrong, 3
there is no kit to verify.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appliance import (  # noqa: E402
    build_authority,
    release_attestation,
    release_trust,
    runtime_gates,
)

VERIFICATION_VERSION = 1
MANIFEST = "kit-manifest.json"
CHECKSUMS = "KIT-SHA256SUMS"
ATTESTATION = "release-attestation.json"
SIGNATURE = "release-attestation.json.asc"
GATE_REPORT = "release-gate-report.txt"
RUNTIME_GATES = "runtime-gates.json"
SOURCE_AUTHORITY = "source-bundle-authority.json"
SOURCE_PARITY = "source-bundle-parity.json"
SOURCE_BUNDLE = "source-bundle.tar.gz"

PRIVATE_KEY_PATTERN = re.compile(rb"-----BEGIN (OPENSSH|RSA|DSA|EC|ENCRYPTED|PGP) PRIVATE KEY")
OPAQUE_SUFFIXES = (".img", ".zst", ".gz", ".xz")


def file_sha256(path, chunk=4 * 1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kit", required=True)
    parser.add_argument("--keyring", default="")
    parser.add_argument("--trusted-fingerprint", action="append", default=[])
    parser.add_argument("--project-root", default="")
    parser.add_argument("--json", default="")
    return parser.parse_args(argv)


def checksum_problems(kit):
    """Every file re-hashed, and every file present accounted for."""

    problems = []
    listing = kit / CHECKSUMS
    if not listing.is_file():
        return [f"{CHECKSUMS} is missing"], 0
    declared = {}
    for line in listing.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        if not name:
            problems.append(f"{CHECKSUMS} carries an unreadable line")
            continue
        # sha256sum(1) writes "./path" from a find; the assembler writes "path".
        declared[name[2:] if name.startswith("./") else name] = f"sha256:{digest.strip()}"

    present = {
        path.relative_to(kit).as_posix()
        for path in kit.rglob("*")
        if path.is_file() and path.name != CHECKSUMS
    }
    for name in sorted(set(declared) - present):
        problems.append(f"{name} is named by {CHECKSUMS} and is not in the kit")
    for name in sorted(present - set(declared)):
        problems.append(f"{name} is in the kit and is named by nothing")
    for name in sorted(set(declared) & present):
        observed = file_sha256(kit / name)
        if observed != declared[name]:
            problems.append(f"{name} hashes to {observed}, {CHECKSUMS} records {declared[name]}")
    return problems, len(declared)


def private_key_problems(kit):
    leaked = []
    for path in sorted(kit.rglob("*")):
        if not path.is_file() or path.suffix in OPAQUE_SUFFIXES:
            continue
        try:
            with path.open("rb") as handle:
                if PRIVATE_KEY_PATTERN.search(handle.read(8 * 1024 * 1024)):
                    leaked.append(path.relative_to(kit).as_posix())
        except OSError:
            continue
    return [f"{name} carries private key material" for name in leaked]


def build_locations(kit, manifest):
    """Where in the kit each profile's artefacts and reports actually are."""

    dist, reports, prefixes, problems = {}, {}, {}, []
    for record in manifest.get("builds") or []:
        profile = str(record.get("profile") or "")
        prefix = str(record.get("prefix") or "")
        directory = kit / profile
        if not profile or not prefix or not directory.is_dir():
            problems.append(f"{profile or 'a build'}: the kit holds no artefact directory")
            continue
        dist[profile] = directory
        reports[profile] = directory
        prefixes[profile] = prefix
    return dist, reports, prefixes, problems


def stale_problems(kit, manifest, attestation):
    """The kit's own build authority against the attestation it carries."""

    problems = []
    for record in manifest.get("builds") or []:
        profile = str(record.get("profile") or "")
        prefix = str(record.get("prefix") or "")
        path = kit / profile / f"{prefix}.build-authority.json"
        if not path.is_file():
            problems.append(f"{profile}: the kit carries no build authority")
            continue
        try:
            authority = build_authority.read(path)
        except build_authority.BuildAuthorityError as error:
            problems.append(f"{profile}: {error.code}: {error.message}")
            continue
        entry = attestation.profile(profile)
        if entry is None:
            problems.append(f"{profile}: the attestation does not describe this build")
            continue
        if entry.build_id != authority.build_id:
            problems.append(
                f"{profile}: the kit carries build {authority.build_id}, the attestation "
                f"names {entry.build_id}"
            )
        if authority.project.revision != str(attestation.project.get("revision") or ""):
            problems.append(f"{profile}: the kit was built from another project revision")
        if authority.project.tree_sha256 != str(attestation.project.get("tree_sha256") or ""):
            problems.append(f"{profile}: the kit was built from another project tree")
    return problems


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    kit = Path(args.kit)
    if not kit.is_dir():
        print(f"appliance-verify-hardware-kit: no kit at {kit}", file=sys.stderr)
        print("RESULT: NOT RUN (kit_unavailable)", file=sys.stderr)
        return 3

    manifest_path = kit / MANIFEST
    if not manifest_path.is_file():
        print(f"appliance-verify-hardware-kit: {kit} carries no {MANIFEST}", file=sys.stderr)
        print("RESULT: NOT RUN (kit_manifest_missing)", file=sys.stderr)
        return 3
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        print(f"appliance-verify-hardware-kit: {MANIFEST} is not valid JSON", file=sys.stderr)
        print("RESULT: FAIL (kit_manifest_unreadable)", file=sys.stderr)
        return 1

    checksums, counted = checksum_problems(kit)
    leaked = private_key_problems(kit)

    policy = release_trust.TrustPolicy.of(args.keyring, args.trusted_fingerprint)
    signature = release_trust.verify_signature(
        kit / ATTESTATION, policy, signature=kit / SIGNATURE
    )

    attestation, attestation_problems, binding, stale = None, [], None, []
    if (kit / ATTESTATION).is_file():
        try:
            attestation = release_attestation.read(kit / ATTESTATION)
        except release_attestation.AttestationError as error:
            attestation_problems.append(f"{error.code}: {error.message}")
    else:
        attestation_problems.append(f"{ATTESTATION} is missing")

    if attestation is not None:
        dist, reports, prefixes, location_problems = build_locations(kit, manifest)
        attestation_problems.extend(location_problems)
        attestation_problems.extend(
            release_attestation.verify(
                attestation,
                dist=dist,
                reports=reports,
                prefixes=prefixes,
                gate_report=kit / GATE_REPORT,
            )
        )
        binding = release_trust.verify_source_binding(
            attestation,
            authority=kit / SOURCE_AUTHORITY,
            bundle=kit / SOURCE_BUNDLE,
            parity=kit / SOURCE_PARITY,
        )
        stale = stale_problems(kit, manifest, attestation)

    gates_ok, gates_detail = False, f"{RUNTIME_GATES} is missing"
    if (kit / RUNTIME_GATES).is_file():
        try:
            gates = runtime_gates.read(kit / RUNTIME_GATES)
        except runtime_gates.RuntimeGateError as error:
            gates_detail = f"{error.code}: {error.message}"
        else:
            gates_ok = gates.required_pass
            gates_detail = ", ".join(f"{k}={v}" for k, v in gates.summary().items())

    freshness = None
    if attestation is not None and args.project_root:
        freshness = release_trust.freshness(attestation, root=args.project_root)

    verdict = release_trust.readiness(
        {
            "production_gate_pass": bool(manifest.get("release_gate", {}).get("ok")),
            "attestation_result_pass": attestation is not None
            and attestation.result == release_attestation.PASS,
            "attestation_signature_present": signature.present,
            "attestation_signature_verified": signature.verified,
            "trusted_signer": signature.trusted,
            "attestation_artefacts_rehashed": attestation is not None
            and not attestation_problems,
            "source_bundle_verified": binding is not None and binding.ok,
            "all_profiles_verified": not stale,
            "all_mandatory_inspections_pass": bool(manifest.get("physical_ready")),
            "runtime_required_gates_pass": gates_ok,
            "release_not_stale": not stale and (freshness is None or not freshness.stale),
            "hardware_kit_verified": not checksums and not leaked,
        },
        required=release_trust.KIT_READINESS_INVARIANTS,
    )

    report = {
        "verification_version": VERIFICATION_VERSION,
        "kit": str(kit),
        "kit_manifest_sha256": file_sha256(manifest_path),
        "files_verified": counted,
        "checksum_problems": checksums,
        "private_key_problems": leaked,
        "attestation": {
            "sha256": file_sha256(kit / ATTESTATION) if (kit / ATTESTATION).is_file() else "",
            "problems": attestation_problems,
            "signature": signature.to_dict(),
        },
        "source_binding": binding.to_dict() if binding else {"ok": False},
        "stale_problems": stale,
        "runtime_gates": {"ok": gates_ok, "detail": gates_detail},
        "freshness": freshness.to_dict() if freshness else {},
        "readiness": verdict.to_dict(),
        "physical_ready": verdict.ready,
    }
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    print(f"kit:            {kit}")
    print(f"files verified: {counted}")
    print(f"signature:      {signature.detail}")
    print(f"runtime gates:  {gates_detail}")
    for problem in checksums + leaked + attestation_problems + stale:
        print(f"  problem: {problem}")
    if binding and not binding.ok:
        for problem in binding.problems:
            print(f"  problem: {problem}")
    print(f"physical_ready: {str(verdict.ready).lower()}")
    if verdict.ready:
        print("RESULT: PASS")
        return 0
    print(f"RESULT: FAIL ({', '.join(verdict.unmet)})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
