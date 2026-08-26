# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a published manager package may claim, and what the appliance believes.

Two things are deliberately not duplicated from the OS release path: the
signature is checked by the same verifier against the same keyring, and state
compatibility is decided by the same comparison against the same record. Two
trust anchors would be two things to rotate, and one of them would be forgotten.

What *is* new is that this artefact can be re-derived. The manifest records the
epoch, the compressor and the dpkg-deb that produced it, so a rebuild that
disagrees can be diagnosed instead of merely disbelieved.
"""

import json

import pytest

from appliance import manager_releases, persistent_state

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

DIGEST = "sha256:" + "a" * 64


def manifest(**overrides):
    payload = {
        "format_version": manager_releases.MANIFEST_FORMAT_VERSION,
        "package": "ems-appliance-manager",
        "version": "0.2.0",
        "architecture": "arm64",
        "build_id": "20260826010000",
        "created_at": "2026-08-26T01:00:00Z",
        "project_revision": "a" * 40,
        "artifact": {
            "name": "ems-appliance-manager_0.2.0_arm64.deb",
            "digest": DIGEST,
            "size_bytes": 368980,
        },
        "reproducibility": {
            "source_date_epoch": 1756000000,
            "dpkg_deb": "Debian dpkg-deb version 1.22.22.",
            "compression": "xz -6",
        },
        "state_schemas": manager_releases.implemented_state_schemas(),
    }
    payload.update(overrides)
    return payload


def test_a_well_formed_manifest_parses(tmp_path):
    release = manager_releases.parse_manifest(manifest())

    assert release.version == "0.2.0"
    assert release.artifact_digest == DIGEST
    assert release.state_implements == persistent_state.implemented_schemas()
    assert not release.signed, "nothing is signed until a signature says so"


def test_the_manifest_records_what_the_build_depended_on():
    """A rebuild that disagrees should be diagnosable, not merely disbelieved."""

    release = manager_releases.parse_manifest(manifest())

    assert release.source_date_epoch == 1756000000
    assert release.compression == "xz -6"
    assert release.dpkg_deb


@pytest.mark.parametrize(
    "field", ["format_version", "package", "version", "architecture", "artifact"]
)
def test_a_manifest_missing_a_required_field_is_refused(field):
    payload = manifest()
    del payload[field]

    with pytest.raises(manager_releases.ManagerReleaseError) as refusal:
        manager_releases.parse_manifest(payload)

    assert refusal.value.code == "manager_manifest_invalid"


def test_a_manifest_for_another_package_is_refused():
    with pytest.raises(manager_releases.ManagerReleaseError) as refusal:
        manager_releases.parse_manifest(manifest(package="something-else"))

    assert "ems-appliance-manager" in refusal.value.message


def test_a_hyphenated_version_is_refused_because_the_two_authorities_disagree():
    """dpkg reads `-rc1` as a revision above the release; this project ranks it below.

    A version the packaging and the appliance order differently cannot be
    ordered at all, so it is refused at the manifest rather than surfacing as a
    revert that installs the wrong direction.
    """

    with pytest.raises(manager_releases.ManagerReleaseError) as refusal:
        manager_releases.parse_manifest(manifest(version="0.2.0-rc1"))

    assert "~" in refusal.value.message


def test_the_tilde_form_is_accepted():
    assert manager_releases.parse_manifest(manifest(version="0.2.0~rc1")).version == "0.2.0~rc1"


@pytest.mark.parametrize(
    "artifact",
    [
        {"name": "x.deb", "digest": DIGEST, "size_bytes": 0},
        {"name": "x.deb", "digest": DIGEST, "size_bytes": "many"},
        {"name": "x.deb", "digest": "not-a-digest", "size_bytes": 10},
        {"name": "x.deb", "size_bytes": 10},
    ],
)
def test_an_artifact_that_cannot_be_checked_is_refused(artifact):
    with pytest.raises(manager_releases.ManagerReleaseError):
        manager_releases.parse_manifest(manifest(artifact=artifact))


@pytest.mark.parametrize(
    "block",
    [
        None,
        "not-an-object",
        {"implements": {}, "reads": {}},
        {"implements": {"ab_state": 1}},
        {"implements": {"ab_state": 0}, "reads": {"ab_state": 1}},
        {"implements": {"ab_state": 1}, "reads": {"other": 1}},
        {"implements": {"ab_state": 1}, "reads": {"ab_state": 2}},
    ],
)
def test_a_package_that_cannot_say_what_it_reads_is_refused(block):
    """Required here, unlike the OS manifest — no package has ever shipped.

    There is no history to be lenient towards, and leniency would mean taking a
    package that cannot say whether it can read this appliance's state.
    """

    with pytest.raises(manager_releases.ManagerReleaseError):
        manager_releases.parse_manifest(manifest(state_schemas=block))


# --- what the appliance does with it -----------------------------------------


def release(**overrides):
    return manager_releases.parse_manifest(manifest(**overrides))


def test_a_package_this_appliance_can_read_is_installable():
    problems = manager_releases.compatibility_problems(
        release(),
        architecture="arm64",
        state_schemas=persistent_state.implemented_schemas(),
    )

    assert problems == []


def test_a_package_for_another_architecture_is_refused():
    problems = manager_releases.compatibility_problems(
        release(architecture="amd64"),
        architecture="arm64",
        state_schemas=persistent_state.implemented_schemas(),
    )

    assert any(p["code"] == "package_architecture_unsupported" for p in problems)


def test_a_package_older_than_this_appliance_s_state_is_refused():
    """The question a version comparison could never answer."""

    ahead = {name: value + 1 for name, value in persistent_state.implemented_schemas().items()}

    problems = manager_releases.compatibility_problems(
        release(), architecture="arm64", state_schemas=ahead
    )

    assert any(p["code"] == "artifact_state_schema_too_old" for p in problems)


def test_going_backwards_is_not_refused_for_being_backwards():
    """An operator may deliberately install an older package.

    Refusing that would take away the recovery this path exists to provide, so
    the version is not consulted at all. The realistic revert is a step back
    across versions that share the state formats — schemas move far less often
    than versions do — and that is what must stay allowed.
    """

    problems = manager_releases.compatibility_problems(
        release(version="0.0.9"),
        architecture="arm64",
        state_schemas=persistent_state.implemented_schemas(),
    )

    assert problems == []


def test_a_revert_across_a_schema_the_older_manager_cannot_read_is_refused():
    """The limit of the freedom above, stated so it is not mistaken for absolute.

    A package that implements less than the disk holds is refused on the axis it
    is behind on, whichever direction the version number went.
    """

    older = manager_releases.implemented_state_schemas()
    older["implements"] = {k: max(1, v - 1) for k, v in older["implements"].items()}
    older["reads"] = {k: min(v, older["implements"][k]) for k, v in older["reads"].items()}

    problems = manager_releases.compatibility_problems(
        release(version="0.0.9", state_schemas=older),
        architecture="arm64",
        state_schemas=persistent_state.implemented_schemas(),
    )

    assert any(p["code"] == "artifact_state_schema_too_old" for p in problems)


def test_an_appliance_that_cannot_say_what_its_state_is_refuses_everything():
    problems = manager_releases.compatibility_problems(
        release(), architecture="arm64", state_schemas=None
    )

    assert [p["code"] for p in problems] == ["state_schemas_unrecorded"]


def test_the_artifact_on_disk_must_be_the_one_the_manifest_names(tmp_path):
    import hashlib

    body = b"a package"
    target = tmp_path / "x.deb"
    target.write_bytes(body)
    digest = "sha256:" + hashlib.sha256(body).hexdigest()

    good = release(artifact={"name": "x.deb", "digest": digest, "size_bytes": len(body)})
    assert manager_releases.verify_artifact(good, target)

    bad = release(artifact={"name": "x.deb", "digest": DIGEST, "size_bytes": len(body)})
    with pytest.raises(manager_releases.ManagerReleaseError) as refusal:
        manager_releases.verify_artifact(bad, target)
    assert refusal.value.code == "manager_artifact_corrupt"


def test_a_manifest_file_that_is_not_there_is_named_rather_than_crashing(tmp_path):
    with pytest.raises(manager_releases.ManagerReleaseError) as refusal:
        manager_releases.read_manifest(tmp_path / "absent.json")

    assert refusal.value.code == "manager_manifest_missing"


def test_the_manifest_round_trips_through_its_own_serialiser(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest()), encoding="utf-8")

    parsed = manager_releases.read_manifest(path)
    again = manager_releases.parse_manifest(
        {**manifest(), "state_schemas": parsed.to_dict()["state_schemas"]}
    )

    assert again.state_implements == parsed.state_implements


def test_one_keyring_verifies_both_artefact_classes():
    """Two trust anchors would be two things to rotate, and one gets forgotten."""

    from appliance import os_releases

    assert manager_releases.VERIFIED_SIGNATURE == os_releases.VERIFIED_SIGNATURE
    source = (
        __import__("pathlib").Path(manager_releases.__file__).read_text(encoding="utf-8")
    )
    assert "SignatureVerifier" not in source, (
        "the manager path must reuse the OS release verifier, not define a second one"
    )


# --- an identifier that reaches a filesystem path ---------------------------


def test_a_build_id_that_is_not_an_identifier_is_refused():
    """It reaches a filesystem path, so it may not be free text.

    ``prepare_revert`` and the release scripts both name files after it. A
    signed manifest declaring ``../../tmp/escape`` would have staged a package
    outside the directory this appliance keeps them in — behind a signature,
    but this project validates identifiers rather than trusting the publisher
    to have been careful.
    """

    for hostile in ("../../tmp/escape", "a/b", "with space", "", "-leading"):
        with pytest.raises(manager_releases.ManagerReleaseError) as refusal:
            manager_releases.parse_manifest(
                manifest(build_id=hostile), release_id="ems-appliance-manager-0.2.0-arm64"
            )
        assert refusal.value.code == "manager_manifest_invalid", hostile


def test_an_ordinary_build_id_is_accepted():
    release = manager_releases.parse_manifest(
        manifest(build_id="20260826010000"), release_id="ems-appliance-manager-0.2.0-arm64"
    )

    assert release.build_id == "20260826010000"
