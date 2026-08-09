# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether an archive of this repository is still this repository.

Persistence activation depends on six symlinks tracked in git:

    packaging/appliance/image/layer/ems-appliance.rootfs-overlay/
        etc/systemd/system/local-fs.target.wants/*.mount

They are what makes each generated bind mount actually mount. Both archives
produced for the last independent review arrived without them — every link had
become a regular file — and an image built from such a tree would generate six
mount units, activate none of them, and lose every write to the shared paths at
the next slot switch. Silently.

Whether that was the packaging or the transport is beside the point: a delivery
path that can drop a symlink and still look complete is the defect. So the
bundle is compared against ``git ls-tree`` object by object — content, file
mode, symlink mode and symlink target — and anything that does not round-trip
is a failure rather than a note.
"""

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from appliance import source_bundle

pytestmark = [pytest.mark.integration, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

WANTS = (
    "packaging/appliance/image/layer/ems-appliance.rootfs-overlay/"
    "etc/systemd/system/local-fs.target.wants"
)

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required to enumerate the tracked tree"
)
requires_tar = pytest.mark.skipif(
    shutil.which("tar") is None, reason="tar is required to build a source bundle"
)


def git_archive(destination, *, ref="HEAD"):
    """A faithful bundle of the tracked tree."""

    subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", "-o", str(destination), ref],
        check=True,
        timeout=600,
    )
    return destination


def flattened_archive(destination, *, ref="HEAD"):
    """The same bundle with every symlink turned into a regular file.

    This is what ``tar -h`` and several archive tools do, and what both review
    archives arrived as. It is produced here rather than by ``tar -h`` because
    these links are dangling by design — they point at units systemd's generator
    writes at boot — and ``tar -h`` refuses a dangling link outright.
    """

    plain = destination.parent / "plain.tar"
    git_archive(plain, ref=ref)
    with tarfile.open(plain) as source, tarfile.open(destination, "w") as target:
        for member in source.getmembers():
            if not member.issym():
                target.addfile(
                    member, source.extractfile(member) if member.isfile() else None
                )
                continue
            payload = member.linkname.encode("utf-8")
            member.type = tarfile.REGTYPE
            member.linkname = ""
            member.size = len(payload)
            member.mode = 0o644
            target.addfile(member, io.BytesIO(payload))
    return destination


# --- the tracked tree itself --------------------------------------------------


@requires_git
def test_the_repository_tracks_six_persistence_activation_links():
    entries = source_bundle.tracked_entries(ROOT, ref="HEAD")
    links = [
        entry
        for entry in entries
        if entry.path.startswith(f"{WANTS}/") and entry.kind == source_bundle.SYMLINK
    ]

    assert len(links) == 6, sorted(entry.path for entry in links)
    for entry in links:
        assert entry.path.endswith(".mount")


@requires_git
def test_the_activation_links_point_at_the_generated_units():
    """Each link activates a unit the slot-shared generator writes at boot."""

    for entry in source_bundle.tracked_entries(ROOT, ref="HEAD"):
        if entry.kind == source_bundle.SYMLINK and entry.path.startswith(f"{WANTS}/"):
            assert entry.target.startswith("/run/systemd/generator/"), entry.path
            assert entry.target.endswith(".mount"), entry.path


# --- finding 10: an archive that drops a link is a failure --------------------


@requires_git
@requires_tar
def test_a_faithful_bundle_matches_the_tracked_tree(tmp_path):
    archive = git_archive(tmp_path / "bundle.tar")

    report = source_bundle.verify(archive, root=ROOT, ref="HEAD")

    assert report.ok, report.problems[:10]
    assert report.missing == ()
    assert report.mismatched == ()


@requires_git
@requires_tar
def test_a_bundle_that_dereferenced_its_symlinks_fails(tmp_path):
    """Exactly the shape both review archives arrived in."""

    archive = flattened_archive(tmp_path / "flattened.tar")

    report = source_bundle.verify(archive, root=ROOT, ref="HEAD")

    assert not report.ok
    dropped = {path for path, _reason in report.mismatched if path.startswith(f"{WANTS}/")}
    assert len(dropped) == 6, sorted(dropped)
    assert all("symlink" in reason for path, reason in report.mismatched if path in dropped)


@requires_git
@requires_tar
def test_a_bundle_missing_a_tracked_file_fails(tmp_path):
    archive = git_archive(tmp_path / "bundle.tar")
    trimmed = tmp_path / "trimmed.tar"
    with tarfile.open(archive) as source, tarfile.open(trimmed, "w") as target:
        for member in source.getmembers():
            if member.name.endswith("appliance/ab_bootstrap.py"):
                continue
            target.addfile(member, source.extractfile(member) if member.isfile() else None)

    report = source_bundle.verify(trimmed, root=ROOT, ref="HEAD")

    assert not report.ok
    assert any(path.endswith("appliance/ab_bootstrap.py") for path in report.missing)


@requires_git
@requires_tar
def test_a_bundle_that_lost_an_executable_bit_fails(tmp_path):
    archive = git_archive(tmp_path / "bundle.tar")
    stripped = tmp_path / "stripped.tar"
    with tarfile.open(archive) as source, tarfile.open(stripped, "w") as target:
        for member in source.getmembers():
            if member.name.endswith("scripts/appliance-check-rpi-image-gen.sh"):
                member.mode = 0o644
            target.addfile(member, source.extractfile(member) if member.isfile() else None)

    report = source_bundle.verify(stripped, root=ROOT, ref="HEAD")

    assert not report.ok
    assert any("mode" in reason for _path, reason in report.mismatched)


@requires_git
@requires_tar
def test_a_bundle_that_changed_a_files_content_fails(tmp_path):
    archive = git_archive(tmp_path / "bundle.tar")
    edited = tmp_path / "edited.tar"
    with tarfile.open(archive) as source, tarfile.open(edited, "w") as target:
        for member in source.getmembers():
            if member.name.endswith("appliance/version.py"):
                payload = b'APPLIANCE_VERSION = "9.9.9"\n'
                member.size = len(payload)
                target.addfile(member, io.BytesIO(payload))
                continue
            target.addfile(member, source.extractfile(member) if member.isfile() else None)

    report = source_bundle.verify(edited, root=ROOT, ref="HEAD")

    assert not report.ok
    assert any("content" in reason for _path, reason in report.mismatched)


@requires_git
@requires_tar
def test_a_bundle_that_retargets_a_symlink_fails(tmp_path):
    archive = git_archive(tmp_path / "bundle.tar")
    retargeted = tmp_path / "retargeted.tar"
    with tarfile.open(archive) as source, tarfile.open(retargeted, "w") as target:
        for member in source.getmembers():
            if member.issym() and member.name.startswith(f"{WANTS}/"):
                member.linkname = "../elsewhere.mount"
            target.addfile(member, source.extractfile(member) if member.isfile() else None)

    report = source_bundle.verify(retargeted, root=ROOT, ref="HEAD")

    assert not report.ok
    assert any("target" in reason for _path, reason in report.mismatched)


# --- an explicit exclusion manifest, never a silent omission -----------------


@requires_git
@requires_tar
def test_an_excluded_path_has_to_be_declared(tmp_path):
    archive = git_archive(tmp_path / "bundle.tar")
    trimmed = tmp_path / "trimmed.tar"
    with tarfile.open(archive) as source, tarfile.open(trimmed, "w") as target:
        for member in source.getmembers():
            if member.name.startswith("develop/"):
                continue
            target.addfile(member, source.extractfile(member) if member.isfile() else None)

    unexplained = source_bundle.verify(trimmed, root=ROOT, ref="HEAD")
    declared = source_bundle.verify(trimmed, root=ROOT, ref="HEAD", exclude=("develop/",))

    assert not unexplained.ok
    assert declared.ok, declared.problems[:10]
    assert declared.excluded


# --- the script the release pipeline runs ------------------------------------


@requires_git
@requires_tar
def test_the_checker_script_reports_parity_as_a_pass(tmp_path):
    archive = git_archive(tmp_path / "bundle.tar")

    result = subprocess.run(
        ["sh", str(SCRIPTS / "appliance-check-source-bundle.sh"), str(archive)],
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout


@requires_git
@requires_tar
def test_the_checker_script_fails_a_flattened_bundle(tmp_path):
    archive = flattened_archive(tmp_path / "flattened.tar")

    result = subprocess.run(
        ["sh", str(SCRIPTS / "appliance-check-source-bundle.sh"), str(archive)],
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert result.returncode == 1
    assert "RESULT: FAIL" in result.stdout + result.stderr
    assert "local-fs.target.wants" in result.stdout + result.stderr


def test_the_checker_script_reports_a_missing_archive_as_not_run(tmp_path):
    result = subprocess.run(
        ["sh", str(SCRIPTS / "appliance-check-source-bundle.sh"), str(tmp_path / "absent.tar")],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 3
    assert "NOT RUN" in result.stderr
