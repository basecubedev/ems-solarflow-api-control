#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Derive one authoritative release result from the reports themselves.

    scripts/appliance_release_result.py --dist DIR --output FILE
                                        [--attestation FILE] [--gate-report FILE]
                                        [--source-authority FILE] [--package FILE]
                                        [--source-bundle FILE] [--source-parity FILE]
                                        [--runtime-gates FILE] [--kit-manifest FILE]
                                        [--keyring FILE] [--trusted-fingerprint FPR]...
                                        [--project-root DIR]
                                        [--profile rpi5]... [--run-id ID]
                                        [--markdown FILE]

The committed evidence summary was written by hand, and drifted from what the
raw reports said: a summary claiming "79 pass, 0 not run" beside an image
report that recorded 79 pass, 2 fail and 1 not run, and a bundle object count
from an older revision. Every count here is read out of the report that
produced it, so the summary cannot disagree with its own evidence.

Readiness is the second thing this had wrong. ``signed`` was
``Path(f"{attestation}.asc").is_file()`` — a statement about a filename — and
readiness never looked at it at all, so an unsigned rehearsal could report
``physical_ready=true``. It is now the invariant list in ``release_trust``: the
attestation's signature has to verify against a keyring the operator named, by
a fingerprint the operator named, and every artefact, report and source
document it binds has to re-hash to what it recorded.

Nothing is inferred and nothing is defaulted: a report that is absent is
absent in the result, and a result whose mandatory evidence is missing is
incomplete rather than passing.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appliance import build_authority, media_sizing, release_trust, runtime_gates  # noqa: E402

SCHEMA_VERSION = 2
ROOT = Path(__file__).resolve().parents[1]


def file_sha256(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def inspection_counts(path):
    """Pass, fail and not-run as the report itself recorded them."""

    payload = read_json(path)
    if payload is None:
        return None
    counts = payload.get("counts") or {}
    return {
        "result": str(payload.get("result") or ""),
        "pass": int(counts.get("pass") or 0),
        "fail": int(counts.get("fail") or 0),
        "not_run": int(counts.get("not_run") or 0),
        "mandatory_not_run": list(payload.get("mandatory_not_run") or []),
        "sha256": file_sha256(path),
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown", default="")
    parser.add_argument("--attestation", default="")
    parser.add_argument("--gate-report", default="")
    parser.add_argument("--source-authority", default="")
    parser.add_argument("--source-bundle", default="")
    parser.add_argument("--source-parity", default="")
    parser.add_argument("--runtime-gates", default="")
    parser.add_argument("--kit-manifest", default="")
    parser.add_argument("--package", default="")
    parser.add_argument("--keyring", default="")
    parser.add_argument("--trusted-fingerprint", action="append", default=[])
    parser.add_argument("--project-root", default=str(ROOT))
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--run-id", default="")
    return parser.parse_args(argv)


def gate_verdict(path):
    if not path or not Path(path).is_file():
        return {"result": "not_run", "detail": "no release gate report"}
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    verdicts = [line.strip() for line in text.splitlines() if line.startswith("RESULT:")]
    verdict = verdicts[-1] if verdicts else ""
    return {
        "result": "pass" if verdict.startswith("RESULT: PASS") else "fail" if verdict else "not_run",
        "detail": verdict or "the report carries no verdict",
        "sha256": file_sha256(path),
    }


def read_runtime_gates(path):
    """The runtime evidence, or an explicit absence. Never a default pass."""

    if not path or not Path(path).is_file():
        return {"result": "not_run", "detail": "no runtime gate evidence", "gates": {}}
    try:
        gates = runtime_gates.read(path)
    except runtime_gates.RuntimeGateError as error:
        return {"result": "fail", "detail": f"{error.code}: {error.message}", "gates": {}}
    return {
        "result": "pass" if gates.required_pass else "fail",
        "detail": "; ".join(f"{name} did not pass" for name in gates.unmet) or "every required "
        "runtime gate passed",
        "sha256": file_sha256(path),
        "gates": gates.summary(),
        "unmet": list(gates.unmet),
    }


def read_kit_manifest(path):
    """What the hardware kit said about itself, re-read from the kit directory."""

    if not path or not Path(path).is_file():
        return {"physical_ready": False, "detail": "no hardware kit manifest"}
    payload = read_json(path)
    if payload is None:
        return {"physical_ready": False, "detail": "the kit manifest could not be read"}
    return {
        "physical_ready": bool(payload.get("physical_ready")),
        "development_kit": bool(payload.get("development_kit")),
        "sha256": file_sha256(path),
        "detail": "the kit verified itself" if payload.get("physical_ready") else "the kit is "
        "not physical-ready",
    }


def profiles_verified(entries, attestation, wanted):
    """Every profile has a build, and the attestation describes exactly those."""

    if not entries or any(entry.get("error") for entry in entries.values()):
        return False
    if attestation is None:
        return False
    described = {entry.profile for entry in attestation.profiles}
    return described == set(wanted)


def inspections_passed(entries):
    """Every mandatory inspection passed, with nothing mandatory left unrun."""

    if not entries:
        return False
    for entry in entries.values():
        for name in ("image_inspection", "update_inspection", "sparse_crosscheck"):
            record = entry.get(name, {})
            if record.get("result") != "pass" or record.get("mandatory_not_run"):
                return False
    return True


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    dist = Path(args.dist)
    reports = dist / "reports"

    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": args.run_id,
        "project": {},
        "source_bundle": {},
        "package": {},
        "builder": {},
        "profiles": {},
        "release_gate": gate_verdict(args.gate_report),
        "release_attestation": {},
        "source_binding": {"ok": False, "problems": ["no release attestation"]},
        "freshness": {"stale": True, "detail": "no release attestation"},
        "certified_revision": "",
        "certified_tree_sha256": "",
        "media": media_sizing.requirements(),
        "physical_ready": False,
        "physical_tested": False,
    }

    if args.source_authority and Path(args.source_authority).is_file():
        from appliance import release_inputs

        try:
            source = release_inputs.read_source_bundle_authority(args.source_authority)
        except release_inputs.ReleaseInputError as error:
            result["source_bundle"] = {"error": f"{error.code}: {error.message}"}
        else:
            result["project"] = {
                "revision": source.revision,
                "tree_sha256": source.tree_sha256,
            }
            result["source_bundle"] = {
                "sha256": source.bundle_sha256,
                "tracked_objects": source.tracked_objects,
                "symlinks": source.symlinks,
            }

    if args.package and Path(args.package).is_file():
        from appliance import release_inputs

        try:
            result["package"] = release_inputs.read_package(args.package).to_dict()
        except release_inputs.ReleaseInputError as error:
            result["package"] = {"error": f"{error.code}: {error.message}"}

    profiles = args.profile or ["rpi4", "rpi5"]
    prefixes = {}
    for profile in profiles:
        matches = sorted(dist.glob(f"*-{profile}-*.build-authority.json"))
        entry = {}
        if len(matches) == 1:
            try:
                authority = build_authority.read(matches[0])
            except build_authority.BuildAuthorityError as error:
                entry["error"] = f"{error.code}: {error.message}"
            else:
                prefix = matches[0].name[: -len(".build-authority.json")]
                prefixes[profile] = prefix
                entry.update(
                    {
                        "build_id": authority.build_id,
                        "project_revision": authority.project.revision,
                        "project_tree_sha256": authority.project.tree_sha256,
                        "builder_environment_sha256": authority.builder_environment_sha256,
                        "image_sha256": authority.image.sha256,
                        "update_sha256": authority.update.sha256,
                        "package_sha256": authority.package_sha256,
                    }
                )
                image = dist / f"{prefix}.img"
                if image.is_file():
                    entry["image_bytes"] = image.stat().st_size
                update = dist / f"{prefix}.update.tar.zst"
                if update.is_file():
                    entry["update_bytes"] = update.stat().st_size
                if not result["builder"]:
                    result["builder"] = {
                        "base_image_lock_id": authority.environment.base_image_lock_id,
                        "base_image_sha512": authority.environment.base_image_sha512,
                        "environment_sha256": authority.builder_environment_sha256,
                    }
        elif matches:
            entry["error"] = f"{len(matches)} build authorities for {profile}"
        else:
            entry["error"] = "no build authority"

        for name, path in (
            ("image_inspection", reports / f"image-inspection-{profile}.json"),
            ("update_inspection", reports / f"update-inspection-{profile}.json"),
            ("sparse_crosscheck", reports / f"sparse-crosscheck-{profile}.json"),
        ):
            counts = inspection_counts(path) if path.is_file() else None
            entry[name] = counts or {"result": "not_run"}
        result["profiles"][profile] = entry

    result["runtime_gates"] = read_runtime_gates(args.runtime_gates)
    result["hardware_kit"] = read_kit_manifest(args.kit_manifest)

    # The trust anchor comes from the operator running this, never from the
    # release: a document that named the key it should be checked against would
    # be certifying itself.
    policy = release_trust.TrustPolicy.of(args.keyring, args.trusted_fingerprint)
    result["trust_policy"] = policy.to_dict()

    attestation = None
    if args.attestation and Path(args.attestation).is_file():
        from appliance import release_attestation

        try:
            attestation = release_attestation.read(args.attestation)
        except release_attestation.AttestationError as error:
            result["release_attestation"] = {"error": f"{error.code}: {error.message}"}
        else:
            signature = release_trust.verify_signature(args.attestation, policy)
            rehash = release_attestation.verify(
                attestation,
                dist=dist,
                reports=reports,
                prefixes=prefixes,
                gate_report=args.gate_report,
            )
            binding = release_trust.verify_source_binding(
                attestation,
                authority=args.source_authority or None,
                bundle=args.source_bundle or None,
                parity=args.source_parity or None,
            )
            fresh = release_trust.freshness(attestation, root=args.project_root)
            result["release_attestation"] = {
                "sha256": file_sha256(args.attestation),
                "canonical_hash": attestation.canonical_hash,
                "result": attestation.result,
                "signature": signature.to_dict(),
                "artefacts_rehashed": not rehash,
                "rehash_problems": list(rehash),
            }
            result["source_binding"] = binding.to_dict()
            result["freshness"] = fresh.to_dict()
            result["certified_revision"] = fresh.certified_revision
            result["certified_tree_sha256"] = fresh.certified_tree

    # Readiness is derived, never asserted: it is the named invariant list, and
    # every one of these has been false while a release still reported ready.
    attested = result.get("release_attestation", {})
    signature = attested.get("signature", {})
    invariants = {
        "production_gate_pass": result["release_gate"].get("result") == "pass",
        "attestation_result_pass": attested.get("result") == "pass",
        "attestation_signature_present": bool(signature.get("present")),
        "attestation_signature_verified": bool(signature.get("verified")),
        "trusted_signer": bool(signature.get("trusted")),
        "attestation_artefacts_rehashed": bool(attested.get("artefacts_rehashed")),
        "source_bundle_verified": bool(result.get("source_binding", {}).get("ok")),
        "all_profiles_verified": profiles_verified(result["profiles"], attestation, profiles),
        "all_mandatory_inspections_pass": inspections_passed(result["profiles"]),
        "runtime_required_gates_pass": result["runtime_gates"].get("result") == "pass",
        "release_not_stale": not result.get("freshness", {}).get("stale", True),
        "hardware_kit_verified": bool(result["hardware_kit"].get("physical_ready")),
    }
    verdict = release_trust.readiness(
        invariants, required=release_trust.KIT_READINESS_INVARIANTS
    )
    result["readiness"] = verdict.to_dict()
    result["physical_ready"] = verdict.ready

    Path(args.output).write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"release result: {args.output}")

    if args.markdown:
        Path(args.markdown).write_text(render_markdown(result), encoding="utf-8")
        print(f"summary:        {args.markdown}")

    print(f"physical_ready: {str(result['physical_ready']).lower()}")
    return 0 if result["physical_ready"] else 1


def render_markdown(result):
    """The same numbers as prose. Generated, so it cannot drift from them."""

    lines = [
        "# Appliance release result",
        "",
        f"Run: `{result['run_id'] or 'unnamed'}`  ",
        f"Project: `{result['project'].get('revision', 'unknown')}`  ",
        f"Tree: `{result['project'].get('tree_sha256', 'unknown')}`",
        "",
        "| Profile | Build | Image inspection | Update inspection | Sparse cross-check |",
        "| --- | --- | --- | --- | --- |",
    ]
    for profile, entry in sorted(result["profiles"].items()):
        def cell(name):
            record = entry.get(name, {})
            if record.get("result") == "not_run":
                return "NOT RUN"
            return (
                f"{record.get('result', '?').upper()} "
                f"({record.get('pass', 0)}/{record.get('fail', 0)}/{record.get('not_run', 0)})"
            )

        lines.append(
            f"| {profile} | `{entry.get('build_id', entry.get('error', '—'))}` | "
            f"{cell('image_inspection')} | {cell('update_inspection')} | "
            f"{cell('sparse_crosscheck')} |"
        )
    signature = result.get("release_attestation", {}).get("signature", {})
    gates = result.get("runtime_gates", {})
    lines += [
        "",
        "Counts are pass/fail/not-run, read out of each report rather than copied.",
        "",
        f"- Release gate: **{result['release_gate'].get('detail', 'unknown')}**",
        f"- Source bundle: {result['source_bundle'].get('tracked_objects', '—')} tracked "
        f"objects, {result['source_bundle'].get('symlinks', '—')} symlinks",
        f"- Package: `{result['package'].get('name', '—')} "
        f"{result['package'].get('version', '')} "
        f"{result['package'].get('architecture', '')}`",
        f"- Attestation signature: **{signature.get('detail', 'no attestation')}** "
        f"(verified: {str(bool(signature.get('verified'))).lower()}, trusted signer: "
        f"{str(bool(signature.get('trusted'))).lower()})",
        f"- Runtime gates: **{gates.get('result', 'not_run')}** "
        f"({', '.join(f'{k}={v}' for k, v in sorted((gates.get('gates') or {}).items())) or '—'})",
        f"- Source binding: **{str(bool(result.get('source_binding', {}).get('ok'))).lower()}**",
        f"- Stale: **{str(bool(result.get('freshness', {}).get('stale', True))).lower()}**",
        f"- Minimum supported medium: **{result['media']['supported_media_label']}**",
        f"- Physical ready: **{str(result['physical_ready']).lower()}**",
        f"- Physical tested: **{str(result['physical_tested']).lower()}**",
        "",
    ]
    unmet = result.get("readiness", {}).get("unmet") or []
    if unmet:
        lines += ["Unmet readiness invariants: " + ", ".join(f"`{name}`" for name in unmet), ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
