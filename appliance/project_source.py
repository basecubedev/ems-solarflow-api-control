# SPDX-License-Identifier: AGPL-3.0-or-later
"""What this repository was, at the moment a builder read it.

The upstream generator's tree is proven immediately before ``./rpi-image-gen
build`` opens it. The project's own tree was not: the build wrapper recorded

    PROJECT_REVISION=$(git rev-parse --short HEAD)

which is a claim about the last commit and says nothing about the files the
build actually packaged. An image built from a working tree with uncommitted
appliance changes, a staged change, or an untracked script under ``packaging/``
was signed as if it had come from that clean revision.

So a project source identity is the full revision *and* a hash of the tree, and
it is refused outright when the tree is not exactly what the revision names. A
development bench may still build from a dirty tree — it simply gets no
production provenance and is never signed.

Read-only. ``git`` is invoked with a fixed argv against a path a build operator
named; nothing here takes a path from a request.
"""

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

DIRTY = "project_source_dirty"
UNTRACKED = "project_source_untracked"
UNAVAILABLE = "project_source_unavailable"
CHANGED_DURING_BUILD = "build_source_changed_during_build"

# The roots whose contents reach an image: the package, the image definition,
# the build wrappers and the CI that drives them. An untracked file under any of
# them is a build input nobody declared.
BUILD_CRITICAL_ROOTS = ("appliance", "packaging", "scripts", "config", ".github")


class ProjectSourceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProjectSource:
    """One repository state a build may be attributed to."""

    revision: str = ""
    tree_sha256: str = ""

    def to_dict(self):
        return {"revision": self.revision, "tree_sha256": self.tree_sha256}


def _git(root, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectSourceError(UNAVAILABLE, f"git could not be run: {exc}")
    return result


def _require(root, *args, code=UNAVAILABLE):
    result = _git(root, *args)
    if result.returncode != 0:
        raise ProjectSourceError(
            code, f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}"
        )
    return result.stdout


def revision(root, ref="HEAD"):
    """The full object name. A short revision names a prefix, not a commit."""

    return _require(root, "rev-parse", ref).strip()


def tree_sha256(root, ref="HEAD"):
    """One hash over every object the revision tracks, mode included.

    Taken from the object tree rather than the working directory, so it is the
    same on any machine that has the commit — and so it cannot be influenced by
    a build's own scratch files.
    """

    entries = []
    for line in _require(root, "ls-tree", "-r", "-z", ref).split("\0"):
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        mode, kind, blob = meta.split()
        entries.append({"path": path, "mode": mode, "type": kind, "object": blob})
    payload = json.dumps(
        sorted(entries, key=lambda item: item["path"]), sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def untracked_build_inputs(root):
    """Untracked files under a root a build reads. Ignored files do not count."""

    stdout = _require(root, "ls-files", "--others", "--exclude-standard", "-z")
    found = []
    for entry in stdout.split("\0"):
        path = entry.strip()
        if not path:
            continue
        if path.split("/", 1)[0] in BUILD_CRITICAL_ROOTS:
            found.append(path)
    return tuple(sorted(found))


def assert_clean(root, ref="HEAD"):
    """Prove the tree is exactly the revision, or refuse to attribute a build.

    All three states are separate defects with the same consequence: an image
    whose contents are not the commit it claims. The commit object itself has to
    be present too — a detached ``.git`` carrying only a ref file proves nothing.
    """

    target = Path(root)
    if _git(target, "rev-parse", "--git-dir").returncode != 0:
        raise ProjectSourceError(UNAVAILABLE, f"{target} is not a git repository")

    head = revision(target, ref)
    if _git(target, "cat-file", "-e", f"{head}^{{commit}}").returncode != 0:
        raise ProjectSourceError(
            UNAVAILABLE, f"{head} is named but the commit object is not in this repository"
        )

    if _git(target, "diff", "--quiet").returncode != 0:
        raise ProjectSourceError(
            DIRTY, "the project tree carries uncommitted changes; a production build needs a "
            "tree that is exactly its revision"
        )
    if _git(target, "diff", "--cached", "--quiet").returncode != 0:
        raise ProjectSourceError(
            DIRTY, "the project tree carries staged changes; a production build needs a tree "
            "that is exactly its revision"
        )
    untracked = untracked_build_inputs(target)
    if untracked:
        raise ProjectSourceError(
            UNTRACKED,
            "the project tree carries untracked build inputs: " + ", ".join(untracked[:10]),
        )
    return ProjectSource(revision=head, tree_sha256=tree_sha256(target, head))


def assert_unchanged(root, identity, ref="HEAD"):
    """The tree a build finished with must be the tree it started from.

    The pre-build proof closes ordinary TOCTOU. A build takes long enough that
    the tree can be edited while it runs, so completed build authority is only
    issued for a tree that is still the one that was proven.
    """

    try:
        current = assert_clean(root, ref)
    except ProjectSourceError as exc:
        raise ProjectSourceError(
            CHANGED_DURING_BUILD,
            "the project source tree is no longer the tree this build started from: "
            + exc.message,
        )
    if current.revision != identity.revision or current.tree_sha256 != identity.tree_sha256:
        raise ProjectSourceError(
            CHANGED_DURING_BUILD,
            "the project source tree changed while the build was running; this build "
            "cannot be attributed to either state",
        )
    return current
