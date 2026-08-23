# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether an archive of this repository is still this repository.

Persistence activation depends on symlinks tracked in git: each generated bind
mount is activated by a link in ``local-fs.target.wants``. A delivery path that
flattens a symlink into a regular file produces a tree that still builds, still
generates six mount units, activates none of them, and loses every write to the
shared paths at the next slot switch. Silently, and only on hardware.

So a bundle is compared against ``git ls-tree`` object by object — content, file
mode, symlink mode and symlink target — and anything that does not round-trip is
a failure. Paths a bundle deliberately leaves out have to be declared: a silent
omission and a dropped file look identical from the far end.

Read-only. ``git`` is invoked with a fixed argv against a repository a build
operator named; nothing here takes a path from a request.
"""

import hashlib
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

REGULAR = "regular"
EXECUTABLE = "executable"
SYMLINK = "symlink"
SUBMODULE = "submodule"

GIT_MODES = {
    "100644": REGULAR,
    "100755": EXECUTABLE,
    "120000": SYMLINK,
    "160000": SUBMODULE,
}


class SourceBundleError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Entry:
    """One tracked object: what it is, and what it should hash or point at."""

    path: str
    kind: str
    blob: str = ""
    target: str = ""

    @property
    def executable(self):
        return self.kind == EXECUTABLE


@dataclass(frozen=True)
class ParityReport:
    """Exact parity: the tracked set and the archive set, both directions.

    A bundle that carries everything tracked *and something else* is not this
    repository. An extra file under ``packaging/``, ``scripts/`` or
    ``.github/`` changes what a build reads while every tracked object still
    round-trips, which is precisely the shape a one-directional check misses.
    """

    missing: tuple = ()
    mismatched: tuple = ()
    excluded: tuple = ()
    unexpected: tuple = ()
    unsafe: tuple = ()
    duplicate: tuple = ()
    symlinks: int = 0
    compared: int = 0
    problems: tuple = field(default_factory=tuple)

    @property
    def ok(self):
        return not (
            self.missing
            or self.mismatched
            or self.unexpected
            or self.unsafe
            or self.duplicate
        )

    def to_dict(self):
        return {
            "ok": self.ok,
            "compared": self.compared,
            "symlinks": self.symlinks,
            "missing": list(self.missing),
            "mismatched": [{"path": path, "reason": reason} for path, reason in self.mismatched],
            "excluded": list(self.excluded),
            "unexpected": list(self.unexpected),
            "unsafe": [{"path": path, "reason": reason} for path, reason in self.unsafe],
            "duplicate": list(self.duplicate),
            "problems": list(self.problems),
        }


def _git(root, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceBundleError("git_unavailable", f"git could not be run: {exc}")
    if result.returncode != 0:
        raise SourceBundleError(
            "git_failed", f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}"
        )
    return result.stdout


def tracked_entries(root, *, ref="HEAD"):
    """Every object ``ref`` tracks, with the mode git recorded for it."""

    entries = []
    for line in _git(root, "ls-tree", "-r", "-z", ref).split("\0"):
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        mode, _kind, blob = meta.split()
        classification = GIT_MODES.get(mode)
        if classification is None or classification == SUBMODULE:
            continue
        target = ""
        if classification == SYMLINK:
            target = _git(root, "cat-file", "blob", blob).strip()
        entries.append(Entry(path=path, kind=classification, blob=blob, target=target))
    return tuple(sorted(entries, key=lambda entry: entry.path))


def blob_hash(payload):
    """The object name git would give these bytes."""

    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def _strip(name, prefix):
    """A tar member name as a repository path. Leading ``./`` and one prefix."""

    cleaned = name[2:] if name.startswith("./") else name
    prefix = str(prefix or "").strip("/")
    if not prefix:
        return cleaned
    if cleaned == prefix:
        return ""
    if cleaned.startswith(f"{prefix}/"):
        return cleaned[len(prefix) + 1 :]
    return cleaned


def _unsafe_name(name):
    """Why this member name may not be extracted anywhere, or nothing."""

    raw = str(name)
    if raw.startswith("/"):
        return "an absolute path"
    if "\0" in raw:
        return "an embedded null byte"
    parts = raw.split("/")
    if ".." in parts:
        return "a path that escapes the tree"
    return ""


def _unsafe_type(member):
    if member.islnk():
        return "a hard link"
    if member.ischr() or member.isblk() or member.isdev():
        return "a device node"
    if member.isfifo():
        return "a FIFO"
    if not (member.isfile() or member.issym() or member.isdir()):
        return f"an unsupported tar member type {member.type!r}"
    return ""


def _archive_members(archive, *, prefix=""):
    """Every member in the bundle: the comparable ones, and the refusals.

    Refused before anything is read out of them. A device node or a hard link
    in a source bundle is not a delivery accident to note in passing.
    """

    members, unsafe, duplicate = {}, [], []
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            reason = _unsafe_name(member.name) or _unsafe_type(member)
            if reason:
                unsafe.append((member.name, reason))
                continue
            path = _strip(member.name, prefix)
            if not path or member.isdir():
                continue
            if path in members:
                duplicate.append(path)
                continue
            if member.issym():
                members[path] = (SYMLINK, member.linkname, b"")
                continue
            stream = handle.extractfile(member)
            payload = stream.read() if stream is not None else b""
            kind = EXECUTABLE if member.mode & 0o111 else REGULAR
            members[path] = (kind, "", payload)
    return members, tuple(unsafe), tuple(sorted(set(duplicate)))


def detect_prefix(archive):
    """The single top-level directory a bundle wraps its tree in, if there is one.

    ``git archive --prefix`` is the usual shape and a reviewer should not have to
    know which name was used. Detection is deliberately all-or-nothing: a bundle
    whose members do not share exactly one root has no prefix, and comparing it
    against a guessed one would report the whole tree as missing.
    """

    roots = set()
    try:
        with tarfile.open(archive) as handle:
            for member in handle.getmembers():
                name = member.name[2:] if member.name.startswith("./") else member.name
                if not name or name.startswith("/"):
                    return ""
                roots.add(name.split("/", 1)[0])
                if len(roots) > 1:
                    return ""
    except tarfile.TarError as exc:
        raise SourceBundleError("bundle_unreadable", f"{archive} could not be read: {exc}")
    return roots.pop() if len(roots) == 1 else ""


def verify(archive, *, root, ref="HEAD", prefix="", exclude=()):
    """Compare a bundle against the tracked tree, object by object."""

    target = Path(archive)
    if not target.is_file():
        raise SourceBundleError("bundle_unavailable", f"{target} is not a file")

    entries = tracked_entries(root, ref=ref)
    try:
        members, unsafe, duplicate = _archive_members(target, prefix=prefix)
    except tarfile.TarError as exc:
        raise SourceBundleError("bundle_unreadable", f"{target} could not be read: {exc}")

    excluded = tuple(str(item) for item in exclude)
    missing, mismatched, compared, skipped = [], [], 0, []
    expected = set()
    for entry in entries:
        expected.add(entry.path)
        if any(entry.path.startswith(item) for item in excluded):
            skipped.append(entry.path)
            continue
        found = members.get(entry.path)
        if found is None:
            missing.append(entry.path)
            continue
        compared += 1
        reason = _difference(entry, found)
        if reason:
            mismatched.append((entry.path, reason))

    # The other direction. Everything the tree does not track is undeclared,
    # and an undeclared build input is exactly what a one-directional check
    # cannot see.
    unexpected = tuple(
        sorted(
            path
            for path in members
            if path not in expected
            and not any(path.startswith(item) for item in excluded)
        )
    )

    return ParityReport(
        missing=tuple(missing),
        mismatched=tuple(mismatched),
        excluded=tuple(skipped),
        unexpected=unexpected,
        unsafe=unsafe,
        duplicate=duplicate,
        symlinks=sum(1 for kind, _target, _payload in members.values() if kind == SYMLINK),
        compared=compared,
    )


def _difference(entry, found):
    kind, target, payload = found
    if entry.kind == SYMLINK:
        if kind != SYMLINK:
            return f"the bundle carries a {kind} where the tree tracks a symlink"
        if target != entry.target:
            return f"the symlink target is {target!r}, the tree tracks {entry.target!r}"
        return ""
    if kind == SYMLINK:
        return "the bundle carries a symlink where the tree tracks a regular file"
    if kind != entry.kind:
        return f"the file mode is {kind}, the tree tracks {entry.kind}"
    if blob_hash(payload) != entry.blob:
        return "the file content differs from the tracked object"
    return ""
