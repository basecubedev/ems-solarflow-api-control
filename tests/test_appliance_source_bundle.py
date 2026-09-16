# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether an archive of this repository is still this repository.

Persistence activation depends on one symlink per shared path, tracked in git:

    packaging/appliance/image/layer/ems-appliance.rootfs-overlay/
        etc/systemd/system/local-fs.target.wants/*.mount

They are what makes each generated bind mount actually mount. Both archives
produced for the last independent review arrived without them — every link had
become a regular file — and an image built from such a tree would generate the
mount units, activate none of them, and lose every write to the shared paths at
the next slot switch. Silently.

Whether that was the packaging or the transport is beside the point: a delivery
path that can drop a symlink and still look complete is the defect. So the
bundle is compared against ``git ls-tree`` object by object — content, file
mode, symlink mode and symlink target — and anything that does not round-trip
is a failure rather than a note.
"""

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from appliance import source_bundle

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

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
def test_a_bundle_missing_a_tracked_file_fails(tmp_path):
    archive = git_archive(tmp_path / "bundle.tar")
    trimmed = tmp_path / "trimmed.tar"
    with tarfile.open(archive) as source, tarfile.open(trimmed, "w") as target:
        for member in source.getmembers():
            if member.name.endswith("appliance/artifact_trust.py"):
                continue
            target.addfile(member, source.extractfile(member) if member.isfile() else None)

    report = source_bundle.verify(trimmed, root=ROOT, ref="HEAD")

    assert not report.ok
    assert any(path.endswith("appliance/artifact_trust.py") for path in report.missing)


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


def test_the_checker_script_reports_a_missing_archive_as_not_run(tmp_path):
    result = subprocess.run(
        ["sh", str(SCRIPTS / "appliance-check-source-bundle.sh"), str(tmp_path / "absent.tar")],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 3
    assert "NOT RUN" in result.stderr


# --- the history carried under .git ------------------------------------------


def _git(*args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    ).stdout


@pytest.fixture
def repository(tmp_path):
    """A small repository with the shapes the bundle has to carry: two commits,
    a lightweight and an annotated tag, an executable and a symlink."""

    root = tmp_path / "repository"
    root.mkdir()
    _git("init", "-q", cwd=root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "run").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "bin" / "run").chmod(0o755)
    os.symlink("README.md", root / "link")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "first", cwd=root)
    _git("tag", "v1.0", cwd=root)
    (root / "README.md").write_text("hello again\n", encoding="utf-8")
    _git("commit", "-q", "-am", "second", cwd=root)
    _git("tag", "-a", "v1.1", "-m", "release", cwd=root)
    return root


def _extract(archive, into):
    with tarfile.open(archive) as handle:
        try:
            handle.extractall(into, filter="data")
        except TypeError:
            handle.extractall(into)
    return into


def _rewritten(archive, destination, *, drop=(), add=(), replace=None):
    """The same bundle with members dropped, added or replaced under the prefix."""

    with tarfile.open(archive) as source, tarfile.open(destination, "w:gz") as target:
        for member in source.getmembers():
            if any(member.name.endswith(name) for name in drop):
                continue
            payload = source.extractfile(member) if member.isfile() else None
            if replace and member.name.endswith(replace[0]):
                data = replace[1]
                member.size = len(data)
                payload = io.BytesIO(data)
            target.addfile(member, payload)
        for name, data in add:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            target.addfile(info, io.BytesIO(data))
    return destination


@requires_git
@requires_tar
def test_a_created_bundle_extracts_as_a_clean_checkout_with_its_tags(repository, tmp_path):
    """The point of carrying .git: git describe and git log answer in the
    extracted tree, and it reads as clean."""

    made = source_bundle.create(tmp_path / "bundle.tar.gz", root=repository, prefix="proj")

    assert made.tags == ("v1.0", "v1.1")
    assert made.commits == 2
    report = source_bundle.verify(tmp_path / "bundle.tar.gz", root=repository, prefix="proj")
    assert report.ok, report.repository + report.unexpected + report.unsafe
    assert report.carried
    assert report.head == made.revision
    assert report.tags == ("v1.0", "v1.1")

    tree = _extract(tmp_path / "bundle.tar.gz", tmp_path / "extracted") / "proj"
    assert _git("status", "--porcelain", cwd=tree) == ""
    assert _git("describe", "--tags", cwd=tree).strip() == "v1.1"
    assert _git("rev-parse", "HEAD", cwd=tree).strip() == made.revision
    assert os.readlink(tree / "link") == "README.md"
    assert os.access(tree / "bin" / "run", os.X_OK)


@requires_git
@requires_tar
def test_the_carried_clone_holds_no_local_state(repository, tmp_path):
    """No remote, no branch, no hook, no reflog, no worktree link: nothing
    that names this machine or runs on the next one."""

    source_bundle.create(tmp_path / "bundle.tar.gz", root=repository, prefix="proj")

    with tarfile.open(tmp_path / "bundle.tar.gz") as handle:
        carried = sorted(
            member.name[len("proj/.git/") :]
            for member in handle.getmembers()
            if member.name.startswith("proj/.git/") and member.isfile()
        )
        config = handle.extractfile("proj/.git/config").read().decode("utf-8")
    assert "HEAD" in carried and "index" in carried and "config" in carried
    assert not any(name.startswith(("hooks/", "logs/", "worktrees/", "refs/heads/", "refs/remotes/")) for name in carried)
    assert "[remote" not in config


@requires_git
@requires_tar
def test_a_bundle_without_a_carried_history_still_verifies(repository, tmp_path):
    """Bundles made before the history was carried keep their standing."""

    plain = tmp_path / "plain.tar"
    _git("archive", "--format=tar", "--prefix=proj/", "-o", str(plain), "HEAD", cwd=repository)

    report = source_bundle.verify(plain, root=repository, prefix="proj")

    assert report.ok, report.problems[:10]
    assert not report.carried
    assert report.to_dict()["repository"] == {"carried": False, "head": "", "tags": [], "problems": []}


@requires_git
@requires_tar
def test_a_carried_history_at_another_revision_is_refused(repository, tmp_path):
    """The tree can round-trip while HEAD names a different commit: an empty
    commit moves HEAD and changes no file. The history has to be the bundled
    revision, not merely a revision with the same tree."""

    source_bundle.create(tmp_path / "bundle.tar.gz", root=repository, prefix="proj")
    _git("commit", "-q", "--allow-empty", "-m", "moved", cwd=repository)

    report = source_bundle.verify(tmp_path / "bundle.tar.gz", root=repository, prefix="proj")

    assert not report.ok
    assert report.missing == () and report.mismatched == ()
    assert any("is at" in problem and "the bundle is revision" in problem for problem in report.repository)


@requires_git
@requires_tar
def test_a_tag_the_bundle_does_not_carry_is_reported(repository, tmp_path):
    source_bundle.create(tmp_path / "bundle.tar.gz", root=repository, prefix="proj")
    _git("tag", "v1.2", cwd=repository)

    report = source_bundle.verify(tmp_path / "bundle.tar.gz", root=repository, prefix="proj")

    assert not report.ok
    assert "the tag v1.2 is not carried" in report.repository


@requires_git
@requires_tar
@pytest.mark.parametrize(
    "name, payload, expected",
    [
        (".git/hooks/pre-commit", b"#!/bin/sh\nrm -rf /\n", "hooks/pre-commit"),
        (".git/objects/info/alternates", b"/somewhere/else/objects\n", "alternates"),
        (".git/logs/HEAD", b"", "logs/HEAD"),
        (".git/shallow", b"0" * 40 + b"\n", "shallow"),
    ],
)
def test_a_carried_history_with_local_or_executable_state_is_refused(
    repository, tmp_path, name, payload, expected
):
    """A repository directory is a place git executes from. Anything in it
    beyond a clean clone is refused before git is asked a single question."""

    source_bundle.create(tmp_path / "bundle.tar.gz", root=repository, prefix="proj")
    tampered = _rewritten(
        tmp_path / "bundle.tar.gz", tmp_path / "tampered.tar.gz", add=((f"proj/{name}", payload),)
    )

    report = source_bundle.verify(tampered, root=repository, prefix="proj")

    assert not report.ok
    assert any(expected in problem for problem in report.repository), report.repository


@requires_git
@requires_tar
@pytest.mark.parametrize(
    "config, expected",
    [
        (b"[core]\n\tbare = false\n\thooksPath = /tmp/hooks\n", "core.hookspath"),
        (b"[core]\n\tbare = false\n\tfsmonitor = /tmp/watch\n", "core.fsmonitor"),
        (b"[core]\n\tbare = false\n[include]\n\tpath = /etc/gitconfig\n", "includes another file"),
        (b"[core]\n\tbare = false\n[remote \"origin\"]\n\turl = /home/someone/repo\n", "remote.origin.url"),
    ],
)
def test_a_carried_config_that_runs_or_names_anything_is_refused(
    repository, tmp_path, config, expected
):
    source_bundle.create(tmp_path / "bundle.tar.gz", root=repository, prefix="proj")
    tampered = _rewritten(
        tmp_path / "bundle.tar.gz", tmp_path / "tampered.tar.gz", replace=(".git/config", config)
    )

    report = source_bundle.verify(tampered, root=repository, prefix="proj")

    assert not report.ok
    assert any(expected in problem for problem in report.repository), report.repository


@requires_git
@requires_tar
def test_a_git_file_in_place_of_the_directory_is_unsafe(repository, tmp_path):
    """A linked worktree keeps a ``.git`` *file* naming its repository. That
    names a path on another machine, and it is refused as unsafe."""

    plain = tmp_path / "plain.tar"
    _git("archive", "--format=tar", "--prefix=proj/", "-o", str(plain), "HEAD", cwd=repository)
    tampered = _rewritten(
        plain, tmp_path / "tampered.tar.gz", add=(("proj/.git", b"gitdir: /home/someone/.git/worktrees/x\n"),)
    )

    report = source_bundle.verify(tampered, root=repository, prefix="proj")

    assert not report.ok
    assert any(".git file" in reason for _name, reason in report.unsafe)


@requires_git
@requires_tar
def test_a_revision_on_no_branch_is_still_carried(repository, tmp_path):
    """A clone transfers branches and tags; a commit reachable from neither
    is fetched on its own rather than silently missing."""

    _git("checkout", "-q", "-b", "scratch", cwd=repository)
    (repository / "extra.txt").write_text("extra\n", encoding="utf-8")
    _git("add", "extra.txt", cwd=repository)
    _git("commit", "-q", "-m", "orphan", cwd=repository)
    orphan = _git("rev-parse", "HEAD", cwd=repository).strip()
    _git("checkout", "-q", "--detach", "v1.1", cwd=repository)
    _git("branch", "-D", "scratch", cwd=repository)

    made = source_bundle.create(tmp_path / "bundle.tar.gz", root=repository, ref=orphan, prefix="proj")

    assert made.revision == orphan
    report = source_bundle.verify(tmp_path / "bundle.tar.gz", root=repository, ref=orphan, prefix="proj")
    assert report.ok, report.repository + report.missing


@requires_git
@requires_tar
def test_a_shallow_repository_is_refused(repository, tmp_path):
    shallow = tmp_path / "shallow"
    _git("clone", "-q", "--depth", "1", f"file://{repository}", str(shallow), cwd=tmp_path)

    with pytest.raises(source_bundle.SourceBundleError) as refused:
        source_bundle.create(tmp_path / "bundle.tar.gz", root=shallow, prefix="proj")

    assert refused.value.code == "repository_shallow"
    assert not (tmp_path / "bundle.tar.gz").exists()


# --- the creator script, end to end on a small repository ----------------------


def _project_copy(repository):
    """The scripts and the package they import, committed into the small
    repository, so the creator can run from a root that is not this checkout."""

    for relative in ("scripts/appliance-create-source-bundle.sh", "scripts/appliance-check-source-bundle.sh"):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, target)
    shutil.copytree(
        ROOT / "appliance", repository / "appliance",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "static", "templates"),
    )
    _git("add", "-A", cwd=repository)
    _git("commit", "-q", "-m", "tooling", cwd=repository)
    _git("tag", "v1.2", cwd=repository)
    return repository


@requires_git
@requires_tar
def test_the_creator_script_writes_a_bundle_that_carries_the_tags(repository, tmp_path):
    root = _project_copy(repository)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "appliance-create-source-bundle.sh"),
         "--output", str(tmp_path / "out" / "bundle.tar.gz")],
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 tag(s) carried under .git" in result.stdout
    assert "3 tag(s) verified under .git" in result.stdout
    assert "RESULT: PASS" in result.stdout
    check = subprocess.run(
        ["sh", str(root / "scripts" / "appliance-check-source-bundle.sh"), str(tmp_path / "out" / "bundle.tar.gz")],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert "3 tag(s) verified under .git" in check.stdout


@requires_git
@requires_tar
def test_the_creator_script_refuses_a_shallow_checkout_as_not_run(repository, tmp_path):
    root = _project_copy(repository)
    shallow = tmp_path / "shallow"
    _git("clone", "-q", "--depth", "1", f"file://{root}", str(shallow), cwd=tmp_path)

    result = subprocess.run(
        ["sh", str(shallow / "scripts" / "appliance-create-source-bundle.sh"),
         "--output", str(tmp_path / "out" / "bundle.tar.gz")],
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "RESULT: NOT RUN (repository_shallow)" in result.stderr
    assert not (tmp_path / "out" / "bundle.tar.gz").exists()
