# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two questions that used to share one verdict.

The strict gate could print ``RESULT: PASS`` while ``sign-rpi4`` and
``sign-rpi5`` both read NOT RUN, because the signature gate was deliberately
optional: an unsigned rehearsal is a legitimate thing to run, and a disposable
builder guest is the wrong place for a production key. The consequence was that
the only verdict the project could produce called an unsigned set of artefacts a
passed release.

So the two questions are separated. Builder qualification asks whether this
source on this builder produces an image and an update that inspect cleanly,
and its best answer is PASS (builder qualification). Production finalization
asks whether the artefacts already built are a release, builds nothing, and
cannot reach PASS without a signature that verifies against a trusted key.
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "scripts/appliance-release-gates.sh"
FINALIZER = ROOT / "scripts/appliance-finalize-rpi-release.sh"
BUILDER_VM = ROOT / "scripts/appliance-builder-vm.sh"


def source(path):
    return path.read_text(encoding="utf-8")


def run_gates(*args, cwd=None):
    return subprocess.run(
        ["sh", str(GATES), *args], capture_output=True, text=True, check=False, cwd=cwd
    )


def test_the_gate_has_exactly_two_modes():
    text = source(GATES)

    assert "--mode builder|production" in text
    assert 'case "$MODE" in' in text


def test_an_unknown_mode_is_refused(tmp_path):
    result = run_gates("--mode", "release", "--output", str(tmp_path))

    assert result.returncode == 2
    assert "builder or production" in result.stderr


def test_builder_qualification_never_calls_itself_a_release():
    text = source(GATES)

    assert "RESULT: PASS (builder qualification" in text
    assert "This is not a release" in text


def test_a_production_pass_names_itself_as_one():
    text = source(GATES)

    assert "RESULT: PASS (production release" in text


def test_signing_is_required_in_production_and_optional_in_a_rehearsal():
    text = source(GATES)
    required = text[text.index("required_gate()") : text.index("report() {")]
    builder, production = required.split("production)")

    assert "sign-" not in builder
    assert "sign-*" in production
    assert "verify-signature-*" in production


def test_production_requires_the_checks_a_rehearsal_may_skip():
    text = source(GATES)
    required = text[text.index("required_gate()") : text.index("report() {")]
    production = required.split("production)")[1]

    for gate in ("inspect-image-*", "inspect-update-*", "crosscheck-*", "source-bundle"):
        assert gate in production, gate


def test_production_does_not_require_a_generator_checkout():
    """A finalizer builds nothing, so it has no rpi-image-gen tree to check.

    What binds the upstream source in production is the build authority, which
    the finalizer verifies before it signs anything. Requiring the checkout too
    would mean putting the generator on the signing host to prove something the
    builder already proved.
    """

    text = source(GATES)
    required = text[text.index("required_gate()") : text.index("report() {")]
    builder, production = required.split("production)")
    accepted = [
        line for line in production.splitlines() if "return 0" in line and "|" in line or
        ("return 0" in line and ")" in line)
    ]

    assert any("source-authority" in line for line in builder.splitlines() if "return 0" in line)
    assert not any("source-authority" in line for line in accepted)
    assert not any("slot-mounts" in line for line in accepted)
    assert "the build authority" in production


def test_production_mode_builds_nothing(tmp_path):
    """A release that rebuilt its artefacts is not the qualified build."""

    result = run_gates("--mode", "production", "--output", str(tmp_path), "--profile", "rpi5")

    assert "artefacts-rpi5" in result.stdout
    assert "build-rpi5" not in result.stdout
    assert result.returncode == 3
    assert "RESULT: NOT RUN" in result.stdout


def test_production_mode_without_artefacts_is_not_a_pass(tmp_path):
    result = run_gates("--mode", "production", "--output", str(tmp_path))

    assert "RESULT: PASS" not in result.stdout
    assert result.returncode == 3


def test_an_unsupported_profile_is_refused_before_anything_runs(tmp_path):
    result = run_gates("--profile", "rpi6", "--output", str(tmp_path))

    assert result.returncode == 2
    assert "build_identifier_invalid" in result.stdout + result.stderr


def test_the_inspection_evidence_is_written_as_json_reports():
    text = source(GATES)

    assert "gate_json" in text
    assert "image-inspection-$profile.json" in text
    assert "update-inspection-$profile.json" in text


def test_the_builder_guest_runs_qualification_and_never_signs():
    """A key reachable from the disposable guest is a key anyone can sign with."""

    text = source(BUILDER_VM)

    assert "--sign-key" not in text
    assert "gpg --" not in text


def test_the_finalizer_signs_and_never_builds():
    text = source(FINALIZER)

    assert "--sign-key" in text
    assert "appliance-build-rpi-ab-image.sh" not in text
    assert "rpi-image-gen build" not in text
    assert "--mode production" in text


def test_the_finalizer_requires_a_keyring_and_verifies_what_it_signed(tmp_path):
    result = subprocess.run(
        ["sh", str(FINALIZER), "--sign-key", "ABC"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--keyring is required" in result.stderr


def test_the_finalizer_refuses_a_keyring_that_is_not_there(tmp_path):
    result = subprocess.run(
        [
            "sh",
            str(FINALIZER),
            "--sign-key",
            "ABC",
            "--keyring",
            str(tmp_path / "absent.gpg"),
            "--dist",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "keyring_missing" in result.stderr


def test_the_finalizer_never_copies_the_private_key():
    text = source(FINALIZER)

    assert "never copied" in text
    assert not re.search(r"cp\s+[^\n]*secring|cp\s+[^\n]*\.key", text)


def test_the_finalizer_assembles_the_kit_only_after_the_gate_passed():
    text = source(FINALIZER)
    gate_index = text.index("appliance-release-gates.sh")
    kit_index = text.index("appliance-hardware-validation-kit.sh")

    assert gate_index < kit_index
    assert "release_gate_failed" in text[gate_index:kit_index]


# --- a gate has to say what it proved ---------------------------------------


def test_every_required_gate_declares_what_it_establishes():
    """"docker_reconstruction: pass" reads as "the EMS containers were proven".
    That gate exercises Docker's save/load/pull mechanics against contract
    stand-ins; the project's real arm64 images run in it nowhere. A gate whose
    name promises more than its evidence is how a release status comes to rest
    on something nobody checked."""

    from appliance import runtime_gates

    for name in runtime_gates.REQUIRED_GATES:
        assert name in runtime_gates.GATE_SCOPES, name
        assert runtime_gates.GATE_SCOPES[name].strip(), name


def test_the_container_gate_names_the_stand_ins_it_used():
    from appliance import runtime_gates

    scope = runtime_gates.GATE_SCOPES["docker_reconstruction"]

    assert "contract" in scope.lower() or "stand-in" in scope.lower()


def test_the_scope_travels_with_the_evidence():
    """A scope only in a source comment is one no reader of the release sees."""

    from appliance import runtime_gates

    record = runtime_gates.record("docker_reconstruction", "pass", environment="test")

    assert record.to_dict()["scope"] == runtime_gates.GATE_SCOPES["docker_reconstruction"]
