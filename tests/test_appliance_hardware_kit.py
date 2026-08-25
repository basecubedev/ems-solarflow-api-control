# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which artefacts an operator is allowed to carry to a Raspberry Pi.

The kit was a filename glob. Everything under ``dist`` matching ``*-rpi5-*``
was copied, whatever had been copied was hashed, and the result was PASS — so a
directory holding two builds produced a kit with one build's image beside
another build's update and a SHA256SUMS file that agreed with itself. A missing
signature, a missing release-gate report and a build authority describing a
different build were all invisible.

The kit is assembled from authority now: one completed BuildAuthority per
profile decides what the kit may carry, every artefact is verified against the
digests that build recorded, and a gate report that does not say PASS is a
failure. A development kit can still be assembled from whatever exists, and
says so — it can never report READY.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from appliance import build_authority, release_attestation, runtime_gates
from tests.test_appliance_release_signature import SigningKey

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "scripts/appliance_hardware_kit.py"
KIT_SCRIPT = ROOT / "scripts/appliance-hardware-validation-kit.sh"

VERSION = "0.1.0"
BUILD_ID = "20260809120000"


def approved_builder():
    """The lock row a release-grade builder has to match, read not restated.

    The fixture used to describe a machine release policy would refuse -- a
    made-up digest and a kernel with no `-cloud-` flavour. Nothing noticed,
    because builder approval was enforced in one script that these tests do not
    reach. A fixture that could not be signed in production is not a fixture for
    a release that is ready.
    """

    import json

    lock = json.loads(
        (ROOT / "packaging/appliance/vm/base-images.lock.json").read_text(encoding="utf-8")
    )
    return lock["images"]["builder"]


def environment():
    builder = approved_builder()
    return build_authority.BuilderEnvironment(
        base_image_lock_id=f"builder:{builder['filename']}",
        base_image_sha512=builder["sha512"],
        os_release="debian 13",
        kernel="Linux 6.12.100+deb13-cloud-amd64",
        architecture="x86_64",
        python_version="Python 3.13.5",
        podman_version="podman version 5.4.2",
        mmdebstrap_version="mmdebstrap 1.5.7",
        qemu_version="qemu-aarch64 version 9.2.4",
        binfmt_handler="qemu-aarch64 enabled",
        dependency_manifest_sha256="sha256:" + "ab" * 32,
        critical_packages=("mmdebstrap 1.5.7",),
        captured_at="2026-08-09T09:00:00Z",
    )


def build_dist(
    tmp_path, *, profile="rpi5", variant="ab", build_id=BUILD_ID, signed=True,
    complete=True, project=None
):
    dist = tmp_path / "dist"
    reports = dist / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    prefix = f"ems-solarflow-appliance-{VERSION}-{profile}-arm64-{variant}"

    image = dist / f"{prefix}.img"
    image.write_bytes(b"an appliance image" * 64)
    update = dist / f"{prefix}.update.tar.zst"
    update.write_bytes(b"an update artefact" * 32)
    archive = dist / f"{prefix}.tar.zst"
    archive.write_bytes(update.read_bytes())
    (dist / f"{prefix}.img.sha256").write_text(f"{build_authority.file_sha256(image)[7:]}  {image.name}\n")

    authority = build_authority.BuildAuthority(
        builder=build_authority.Builder(
            source_form="git", revision="a" * 40, source_tree_sha256="sha256:" + "b" * 64
        ),
        project=project or build_authority.Project(
            revision="c" * 40, tree_sha256="sha256:" + "d" * 64
        ),
        profile=profile,
        variant=variant,
        build_id=build_id,
        image=build_authority.Artefact(
            path=str(image), sha256=build_authority.file_sha256(image)
        ),
        update=build_authority.Artefact(
            path=str(update), sha256=build_authority.file_sha256(update)
        ),
        package_sha256="e" * 64,
        completed=complete,
        environment=environment(),
    )
    (dist / f"{prefix}{build_authority.AUTHORITY_NAME and '.build-authority.json'}").write_text(
        json.dumps(authority.to_dict(), indent=2, sort_keys=True) + "\n"
    )
    (dist / f"{prefix}.build.json").write_text(json.dumps({"build_id": build_id}))
    (dist / f"{prefix}.builder-environment.json").write_text(
        json.dumps(authority.environment.to_dict())
    )

    manifest = {
        "build_id": build_id,
        "release_version": VERSION,
        "provenance": {
            "verified": True,
            "build_authority_sha256": authority.canonical_hash,
            "builder_environment_sha256": authority.builder_environment_sha256,
        },
        "archive": {
            "name": archive.name,
            "digest": build_authority.file_sha256(archive),
        },
    }
    (dist / f"{prefix}.manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    if signed:
        (dist / f"{prefix}.manifest.json.asc").write_text(
            "-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n"
        )

    for kind in ("image", "update"):
        (reports / f"{kind}-inspection-{profile}.json").write_text(
            json.dumps(
                {
                    "result": "pass",
                    "counts": {"pass": 12, "fail": 0, "not_run": 0},
                    "mandatory_not_run": [],
                }
            )
        )
    (reports / f"sparse-crosscheck-{profile}.json").write_text(
        json.dumps({"schema_version": 1, "result": "pass", "members": []})
    )
    return dist, prefix, authority


def gate_report(tmp_path, verdict="RESULT: PASS"):
    report = tmp_path / "release-gate-report.txt"
    report.write_text(f"source-authority PASS\n{verdict}\n")
    return report


def write_attestation(tmp_path, dist, prefix, authority, gate, *, profiles=None, name=None):
    """The signed record a production kit is assembled against.

    Built the way the finalizer builds it — by hashing the files that are
    actually there — so a test that changes an artefact afterwards is testing
    exactly what a stale or swapped file does to a kit.
    """

    entries = []
    for profile, build_id in (profiles or [(authority.profile, authority.build_id)]):
        entries.append(
            release_attestation.describe_profile(
                profile,
                dist=dist,
                prefix=prefix if profile == authority.profile else f"{prefix}-{profile}",
                reports=dist / "reports",
                build_id=build_id,
                gate_report=gate,
            )
        )
    source = source_documents(tmp_path, authority)
    attestation = release_attestation.build(
        project={
            "revision": authority.project.revision,
            "tree_sha256": authority.project.tree_sha256,
        },
        source=source_binding(source),
        package={
            "name": "ems-appliance-manager",
            "version": VERSION,
            "architecture": "arm64",
            "sha256": "sha256:" + authority.package_sha256,
        },
        builder={
            "base_image_lock_id": authority.environment.base_image_lock_id,
            "base_image_sha512": authority.environment.base_image_sha512,
            "environment_sha256": authority.builder_environment_sha256,
        },
        profiles=entries,
    )
    target = tmp_path / (name or "release-attestation.json")
    release_attestation.write(target, attestation)
    return target


def source_documents(tmp_path, authority):
    """The bundle, its authority and the parity report a release is bound to."""

    directory = tmp_path / "source"
    directory.mkdir(exist_ok=True)
    bundle = directory / "source-bundle.tar.gz"
    if not bundle.is_file():
        bundle.write_bytes(b"a canonical source bundle" * 16)
    authority_document = directory / "source-bundle-authority.json"
    authority_document.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_sha256": build_authority.file_sha256(bundle),
                "project": {
                    "revision": authority.project.revision,
                    "tree_sha256": authority.project.tree_sha256,
                },
                "tracked_objects": 1142,
                "symlinks": 6,
                "created_at": "2026-08-10T00:00:00Z",
            },
            indent=2,
            sort_keys=True,
        )
    )
    parity = directory / "source-bundle-parity.json"
    parity.write_text(
        json.dumps(
            {
                "ok": True,
                "compared": 1142,
                "symlinks": 6,
                "missing": [],
                "mismatched": [],
                "excluded": [],
                "unexpected": [],
                "unsafe": [],
                "duplicate": [],
                "problems": [],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return {"bundle": bundle, "authority": authority_document, "parity": parity}


def source_binding(source):
    return {
        "bundle_sha256": build_authority.file_sha256(source["bundle"]),
        "authority_sha256": build_authority.file_sha256(source["authority"]),
        "parity_sha256": build_authority.file_sha256(source["parity"]),
        "tracked_objects": 1142,
        "symlinks": 6,
    }


def runtime_gate_evidence(tmp_path, *, results=None):
    """Every required runtime gate passing, unless a test says otherwise."""

    target = tmp_path / "runtime-gates.json"
    answers = dict.fromkeys(runtime_gates.REQUIRED_GATES, "pass")
    answers.update(results or {})
    records = [
        runtime_gates.record(
            name,
            result,
            reason="" if result != "not_run" else "the prerequisite is not installed",
            environment="test",
        )
        for name, result in answers.items()
    ]
    runtime_gates.write(target, runtime_gates.build(records, created_at="2026-08-10T00:00:00Z"))
    return target


def ready_args(tmp_path, dist, prefix, authority, *, gates=None):
    """Everything a kit needs before it may call itself physical_ready."""

    gate = gate_report(tmp_path)
    attestation = write_attestation(tmp_path, dist, prefix, authority, gate)
    key = SigningKey(tmp_path / "gnupg", "EMS Kit Test <kit@ems.invalid>")
    keyring = key.keyring(tmp_path / "trusted.gpg")
    key.sign(attestation)
    source = source_documents(tmp_path, authority)
    return [
        "--gate-report", str(gate),
        "--attestation", str(attestation),
        "--keyring", str(keyring),
        "--trusted-fingerprint", key.fingerprint,
        "--runtime-gates", str(runtime_gate_evidence(tmp_path, results=gates)),
        "--source-authority", str(source["authority"]),
        "--source-bundle", str(source["bundle"]),
        "--source-parity", str(source["parity"]),
    ]


def run_kit(dist, output, *args):
    return subprocess.run(
        [sys.executable, str(KIT), "--dist", str(dist), "--output", str(output), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def kit_manifest(output):
    return json.loads((output / "kit-manifest.json").read_text())


def test_a_complete_signed_build_assembles_a_ready_kit(tmp_path):
    dist, prefix, authority = build_dist(tmp_path)
    output = tmp_path / "kit"

    result = run_kit(dist, output, *ready_args(tmp_path, dist, prefix, authority))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
    manifest = kit_manifest(output)
    assert manifest["physical_ready"] is True
    assert manifest["builds"][0]["build_id"] == BUILD_ID
    assert manifest["builds"][0]["builder_environment_sha256"].startswith("sha256:")
    assert (output / "rpi5" / f"{prefix}.img").is_file()
    assert (output / "rpi5" / f"{prefix}.manifest.json.asc").is_file()


def test_a_kit_without_a_signature_is_a_failure(tmp_path):
    dist, _, _ = build_dist(tmp_path, signed=False)
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate_report(tmp_path)))

    assert result.returncode == 1
    assert "signature" in result.stdout
    assert kit_manifest(output)["physical_ready"] is False


def test_a_kit_without_a_release_gate_report_is_a_failure(tmp_path):
    dist, _, _ = build_dist(tmp_path)
    output = tmp_path / "kit"

    result = run_kit(dist, output)

    assert result.returncode == 1
    assert "release gate" in result.stdout


def test_a_release_gate_that_did_not_pass_is_a_failure(tmp_path):
    dist, _, _ = build_dist(tmp_path)
    output = tmp_path / "kit"

    result = run_kit(
        dist, output, "--gate-report", str(gate_report(tmp_path, "RESULT: NOT RUN (2 required)"))
    )

    assert result.returncode == 1
    assert "NOT RUN" in result.stdout


def test_two_builds_for_one_profile_are_refused(tmp_path):
    dist, _, _ = build_dist(tmp_path)
    other = tmp_path / "second"
    other.mkdir()
    second_dist, second_prefix, _ = build_dist(other, build_id="20260809180000")
    for item in second_dist.glob("*.build-authority.json"):
        (dist / f"second-{item.name}").write_text(item.read_text())
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate_report(tmp_path)))

    assert result.returncode == 1
    assert "different builds" in result.stderr
    assert "build_authority_ambiguous" in result.stderr


def test_an_image_that_is_not_the_one_the_build_recorded_is_refused(tmp_path):
    dist, prefix, _ = build_dist(tmp_path)
    (dist / f"{prefix}.img").write_bytes(b"a different image")
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate_report(tmp_path)))

    assert result.returncode == 1
    assert "build_authority_mismatch" in result.stdout


def test_a_manifest_from_another_build_is_refused(tmp_path):
    dist, prefix, _ = build_dist(tmp_path)
    manifest = json.loads((dist / f"{prefix}.manifest.json").read_text())
    manifest["build_id"] = "20250101000000"
    (dist / f"{prefix}.manifest.json").write_text(json.dumps(manifest))
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate_report(tmp_path)))

    assert result.returncode == 1
    assert "the manifest names build" in result.stdout


def test_an_unsigned_development_manifest_is_refused_for_a_production_kit(tmp_path):
    dist, prefix, _ = build_dist(tmp_path)
    manifest = json.loads((dist / f"{prefix}.manifest.json").read_text())
    manifest["provenance"]["verified"] = False
    (dist / f"{prefix}.manifest.json").write_text(json.dumps(manifest))
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate_report(tmp_path)))

    assert result.returncode == 1
    assert "development artefact" in result.stdout


def test_an_inspection_with_a_check_that_never_ran_is_refused(tmp_path):
    dist, prefix, authority = build_dist(tmp_path)
    (dist / "reports/image-inspection-rpi5.json").write_text(
        json.dumps({"result": "pass", "counts": {"pass": 5, "fail": 0, "not_run": 5}})
    )
    output = tmp_path / "kit"

    result = run_kit(dist, output, *ready_args(tmp_path, dist, prefix, authority))

    assert result.returncode == 1
    assert "never ran" in result.stdout


def test_an_incomplete_build_authority_is_refused(tmp_path):
    dist, _, _ = build_dist(tmp_path, complete=False)
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate_report(tmp_path)))

    assert result.returncode == 1
    assert "no completed build authority" in result.stderr


def test_a_development_kit_never_claims_readiness(tmp_path):
    dist, prefix, _ = build_dist(tmp_path, signed=False)
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--development-kit")

    assert result.returncode == 0
    assert "RESULT: INCOMPLETE" in result.stdout
    assert "RESULT: PASS" not in result.stdout
    manifest = kit_manifest(output)
    assert manifest["physical_ready"] is False
    assert manifest["development_kit"] is True


def test_a_private_key_beside_the_artefacts_destroys_the_kit(tmp_path):
    dist, _, _ = build_dist(tmp_path)
    (dist / "reports/image-inspection-rpi5.json").write_text(
        json.dumps({"result": "pass", "counts": {"pass": 1, "fail": 0, "not_run": 0}})
        + "\n-----BEGIN OPENSSH PRIVATE KEY-----\n"
    )
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate_report(tmp_path)))

    assert result.returncode == 1
    assert "private_key_in_kit" in result.stderr
    assert not output.exists()


def test_an_image_is_never_scanned_as_text(tmp_path):
    """Every OpenSSH binary carries the banner the scan looks for."""

    dist, prefix, authority = build_dist(tmp_path)
    (dist / f"{prefix}.img").write_bytes(
        b"-----BEGIN OPENSSH PRIVATE KEY-----" + b"\x00" * 64
    )
    authority = json.loads((dist / f"{prefix}.build-authority.json").read_text())
    authority["image"]["sha256"] = build_authority.file_sha256(dist / f"{prefix}.img")
    (dist / f"{prefix}.build-authority.json").write_text(json.dumps(authority))
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate_report(tmp_path)))

    assert "private_key_in_kit" not in result.stderr


def test_the_kit_records_every_identity_a_reviewer_needs(tmp_path):
    dist, prefix, authority = build_dist(tmp_path)
    output = tmp_path / "kit"

    run_kit(dist, output, *ready_args(tmp_path, dist, prefix, authority))

    record = kit_manifest(output)["builds"][0]
    assert record["project_revision"] == authority.project.revision
    assert record["project_tree_sha256"] == authority.project.tree_sha256
    assert record["upstream_revision"] == authority.builder.revision
    assert record["build_authority_sha256"] == authority.canonical_hash
    assert record["package_sha256"] == authority.package_sha256


def test_the_shell_entry_point_delegates_to_the_authority_assembler():
    text = KIT_SCRIPT.read_text(encoding="utf-8")

    assert "appliance_hardware_kit.py" in text
    assert "find \"$DIST\" -name" not in text


# --- the kit is assembled against one signed attestation --------------------
#
# Filenames carry no build id, no revision and no freshness. A kit assembled
# from whatever a directory contained could not tell a stale artefact, a report
# for another profile, or two profiles from different builds apart — and the
# text "RESULT: PASS" in a report is a claim, not evidence.


def test_a_production_kit_without_an_attestation_is_never_ready(tmp_path):
    dist, _, _ = build_dist(tmp_path)
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate_report(tmp_path)))

    assert result.returncode == 1
    assert "release attestation" in result.stdout
    assert kit_manifest(output)["physical_ready"] is False


def test_an_artefact_replaced_after_the_attestation_was_written_is_refused(tmp_path):
    dist, prefix, authority = build_dist(tmp_path)
    args = ready_args(tmp_path, dist, prefix, authority)
    # The image the attestation measured is not the image on disk any more.
    (dist / f"{prefix}.img").write_bytes(b"a different appliance image" * 64)
    output = tmp_path / "kit"

    result = run_kit(dist, output, *args)

    assert result.returncode == 1
    assert "release_attestation_mismatch" in result.stdout


def test_a_stale_inspection_report_is_refused(tmp_path):
    """A report that passed once, for an image that is no longer this one."""

    dist, prefix, authority = build_dist(tmp_path)
    args = ready_args(tmp_path, dist, prefix, authority)
    (dist / "reports/image-inspection-rpi5.json").write_text(
        json.dumps({"result": "pass", "counts": {"pass": 99, "fail": 0, "not_run": 0}})
    )
    output = tmp_path / "kit"

    result = run_kit(dist, output, *args)

    assert result.returncode == 1
    assert "image_inspection" in result.stdout


def test_an_attestation_for_another_build_is_refused(tmp_path):
    dist, prefix, authority = build_dist(tmp_path)
    gate = gate_report(tmp_path)
    attestation = write_attestation(
        tmp_path, dist, prefix, authority, gate, profiles=[("rpi5", "20990101000000")]
    )
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate), "--attestation", str(attestation))

    assert result.returncode == 1
    assert "the attestation names build" in result.stdout


def test_an_attestation_for_another_project_revision_is_refused(tmp_path):
    dist, prefix, authority = build_dist(tmp_path)
    gate = gate_report(tmp_path)
    attestation = write_attestation(tmp_path, dist, prefix, authority, gate)
    payload = json.loads(attestation.read_text())
    payload["project"]["revision"] = "f" * 40
    attestation.write_text(json.dumps(payload))
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate), "--attestation", str(attestation))

    assert result.returncode == 1
    assert "the attestation names project" in result.stdout


def test_an_attestation_describing_a_profile_this_kit_does_not_carry_is_refused(tmp_path):
    dist, prefix, authority = build_dist(tmp_path)
    gate = gate_report(tmp_path)
    attestation = write_attestation(
        tmp_path,
        dist,
        prefix,
        authority,
        gate,
        profiles=[("rpi5", authority.build_id), ("rpi4", authority.build_id)],
    )
    output = tmp_path / "kit"

    result = run_kit(
        dist, output, "--profile", "rpi5", "--gate-report", str(gate),
        "--attestation", str(attestation),
    )

    assert result.returncode == 1
    assert "also describes rpi4" in result.stdout


def test_an_attestation_of_another_schema_is_refused(tmp_path):
    dist, prefix, authority = build_dist(tmp_path)
    gate = gate_report(tmp_path)
    attestation = write_attestation(tmp_path, dist, prefix, authority, gate)
    payload = json.loads(attestation.read_text())
    payload["schema_version"] = 99
    attestation.write_text(json.dumps(payload))
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate), "--attestation", str(attestation))

    assert result.returncode == 1
    assert "release_attestation_unsupported" in result.stdout


def test_a_gate_report_the_attestation_never_measured_is_refused(tmp_path):
    """The report is bound by hash, so its verdict is about a known file."""

    dist, prefix, authority = build_dist(tmp_path)
    gate = gate_report(tmp_path)
    attestation = write_attestation(tmp_path, dist, prefix, authority, gate)
    gate.write_text("source-authority PASS\nRESULT: PASS\n# edited afterwards\n")
    output = tmp_path / "kit"

    result = run_kit(dist, output, "--gate-report", str(gate), "--attestation", str(attestation))

    assert result.returncode == 1
    assert "release_gate" in result.stdout


def test_a_ready_kit_carries_the_attestation_it_was_assembled_against(tmp_path):
    dist, prefix, authority = build_dist(tmp_path)
    output = tmp_path / "kit"

    result = run_kit(dist, output, *ready_args(tmp_path, dist, prefix, authority))

    assert result.returncode == 0, result.stdout + result.stderr
    assert (output / "release-attestation.json").is_file()
    manifest = kit_manifest(output)
    assert manifest["release_attestation"]["ok"] is True
    assert manifest["release_attestation"]["detail"].startswith("sha256:")


def test_the_baseline_capture_calls_subcommands_that_exist():
    """A capture of a subcommand that does not exist writes an empty file, and
    the run still printed RESULT: PASS over it."""

    import re

    from appliance import cli

    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "appliance-hardware-capture-baseline.sh").read_text(
        encoding="utf-8"
    )
    parser_source = (root / "appliance" / "cli.py").read_text(encoding="utf-8")

    invoked = set(re.findall(r"^capture \S+ ems-appliance ([\w-]+)", script, re.M))

    assert invoked, "no ems-appliance capture was found"
    for command in invoked:
        assert f'"{command}"' in parser_source, f"{command} is not a subcommand"
    assert cli  # the module imports, so the parser above is the real one


def test_a_broken_persistence_is_a_failure_not_an_unreachable_runtime():
    """`verify-persistence` exits non-zero *because* the persistence is broken.
    Conflating that with an unreachable runtime turned every real finding into
    NOT RUN -- neither a pass nor a problem anyone looks at."""

    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "appliance-hardware-verify-persistence.sh").read_text(
        encoding="utf-8"
    )

    assert 'REPORT=$(ems-appliance ab verify-persistence --json 2>/dev/null)\n' in script
    assert "|| not_run" not in script.split("REPORT=")[1].split("\n")[0]
