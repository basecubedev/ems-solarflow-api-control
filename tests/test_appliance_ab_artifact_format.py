# SPDX-License-Identifier: AGPL-3.0-or-later
"""The real rpi-image-gen update artifact, fed through the real runtime path.

``image-rota``'s ``post-image.sh`` produces ``update.tar.zst`` holding exactly
two members, ``boot`` and ``system``. These tests build an archive the same way
— ``tar -I zstd`` over those two names — and put it through the project's own
manifest parser, member allowlist and extraction, so the format the appliance
will actually receive is the format that is tested.

The zstd part is not incidental. Python's ``tarfile`` gained zstd support in
3.14 and the appliance runs 3.13, so an artifact this shape would have been
unreadable on hardware while every fixture built from an uncompressed tar
passed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from appliance import os_artifacts, os_releases, rpi_image_gen, sparse
from appliance.os_artifacts import ArtifactError
from tests.helpers import android_sparse, appliance_ab_filesystems as filesystems

pytestmark = [pytest.mark.integration, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# What image-rota actually packs: each payload wrapped in an Android Sparse
# container, symlinked to "boot" and "system" and tarred with -h.
BOOT_FS = filesystems.fat_image(8 * 1024 * 1024)
SYSTEM_FS = filesystems.ext4_image(16 * 1024 * 1024)
BOOT_CHUNKS = android_sparse.image_of(BOOT_FS, tail_blocks=16)
SYSTEM_CHUNKS = android_sparse.image_of(SYSTEM_FS, tail_blocks=64)
BOOT = android_sparse.build(BOOT_CHUNKS)
SYSTEM = android_sparse.build(SYSTEM_CHUNKS)
BOOT_EXPANDED = android_sparse.expanded(BOOT_CHUNKS)
SYSTEM_EXPANDED = android_sparse.expanded(SYSTEM_CHUNKS)

requires_zstd = pytest.mark.skipif(
    shutil.which("zstd") is None or shutil.which("tar") is None,
    reason="zstd and tar are required to build the real artifact format",
)


def digest_of(payload):
    import hashlib

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def build_upstream_archive(directory, *, members=None):
    """An archive packed exactly the way image-rota's post-image.sh packs one."""

    members = members or {"boot": BOOT, "system": SYSTEM}
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in members.items():
        (directory / name).write_bytes(payload)
    archive = directory / "update.tar.zst"
    subprocess.run(
        ["tar", "-I", "zstd", "-cf", str(archive), "-C", str(directory), *sorted(members)],
        check=True,
        timeout=300,
    )
    return archive


def manifest_for(archive, *, members=None):
    members = members or {"boot": BOOT, "system": SYSTEM}
    return {
        "format_version": os_releases.MANIFEST_FORMAT_VERSION,
        "release_version": "1.5.0",
        "build_id": "20260807-1",
        "created_at": "2026-08-07T00:00:00Z",
        "architecture": "arm64",
        "device_layer": "rpi4",
        "compatible_hardware": ["pi4"],
        "os_release": "Raspberry Pi OS Trixie arm64",
        "image_layer": "image-rota",
        "project_revision": "abc1234",
        "appliance_manager_version": "0.9.0",
        "minimum_appliance_manager_version": "0.9.0",
        "layout_id": "ems-appliance-rota-v1",
        "slot_schema_version": 2,
        "persistent_schema_version": 2,
        "archive": {
            "name": archive.name,
            "digest": os_releases.file_digest(archive),
            "size_bytes": archive.stat().st_size,
            "compression": "zstd",
        },
        "members": {
            "boot": _member(members["boot"], role="boot", filesystem="vfat"),
            "system": _member(members["system"], role="root", filesystem="ext4"),
        },
    }


def _member(encoded, *, role, filesystem):
    """Both identities of one member, as a format 2 manifest declares them."""

    expanded = {BOOT: BOOT_EXPANDED, SYSTEM: SYSTEM_EXPANDED}.get(encoded, encoded)
    return {
        "role": role,
        "encoding": sparse.ENCODING_ANDROID_SPARSE,
        "encoded_sha256": digest_of(encoded),
        "expanded_sha256": digest_of(expanded),
        "expanded_size": len(expanded),
        "filesystem": filesystem,
    }


# --- the pinned contract ------------------------------------------------------


def test_the_lock_and_the_parser_agree_on_the_member_names():
    lock = rpi_image_gen.read_lock()

    assert tuple(lock.update_members) == os_releases.REQUIRED_MEMBERS
    assert os_releases.REQUIRED_MEMBERS == ("boot", "system")


# --- the real archive ---------------------------------------------------------


@requires_zstd
def test_a_real_upstream_archive_parses_and_extracts(tmp_path):
    archive = build_upstream_archive(tmp_path / "work")
    release = os_releases.parse_manifest(manifest_for(archive), release_id="r-1")

    staged = os_artifacts.extract(archive, tmp_path / "staging", release)

    assert sorted(staged.members) == ["boot", "system"]
    assert staged.path("boot").read_bytes() == BOOT
    assert staged.path("system").read_bytes() == SYSTEM


@requires_zstd
def test_a_zstd_archive_is_not_copied_out_before_it_is_read(tmp_path):
    """A multi-gigabyte artifact must not need a second copy to be opened."""

    archive = build_upstream_archive(tmp_path / "work")

    with os_artifacts.open_archive(archive) as handle:
        names = []
        while True:
            member = handle.next()
            if member is None:
                break
            names.append(member.name)

    assert sorted(names) == ["boot", "system"]


@requires_zstd
def test_a_member_whose_digest_does_not_match_is_refused(tmp_path):
    archive = build_upstream_archive(tmp_path / "work")
    payload = manifest_for(archive)
    payload["members"]["system"]["encoded_sha256"] = digest_of(b"something else")
    release = os_releases.parse_manifest(payload, release_id="r-1")

    with pytest.raises(ArtifactError) as caught:
        os_artifacts.extract(archive, tmp_path / "staging", release)

    assert caught.value.code == "artifact_member_digest_mismatch"


@requires_zstd
def test_an_archive_carrying_an_unexpected_member_is_refused(tmp_path):
    archive = build_upstream_archive(
        tmp_path / "work", members={"boot": BOOT, "system": SYSTEM, "extra": b"x"}
    )
    release = os_releases.parse_manifest(manifest_for(archive), release_id="r-1")

    with pytest.raises(ArtifactError) as caught:
        os_artifacts.extract(archive, tmp_path / "staging", release)

    assert caught.value.code == "artifact_member_refused"


@requires_zstd
def test_a_refused_archive_leaves_no_staging_directory(tmp_path):
    archive = build_upstream_archive(
        tmp_path / "work", members={"boot": BOOT, "system": SYSTEM, "extra": b"x"}
    )
    release = os_releases.parse_manifest(manifest_for(archive), release_id="r-1")
    staging = tmp_path / "staging"

    with pytest.raises(ArtifactError):
        os_artifacts.extract(archive, staging, release)

    assert not staging.exists()


def test_an_archive_that_is_not_zstd_still_reads(tmp_path):
    """A plain tar remains readable, so a local build is not a special case."""

    work = tmp_path / "work"
    work.mkdir()
    (work / "boot").write_bytes(BOOT)
    (work / "system").write_bytes(SYSTEM)
    archive = tmp_path / "update.tar"
    subprocess.run(
        ["tar", "-cf", str(archive), "-C", str(work), "boot", "system"], check=True, timeout=120
    )
    release = os_releases.parse_manifest(manifest_for(archive), release_id="r-1")

    staged = os_artifacts.extract(archive, tmp_path / "staging", release)

    assert sorted(staged.members) == ["boot", "system"]


# --- the release scripts ------------------------------------------------------


@requires_zstd
def test_the_build_script_describes_a_real_upstream_artifact(tmp_path):
    """The manifest is written for upstream's archive, never for a repack."""

    archive = build_upstream_archive(tmp_path / "work")
    output = tmp_path / "out"
    output.mkdir()
    shutil.copy(archive, output / "update.tar.zst")

    result = subprocess.run(
        [
            "sh",
            str(SCRIPTS / "appliance-build-rpi-ab-update.sh"),
            "--output",
            str(output),
            "--update",
            str(output / "update.tar.zst"),
            "--build-id",
            "20260807-1",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = next(output.glob("*.manifest.json"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert sorted(payload["members"]) == ["boot", "system"]
    boot = payload["members"]["boot"]
    assert boot["encoded_sha256"] == digest_of(BOOT)
    assert boot["expanded_sha256"] == digest_of(BOOT_EXPANDED)
    assert boot["expanded_size"] == len(BOOT_EXPANDED)
    assert boot["encoding"] == "android_sparse"
    assert payload["archive"]["compression"] == "zstd"


@requires_zstd
def test_the_build_script_refuses_an_archive_that_is_not_upstreams(tmp_path):
    archive = build_upstream_archive(
        tmp_path / "work", members={"rootfs": SYSTEM, "bootfs": BOOT}
    )
    output = tmp_path / "out"
    output.mkdir()
    shutil.copy(archive, output / "update.tar.zst")

    result = subprocess.run(
        [
            "sh",
            str(SCRIPTS / "appliance-build-rpi-ab-update.sh"),
            "--output",
            str(output),
            "--update",
            str(output / "update.tar.zst"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 1
    assert "image-rota produces" in result.stdout + result.stderr


@requires_zstd
def test_the_inspect_script_accepts_the_artifact_the_build_script_described(tmp_path):
    archive = build_upstream_archive(tmp_path / "work")
    output = tmp_path / "out"
    output.mkdir()
    shutil.copy(archive, output / "update.tar.zst")
    subprocess.run(
        [
            "sh",
            str(SCRIPTS / "appliance-build-rpi-ab-update.sh"),
            "--output",
            str(output),
            "--update",
            str(output / "update.tar.zst"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    manifest = next(output.glob("*.manifest.json"))

    result = subprocess.run(
        ["sh", str(SCRIPTS / "appliance-inspect-rpi-ab-update.sh"), "--json", str(manifest)],
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary["result"] == "pass"
    checks = {finding["check"]: finding["result"] for finding in summary["findings"]}
    assert checks["members_are_upstreams"] == "pass"
    assert checks["members_extract_and_verify"] == "pass"
    # Unsigned artifacts are reported, never quietly accepted as release-ready.
    assert checks["detached_signature"] == "not_run"


def test_the_inspect_script_reports_a_missing_artifact_as_not_run(tmp_path):
    result = subprocess.run(
        ["sh", str(SCRIPTS / "appliance-inspect-rpi-ab-update.sh"), str(tmp_path / "absent.json")],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 3
    assert "NOT RUN (artifact_unavailable)" in result.stderr


# --- what a partition ends up holding -----------------------------------------


@requires_zstd
def test_the_expanded_members_are_the_filesystems_the_manifest_names(tmp_path):
    """The whole chain, end to end: zstd, tar, sparse, filesystem."""

    archive = build_upstream_archive(tmp_path / "work")
    release = os_releases.parse_manifest(manifest_for(archive), release_id="r-1")
    staged = os_artifacts.extract(archive, tmp_path / "staging", release)

    for name, expected in (("boot", "vfat"), ("system", "ext4")):
        member = release.member(name)
        assert sparse.is_sparse(staged.path(name))
        report = sparse.expand(
            staged.path(name),
            tmp_path / f"{name}.img",
            expected_size=member.expanded_size,
            expected_digest=member.expanded_digest,
        )
        blob = (tmp_path / f"{name}.img").read_bytes()
        assert report.digest == member.expanded_digest
        assert filesystems.filesystem_of(blob) == expected == member.filesystem


def test_the_boot_filesystem_is_a_real_fat_when_mtools_is_available(tmp_path):
    reason = filesystems.unavailable("vfat")
    if reason:
        pytest.skip(f"a real FAT filesystem cannot be built here: {reason}")

    image = tmp_path / "boot.vfat"
    image.write_bytes(filesystems.fat_image(8 * 1024 * 1024, files={"config.txt": b"arm_64bit=1\n"}))
    listing = subprocess.run(
        [shutil.which("mdir"), "-i", str(image), "::"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "MTOOLS_SKIP_CHECK": "1"},
    )

    assert listing.returncode == 0, listing.stderr
    assert "config" in listing.stdout.lower()


def test_the_system_filesystem_is_a_real_ext4_when_e2fsprogs_is_available(tmp_path):
    reason = filesystems.unavailable("ext4")
    if reason:
        pytest.skip(f"a real ext4 filesystem cannot be built here: {reason}")

    image = tmp_path / "system.ext4"
    image.write_bytes(filesystems.ext4_image(16 * 1024 * 1024))

    assert filesystems.filesystem_of(image.read_bytes()) == "ext4"
