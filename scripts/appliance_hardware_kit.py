#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assemble the one directory an operator carries to the Raspberry Pi.

    scripts/appliance_hardware_kit.py --dist DIR --output DIR
                                      [--gate-report FILE] [--profile rpi5]...
                                      [--attestation FILE] [--keyring FILE]
                                      [--trusted-fingerprint FPR]...
                                      [--runtime-gates FILE] [--source-authority FILE]
                                      [--source-bundle FILE] [--source-parity FILE]
                                      [--development-kit]

The kit used to be a filename glob: everything under ``dist`` matching
``*-rpi5-*`` was copied, whatever was hashed was recorded, and the result was
PASS. Two builds in one output directory produced a kit holding an image from
one and an update from the other, with a SHA256SUMS file that agreed with
itself. A missing signature, a missing release-gate report and a build
authority describing a different build were all invisible.

So the kit is assembled from authority instead. Each profile has exactly one
completed BuildAuthority, and that record names the build the kit is allowed to
carry: the image and the update are verified against the digests it recorded,
the manifest against the archive it describes, and every artefact has to belong
to that one build id. Anything else is a failure, not a smaller kit.

A kit copied the attestation's ``.asc`` beside it and never verified it, so the
signature was a file the kit carried rather than a thing the kit knew. The
signature is now checked against a keyring and a fingerprint the operator names
— never one the kit or the attestation carries — and a kit whose signature does
not verify by a trusted key is not physical-ready however complete it is.

``--development-kit`` assembles whatever exists for a bench, and says so:
RESULT: INCOMPLETE and physical_ready=false. It can never report READY.

No private key is copied, and nothing here signs, publishes or uploads.

Exit status: 0 the kit is authoritative and complete, 1 it is not, 2 the
command line is wrong, 3 there was nothing to assemble.
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from appliance import (  # noqa: E402
    build_authority,
    media_sizing,
    release_inputs,
    release_trust,
    runtime_gates,
)
from appliance.release_inputs import gate_passed, inspection_passed  # noqa: E402

BUILDER_LOCK_NAME = "base-images.lock.json"
KIT_VERSION = 2
AUTHORITY_SUFFIX = ".build-authority.json"

# The release documents a kit carries so it can be re-verified away from the
# machine that assembled it, under the names the verifier looks for.
RELEASE_DOCUMENTS = (
    ("attestation", "release-attestation.json"),
    ("attestation_signature", "release-attestation.json.asc"),
    ("gate_report", "release-gate-report.txt"),
    ("runtime_gates", "runtime-gates.json"),
    ("source_authority", "source-bundle-authority.json"),
    ("source_parity", "source-bundle-parity.json"),
    ("source_bundle", "source-bundle.tar.gz"),
)

PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN (OPENSSH|RSA|DSA|EC|ENCRYPTED|PGP) PRIVATE KEY"
)

# Never scanned as text: every OpenSSH binary carries the private-key banner in
# its string table, so grepping a root filesystem matches ssh-keygen itself.
# That an image ships no host key is proven by the image content inspection.
OPAQUE_SUFFIXES = (".img", ".zst", ".gz", ".xz")


def file_sha256(path, chunk=4 * 1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


class Problem(Exception):
    pass


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--gate-report", default="")
    parser.add_argument("--attestation", default="")
    parser.add_argument("--keyring", default="")
    parser.add_argument("--trusted-fingerprint", action="append", default=[])
    parser.add_argument("--runtime-gates", default="")
    parser.add_argument("--source-authority", default="")
    parser.add_argument("--source-bundle", default="")
    parser.add_argument("--source-parity", default="")
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--checklist", default="")
    parser.add_argument(
        "--builder-lock",
        default=str(ROOT / "packaging" / "appliance" / "vm" / "base-images.lock.json"),
        help="release policy for the machine an image may be assembled on",
    )
    parser.add_argument("--development-kit", action="store_true")
    return parser.parse_args(argv)


def authorities_by_profile(dist):
    """Exactly one completed build authority per profile, or say which."""

    found = {}
    for path in sorted(Path(dist).glob(f"*{AUTHORITY_SUFFIX}")):
        try:
            authority = build_authority.read(path)
        except build_authority.BuildAuthorityError as error:
            raise Problem(f"{path.name}: {error.code}: {error.message}")
        found.setdefault(authority.profile, []).append((path, authority))

    resolved = {}
    for profile, entries in found.items():
        completed = [(path, item) for path, item in entries if item.completed]
        if not completed:
            raise Problem(f"{profile}: no completed build authority in {dist}")
        build_ids = {item.build_id for _path, item in completed}
        if len(build_ids) > 1:
            raise Problem(
                f"{profile}: {len(build_ids)} different builds in one output directory "
                f"({', '.join(sorted(build_ids))}); a kit names one build"
            )
        if len(completed) > 1:
            raise Problem(f"{profile}: {len(completed)} build authorities for one build id")
        resolved[profile] = completed[0]
    return resolved


def required_artefacts(dist, authority_path, authority, reports):
    """Every file the kit must carry for this build, by name and by authority."""

    prefix = authority_path.name[: -len(AUTHORITY_SUFFIX)]
    dist = Path(dist)
    profile = authority.profile
    entries = {
        "build_authority": authority_path,
        "build_metadata": dist / f"{prefix}.build.json",
        "builder_environment": dist / f"{prefix}.builder-environment.json",
        "image": dist / f"{prefix}.img",
        "image_checksum": dist / f"{prefix}.img.sha256",
        "update": dist / f"{prefix}.update.tar.zst",
        "release_archive": dist / f"{prefix}.tar.zst",
        "manifest": dist / f"{prefix}.manifest.json",
        "signature": dist / f"{prefix}.manifest.json.asc",
        "image_inspection": reports / f"image-inspection-{profile}.json",
        "update_inspection": reports / f"update-inspection-{profile}.json",
        # The attestation binds it, so a kit that could not re-hash it could not
        # verify itself away from the machine that assembled it.
        "sparse_crosscheck": reports / f"sparse-crosscheck-{profile}.json",
    }
    return prefix, entries


def verify_build(authority, entries):
    """The artefacts in front of us against the build that produced them."""

    problems = []
    image = entries["image"]
    update = entries["update"]

    if image.is_file():
        problems.extend(
            build_authority.verify_image(
                authority,
                image,
                profile=authority.profile,
                build_id=authority.build_id,
                require_environment=True,
            )
        )
    if update.is_file():
        problems.extend(
            build_authority.verify_update(
                authority,
                update,
                profile=authority.profile,
                build_id=authority.build_id,
                require_environment=True,
            )
        )

    manifest_path = entries["manifest"]
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            problems.append(f"{manifest_path.name} is not valid JSON")
            manifest = {}
        if manifest:
            if str(manifest.get("build_id") or "") != authority.build_id:
                problems.append(
                    f"the manifest names build {manifest.get('build_id')!r}, the authority "
                    f"names {authority.build_id!r}"
                )
            provenance = manifest.get("provenance") or {}
            if not provenance.get("verified"):
                problems.append("the manifest is a development artefact, not a release")
            if provenance.get("build_authority_sha256") != authority.canonical_hash:
                problems.append(
                    "the manifest was not described from this build authority"
                )
            if (
                provenance.get("builder_environment_sha256")
                != authority.builder_environment_sha256
            ):
                problems.append("the manifest names a different builder environment")
            archive = manifest.get("archive") or {}
            published = entries["release_archive"]
            if published.is_file() and archive.get("digest"):
                observed = file_sha256(published)
                if observed != archive["digest"]:
                    problems.append(
                        f"{published.name} hashes to {observed}, the manifest declares "
                        f"{archive['digest']}"
                    )
    return problems


def attestation_problems(
    attestation, dist, reports, gate_report, wanted, authorities, runtime_gates=None
):
    """Does one signed attestation describe exactly these builds?

    A kit assembled from whatever a directory happened to contain could not tell
    a stale artefact, a report for another profile or two profiles from
    different builds apart, because filenames carry none of that. Every digest
    below is recomputed from the file about to be packed.
    """

    from appliance import release_attestation

    problems = []
    prefixes = {}
    for profile in wanted:
        entry = attestation.profile(profile)
        if entry is None:
            problems.append(f"{profile}: the attestation does not describe this profile")
            continue
        if profile not in authorities:
            continue
        authority_path, authority = authorities[profile]
        prefixes[profile] = authority_path.name[: -len(".build-authority.json")]
        if entry.build_id != authority.build_id:
            problems.append(
                f"{profile}: the attestation names build {entry.build_id!r}, the "
                f"artefacts are build {authority.build_id!r}"
            )
        if authority.project.revision != str(attestation.project.get("revision") or ""):
            problems.append(
                f"{profile}: the attestation names project "
                f"{str(attestation.project.get('revision') or '')[:12]}, this build is "
                f"{authority.project.revision[:12]}"
            )
        if authority.project.tree_sha256 != str(attestation.project.get("tree_sha256") or ""):
            problems.append(f"{profile}: the attestation names a different project tree")

    extra = sorted({entry.profile for entry in attestation.profiles} - set(wanted))
    if extra:
        problems.append(
            "the attestation also describes " + ", ".join(extra) + "; this kit does not"
        )

    problems.extend(
        release_attestation.verify(
            attestation,
            dist=dist,
            reports=reports,
            prefixes=prefixes,
            gate_report=gate_report,
            runtime_gates=runtime_gates,
        )
    )
    return problems


def runtime_gate_verdict(path):
    """Whether the runtime the kit is about was actually exercised.

    An image that inspects cleanly is not an appliance that logs in over SFTP,
    survives a purge or rebuilds its containers from a seed. A kit with no
    runtime evidence is missing that answer, which is not the same as having it.
    """

    if not path or not Path(path).is_file():
        return False, "no runtime gate evidence"
    try:
        gates = runtime_gates.read(path)
    except runtime_gates.RuntimeGateError as error:
        return False, f"{error.code}: {error.message}"
    if not gates.required_pass:
        return False, "required runtime gate(s) did not pass: " + ", ".join(gates.unmet)
    return True, ", ".join(f"{name}={result}" for name, result in gates.summary().items())


def scan_for_private_keys(directory):
    leaked = []
    for path in sorted(Path(directory).rglob("*")):
        if not path.is_file() or path.suffix in OPAQUE_SUFFIXES:
            continue
        try:
            with path.open("rb") as handle:
                if PRIVATE_KEY_PATTERN.search(handle.read(8 * 1024 * 1024)):
                    leaked.append(str(path))
        except OSError:
            continue
    return leaked


def assemble_profile(profile, authority_path, authority, args, reports, target):
    prefix, entries = required_artefacts(args.dist, authority_path, authority, reports)
    problems = []
    missing = sorted(name for name, path in entries.items() if not path.is_file())
    if missing:
        problems.append(f"missing: {', '.join(missing)}")

    problems.extend(verify_build(authority, entries))

    for name in ("image_inspection", "update_inspection", "sparse_crosscheck"):
        if entries[name].is_file():
            ok, detail = inspection_passed(entries[name])
            if not ok:
                problems.append(f"{name}: {detail}")

    target.mkdir(parents=True, exist_ok=True)
    copied = {}
    for name, path in entries.items():
        if not path.is_file():
            continue
        destination = target / path.name
        shutil.copy2(path, destination)
        observed = file_sha256(destination)
        if observed != file_sha256(path):
            problems.append(f"{path.name} changed while it was copied")
        copied[name] = {"file": path.name, "sha256": observed}

    record = {
        "profile": profile,
        "build_id": authority.build_id,
        "prefix": prefix,
        "project_revision": authority.project.revision,
        "project_tree_sha256": authority.project.tree_sha256,
        "upstream_revision": authority.builder.revision,
        "upstream_tree_sha256": authority.builder.source_tree_sha256,
        "builder_environment_sha256": authority.builder_environment_sha256,
        "package_sha256": authority.package_sha256,
        "build_authority_sha256": authority.canonical_hash,
        "artefacts": copied,
        "problems": problems,
    }
    return record


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    dist = Path(args.dist)
    if not dist.is_dir():
        print(f"appliance-hardware-kit: no build output at {dist}", file=sys.stderr)
        print("RESULT: NOT RUN (dist_unavailable)", file=sys.stderr)
        return 3

    reports = dist / "reports"
    try:
        authorities = authorities_by_profile(dist)
    except Problem as problem:
        print(f"appliance-hardware-kit: {problem}", file=sys.stderr)
        print("RESULT: FAIL (build_authority_ambiguous)", file=sys.stderr)
        return 1

    wanted = args.profile or sorted(authorities)
    if not authorities:
        print("appliance-hardware-kit: no build authority under the dist directory",
              file=sys.stderr)
        print("RESULT: NOT RUN (artefacts_unavailable)", file=sys.stderr)
        return 3

    output = Path(args.output)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    records = []
    for profile in wanted:
        if profile not in authorities:
            records.append(
                {"profile": profile, "problems": [f"no completed build for {profile}"],
                 "artefacts": {}}
            )
            continue
        authority_path, authority = authorities[profile]
        record = assemble_profile(
            profile, authority_path, authority, args, reports, output / profile
        )
        # Completeness -- what require_environment asks -- is whether the fields
        # are filled in, not whether release policy approves the machine they
        # describe. Without this a kit assembled outside the finalizer reached
        # physical_ready with the lock never consulted once.
        approval = list(
            release_inputs.verify_builder_environment(authority, lock=args.builder_lock)
        )
        record["builder_environment_problems"] = approval
        record.setdefault("problems", [])
        record["problems"] = list(record["problems"]) + approval
        records.append(record)

    gate_ok, gate_detail = (False, "no release gate report was passed to the kit")
    if args.gate_report:
        gate_ok, gate_detail = gate_passed(args.gate_report)

    sources = {
        "attestation": args.attestation,
        "attestation_signature": f"{args.attestation}.asc" if args.attestation else "",
        "gate_report": args.gate_report,
        "runtime_gates": args.runtime_gates,
        "source_authority": args.source_authority,
        "source_parity": args.source_parity,
        "source_bundle": args.source_bundle,
    }
    carried = {}
    for key, name in RELEASE_DOCUMENTS:
        origin = sources.get(key) or ""
        if origin and Path(origin).is_file():
            shutil.copy2(origin, output / name)
            carried[key] = output / name

    # The authority a physical-ready kit rests on. A development kit may be
    # assembled without one; it is the reason such a kit is never physical_ready.
    policy = release_trust.TrustPolicy.of(args.keyring, args.trusted_fingerprint)
    attestation_ok, attestation_detail = (False, "no release attestation was passed to the kit")
    attestation_problems_found = []
    signature = release_trust.SignatureVerdict(detail="no release attestation")
    binding = release_trust.SourceBinding(problems=("no release attestation",))
    if args.attestation:
        from appliance import release_attestation

        try:
            attestation = release_attestation.read(args.attestation)
        except release_attestation.AttestationError as error:
            attestation_detail = f"{error.code}: {error.message}"
        else:
            attestation_problems_found = attestation_problems(
                attestation,
                dist,
                reports,
                args.gate_report,
                wanted,
                authorities,
                runtime_gates=args.runtime_gates or None,
            )
            attestation_ok = not attestation_problems_found
            attestation_detail = (
                attestation.canonical_hash
                if attestation_ok
                else "; ".join(attestation_problems_found[:4])
            )
            # Against the copies about to be carried, not against the originals:
            # a kit is verified again where it is used, from what it holds.
            signature = release_trust.verify_signature(
                carried.get("attestation", args.attestation),
                policy,
                signature=carried.get("attestation_signature"),
            )
            binding = release_trust.verify_source_binding(
                attestation,
                authority=carried.get("source_authority"),
                bundle=carried.get("source_bundle"),
                parity=carried.get("source_parity"),
            )

    gates_ok, gates_detail = runtime_gate_verdict(carried.get("runtime_gates"))

    checklist = Path(args.checklist) if args.checklist else None
    if checklist and checklist.is_file():
        shutil.copy2(checklist, output / "validation-checklist.md")

    # The kit travels to a machine that has no repository, and the verifier is
    # built to distrust the manifest's own summary of what it checked. So the
    # policy the builders were judged against travels with it and is re-applied
    # there, rather than being taken on the kit's word.
    builder_lock = Path(args.builder_lock)
    if builder_lock.is_file():
        shutil.copy2(builder_lock, output / BUILDER_LOCK_NAME)

    leaked = scan_for_private_keys(output)
    if leaked:
        print("appliance-hardware-kit: private key material in the kit:", file=sys.stderr)
        for path in leaked:
            print(f"  {path}", file=sys.stderr)
        shutil.rmtree(output)
        print("RESULT: FAIL (private_key_in_kit)", file=sys.stderr)
        return 1

    problems = [problem for record in records for problem in record.get("problems", ())]
    if not gate_ok:
        problems.append(f"release gate: {gate_detail}")
    if not attestation_ok:
        problems.append(f"release attestation: {attestation_detail}")
    if not signature.ok:
        problems.append(f"attestation signature: {signature.code or 'unverified'}: {signature.detail}")
    if not binding.ok:
        problems.append(f"source bundle: {'; '.join(binding.problems[:3])}")
    if not gates_ok:
        problems.append(f"runtime gates: {gates_detail}")

    verdict = release_trust.readiness(
        {
            "production_gate_pass": gate_ok,
            "attestation_result_pass": attestation_ok,
            "attestation_signature_present": signature.present,
            "attestation_signature_verified": signature.verified,
            "trusted_signer": signature.trusted,
            "attestation_artefacts_rehashed": attestation_ok,
            "source_bundle_verified": binding.ok,
            "all_profiles_verified": not any(record.get("problems") for record in records),
            "all_mandatory_inspections_pass": not any(
                record.get("problems") for record in records
            ),
            "runtime_required_gates_pass": gates_ok,
            "release_not_stale": attestation_ok,
            "builder_environment_approved": bool(records)
            and all(not record.get("builder_environment_problems", ["unchecked"]) for record in records),
        }
    )
    ready = verdict.ready and not problems and not args.development_kit
    manifest = {
        "kit_version": KIT_VERSION,
        "development_kit": bool(args.development_kit),
        "physical_ready": ready,
        "readiness": verdict.to_dict(),
        "release_gate": {"ok": gate_ok, "detail": gate_detail},
        # An operator flashing a card needs the number before they buy one,
        # and a kit that carried the image but not its media requirement
        # leaves that to be remembered.
        "media": media_sizing.requirements(),
        "release_attestation": {
            "ok": attestation_ok,
            "detail": attestation_detail,
            "problems": attestation_problems_found,
            "signature": signature.to_dict(),
        },
        "trust_policy": policy.to_dict(),
        "source_binding": binding.to_dict(),
        "runtime_gates": {"ok": gates_ok, "detail": gates_detail},
        "documents": sorted(name for _key, name in RELEASE_DOCUMENTS if (output / name).is_file()),
        "builds": records,
    }
    (output / "kit-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    checksums = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "KIT-SHA256SUMS":
            checksums.append(
                f"{file_sha256(path)[7:]}  {path.relative_to(output).as_posix()}"
            )
    (output / "KIT-SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")

    for record in records:
        detail = "; ".join(record.get("problems", ())) or "complete"
        print(f"{record['profile']:<6} {len(record.get('artefacts', {})):>2} artefact(s)  {detail}")
    if not gate_ok:
        print(f"gate   release gate: {gate_detail}")
    if not attestation_ok:
        print(f"attest release attestation: {attestation_detail}")
    print()
    print(f"kit: {output}")

    if args.development_kit:
        print("RESULT: INCOMPLETE (development kit, physical_ready=false)")
        return 0
    if problems:
        print(f"RESULT: FAIL ({len(problems)} problem(s))")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
