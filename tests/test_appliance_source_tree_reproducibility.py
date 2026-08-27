# SPDX-License-Identifier: AGPL-3.0-or-later
"""The source-authority digest has to mean the same thing on every host.

``tree_manifest`` records each file's mode, and the fetch extracts with
``--no-same-permissions`` so a downloaded archive cannot set its own. That
combination made the digest depend on the umask of whoever ran the fetch: the
lock was recorded at 0002, and at the far more common 0022 the same archive
yields different modes and a different digest, so the gate refuses a perfectly
good tree.

Root inside a container runs at 0022. So does a CI runner. The first step of an
automated release could never have passed, on any machine but a developer's own.

The extraction was pinned for this. The clone beside it was not, and it writes a
working tree with the caller's umask just as surely -- so the same defect
survived in the other half of the same script, where it broke the nightly
upstream tier on every runner while the tarball form was reproducible.
"""

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from appliance.rpi_image_gen import tree_digest

ROOT = Path(__file__).resolve().parents[1]
FETCH = ROOT / "scripts" / "appliance-fetch-rpi-image-gen.sh"

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

UMASKS = ("0002", "0022", "0077")


def build_archive(path):
    """An archive whose recorded modes a umask would visibly alter."""

    tree = path.parent / "src"
    (tree / "top" / "nested").mkdir(parents=True)
    for name, mode in (("readable.txt", 0o664), ("script.sh", 0o775), ("tight.conf", 0o600)):
        target = tree / "top" / name
        target.write_text(name, encoding="utf-8")
        target.chmod(mode)
    (tree / "top" / "nested" / "deep.txt").write_text("deep", encoding="utf-8")
    (tree / "top" / "link").symlink_to("readable.txt")

    with tarfile.open(path, "w:gz") as archive:
        archive.add(tree / "top", arcname="top")
    return path


def extract(archive, destination, *, caller_umask, pinned):
    """Extract the way the fetch script does, under a stated caller umask."""

    destination.mkdir(parents=True)
    inner = (
        f"umask {pinned}; " if pinned else ""
    ) + f"tar -xzf {archive} -C {destination} --no-same-owner --no-same-permissions"
    subprocess.run(
        ["sh", "-c", f"umask {caller_umask}; sh -c '{inner}'"], check=True, capture_output=True
    )
    return destination / "top"


def test_the_digest_used_to_depend_on_who_ran_the_fetch(tmp_path):
    """The defect itself, so the fix cannot be quietly undone."""

    archive = build_archive(tmp_path / "src.tar.gz")

    digests = {
        mask: tree_digest(extract(archive, tmp_path / f"un{mask}", caller_umask=mask, pinned=None))
        for mask in UMASKS
    }

    assert len(set(digests.values())) == len(UMASKS), (
        "this archive no longer distinguishes umasks, so the test below proves nothing"
    )


def test_pinning_the_umask_makes_the_digest_the_same_everywhere(tmp_path):
    archive = build_archive(tmp_path / "src.tar.gz")

    digests = {
        mask: tree_digest(
            extract(archive, tmp_path / f"pin{mask}", caller_umask=mask, pinned="0002")
        )
        for mask in UMASKS
    }

    assert len(set(digests.values())) == 1, digests


def test_the_fetch_pins_it_and_still_refuses_the_archive_its_own_permissions():
    """Both halves matter.

    Dropping --no-same-permissions would also make the digest reproducible, by
    handing a downloaded archive authority over the modes it is extracted with.
    That is the wrong half to give up.
    """

    script = FETCH.read_text(encoding="utf-8")

    assert "( umask 0002; tar -xzf" in script
    assert "--no-same-permissions" in script
    assert "--no-same-owner" in script


def test_the_mode_is_part_of_what_the_manifest_measures(tmp_path):
    """Why the umask could reach the digest at all — recorded, not incidental."""

    tree = tmp_path / "tree"
    (tree).mkdir()
    target = tree / "hook.sh"
    target.write_text("#!/bin/sh\n", encoding="utf-8")

    target.chmod(0o755)
    executable = tree_digest(tree)
    target.chmod(0o644)

    assert tree_digest(tree) != executable, "a hook that stopped being executable must show"


# --- the same question for the other half of the same script -----------------

git_required = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required to clone anything"
)


def build_repository(path):
    """A repository whose checkout a umask would visibly alter."""

    path.mkdir(parents=True)
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(path), *args], check=True, capture_output=True
    )
    run("init", "--quiet")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "test")
    (path / "top").mkdir()
    for name, mode in (("readable.txt", 0o664), ("script.sh", 0o775)):
        target = path / "top" / name
        target.write_text(name, encoding="utf-8")
        target.chmod(mode)
    run("add", "-A")
    run("commit", "--quiet", "-m", "one")
    return path


def clone(source, destination, *, caller_umask, pinned):
    """Clone the way the fetch script does, under a stated caller umask."""

    inner = (f"umask {pinned}; " if pinned else "") + f"git clone --quiet {source} {destination}"
    subprocess.run(
        ["sh", "-c", f"umask {caller_umask}; sh -c '{inner}'"], check=True, capture_output=True
    )
    return destination


@git_required
def test_the_clone_used_to_depend_on_who_ran_the_fetch(tmp_path):
    """The defect in the git form, so this fix cannot be quietly undone either."""

    source = build_repository(tmp_path / "upstream")

    digests = {
        mask: tree_digest(clone(source, tmp_path / f"un{mask}", caller_umask=mask, pinned=None))
        for mask in UMASKS
    }

    assert len(set(digests.values())) == len(UMASKS), (
        "this repository no longer distinguishes umasks, so the test below proves nothing"
    )


@git_required
def test_pinning_the_umask_makes_the_clone_the_same_everywhere(tmp_path):
    source = build_repository(tmp_path / "upstream")

    digests = {
        mask: tree_digest(clone(source, tmp_path / f"pin{mask}", caller_umask=mask, pinned="0002"))
        for mask in UMASKS
    }

    assert len(set(digests.values())) == 1, digests


def test_the_fetch_pins_the_umask_for_the_clone_as_well_as_the_extraction():
    """One script, two source forms, and the pin belongs to both. It was on the
    extraction alone, which is why a tarball fetch was reproducible everywhere
    and a clone of the same commit was not."""

    script = FETCH.read_text(encoding="utf-8")

    assert "( umask 0002; git clone" in script
    assert "( umask 0002; git -C" in script
