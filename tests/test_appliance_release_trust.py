# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a release has to prove before an operator may carry it to hardware.

``physical_ready`` was derived from ``Path(f"{attestation}.asc").is_file()`` and
a release-gate report containing the words ``RESULT: PASS``. Both are claims
anyone can write. An unsigned rehearsal, a release signed by a developer's
throwaway key, a release whose source bundle had been swapped for another
commit's and a release nobody had rebuilt after editing the source were all
indistinguishable from a signed production release.

Each case below is one of those, run end to end against the real generator with
a real gpg key that lives in a temporary GNUPGHOME and never leaves it. The
trust anchor — the keyring and the fingerprint — is supplied by the caller in
every one of them, because a document that named the key it should be checked
against would be certifying itself.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from appliance import build_authority, release_attestation, release_trust, runtime_gates
from tests.test_appliance_hardware_kit import (
    VERSION,
    build_dist,
    gate_report,
    runtime_gate_evidence,
    source_binding,
    source_documents,
)
from tests.test_appliance_release_signature import SigningKey

pytestmark = [pytest.mark.integration, pytest.mark.system_build]

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "scripts/appliance_release_result.py"
KIT = ROOT / "scripts/appliance_hardware_kit.py"

requires_gpg = pytest.mark.skipif(
    shutil.which("gpg") is None, reason="gpg signs and verifies a release attestation"
)


def certified_checkout(tmp_path):
    """A real git checkout, so freshness is answered by git rather than by a mock."""

    from appliance import project_source

    root = tmp_path / "checkout"
    root.mkdir()
    (root / "appliance").mkdir()
    (root / "appliance" / "version.py").write_text(f'APPLIANCE_VERSION = "{VERSION}"\n')
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "release@ems.invalid"],
        ["config", "user.name", "Release Test"],
        ["add", "-A"],
        ["commit", "-q", "-m", "the certified revision"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return root, project_source.assert_clean(root)


class Release:
    """One complete, signed, verifiable release, and the knobs to break it."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.root, identity = certified_checkout(tmp_path)
        self.dist, self.prefix, self.authority = build_dist(
            tmp_path,
            project=build_authority.Project(
                revision=identity.revision, tree_sha256=identity.tree_sha256
            ),
        )
        self.gate = gate_report(tmp_path)
        self.source = source_documents(tmp_path, self.authority)
        self.gates = runtime_gate_evidence(tmp_path)
        self.key = SigningKey(tmp_path / "gnupg", "EMS Release Test <release@ems.invalid>")
        self.keyring = self.key.keyring(tmp_path / "trusted.gpg")
        self.attestation = self.write_attestation()
        self.signature = self.key.sign(self.attestation)
        self.kit = tmp_path / "kit"

    def write_attestation(self, *, target=None):
        entry = release_attestation.describe_profile(
            self.authority.profile,
            dist=self.dist,
            prefix=self.prefix,
            reports=self.dist / "reports",
            build_id=self.authority.build_id,
            gate_report=self.gate,
        )
        attestation = release_attestation.build(
            project={
                "revision": self.authority.project.revision,
                "tree_sha256": self.authority.project.tree_sha256,
            },
            source=source_binding(self.source),
            package={
                "name": "ems-appliance-manager",
                "version": VERSION,
                "architecture": "arm64",
                "sha256": "sha256:" + self.authority.package_sha256,
            },
            builder={
                "base_image_lock_id": self.authority.environment.base_image_lock_id,
                "base_image_sha512": self.authority.environment.base_image_sha512,
                "environment_sha256": self.authority.builder_environment_sha256,
            },
            profiles=[entry],
            runtime_gates={"sha256": release_trust.file_sha256(self.gates)},
            release_gate={"sha256": release_trust.file_sha256(self.gate)},
            minimum_media_bytes=32_000_000_000,
        )
        target = target or self.tmp_path / "release-attestation.json"
        release_attestation.write(target, attestation)
        return target

    def assemble_kit(self, *extra):
        return subprocess.run(
            [
                sys.executable, str(KIT),
                "--dist", str(self.dist), "--output", str(self.kit),
                "--profile", self.authority.profile,
                "--gate-report", str(self.gate),
                "--attestation", str(self.attestation),
                "--keyring", str(self.keyring),
                "--trusted-fingerprint", self.key.fingerprint,
                "--runtime-gates", str(self.gates),
                "--source-authority", str(self.source["authority"]),
                "--source-bundle", str(self.source["bundle"]),
                "--source-parity", str(self.source["parity"]),
                *extra,
            ],
            capture_output=True, text=True, check=False, timeout=300,
        )

    def derive(self, *extra, fingerprint=None, keyring=None, with_kit=True):
        if with_kit:
            self.assemble_kit()
        output = self.tmp_path / "release-result.json"
        result = subprocess.run(
            [
                sys.executable, str(RESULT),
                "--dist", str(self.dist), "--output", str(output),
                "--profile", self.authority.profile,
                "--gate-report", str(self.gate),
                "--attestation", str(self.attestation),
                "--keyring", str(keyring or self.keyring),
                "--trusted-fingerprint", fingerprint or self.key.fingerprint,
                "--runtime-gates", str(self.gates),
                "--source-authority", str(self.source["authority"]),
                "--source-bundle", str(self.source["bundle"]),
                "--source-parity", str(self.source["parity"]),
                "--kit-manifest", str(self.kit / "kit-manifest.json"),
                "--project-root", str(self.root),
                *extra,
            ],
            capture_output=True, text=True, check=False, timeout=300,
        )
        payload = json.loads(output.read_text()) if output.is_file() else {}
        return result, payload


@pytest.fixture
def release(tmp_path):
    return Release(tmp_path)


# --- the release that is actually ready --------------------------------------


@requires_gpg
def test_a_signed_verified_release_is_the_only_one_that_is_ready(release):
    result, payload = release.derive()

    assert payload["physical_ready"] is True, payload["readiness"]["unmet"]
    assert result.returncode == 0
    assert payload["release_attestation"]["signature"]["verified"] is True
    assert payload["release_attestation"]["signature"]["trusted"] is True
    assert payload["release_attestation"]["signature"]["fingerprints"] == [release.key.fingerprint]
    assert payload["readiness"]["unmet"] == []
    assert payload["certified_revision"] == release.authority.project.revision


# --- the signature is verified, not counted ----------------------------------


@requires_gpg
def test_an_unsigned_attestation_is_never_ready(release):
    release.signature.unlink()

    result, payload = release.derive()

    signature = payload["release_attestation"]["signature"]
    assert signature["present"] is False
    assert signature["code"] == release_trust.UNSIGNED
    assert payload["physical_ready"] is False
    assert result.returncode == 1


@requires_gpg
def test_a_kit_with_no_signature_is_never_physical_ready(release):
    release.signature.unlink()

    outcome = release.assemble_kit()

    manifest = json.loads((release.kit / "kit-manifest.json").read_text())
    assert manifest["physical_ready"] is False
    assert manifest["release_attestation"]["signature"]["present"] is False
    assert outcome.returncode == 1


@requires_gpg
def test_an_attestation_edited_after_signing_fails(release):
    payload = json.loads(release.attestation.read_text())
    payload["created_at"] = "2020-01-01T00:00:00Z"
    release.attestation.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    _result, derived = release.derive()

    signature = derived["release_attestation"]["signature"]
    assert signature["verified"] is False
    assert signature["code"] == release_trust.UNVERIFIED
    assert derived["physical_ready"] is False


@requires_gpg
def test_a_tampered_signature_fails(release):
    armour = release.signature.read_text().splitlines()
    body = next(index for index, line in enumerate(armour) if index > 2 and line.strip())
    armour[body] = ("B" if armour[body][0] != "B" else "C") + armour[body][1:]
    release.signature.write_text("\n".join(armour) + "\n")

    _result, derived = release.derive()

    assert derived["release_attestation"]["signature"]["verified"] is False
    assert derived["physical_ready"] is False


@requires_gpg
def test_a_valid_signature_over_another_attestation_fails(release):
    other = release.write_attestation(target=release.tmp_path / "other-attestation.json")
    payload = json.loads(other.read_text())
    payload["created_at"] = "2019-05-05T05:05:05Z"
    other.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    release.key.sign(other)
    shutil.copy2(f"{other}.asc", release.signature)

    _result, derived = release.derive()

    assert derived["release_attestation"]["signature"]["verified"] is False
    assert derived["physical_ready"] is False


# --- the trust anchor is the operator's, not the document's -------------------


@requires_gpg
def test_a_correct_signature_by_an_untrusted_fingerprint_is_refused(release):
    _result, derived = release.derive(fingerprint="0" * 40)

    signature = derived["release_attestation"]["signature"]
    assert signature["verified"] is True
    assert signature["trusted"] is False
    assert signature["code"] == release_trust.UNTRUSTED
    assert derived["physical_ready"] is False


@requires_gpg
def test_a_signer_the_trusted_keyring_does_not_hold_is_refused(release, tmp_path):
    stranger = SigningKey(tmp_path / "stranger", "Someone Else <else@ems.invalid>")
    stranger.sign(release.attestation)

    _result, derived = release.derive()

    assert derived["release_attestation"]["signature"]["verified"] is False
    assert derived["physical_ready"] is False


@requires_gpg
def test_a_keyring_with_no_fingerprint_policy_is_not_a_trust_policy(release):
    policy = release_trust.TrustPolicy.of(release.keyring, [])

    verdict = release_trust.verify_signature(release.attestation, policy)

    assert verdict.present is True
    assert verdict.trusted is False
    assert verdict.code == release_trust.NO_POLICY


def test_an_attestation_that_supplies_its_own_trust_anchor_is_refused():
    payload = {
        "schema_version": release_attestation.SCHEMA_VERSION,
        "profiles": [{"profile": "rpi5", "artefacts": {}, "reports": {}}],
        "trusted_fingerprints": ["ABCD"],
    }

    with pytest.raises(release_attestation.AttestationError) as error:
        release_attestation.parse(payload)

    assert error.value.code == release_attestation.SELF_TRUSTED


def test_an_attestation_carrying_a_public_key_is_refused():
    payload = {
        "schema_version": release_attestation.SCHEMA_VERSION,
        "profiles": [{"profile": "rpi5", "artefacts": {}, "reports": {}}],
        "public_key": "-----BEGIN PGP PUBLIC KEY BLOCK-----",
    }

    with pytest.raises(release_attestation.AttestationError) as error:
        release_attestation.parse(payload)

    assert error.value.code == release_attestation.SELF_TRUSTED


# --- the source bundle is the tracked tree, proven by comparing it ------------


@requires_gpg
def test_a_source_bundle_substituted_after_the_attestation_is_refused(release):
    release.source["bundle"].write_bytes(b"another project's source entirely")

    _result, derived = release.derive()

    assert derived["source_binding"]["ok"] is False
    assert derived["physical_ready"] is False
    assert any("source bundle" in problem for problem in derived["source_binding"]["problems"])


@requires_gpg
def test_a_source_authority_substituted_after_the_attestation_is_refused(release):
    payload = json.loads(release.source["authority"].read_text())
    payload["project"]["revision"] = "f" * 40
    release.source["authority"].write_text(json.dumps(payload, indent=2, sort_keys=True))

    _result, derived = release.derive()

    assert derived["source_binding"]["ok"] is False
    assert derived["physical_ready"] is False


@requires_gpg
def test_a_parity_report_that_compared_nothing_proves_nothing(release):
    payload = json.loads(release.source["parity"].read_text())
    payload["compared"] = 0
    release.source["parity"].write_text(json.dumps(payload, indent=2, sort_keys=True))
    release.attestation = release.write_attestation()
    release.signature = release.key.sign(release.attestation)

    _result, derived = release.derive()

    assert derived["source_binding"]["ok"] is False
    assert derived["physical_ready"] is False


@requires_gpg
def test_a_bundle_the_parity_check_rejected_is_refused(release):
    payload = json.loads(release.source["parity"].read_text())
    payload["ok"] = False
    payload["unexpected"] = ["packaging/appliance/extra-input.sh"]
    release.source["parity"].write_text(json.dumps(payload, indent=2, sort_keys=True))
    release.attestation = release.write_attestation()
    release.signature = release.key.sign(release.attestation)

    _result, derived = release.derive()

    assert derived["source_binding"]["ok"] is False
    assert derived["physical_ready"] is False


@requires_gpg
def test_an_attestation_that_binds_no_source_authority_is_refused(release):
    payload = json.loads(release.attestation.read_text())
    del payload["source_bundle"]["authority_sha256"]
    release.attestation.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    release.signature = release.key.sign(release.attestation)

    _result, derived = release.derive()

    assert derived["source_binding"]["ok"] is False
    assert derived["physical_ready"] is False


# --- a release nobody rebuilt ------------------------------------------------


@requires_gpg
def test_a_tracked_file_edited_after_the_release_makes_it_stale(release):
    (release.root / "appliance" / "version.py").write_text('APPLIANCE_VERSION = "9.9.9"\n')

    _result, derived = release.derive()

    assert derived["freshness"]["stale"] is True
    assert derived["physical_ready"] is False


@requires_gpg
def test_a_release_certifying_another_revision_is_stale(release):
    subprocess.run(
        ["git", "-C", str(release.root), "commit", "-q", "--allow-empty", "-m", "later work"],
        check=True, capture_output=True,
    )

    _result, derived = release.derive()

    assert derived["freshness"]["stale"] is True
    assert "certifies" in derived["freshness"]["detail"]
    assert derived["physical_ready"] is False


# --- runtime evidence --------------------------------------------------------


@requires_gpg
def test_a_release_with_no_runtime_evidence_is_not_ready(release):
    release.gates.unlink()

    _result, derived = release.derive()

    assert derived["runtime_gates"]["result"] == "not_run"
    assert derived["physical_ready"] is False


@requires_gpg
def test_a_required_runtime_gate_that_did_not_run_is_not_a_pass(release, tmp_path):
    release.gates = runtime_gate_evidence(tmp_path, results={"sftp": "not_run"})
    release.attestation = release.write_attestation()
    release.signature = release.key.sign(release.attestation)

    _result, derived = release.derive()

    assert derived["runtime_gates"]["result"] == "fail"
    assert derived["runtime_gates"]["unmet"] == ["sftp"]
    assert derived["physical_ready"] is False


def test_a_gate_that_did_not_run_has_to_name_its_prerequisite():
    with pytest.raises(runtime_gates.RuntimeGateError):
        runtime_gates.record("arm64_guest", "not_run")


def test_the_optional_arm64_guest_never_blocks_a_release():
    gates = runtime_gates.build(
        [runtime_gates.record(name, "pass") for name in runtime_gates.REQUIRED_GATES]
        + [
            runtime_gates.record(
                "arm64_guest", "not_run", reason="qemu-system-aarch64 is not installed"
            )
        ]
    )

    assert gates.required_pass is True
    assert gates.unmet == ()


# --- the readiness rule itself -----------------------------------------------


def test_every_named_invariant_alone_blocks_readiness():
    complete = dict.fromkeys(release_trust.KIT_READINESS_INVARIANTS, True)

    assert release_trust.readiness(
        complete, required=release_trust.KIT_READINESS_INVARIANTS
    ).ready is True
    for name in release_trust.KIT_READINESS_INVARIANTS:
        broken = dict(complete, **{name: False})
        verdict = release_trust.readiness(
            broken, required=release_trust.KIT_READINESS_INVARIANTS
        )
        assert verdict.ready is False
        assert verdict.unmet == (name,)


# --- the kit verifies itself, from the directory an operator carries ----------


def verify_kit(kit, *args):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/appliance_verify_hardware_kit.py"),
         "--kit", str(kit), *args],
        capture_output=True, text=True, check=False, timeout=300,
    )


@requires_gpg
def test_an_assembled_kit_verifies_itself_from_disk(release, tmp_path):
    release.assemble_kit()
    carried = tmp_path / "carried"
    shutil.copytree(release.kit, carried)

    outcome = verify_kit(
        carried, "--keyring", str(release.keyring),
        "--trusted-fingerprint", release.key.fingerprint,
        "--json", str(tmp_path / "kit-verification.json"),
    )

    report = json.loads((tmp_path / "kit-verification.json").read_text())
    assert outcome.returncode == 0, outcome.stdout + outcome.stderr
    assert report["physical_ready"] is True
    assert report["checksum_problems"] == []
    assert report["attestation"]["signature"]["trusted"] is True
    assert report["files_verified"] > 0


@requires_gpg
def test_a_kit_file_changed_after_assembly_is_refused(release, tmp_path):
    release.assemble_kit()
    carried = tmp_path / "carried"
    shutil.copytree(release.kit, carried)
    image = next(carried.rglob("*.img"))
    image.write_bytes(image.read_bytes() + b"one more byte")

    outcome = verify_kit(
        carried, "--keyring", str(release.keyring),
        "--trusted-fingerprint", release.key.fingerprint,
    )

    assert outcome.returncode == 1
    assert "hashes to" in outcome.stdout


@requires_gpg
def test_a_file_added_to_a_kit_after_assembly_is_refused(release, tmp_path):
    release.assemble_kit()
    carried = tmp_path / "carried"
    shutil.copytree(release.kit, carried)
    (carried / "flash-me-first.sh").write_text("#!/bin/sh\necho anything\n")

    outcome = verify_kit(
        carried, "--keyring", str(release.keyring),
        "--trusted-fingerprint", release.key.fingerprint,
    )

    assert outcome.returncode == 1
    assert "is named by nothing" in outcome.stdout


@requires_gpg
def test_a_kit_whose_signature_was_removed_is_refused(release, tmp_path):
    release.assemble_kit()
    carried = tmp_path / "carried"
    shutil.copytree(release.kit, carried)
    (carried / "release-attestation.json.asc").unlink()

    outcome = verify_kit(
        carried, "--keyring", str(release.keyring),
        "--trusted-fingerprint", release.key.fingerprint,
    )

    assert outcome.returncode == 1
    assert "attestation_signature_present" in outcome.stdout


@requires_gpg
def test_a_kit_verified_without_a_trust_policy_is_never_ready(release, tmp_path):
    release.assemble_kit()
    carried = tmp_path / "carried"
    shutil.copytree(release.kit, carried)

    outcome = verify_kit(carried)

    assert outcome.returncode == 1
    assert "trusted_signer" in outcome.stdout


@requires_gpg
def test_a_kit_carrying_a_private_key_is_refused(release, tmp_path):
    release.assemble_kit()
    carried = tmp_path / "carried"
    shutil.copytree(release.kit, carried)
    (carried / "operator-notes.txt").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nnot really\n"
    )

    outcome = verify_kit(
        carried, "--keyring", str(release.keyring),
        "--trusted-fingerprint", release.key.fingerprint,
    )

    assert outcome.returncode == 1
    assert "private key material" in outcome.stdout


# --- the finalizer refuses to sign without a trust policy --------------------


FINALIZER = ROOT / "scripts/appliance-finalize-rpi-release.sh"


def finalize(*args):
    return subprocess.run(
        ["sh", str(FINALIZER), *args], capture_output=True, text=True, check=False, timeout=300
    )


def test_the_finalizer_refuses_to_sign_without_a_trusted_fingerprint(tmp_path):
    keyring = tmp_path / "trusted.gpg"
    keyring.write_bytes(b"")

    outcome = finalize("--sign-key", "release@ems.invalid", "--keyring", str(keyring))

    assert outcome.returncode == 2
    assert "--trusted-fingerprint is required" in outcome.stderr


def test_the_finalizer_still_requires_a_keyring(tmp_path):
    outcome = finalize("--sign-key", "release@ems.invalid", "--trusted-fingerprint", "A" * 40)

    assert outcome.returncode == 2
    assert "--keyring is required" in outcome.stderr


def test_the_finalizer_binds_the_bundle_the_authority_and_the_parity_report():
    text = FINALIZER.read_text(encoding="utf-8")

    assert "appliance-check-source-bundle.sh\" --json" in text
    assert "parity_sha256" in text
    assert "authority_sha256" in text
    assert "bundle_sha256" in text


def test_the_finalizer_verifies_the_kit_it_just_assembled():
    text = FINALIZER.read_text(encoding="utf-8")

    assert "appliance_verify_hardware_kit.py" in text


# --- the runtime gate evidence is read out of the logs, never asserted -------


ASSEMBLER = ROOT / "scripts/appliance_runtime_gates.py"


def assemble_gates(tmp_path, *args):
    output = tmp_path / "runtime-gates.json"
    result = subprocess.run(
        [sys.executable, str(ASSEMBLER), "--output", str(output), *args],
        capture_output=True, text=True, check=False, timeout=120,
    )
    payload = json.loads(output.read_text()) if output.is_file() else {}
    return result, payload


def guest_log(tmp_path, name, verdict):
    target = tmp_path / f"{name}.log"
    target.write_text(f"  PASS  something\n\n{verdict}\n")
    return target


def test_the_verdict_comes_from_the_log_the_guest_wrote(tmp_path):
    logs = [
        f"--from-log={name}={guest_log(tmp_path, name, 'RESULT: PASS')}"
        for name in runtime_gates.REQUIRED_GATES
    ]

    result, payload = assemble_gates(tmp_path, *logs)

    assert result.returncode == 0, result.stdout + result.stderr
    assert payload["result"] == "pass"
    for name in runtime_gates.REQUIRED_GATES:
        assert payload["gates"][name]["result"] == "pass"
        assert payload["gates"][name]["evidence_sha256"].startswith("sha256:")


def test_a_guest_that_reported_not_run_is_not_a_pass(tmp_path):
    logs = [
        f"--from-log={name}="
        + str(guest_log(tmp_path, name, "RESULT: PASS" if name != "sftp" else "RESULT: NOT RUN (5)"))
        for name in runtime_gates.REQUIRED_GATES
    ]

    result, payload = assemble_gates(tmp_path, *logs)

    assert result.returncode == 1
    assert payload["gates"]["sftp"]["result"] == "not_run"
    assert payload["result"] == "fail"


def test_a_missing_log_is_not_run_with_the_prerequisite_named(tmp_path):
    result, payload = assemble_gates(tmp_path, "--from-log=sftp=" + str(tmp_path / "absent.log"))

    assert result.returncode == 1
    assert payload["gates"]["sftp"]["result"] == "not_run"
    assert "absent.log" in payload["gates"]["sftp"]["reason"]


def test_a_command_line_verdict_may_not_overrule_a_log(tmp_path):
    log = guest_log(tmp_path, "sftp", "RESULT: FAIL (1)")

    result, payload = assemble_gates(
        tmp_path, f"--from-log=sftp={log}", "--gate=sftp=pass:it works honestly"
    )

    assert result.returncode == 2
    assert payload == {}


def test_the_evidence_digest_changes_with_the_log(tmp_path):
    log = guest_log(tmp_path, "sftp", "RESULT: PASS")
    _first, before = assemble_gates(tmp_path, f"--from-log=sftp={log}")
    log.write_text(log.read_text() + "  PASS  one more thing\n\nRESULT: PASS\n")
    _second, after = assemble_gates(tmp_path, f"--from-log=sftp={log}")

    assert before["gates"]["sftp"]["evidence_sha256"] != after["gates"]["sftp"]["evidence_sha256"]
