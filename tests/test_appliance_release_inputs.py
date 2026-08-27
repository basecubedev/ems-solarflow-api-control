# SPDX-License-Identifier: AGPL-3.0-or-later
"""Three artefacts that each validate, and whether they describe one release.

Every gap here was reachable with individually valid inputs:

- a build from commit A signed beside a source bundle from commit B, because
  nothing compared the two;
- ``package_sha256`` recorded by the builder and never compared against a
  package again, so the digest was a note rather than a claim;
- a structurally perfect build authority from a builder base image nobody
  approved, because the authority was only ever checked against itself.

The .deb reader is exercised against a real archive built here rather than a
fixture, because "can this parse a Debian package" is the whole point of it.
"""

import json
import tarfile
import time
from pathlib import Path

import pytest

from appliance import build_authority, release_inputs

pytestmark = [pytest.mark.unit, pytest.mark.system_build, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
BUILDER_LOCK = ROOT / "packaging/appliance/vm/base-images.lock.json"

REVISION_A = "a" * 40
REVISION_B = "b" * 40
TREE_A = "sha256:" + "1" * 64
TREE_B = "sha256:" + "2" * 64


# --- fixtures ---------------------------------------------------------------


def make_deb(
    tmp_path,
    *,
    name="ems-appliance-manager",
    version="0.1.0",
    architecture="arm64",
    control_compression="xz",
):
    """A real ar archive with a real compressed control tarball inside it."""

    stanza = (
        f"Package: {name}\n"
        f"Version: {version}\n"
        f"Architecture: {architecture}\n"
        "Maintainer: EMS <ems@example.invalid>\n"
        "Description: a test package\n"
    ).encode()

    control_dir = tmp_path / f"control-{name}-{version}-{architecture}"
    control_dir.mkdir(exist_ok=True)
    (control_dir / "control").write_bytes(stanza)
    suffix = {"xz": ".xz", "gz": ".gz", "none": ""}[control_compression]
    mode = {"xz": "w:xz", "gz": "w:gz", "none": "w"}[control_compression]
    control_tar = tmp_path / f"control{suffix}.tar"
    with tarfile.open(control_tar, mode) as handle:
        handle.add(control_dir / "control", arcname="./control")
    control_bytes = control_tar.read_bytes()

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "payload").write_text("x")
    data_tar = tmp_path / "data.tar"
    with tarfile.open(data_tar, "w") as handle:
        handle.add(data_dir / "payload", arcname="./usr/bin/ems-appliance")
    data_bytes = data_tar.read_bytes()

    def member(member_name, payload):
        header = (
            f"{member_name:<16}{int(time.time()):<12}{0:<6}{0:<6}{100644:<8}"
            f"{len(payload):<10}"
        ).encode("ascii") + b"\x60\n"
        assert len(header) == 60, len(header)
        return header + payload + (b"\n" if len(payload) % 2 else b"")

    target = tmp_path / f"{name}_{version}_{architecture}.deb"
    target.write_bytes(
        b"!<arch>\n"
        + member("debian-binary", b"2.0\n")
        + member(f"control.tar{suffix}", control_bytes)
        + member("data.tar", data_bytes)
    )
    return target


def authority(tmp_path, *, package_sha256="", revision=REVISION_A, tree=TREE_A):
    return build_authority.BuildAuthority(
        builder=build_authority.Builder(
            source_form="tarball", revision="c" * 40, source_tree_sha256="sha256:" + "3" * 64
        ),
        project=build_authority.Project(revision=revision, tree_sha256=tree),
        profile="rpi5",
        build_id="20260809135018",
        package_sha256=package_sha256,
        completed=True,
    )


def source_authority(*, revision=REVISION_A, tree=TREE_A, bundle_sha256="sha256:" + "4" * 64):
    return release_inputs.SourceBundleAuthority(
        bundle_sha256=bundle_sha256,
        revision=revision,
        tree_sha256=tree,
        tracked_objects=1142,
        symlinks=6,
        created_at="2026-08-09T20:00:00Z",
    )


# --- the source bundle binds to the build ------------------------------------


def test_a_bundle_and_a_build_from_one_commit_are_accepted():
    assert release_inputs.verify_source_matches_build(
        authority(None), source_authority()
    ) == ()


def test_a_build_from_one_commit_and_a_bundle_from_another_are_refused():
    """The regression: both artefacts validate, and they are not one release."""

    problems = release_inputs.verify_source_matches_build(
        authority(None, revision=REVISION_A, tree=TREE_A),
        source_authority(revision=REVISION_B, tree=TREE_B),
    )

    assert problems
    assert all(release_inputs.SOURCE_BUILD_MISMATCH in problem for problem in problems)


def test_the_same_commit_with_a_different_tree_is_still_refused():
    """A revision names a commit; it says nothing about the files delivered."""

    problems = release_inputs.verify_source_matches_build(
        authority(None, revision=REVISION_A, tree=TREE_A),
        source_authority(revision=REVISION_A, tree=TREE_B),
    )

    assert len(problems) == 1
    assert release_inputs.SOURCE_BUILD_MISMATCH in problems[0]
    assert "tree" in problems[0]


def test_the_same_tree_under_a_different_commit_is_still_refused():
    problems = release_inputs.verify_source_matches_build(
        authority(None, revision=REVISION_A, tree=TREE_A),
        source_authority(revision=REVISION_B, tree=TREE_A),
    )

    assert len(problems) == 1
    assert "project" in problems[0]


def test_an_archive_that_is_not_the_one_the_authority_describes_is_refused(tmp_path):
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"not the archive that was measured")

    problems = release_inputs.verify_source_bundle(source_authority(), bundle)

    assert problems
    assert release_inputs.SOURCE_BUILD_MISMATCH in problems[0]


def test_a_source_authority_round_trips_through_its_own_file(tmp_path):
    target = tmp_path / "src.source-authority.json"
    release_inputs.write_source_bundle_authority(target, source_authority())

    assert release_inputs.read_source_bundle_authority(target) == source_authority()


def test_a_source_authority_of_another_schema_is_refused(tmp_path):
    target = tmp_path / "src.source-authority.json"
    target.write_text(json.dumps({"schema_version": 99, "project": {}}))

    with pytest.raises(release_inputs.ReleaseInputError) as error:
        release_inputs.read_source_bundle_authority(target)

    assert error.value.code == release_inputs.UNSUPPORTED


# --- the package binds to the build ------------------------------------------


def test_a_real_debian_package_is_read_without_dpkg(tmp_path):
    package = make_deb(tmp_path)

    record = release_inputs.read_package(package)

    assert record.name == "ems-appliance-manager"
    assert record.version == "0.1.0"
    assert record.architecture == "arm64"
    assert record.sha256.startswith("sha256:")


@pytest.mark.parametrize("compression", ["xz", "gz", "none"])
def test_every_control_compression_a_build_produces_is_readable(tmp_path, compression):
    package = make_deb(tmp_path, control_compression=compression)

    assert release_inputs.read_package(package).name == "ems-appliance-manager"


def test_the_shipped_package_is_a_real_archive_this_reader_understands():
    """Against the .deb in dist/, not a fixture: the reader has to read those."""

    candidates = sorted((ROOT / "dist").glob("ems-appliance-manager_*_arm64.deb"))
    if not candidates:
        pytest.skip("no built arm64 package in dist/")

    record = release_inputs.read_package(candidates[0])

    assert record.name == "ems-appliance-manager"
    assert record.architecture == "arm64"


def test_the_exact_package_the_build_recorded_is_accepted(tmp_path):
    package = make_deb(tmp_path)
    digest = release_inputs.file_sha256(package)

    assert (
        release_inputs.verify_package(
            authority(tmp_path, package_sha256=digest),
            package,
            name="ems-appliance-manager",
            version="0.1.0",
            architecture="arm64",
        )
        == ()
    )


def test_a_package_the_build_did_not_produce_is_refused(tmp_path):
    """The digest stops being a note the moment something compares it."""

    built = make_deb(tmp_path, version="0.1.0")
    other = make_deb(tmp_path, version="0.2.0")

    problems = release_inputs.verify_package(
        authority(tmp_path, package_sha256=release_inputs.file_sha256(built)), other
    )

    assert problems
    assert release_inputs.PACKAGE_MISMATCH in problems[0]


def test_a_bare_hex_digest_and_a_prefixed_one_are_the_same_claim(tmp_path):
    package = make_deb(tmp_path)
    digest = release_inputs.file_sha256(package)

    assert (
        release_inputs.verify_package(
            authority(tmp_path, package_sha256=digest.removeprefix("sha256:")), package
        )
        == ()
    )


@pytest.mark.parametrize(
    "field,value",
    [("name", "ems-appliance-manager-dev"), ("version", "0.2.0"), ("architecture", "amd64")],
)
def test_the_right_bytes_under_the_wrong_identity_are_refused(tmp_path, field, value):
    package = make_deb(tmp_path, **{field: value})
    digest = release_inputs.file_sha256(package)

    problems = release_inputs.verify_package(
        authority(tmp_path, package_sha256=digest),
        package,
        name="ems-appliance-manager",
        version="0.1.0",
        architecture="arm64",
    )

    assert problems
    assert release_inputs.PACKAGE_MISMATCH in problems[0]


def test_a_build_that_records_no_package_digest_cannot_be_finalized(tmp_path):
    package = make_deb(tmp_path)

    problems = release_inputs.verify_package(authority(tmp_path, package_sha256=""), package)

    assert any(release_inputs.UNREADABLE in problem for problem in problems)


def test_something_that_is_not_a_package_is_refused(tmp_path):
    imposter = tmp_path / "not.deb"
    imposter.write_bytes(b"this is not an ar archive")

    problems = release_inputs.verify_package(
        authority(tmp_path, package_sha256="sha256:" + "0" * 64), imposter
    )

    assert any(release_inputs.UNREADABLE in problem for problem in problems)


# --- the builder binds to release policy -------------------------------------


def environment(**overrides):
    lock = json.loads(BUILDER_LOCK.read_text())["images"]["builder"]
    fields = {
        "base_image_lock_id": f"builder:{lock['filename']}",
        "base_image_sha512": lock["sha512"],
        "os_release": "debian 13",
        "architecture": "x86_64",
        "kernel": "Linux 6.12.100+deb13-cloud-amd64",
        "python_version": "Python 3.13.5",
        "podman_version": "podman version 5.4.2",
        "mmdebstrap_version": "mmdebstrap 1.5.7",
        "binfmt_handler": "qemu-aarch64 enabled",
        "dependency_manifest_sha256": "sha256:" + "5" * 64,
        "captured_at": "2026-08-09T13:50:16Z",
    }
    fields.update(overrides)
    return build_authority.BuilderEnvironment(**fields)


def built_by(**overrides):
    base = authority(None)
    return build_authority.BuildAuthority(
        builder=base.builder,
        project=base.project,
        profile=base.profile,
        build_id=base.build_id,
        package_sha256=base.package_sha256,
        completed=True,
        environment=environment(**overrides),
    )


def test_the_approved_builder_image_is_accepted():
    assert release_inputs.verify_builder_environment(built_by(), lock=BUILDER_LOCK) == ()


def test_the_real_recorded_builder_environment_is_approved():
    """Against the environment the real rpi5 build actually captured."""

    recorded = ROOT / "reports/appliance/2026-08-09-rc/build-authority-rpi5.json"
    if not recorded.is_file():
        pytest.skip("no recorded build authority to check")

    problems = release_inputs.verify_builder_environment(
        build_authority.read(recorded), lock=BUILDER_LOCK
    )

    assert problems == (), problems


def test_a_builder_image_release_policy_does_not_name_is_untrusted():
    problems = release_inputs.verify_builder_environment(
        built_by(base_image_lock_id="builder:debian-13-genericcloud-amd64-something-else.qcow2"),
        lock=BUILDER_LOCK,
    )

    assert problems
    assert release_inputs.BUILDER_UNTRUSTED in problems[0]


def test_an_approved_name_carrying_an_unapproved_digest_is_untrusted():
    """A structurally valid authority is not evidence about its own builder."""

    problems = release_inputs.verify_builder_environment(
        built_by(base_image_sha512="f" * 128), lock=BUILDER_LOCK
    )

    assert problems
    assert release_inputs.BUILDER_UNTRUSTED in problems[0]


def test_a_builder_on_another_distribution_is_untrusted():
    problems = release_inputs.verify_builder_environment(
        built_by(os_release="ubuntu 24.04"), lock=BUILDER_LOCK
    )

    assert problems
    assert release_inputs.BUILDER_UNTRUSTED in problems[0]


def test_a_builder_on_another_debian_release_is_untrusted():
    problems = release_inputs.verify_builder_environment(
        built_by(os_release="debian 12"), lock=BUILDER_LOCK
    )

    assert problems


def test_a_builder_on_another_architecture_is_untrusted():
    problems = release_inputs.verify_builder_environment(
        built_by(architecture="aarch64"), lock=BUILDER_LOCK
    )

    assert problems
    assert release_inputs.BUILDER_UNTRUSTED in problems[0]


def test_the_kernels_name_for_an_architecture_and_debians_are_one_fact():
    assert release_inputs.verify_builder_environment(
        built_by(architecture="amd64"), lock=BUILDER_LOCK
    ) == ()


def test_a_role_release_policy_does_not_approve_at_all_is_untrusted(tmp_path):
    empty = tmp_path / "lock.json"
    empty.write_text(json.dumps({"lock_version": 1, "images": {}}))

    problems = release_inputs.verify_builder_environment(built_by(), lock=empty)

    assert problems
    assert release_inputs.BUILDER_UNTRUSTED in problems[0]
