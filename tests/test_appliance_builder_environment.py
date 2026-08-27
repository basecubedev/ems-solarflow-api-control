# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which machine built a release, and what a release identifier may contain.

Build provenance bound both source trees and neither builder. Two identical
trees assembled on different machines are not the same supply chain: mmdebstrap
decides what the root filesystem contains, podman runs the foreign-architecture
stages, and whether an aarch64 binfmt handler was registered decides whether
those stages ran at all. A release that cannot name its builder cannot be
diagnosed afterwards, so schema 3 carries a bounded builder environment and
production signing requires it.

The identifiers are the other half. A build id names a directory
(``build-<id>``) and travels through release scripts as an argument, and the
build script interpolated it straight into a ``python3 -c`` program. Both are
validated against a fixed shape now, and the values reach Python as argv
members instead of as program text.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from appliance import build_authority

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts/appliance-capture-builder-environment.sh"
BUILD_IMAGE = ROOT / "scripts/appliance-build-rpi-image.sh"
BUILD_UPDATE = ROOT / "scripts/appliance-build-rpi-image.sh"


def complete_environment(**overrides):
    values = {
        "base_image_lock_id": "builder:20260803-2559",
        "base_image_sha512": "7695626" + "0" * 121,
        "os_release": "debian 13",
        "kernel": "Linux 6.12.43+deb13-amd64",
        "architecture": "x86_64",
        "python_version": "Python 3.13.5",
        "podman_version": "podman version 5.4.2",
        "mmdebstrap_version": "mmdebstrap 1.5.7",
        "qemu_version": "qemu-aarch64 version 9.2.4",
        "binfmt_handler": "qemu-aarch64 enabled /usr/libexec/qemu-binfmt/aarch64-binfmt-P",
        "dependency_manifest_sha256": "sha256:" + "ab" * 32,
        "critical_packages": ("mmdebstrap 1.5.7", "podman 5.4.2"),
        "captured_at": "2026-08-09T09:00:00Z",
    }
    values.update(overrides)
    return build_authority.BuilderEnvironment(**values)


def authority_with(environment, tmp_path):
    artefact = tmp_path / "image.img"
    artefact.write_bytes(b"an image")
    return build_authority.BuildAuthority(
        builder=build_authority.Builder(
            source_form="git", revision="a" * 40, source_tree_sha256="sha256:" + "b" * 64
        ),
        project=build_authority.Project(
            revision="c" * 40, tree_sha256="sha256:" + "d" * 64
        ),
        profile="rpi5",
        build_id="20260809120000",
        image=build_authority.Artefact(
            path=str(artefact), sha256=build_authority.file_sha256(artefact)
        ),
        package_sha256="e" * 64,
        completed=True,
        environment=environment,
    ), artefact


@pytest.mark.parametrize("profile", ["rpi4", "rpi5"])
def test_a_supported_profile_is_accepted(profile):
    assert build_authority.validate_profile(profile) == profile


@pytest.mark.parametrize(
    "profile",
    ["", "rpi6", "RPI5", "rpi5 ", "../rpi5", "rpi5;touch /tmp/x", "rpi5\nrpi4", "rpi5\x00"],
)
def test_an_unsupported_profile_is_refused(profile):
    with pytest.raises(build_authority.BuildAuthorityError) as raised:
        build_authority.validate_profile(profile)

    assert raised.value.code == build_authority.INVALID_IDENTIFIER


@pytest.mark.parametrize("build_id", ["20260809120000", "rc-1.2.3", "a", "A_b.c-1", "x" * 96])
def test_a_usable_build_id_is_accepted(build_id):
    assert build_authority.validate_build_id(build_id) == build_id


@pytest.mark.parametrize(
    "build_id",
    [
        "",
        " ",
        "../escape",
        "/absolute",
        "a/b",
        "a b",
        "a\tb",
        "a\nb",
        "a\x00b",
        "a$(id)",
        "a;touch /tmp/x",
        "a'b",
        '"a',
        "-leading",
        ".leading",
        "x" * 97,
    ],
)
def test_a_build_id_that_could_name_another_path_is_refused(build_id):
    with pytest.raises(build_authority.BuildAuthorityError) as raised:
        build_authority.validate_build_id(build_id)

    assert raised.value.code == build_authority.INVALID_IDENTIFIER


def test_a_build_directory_cannot_be_claimed_outside_the_output_root(tmp_path):
    with pytest.raises(build_authority.BuildAuthorityError) as raised:
        build_authority.prepare_output(tmp_path, build_id="../../etc")

    assert raised.value.code == build_authority.INVALID_IDENTIFIER
    assert not (tmp_path.parent.parent / "build-../../etc").exists()


def test_the_authority_carries_the_builder_environment_and_its_hash(tmp_path):
    environment = complete_environment()
    authority, _ = authority_with(environment, tmp_path)

    payload = authority.to_dict()

    assert payload["schema_version"] == 3
    assert payload["builder_environment"]["kernel"] == environment.kernel
    assert payload["builder_environment_sha256"] == environment.canonical_hash


def test_the_environment_hash_is_independent_of_key_order(tmp_path):
    one = complete_environment()
    other = build_authority.parse_environment(json.loads(json.dumps(one.to_dict())))

    assert other.canonical_hash == one.canonical_hash


def test_an_environment_edited_after_the_build_is_detected(tmp_path):
    authority, _ = authority_with(complete_environment(), tmp_path)
    payload = authority.to_dict()
    payload["builder_environment"]["kernel"] = "Linux 1.0-someone-elses-builder"

    with pytest.raises(build_authority.BuildAuthorityError) as raised:
        build_authority.parse(payload)

    assert raised.value.code == build_authority.MISMATCH


def test_production_signing_requires_a_complete_builder_environment(tmp_path):
    authority, image = authority_with(build_authority.BuilderEnvironment(), tmp_path)

    unsigned = build_authority.verify_image(authority, image)
    signing = build_authority.verify_image(authority, image, require_environment=True)

    assert unsigned == ()
    assert any("builder environment" in problem for problem in signing)


@pytest.mark.parametrize("absent", build_authority.ENVIRONMENT_REQUIRED)
def test_every_required_builder_identity_is_missing_loudly(tmp_path, absent):
    environment = complete_environment(**{absent: ""})
    authority, image = authority_with(environment, tmp_path)

    problems = build_authority.verify_image(authority, image, require_environment=True)

    assert any(absent in problem for problem in problems)


def test_a_complete_environment_satisfies_production_signing(tmp_path):
    authority, image = authority_with(complete_environment(), tmp_path)

    assert build_authority.verify_image(authority, image, require_environment=True) == ()


def test_an_authority_naming_an_unknown_profile_is_refused(tmp_path):
    authority, image = authority_with(complete_environment(), tmp_path)
    authority = build_authority.BuildAuthority(
        builder=authority.builder,
        project=authority.project,
        profile="rpi6",
        build_id=authority.build_id,
        image=authority.image,
        package_sha256=authority.package_sha256,
        completed=True,
        environment=authority.environment,
    )

    problems = build_authority.verify_image(authority, image)

    assert any(build_authority.INVALID_IDENTIFIER in problem for problem in problems)


def test_the_capture_script_records_a_bounded_identity_and_no_environment(tmp_path):
    target = tmp_path / "builder-environment.json"

    result = subprocess.run(
        [
            "sh",
            str(CAPTURE),
            "--output",
            str(target),
            "--base-image-lock-id",
            "builder:20260803-2559",
            "--base-image-sha512",
            "0" * 128,
            "--depends",
            str(ROOT / "packaging/appliance/image/rpi-image-gen.lock"),
            "--captured-at",
            "2026-08-09T09:00:00Z",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "EMS_SECRET_TOKEN": "hunter2"},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema_version",
        "base_image_lock_id",
        "base_image_sha512",
        "os_release",
        "kernel",
        "architecture",
        "python_version",
        "podman_version",
        "mmdebstrap_version",
        "qemu_version",
        "binfmt_handler",
        "dependency_manifest_sha256",
        "critical_packages",
        "captured_at",
    }
    assert "hunter2" not in target.read_text(encoding="utf-8")
    assert payload["captured_at"] == "2026-08-09T09:00:00Z"
    assert payload["dependency_manifest_sha256"].startswith("sha256:")
    build_authority.parse_environment(payload)


@pytest.mark.parametrize(
    ("script", "argument"),
    [
        (BUILD_IMAGE, ["--profile", "rpi5; touch /tmp/ems-injected"]),
        (BUILD_IMAGE, ["--build-id", "../escape"]),
        (BUILD_IMAGE, ["--build-id", "x'); import os; os.system('touch /tmp/ems-injected"]),
        (BUILD_UPDATE, ["--profile", "../../etc"]),
        (BUILD_UPDATE, ["--build-id", "a b"]),
    ],
)
def test_a_release_script_refuses_an_identifier_that_could_name_something_else(
    script, argument, tmp_path
):
    marker = Path("/tmp/ems-injected")
    before = marker.exists()

    result = subprocess.run(
        ["sh", str(script), *argument, "--output", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert build_authority.INVALID_IDENTIFIER in result.stdout + result.stderr
    assert marker.exists() == before


def test_the_build_script_never_interpolates_a_caller_value_into_a_python_program():
    text = BUILD_IMAGE.read_text(encoding="utf-8")

    assert "python3 -c" not in text
    for interpolated in ("'$CONFIG'", "'$BUILD_ID'", "'$OUTPUT'", "'$PROFILE'"):
        assert interpolated not in text


def test_the_capture_script_is_reachable_from_the_builder_guest():
    builder = (ROOT / "scripts/appliance-builder-vm.sh").read_text(encoding="utf-8")

    assert "appliance-capture-builder-environment.sh" in builder


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
