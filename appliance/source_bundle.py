# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether an archive of this repository is still this repository.

A build can be handed a source archive rather than a checkout, and an archive is
easy to produce badly. A delivery path that rewrites a file mode drops the
executable bit off a build hook; one that flattens a symlink turns it into a
regular file that still parses. Either produces a tree that still builds and is
not this project -- silently, and only at the far end.

So a bundle is compared against ``git ls-tree`` object by object -- content, file
mode, symlink mode and symlink target -- and anything that does not round-trip is
a failure. Paths a bundle deliberately leaves out have to be declared: a silent
omission and a dropped file look identical from the far end.

The bundle also carries the history. Under ``.git`` sits a clean clone -- every
tag, HEAD detached at the archived revision, no remotes, hooks, reflogs or
worktree links -- so the extracted tree is a checkout whose ``git describe`` and
``git log`` answer. That clone is verified as strictly as the tree: it must be
at the bundled revision, carry exactly the repository's tags, hold every object
its history needs, and contain nothing that would run code on the machine that
extracts it. A repository directory is a place git executes from, which is why
hooks, ``core.hooksPath`` and an alternates file are refused before git is
asked anything about it.

Read-only with respect to the repository. ``git`` is invoked with a fixed argv
against a repository a build operator named; nothing here takes a path from a
request.
"""

import gzip
import hashlib
import re
import shutil
import subprocess
import tarfile
import tempfile
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

GIT_DIR = ".git"
REPOSITORY_FILES = frozenset({"HEAD", "config", "index", "packed-refs", "description"})
REPOSITORY_DIRECTORIES = ("refs", "refs/heads", "refs/tags", "objects")
REPOSITORY_TREES = ("refs/", "objects/", "info/")
REPOSITORY_REFUSED = (
    ("hooks/", "a hook runs code on the machine that extracts the bundle"),
    ("logs/", "reflogs are local state"),
    ("worktrees/", "worktree links are local state"),
    ("modules/", "submodules are not carried"),
)
CONFIG_KEYS = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.ignorecase",
        "core.symlinks",
        "core.precomposeunicode",
    }
)
_INCLUDE_SECTION = re.compile(r"^\s*\[\s*include", re.IGNORECASE | re.MULTILINE)


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
    carried: bool = False
    head: str = ""
    tags: tuple = ()
    repository: tuple = ()

    @property
    def ok(self):
        return not (
            self.missing
            or self.mismatched
            or self.unexpected
            or self.unsafe
            or self.duplicate
            or self.repository
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
            "repository": {
                "carried": self.carried,
                "head": self.head,
                "tags": list(self.tags),
                "problems": list(self.repository),
            },
        }


@dataclass(frozen=True)
class CreatedBundle:
    """What ``create`` wrote: the revision, and the history carried with it."""

    revision: str
    commits: int
    tags: tuple


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


def _tags(root, *git_options):
    """Every tag as ``name -> object``, the object being what the ref names."""

    tags = {}
    output = _git(root, *git_options, "for-each-ref", "--format=%(refname:short)%00%(objectname)", "refs/tags")
    for line in output.splitlines():
        name, _, target = line.partition("\0")
        if name:
            tags[name] = target
    return tags


def create(archive, *, root, ref="HEAD", prefix=""):
    """Write the bundle: the tracked tree, and a clean clone of the history.

    The tree comes out of ``git archive``, so modes and symlinks are what git
    recorded and nothing untracked is picked up. The history is a fresh
    ``--no-local`` clone reduced to what a reviewer needs -- the tags and the
    commits behind them, HEAD detached at the revision, an index that matches
    it -- with remotes, branches, reflogs, hooks and templates removed, so the
    extracted tree reads as a clean checkout and carries no local state.

    The refs and objects directories go in as directory members of their own:
    git recognises a repository by them, and with every ref packed the refs
    tree holds no file that would carry it along.

    A shallow repository is refused rather than bundled: its history and tags
    are not all here, and a bundle that silently lacked them would be exactly
    the far-end surprise this script exists to prevent.
    """

    source = Path(root).resolve()
    revision = _git(source, "rev-parse", ref).strip()
    if _git(source, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise SourceBundleError(
            "repository_shallow",
            "the repository is a shallow clone, so its history and tags are not all here to carry",
        )
    head = str(prefix or "").strip("/")
    head = f"{head}/" if head else ""
    target = Path(archive)
    target.parent.mkdir(parents=True, exist_ok=True)
    commit_time = int(_git(source, "log", "-1", "--format=%ct", revision).strip())

    with tempfile.TemporaryDirectory(prefix="source-bundle-create-") as scratch:
        plain = Path(scratch) / "bundle.tar"
        _git(source, "archive", "--format=tar", f"--prefix={head}", "-o", str(plain), revision)

        clone = Path(scratch) / "clone"
        _git(
            source, "clone", "--quiet", "--no-local", "--no-hardlinks", "--no-checkout",
            "--template=", str(source), str(clone),
        )
        _git(clone, "remote", "remove", "origin")
        try:
            _git(clone, "cat-file", "-e", f"{revision}^{{commit}}")
        except SourceBundleError:
            _git(
                clone, "-c", "uploadpack.allowAnySHA1InWant=true",
                "fetch", "--quiet", "--no-tags", str(source), revision,
            )
        for name in _git(clone, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes").split():
            _git(clone, "update-ref", "-d", name)
        _git(clone, "update-ref", "--no-deref", "HEAD", revision)
        _git(clone, "read-tree", revision)
        _git(clone, "reflog", "expire", "--expire=now", "--all")
        _git(clone, "gc", "--quiet", "--prune=now")
        git_dir = clone / GIT_DIR
        for stray in ("logs", "hooks", "info", "description", "FETCH_HEAD", "ORIG_HEAD", "COMMIT_EDITMSG"):
            path = git_dir / stray
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        tags = tuple(sorted(_tags(clone)))
        commits = int(_git(clone, "rev-list", "--count", "HEAD").strip())

        with tarfile.open(plain, "a") as tar:
            for directory in REPOSITORY_DIRECTORIES:
                info = tarfile.TarInfo(f"{head}{GIT_DIR}/{directory}")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.mtime = commit_time
                tar.addfile(info)
            for file in sorted(path for path in git_dir.rglob("*") if path.is_file()):
                relative = file.relative_to(git_dir).as_posix()
                info = tar.gettarinfo(str(file), arcname=f"{head}{GIT_DIR}/{relative}")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = commit_time
                info.mode = 0o644
                with file.open("rb") as handle:
                    tar.addfile(info, handle)
        with plain.open("rb") as raw, gzip.GzipFile(str(target), "wb", mtime=0) as packed:
            shutil.copyfileobj(raw, packed)

    return CreatedBundle(revision=revision, commits=commits, tags=tags)


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

    members, embedded, unsafe, duplicate = {}, {}, [], []
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            reason = _unsafe_name(member.name) or _unsafe_type(member)
            if reason:
                unsafe.append((member.name, reason))
                continue
            path = _strip(member.name, prefix)
            if not path:
                continue
            if member.isdir():
                if path.startswith(f"{GIT_DIR}/"):
                    embedded[path[len(GIT_DIR) + 1 :].rstrip("/") + "/"] = None
                continue
            if path == GIT_DIR or path.startswith(f"{GIT_DIR}/"):
                if path == GIT_DIR:
                    unsafe.append((member.name, "a .git file where only a repository directory may be carried"))
                    continue
                if member.issym():
                    unsafe.append((member.name, "a symlink inside the embedded repository"))
                    continue
                relative = path[len(GIT_DIR) + 1 :]
                if relative in embedded:
                    duplicate.append(path)
                    continue
                stream = handle.extractfile(member)
                embedded[relative] = stream.read() if stream is not None else b""
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
    return members, embedded, tuple(unsafe), tuple(sorted(set(duplicate)))


def _repository_member_problem(relative):
    """Why a file under ``.git`` may not be carried, or nothing."""

    if relative in REPOSITORY_FILES:
        return ""
    if relative == "objects/info/alternates":
        return "an alternates file points the history at another object store"
    for tree, why in REPOSITORY_REFUSED:
        if relative.startswith(tree):
            return why
    if relative.startswith(REPOSITORY_TREES):
        return ""
    return "not part of a clean clone"


def _config_problems(payload):
    """Keys a carried config may set: the ones ``git clone`` writes, no more.

    ``core.hooksPath``, ``core.fsmonitor``, ``core.sshCommand`` and an include
    each make git run something the moment it is used in the extracted tree.
    """

    problems = []
    text = payload.decode("utf-8", errors="replace")
    if _INCLUDE_SECTION.search(text):
        problems.append("the embedded repository's config includes another file")
        return problems
    with tempfile.NamedTemporaryFile(prefix="source-bundle-config-", suffix=".ini") as handle:
        handle.write(payload)
        handle.flush()
        try:
            listed = _git(Path(handle.name).parent, "config", "--file", handle.name, "--list", "--name-only", "-z")
        except SourceBundleError as exc:
            return [f"the embedded repository's config could not be read: {exc.message}"]
    for key in sorted({name for name in listed.split("\0") if name}):
        if key not in CONFIG_KEYS:
            problems.append(f"the embedded repository's config sets {key}, which a clean clone does not")
    return problems


def _repository_report(embedded, *, root, ref):
    """Is the carried history the bundled revision, with the repository's tags?

    Static refusals come first, and git is only asked about a repository that
    passed them: a hook or a config key would otherwise run right here.
    """

    problems = []
    directories = {name for name, payload in embedded.items() if payload is None}
    embedded = {name: payload for name, payload in embedded.items() if payload is not None}
    for relative in sorted(embedded):
        why = _repository_member_problem(relative)
        if why:
            problems.append(f"the embedded repository carries {relative}: {why}")
    for relative in sorted(directories):
        why = _repository_member_problem(relative)
        if why:
            problems.append(f"the embedded repository carries {relative}: {why}")
    for required, why in (
        ("HEAD", "has no HEAD"),
        ("config", "has no config"),
        ("index", "has no index, so the extracted tree would not read as clean"),
    ):
        if required not in embedded:
            problems.append(f"the embedded repository {why}")
    if "refs/" not in directories:
        problems.append(
            "the embedded repository has no refs directory, so git would not recognise it once extracted"
        )
    if "config" in embedded:
        problems.extend(_config_problems(embedded["config"]))
    if problems:
        return tuple(problems), "", ()

    revision = _git(root, "rev-parse", ref).strip()
    expected_tags = _tags(root)
    with tempfile.TemporaryDirectory(prefix="source-bundle-history-") as scratch:
        git_dir = Path(scratch) / GIT_DIR
        for directory in directories:
            (git_dir / directory).mkdir(parents=True, exist_ok=True)
        for relative, payload in embedded.items():
            target = git_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        tree = Path(scratch) / "tree"
        tree.mkdir()
        options = (f"--git-dir={git_dir}", f"--work-tree={tree}", "-c", "core.hooksPath=/dev/null")
        try:
            head = _git(tree, *options, "rev-parse", "--verify", "HEAD^{commit}").strip()
        except SourceBundleError as exc:
            return (f"the embedded repository has no readable HEAD: {exc.message}",), "", ()
        if head != revision:
            problems.append(
                f"the embedded repository is at {head[:12]}, the bundle is revision {revision[:12]}"
            )
        try:
            _git(tree, *options, "fsck", "--connectivity-only", "--no-dangling", "--no-progress")
        except SourceBundleError as exc:
            problems.append(f"the embedded history is incomplete: {exc.message}")
        try:
            _git(tree, *options, "diff-index", "--cached", "--quiet", "HEAD")
        except SourceBundleError:
            problems.append(
                "the embedded index does not match HEAD, so the extracted tree would not read as clean"
            )
        carried_tags = _tags(tree, *options)
    for name, target in sorted(expected_tags.items()):
        if name not in carried_tags:
            problems.append(f"the tag {name} is not carried")
        elif carried_tags[name] != target:
            problems.append(
                f"the tag {name} names {carried_tags[name][:12]} in the bundle and {target[:12]} in the repository"
            )
    for name in sorted(set(carried_tags) - set(expected_tags)):
        problems.append(f"the bundle carries a tag {name} the repository does not have")
    return tuple(problems), head, tuple(sorted(carried_tags))


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
        members, embedded, unsafe, duplicate = _archive_members(target, prefix=prefix)
    except tarfile.TarError as exc:
        raise SourceBundleError("bundle_unreadable", f"{target} could not be read: {exc}")
    carried = bool(embedded)
    repository, head, tags = (
        _repository_report(embedded, root=root, ref=ref) if carried else ((), "", ())
    )

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
        carried=carried,
        head=head,
        tags=tags,
        repository=repository,
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
