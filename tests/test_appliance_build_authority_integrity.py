# SPDX-License-Identifier: AGPL-3.0-or-later
"""The binding between a build authority and the environment that produced it.

`builder_environment_sha256` is recomputed on every parse and compared against
what was recorded, so an environment edited after the build is caught. That
makes the hash load-bearing for every piece of release evidence in this
repository — and it makes any change to how the record is parsed or serialised
able to invalidate all of it at once, silently, until a signing run fails.

This is the net that has to be in place before the schema constant ever moves
again. It reads the real committed evidence rather than a fixture, because a
fixture written against the new code cannot tell you the old records still
verify.
"""

import json
from pathlib import Path

import pytest

from appliance import build_authority

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "appliance"

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

# The canonical hash of an all-empty environment. Recorded here so the gate-path
# authorities — which carry no environment at all — are asserted to be exactly
# that shape rather than merely "some hash".
EMPTY_ENVIRONMENT = "sha256:ff9d8b04d89fb6db15d4fca6de9101f3f402d6435112ab7a01f6e40dce154f7d"


def authority_files():
    return sorted(REPORTS.glob("**/build-authority*.json"))


def test_the_repository_still_carries_the_evidence_this_guards():
    """A net over nothing passes for the wrong reason."""

    assert len(authority_files()) >= 13


@pytest.mark.parametrize("path", authority_files(), ids=lambda p: p.parent.name + "/" + p.name)
def test_every_recorded_authority_still_parses_and_binds_its_environment(path):
    authority = build_authority.read(path)
    recorded = json.loads(path.read_text(encoding="utf-8"))

    assert authority.environment.canonical_hash == recorded["builder_environment_sha256"]


@pytest.mark.parametrize("path", authority_files(), ids=lambda p: p.parent.name + "/" + p.name)
def test_a_recorded_environment_round_trips_through_its_own_serialiser(path):
    """to_dict() must reproduce what was written, key for key.

    A serialiser that gains or loses a key changes the canonical hash of every
    record already committed, and the first thing to notice would be a release
    that cannot be signed.
    """

    recorded = json.loads(path.read_text(encoding="utf-8"))["builder_environment"]
    parsed = build_authority.parse_environment(recorded)

    assert parsed.to_dict() == recorded


def test_an_authority_with_no_environment_hashes_to_the_empty_record():
    """The gate path records none, and that shape is asserted, not assumed."""

    empty = build_authority.BuilderEnvironment()

    assert empty.canonical_hash == EMPTY_ENVIRONMENT
    assert empty.missing(), "an empty environment must be incomplete, not merely hashable"


def test_the_recorded_schema_version_survives_being_parsed():
    """Not inert: the constructor took the class default instead of the record.

    While one schema exists the two agree and nothing shows. The moment the
    constant moves, every committed record re-serialises under the new version,
    its canonical hash stops matching what was recorded, and all of the evidence
    above fails to parse at once — with the pressure fix being to delete the
    comparison that binds an authority to its environment.
    """

    recorded = json.loads(authority_files()[0].read_text(encoding="utf-8"))["builder_environment"]
    parsed = build_authority.parse_environment(recorded)

    assert parsed.schema_version == recorded["schema_version"]


@pytest.mark.parametrize("version", [True, False, 1.0, "1", None, [1]])
def test_a_schema_version_that_is_not_a_number_is_refused(version):
    """`True == 1` in Python, so a boolean would pass an equality check."""

    payload = {"schema_version": version, "base_image_lock_id": "builder:x"}

    with pytest.raises(build_authority.BuildAuthorityError):
        build_authority.parse_environment(payload)


def test_a_required_field_of_whitespace_is_not_a_recorded_fact():
    """`base_image_lock_id=" "` was "present" — a one-line fail-open."""

    blank = build_authority.BuilderEnvironment(
        base_image_lock_id="   ",
        base_image_sha512="\t",
        os_release="x",
        kernel="x",
        architecture="amd64",
        python_version="x",
        podman_version="x",
        mmdebstrap_version="x",
        binfmt_handler="x",
        dependency_manifest_sha256="x",
        captured_at="x",
    )

    assert "base_image_lock_id" in blank.missing()
    assert "base_image_sha512" in blank.missing()


# --- from recorded to checked ------------------------------------------------


def build_with(**overrides):
    from appliance.build_authority import BuilderEnvironment

    values = {
        "base_image_lock_id": "builder:debian-13-genericcloud-amd64-20260803-2559.qcow2",
        "base_image_sha512": json.loads(
            (ROOT / "packaging/appliance/vm/base-images.lock.json").read_text(encoding="utf-8")
        )["images"]["builder"]["sha512"],
        "os_release": "Debian GNU/Linux 13 (trixie)",
        "kernel": "Linux 6.12.100+deb13-cloud-amd64",
        "architecture": "x86_64",
        "python_version": "3.13.0",
        "podman_version": "5.0.0",
        "mmdebstrap_version": "1.5.0",
        "binfmt_handler": "qemu-aarch64 enabled /usr/libexec/qemu-binfmt/aarch64-binfmt-P",
        "dependency_manifest_sha256": "sha256:" + "a" * 64,
        "captured_at": "2026-08-09T00:00:00Z",
    }
    values.update(overrides)

    class Authority:
        environment = BuilderEnvironment(**values)

    return Authority()


def verify(authority):
    from appliance import release_inputs

    return release_inputs.verify_builder_environment(
        authority, lock=str(ROOT / "packaging/appliance/vm/base-images.lock.json")
    )


def test_a_builder_environment_matching_the_pinned_image_is_approved():
    assert verify(build_with()) == ()


@pytest.mark.parametrize(
    "kernel",
    [
        "Linux 6.12.95+deb13-amd64",       # a Debian workstation
        "Linux 6.11.0-1018-azure",          # a GitHub-hosted runner
        "Linux 6.12.100+deb13-cloud-arm64",  # the right flavour, wrong architecture
        "",
    ],
)
def test_a_kernel_the_pinned_image_never_booted_is_refused(kernel):
    """The one fact a builder somewhere else cannot reproduce by accident.

    Every other recorded value is either copied out of the lock or true of any
    Debian host. The running kernel belongs to the image that booted, and the
    genericcloud flavour belongs to the pinned artefact alone.
    """

    problems = verify(build_with(kernel=kernel))

    assert any("release policy approves" in problem for problem in problems)


def test_a_policy_row_with_no_expected_kernel_refuses_rather_than_exempts(tmp_path):
    """Otherwise deleting one line from the lock deletes the check."""

    from appliance import release_inputs

    lock = json.loads(
        (ROOT / "packaging/appliance/vm/base-images.lock.json").read_text(encoding="utf-8")
    )
    del lock["images"]["builder"]["kernel_pattern"]
    stripped = tmp_path / "base-images.lock.json"
    stripped.write_text(json.dumps(lock), encoding="utf-8")

    problems = release_inputs.verify_builder_environment(build_with(), lock=str(stripped))

    assert any("states no expected kernel" in problem for problem in problems)


def test_a_builder_that_registered_no_emulator_is_refused():
    """`none` is what the capture script writes when it finds no handler.

    It is truthy, so it satisfied the completeness check while recording the
    opposite of what completeness was meant to establish.
    """

    problems = verify(build_with(binfmt_handler="none"))

    assert any("registered no aarch64 binfmt handler" in problem for problem in problems)


def test_the_recorded_evidence_still_satisfies_both_new_checks():
    """The checks are derived from this evidence, so it must still pass."""

    from appliance import build_authority as ba

    approved = 0
    for path in authority_files():
        authority = ba.read(path)
        if not authority.environment.recorded:
            continue
        assert verify(authority) == (), path.name
        approved += 1

    assert approved == 10


# --- where approval is actually enforced -------------------------------------


def test_builder_approval_is_a_readiness_invariant_not_one_script_s_refusal():
    """It was enforced in exactly one place: the finalizer's pre-signature check.

    That refusal aborts the run it belongs to, so it protects the path that runs
    it and nothing else. A standalone kit, or a result re-derived over an
    existing dist, reached physical_ready with release policy never consulted.
    """

    from appliance import release_trust

    assert "builder_environment_approved" in release_trust.READINESS_INVARIANTS
    assert "builder_environment_approved" in release_trust.KIT_READINESS_INVARIANTS


@pytest.mark.parametrize(
    "script",
    [
        "appliance_release_result.py",
        "appliance_hardware_kit.py",
        "appliance_verify_hardware_kit.py",
    ],
)
def test_every_script_that_derives_readiness_answers_the_new_invariant(script):
    """An unanswered invariant is false, so a missed caller fails closed.

    It also makes physical_ready unreachable for that path, which is why each
    one has to answer rather than be left to default.
    """

    source = (ROOT / "scripts" / script).read_text(encoding="utf-8")

    assert "builder_environment_approved" in source
    assert "verify_builder_environment" in source


def test_a_kit_carries_the_policy_it_was_judged_against():
    """The verifier re-derives approval and must not take the manifest's word.

    A kit travels to a machine with no repository, so the lock travels with it.
    """

    assembler = (ROOT / "scripts" / "appliance_hardware_kit.py").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts" / "appliance_verify_hardware_kit.py").read_text(encoding="utf-8")

    assert "BUILDER_LOCK_NAME" in assembler and "shutil.copy2(builder_lock" in assembler
    assert "BUILDER_LOCK_NAME" in verifier
    assert "carries no" in verifier, "a kit with no policy must be unapproved, not unchecked"
