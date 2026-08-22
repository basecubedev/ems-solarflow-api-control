# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which OS artifact this appliance is allowed to install, and why not.

Two rules shape every test here. An artifact is only installable when a signature
this appliance's own keyring verified says so — presence in a directory is not
authority. And the browser names a release; it never names a URL, a path, a
device, a key or a checksum, because a request that could supply any of those
could supply an image of its own choosing.
"""

import json

import pytest

from appliance import os_artifacts, os_releases
from appliance.ab_layout import parse_layout_manifest
from appliance.os_artifacts import ArtifactError
from appliance.os_releases import VERIFIED_DEVELOPMENT, VERIFIED_SIGNATURE, ReleaseError
from appliance.ab_persistence import PERSISTENT_SCHEMA_VERSION
from tests.helpers.appliance_ab import layout_manifest
from tests.helpers.appliance_ab_artifacts import (
    BOARD,
    BOOT,
    ROOT,
    ReleaseDirectory,
    digest_of,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


@pytest.fixture
def releases(tmp_path):
    return ReleaseDirectory(tmp_path)


@pytest.fixture
def layout():
    return parse_layout_manifest(layout_manifest())


# --- signature authority -----------------------------------------------------


def test_a_signed_release_resolves(releases):
    release_id = releases.publish()

    release = releases.catalogue().get(release_id)

    assert release.verified == VERIFIED_SIGNATURE
    assert release.signed is True
    assert release.release_version == "1.5.0"
    assert release.archive_digest.startswith("sha256:")


def test_an_unsigned_release_is_refused(releases):
    release_id = releases.publish()
    releases.unsign(release_id)

    with pytest.raises(ReleaseError) as caught:
        releases.catalogue().get(release_id)

    assert caught.value.code == "release_signature_missing"


def test_a_signature_that_does_not_verify_is_refused(releases):
    release_id = releases.publish()
    releases.gpg_ok = False

    with pytest.raises(ReleaseError) as caught:
        releases.catalogue().get(release_id)

    assert caught.value.code == "release_signature_invalid"


def test_a_missing_keyring_refuses_every_release(releases):
    release_id = releases.publish()

    with pytest.raises(ReleaseError) as caught:
        releases.catalogue(keyring="").get(release_id)

    assert caught.value.code == "release_keyring_missing"


def test_the_development_override_is_never_a_signature(releases):
    """It exists for a bench. Nothing may read it as a release-gate pass."""

    release_id = releases.publish()
    releases.unsign(release_id)

    release = releases.catalogue(allow_unsigned=True).get(release_id)

    assert release.verified == VERIFIED_DEVELOPMENT
    assert release.signed is False


def test_the_verification_call_names_the_configured_keyring_only(releases):
    release_id = releases.publish()
    catalogue = releases.catalogue()

    catalogue.get(release_id)

    tool, args, _ = catalogue.runner.calls[0]
    assert tool == "gpgv"
    assert args.count("--keyring") == 1
    assert args[args.index("--keyring") + 1] == str(releases.keyring_path)
    # gpgv verifies and nothing else, so the trailing pair is the whole subject:
    # a detached signature over the manifest it is supposed to cover.
    assert args[-1].endswith(".manifest.json")
    assert args[-2] == f"{args[-1]}.asc"


def test_an_unknown_release_id_is_refused(releases):
    releases.publish()

    with pytest.raises(ReleaseError) as caught:
        releases.catalogue().get("something-else")

    assert caught.value.code == "unknown_release"


@pytest.mark.parametrize(
    "value", ["../etc/passwd", "/tmp/evil", "release id", "a" * 200, ""]
)
def test_a_release_id_that_is_not_an_identifier_is_refused(value):
    with pytest.raises(ReleaseError) as caught:
        os_releases.validate_release_id(value)

    assert caught.value.code == "invalid_release_id"


def test_a_listing_skips_what_it_cannot_verify(releases):
    good = releases.publish("good-1.5.0")
    releases.publish("bad-1.6.0")
    releases.unsign("bad-1.6.0")

    available = [item.release_id for item in releases.catalogue().available()]

    assert available == [good]


# --- the manifest ------------------------------------------------------------


def test_a_manifest_of_another_format_is_refused(releases):
    release_id = releases.publish()
    releases.rewrite_manifest(release_id, lambda payload: payload.update(format_version=99))

    with pytest.raises(ReleaseError) as caught:
        releases.catalogue().get(release_id)

    assert caught.value.code == "release_manifest_unsupported"


@pytest.mark.parametrize(
    "field",
    ["release_version", "build_id", "architecture", "layout_id", "archive", "members"],
)
def test_a_manifest_missing_a_required_field_is_refused(releases, field):
    release_id = releases.publish()
    releases.rewrite_manifest(release_id, lambda payload: payload.pop(field))

    with pytest.raises(ReleaseError) as caught:
        releases.catalogue().get(release_id)

    assert caught.value.code == "release_manifest_invalid"


def test_a_manifest_carrying_key_material_is_refused(releases):
    release_id = releases.publish()
    releases.rewrite_manifest(
        release_id, lambda payload: payload.update(signing_key="-----BEGIN PRIVATE KEY-----")
    )

    with pytest.raises(ReleaseError) as caught:
        releases.catalogue().get(release_id)

    assert caught.value.code == "release_manifest_invalid"
    assert "signing_key" in caught.value.message


def test_a_manifest_declaring_extra_members_is_refused(releases):
    release_id = releases.publish()

    def add_member(payload):
        payload["members"]["postinst.sh"] = {"digest": digest_of(b""), "role": "script"}

    releases.rewrite_manifest(release_id, add_member)

    with pytest.raises(ReleaseError) as caught:
        releases.catalogue().get(release_id)

    assert caught.value.code == "release_manifest_invalid"


def test_a_digest_that_is_not_sha256_is_refused(releases):
    release_id = releases.publish()
    releases.rewrite_manifest(
        release_id, lambda payload: payload["archive"].update(digest="md5:abc")
    )

    with pytest.raises(ReleaseError) as caught:
        releases.catalogue().get(release_id)

    assert caught.value.code == "artifact_digest_invalid"


# --- the archive itself ------------------------------------------------------


def test_an_archive_whose_digest_does_not_match_is_refused(releases):
    release_id = releases.publish()
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)
    archive = catalogue.archive_path(release)
    archive.write_bytes(archive.read_bytes() + b"tamper")

    with pytest.raises(ReleaseError) as caught:
        catalogue.verify_archive(release, archive)

    assert caught.value.code == "artifact_digest_mismatch"


def test_a_verified_archive_stages_every_declared_member(releases, tmp_path):
    release_id = releases.publish()
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)
    archive = catalogue.archive_path(release)
    catalogue.verify_archive(release, archive)

    staged = os_artifacts.extract(archive, tmp_path / "staging", release)

    assert sorted(staged.members) == ["boot", "system"]
    assert staged.path("system").read_bytes() == ROOT
    assert staged.path("boot").read_bytes() == BOOT


def test_a_staged_member_is_root_only(releases, tmp_path):
    release_id = releases.publish()
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)

    staged = os_artifacts.extract(catalogue.archive_path(release), tmp_path / "staging", release)

    assert staged.directory.stat().st_mode & 0o777 == 0o700
    assert staged.path("system").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "../../etc/passwd",
        "subdir/root.img",
        "boot/../../root.img",
    ],
    ids=["absolute", "traversal", "nested", "traversing_nested"],
)
def test_a_member_that_chooses_where_it_lands_is_refused(releases, tmp_path, name):
    release_id = releases.publish(archive_members={name: ROOT})
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)

    with pytest.raises(ArtifactError) as caught:
        os_artifacts.extract(catalogue.archive_path(release), tmp_path / "staging", release)

    assert caught.value.code == "artifact_member_refused"
    assert not (tmp_path / "staging").exists()


def test_an_archive_containing_a_link_is_refused(releases, tmp_path):
    import tarfile

    release_id = releases.publish()
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)
    archive = catalogue.archive_path(release)
    with tarfile.open(archive, "w") as handle:
        info = tarfile.TarInfo("system")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/shadow"
        handle.addfile(info)

    with pytest.raises(ArtifactError) as caught:
        os_artifacts.extract(archive, tmp_path / "staging", release)

    assert caught.value.code == "artifact_member_refused"


def test_an_archive_containing_a_device_node_is_refused(releases, tmp_path):
    import tarfile

    release_id = releases.publish()
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)
    archive = catalogue.archive_path(release)
    with tarfile.open(archive, "w") as handle:
        info = tarfile.TarInfo("system")
        info.type = tarfile.BLKTYPE
        handle.addfile(info)

    with pytest.raises(ArtifactError) as caught:
        os_artifacts.extract(archive, tmp_path / "staging", release)

    assert caught.value.code == "artifact_member_refused"


def test_a_member_whose_content_does_not_match_its_digest_is_refused(releases, tmp_path):
    release_id = releases.publish(
        blobs={"boot": BOOT, "system": b"different" * 100}
    )
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)

    with pytest.raises(ArtifactError) as caught:
        os_artifacts.extract(catalogue.archive_path(release), tmp_path / "staging", release)

    assert caught.value.code == "artifact_member_digest_mismatch"
    assert not (tmp_path / "staging").exists()


def test_an_archive_missing_a_declared_member_is_refused(releases, tmp_path):
    release_id = releases.publish(archive_members={"system": ROOT})
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)

    with pytest.raises(ArtifactError) as caught:
        os_artifacts.extract(catalogue.archive_path(release), tmp_path / "staging", release)

    assert caught.value.code == "artifact_member_missing"


def test_a_member_larger_than_the_bound_is_refused(releases, tmp_path, monkeypatch):
    monkeypatch.setattr(os_artifacts, "MAX_MEMBER_BYTES", 8)
    release_id = releases.publish()
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)

    with pytest.raises(ArtifactError) as caught:
        os_artifacts.extract(catalogue.archive_path(release), tmp_path / "staging", release)

    assert caught.value.code == "artifact_member_refused"


def test_too_many_members_are_refused(releases, tmp_path, monkeypatch):
    monkeypatch.setattr(os_artifacts, "MAX_MEMBERS", 1)
    release_id = releases.publish()
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)

    with pytest.raises(ArtifactError) as caught:
        os_artifacts.extract(catalogue.archive_path(release), tmp_path / "staging", release)

    assert caught.value.code == "artifact_member_refused"


def test_staging_starts_from_an_empty_directory(releases, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "leftover.img").write_bytes(b"from an earlier attempt")
    release_id = releases.publish()
    catalogue = releases.catalogue()
    release = catalogue.get(release_id)

    staged = os_artifacts.extract(catalogue.archive_path(release), staging, release)

    assert sorted(item.name for item in staged.directory.iterdir()) == [
        "boot",
        "system",
    ]


# --- compatibility -----------------------------------------------------------


def problems(release, layout, **overrides):
    values = {
        "layout": layout,
        "board": BOARD,
        "appliance_version": "0.9.0",
        "persistent_schema_version": PERSISTENT_SCHEMA_VERSION,
        "current_build_id": "20260801-1",
    }
    values.update(overrides)
    return [entry["code"] for entry in os_releases.compatibility_problems(release, **values)]


def test_a_compatible_artifact_has_no_problems(releases, layout):
    release = releases.catalogue().get(releases.publish())

    assert problems(release, layout) == []


def test_a_foreign_architecture_is_refused(releases, layout):
    release_id = releases.publish(manifest_overrides={"architecture": "armhf"})
    release = releases.catalogue().get(release_id)

    assert "artifact_architecture_unsupported" in problems(release, layout)


def test_an_artifact_that_does_not_list_this_board_is_refused(releases, layout):
    release_id = releases.publish(
        manifest_overrides={"compatible_hardware": ("cm4",)}
    )
    release = releases.catalogue().get(release_id)

    assert "artifact_hardware_incompatible" in problems(release, layout)


def test_an_artifact_for_another_layout_is_refused(releases, layout):
    release_id = releases.publish(manifest_overrides={"layout_id": "some-other-layout"})
    release = releases.catalogue().get(release_id)

    assert "artifact_layout_unknown" in problems(release, layout)


def test_an_unknown_slot_schema_is_refused(releases, layout):
    release_id = releases.publish(manifest_overrides={"slot_schema_version": 9})
    release = releases.catalogue().get(release_id)

    assert "artifact_slot_schema_unknown" in problems(release, layout)


def test_a_persistent_schema_this_appliance_does_not_implement_is_refused(releases, layout):
    release_id = releases.publish(manifest_overrides={"persistent_schema_version": 4})
    release = releases.catalogue().get(release_id)

    assert "artifact_persistent_schema_too_new" in problems(release, layout)


def test_an_artifact_needing_a_newer_appliance_manager_is_refused(releases, layout):
    release_id = releases.publish(
        manifest_overrides={"minimum_appliance_manager_version": "9.0.0"}
    )
    release = releases.catalogue().get(release_id)

    assert "appliance_manager_too_old" in problems(release, layout)


def test_the_running_build_is_refused_unless_it_is_an_explicit_repair(releases, layout):
    release_id = releases.publish(manifest_overrides={"build_id": "20260801-1"})
    release = releases.catalogue().get(release_id)

    assert "artifact_already_active" in problems(release, layout)
    assert problems(release, layout, repair=True) == []


def test_an_older_release_is_not_a_normal_update(releases, layout):
    release_id = releases.publish(manifest_overrides={"release_version": "1.0.0"})
    release = releases.catalogue().get(release_id)

    problem = os_releases.downgrade_problem(release, current_version="1.4.0")

    assert problem["code"] == "artifact_older_than_current"
    assert os_releases.downgrade_problem(release, current_version="1.4.0", rollback=True) is None


def test_the_release_projection_survives_json(releases):
    release = releases.catalogue().get(releases.publish())

    payload = json.loads(json.dumps(release.to_dict()))

    assert payload["signed"] is True
    assert payload["members"]["system"]["role"] == "root"
    assert "keyring" not in payload


def test_readiness_reports_a_missing_release_keyring(tmp_path):
    """The keyring path is configured by default and shipped by no package, so
    without this the appliance looks update-ready and refuses every artifact at
    the last moment instead of saying up front that it can verify nothing.
    """

    from tests.helpers.appliance import build_test_services

    services = build_test_services(tmp_path)
    readiness = services.os_update.status()["readiness"]

    assert readiness["release_keyring_ready"] is False


def test_readiness_accepts_a_keyring_that_is_present(tmp_path):
    from tests.helpers.appliance import build_test_services

    keyring = tmp_path / "os-release-keyring.gpg"
    keyring.write_bytes(b"\x99 a public key")
    services = build_test_services(tmp_path)
    services.os_update.config = services.os_update.config.__class__(
        **{**services.os_update.config.__dict__, "os_release_keyring": str(keyring)}
    )

    assert services.os_update.status()["readiness"]["release_keyring_ready"] is True
