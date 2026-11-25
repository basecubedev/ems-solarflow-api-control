# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hidden staging folders a backup owns while it runs, and the sweep that
removes the ones a killed backup left behind.

A folder is made with a claim file inside, which its process holds under an
exclusive ``flock`` until the folder is gone. The kernel drops that lock when
the process ends, however it ends, so a claim whose lock can be taken belongs
to no running backup. The claim's content is written only while the lock is
held, and names its owner and the folder's kind.

The sweep deletes a folder only with that proof: a real directory directly in
the parent, named with the kind's prefix and owned by this process's account,
holding a regular claim file of that account that names this owner and that
kind and whose lock it could take. A folder with no claim, an unreadable or
foreign one, or one still locked stays where it is.

A process uses only a folder it found empty when it made it and that shows
the same owner as the claim it then created in it, on whatever filesystem:
one someone moved in under the name is refused untouched. It removes its own
folder by what it put there and nothing else, and whatever someone added
keeps the folder standing. Nothing is ever removed recursively.

Import-side-effect-free.
"""

import errno
import fcntl
import json
import os
import stat
import tempfile
from datetime import datetime, timezone

DATABASE_SNAPSHOTS = ".ems-db-backup-"
ARCHIVE_PARTS = ".ems-backup-part-"
IMPORT_UPLOADS = ".ems-backup-import-"
RESTORE_PARTS = ".ems-restore-part-"
KINDS = (DATABASE_SNAPSHOTS, ARCHIVE_PARTS, IMPORT_UPLOADS, RESTORE_PARTS)

CLAIM_NAME = ".ems-staging-claim"
CLAIM_OWNER = "ems-backup"
CLAIM_SCHEMA = 1
CLAIM_MAX_BYTES = 4096
SQLITE_COMPANIONS = ("-journal", "-wal", "-shm")

_NO_FILE_LOCKS = frozenset({errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP})


class StagingFolder:
    """A claimed staging folder; ``release()`` removes it, then drops the lock.

    The folder is held open as the descriptor ``claim_staging`` checked, and
    ``create``, ``reach`` and ``move_into_place`` go through it, never through
    the folder's path again: in a parent another account may write, that path
    can be renamed away and replaced, the descriptor cannot.
    """

    def __init__(self, path, claim_fd, folder_fd=None):
        self.path = path
        self._claim_fd = claim_fd
        self._folder_fd = folder_fd
        self._created = []
        self._released = False

    def new_file(self, name):
        """An empty file in this folder, readable by its owner only; return a path to it.

        The path leads through the folder's descriptor (``reach``), so an API
        that reopens it by path writes into this folder even after the folder's
        own name was renamed away and replaced. A file written over keeps its
        mode, and an archive linked out of the folder is this same file, so a
        backup stays 0600 in the backup folder.
        """

        os.close(self.create(name))
        return self.reach(name)

    def create(self, name):
        """A new file in this folder, readable by its owner only, open for writing; return its descriptor."""

        if not name or os.sep in name or name in (".", "..", CLAIM_NAME):
            raise ValueError(f"not a staging file name: {name!r}")
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(name, flags, 0o600, dir_fd=self._folder_fd)
        self._created.append(name)
        return fd

    def reach(self, name):
        """A path to ``name`` that leads through the folder's descriptor, for an API that takes only a path."""

        return f"/proc/self/fd/{self._folder_fd}/{name}"

    def move_into_place(self, name, target):
        """Rename ``name`` from this folder to ``target``, which must be on the same filesystem."""

        os.replace(name, target, src_dir_fd=self._folder_fd)

    def release(self):
        """Remove what this process put in the folder, the claim last, then the folder.

        The claim is closed before the folder goes: on a network share a file
        still open when it is unlinked stays behind under another name until
        it is closed, and would keep the folder from being removed. Anything
        else in it keeps the folder standing.
        """

        if self._released:
            return
        self._released = True
        emptied = False
        folder, self._folder_fd = self._folder_fd, None
        try:
            if folder is not None:
                emptied = _remove_created(folder, self._created, self._claim_fd is not None)
        finally:
            if folder is not None:
                _close_quietly(folder)
            claim, self._claim_fd = self._claim_fd, None
            if claim is not None:
                _close_quietly(claim)
        if emptied:
            _remove_empty(self.path)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.release()


def claim_staging(parent, kind):
    """Make and claim a new staging folder of ``kind`` in ``parent``.

    The claim is locked before a byte of it is written, so a sweep never sees
    a readable claim whose lock is free while its process lives. On a
    filesystem without file locks the folder gets no claim: it works as
    before, and a killed backup's folder is not swept there.
    """

    if kind not in KINDS:
        raise ValueError(f"unknown staging kind: {kind!r}")
    path = tempfile.mkdtemp(prefix=kind, dir=parent)
    folder = _open_folder(path)
    try:
        with os.scandir(folder) as listing:
            found = next(listing, None)
    except BaseException:
        _close_quietly(folder)
        raise
    if found is not None:
        _close_quietly(folder)
        raise OSError(errno.EEXIST, f"{path} is not the empty folder this process made")
    try:
        fd = os.open(CLAIM_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=folder)
    except BaseException:
        _close_quietly(folder)
        raise
    if os.fstat(folder).st_uid != os.fstat(fd).st_uid:
        _close_quietly(fd)
        try:
            os.unlink(CLAIM_NAME, dir_fd=folder)
        except OSError:
            pass
        _close_quietly(folder)
        raise OSError(errno.EPERM, f"{path} is not a folder this process made")
    staging = StagingFolder(path, None, folder)
    try:
        staging._claim_fd = fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            if exc.errno not in _NO_FILE_LOCKS:
                raise
            staging._claim_fd = None
            _close_quietly(fd)
            os.unlink(CLAIM_NAME, dir_fd=folder)
        else:
            os.write(fd, _claim_text(kind))
            os.fsync(fd)
    except BaseException:
        staging.release()
        raise
    return staging


def sweep_abandoned(parent, kinds):
    """Remove the folders of ``kinds`` in ``parent`` a killed backup left; return their paths."""

    try:
        with os.scandir(parent) as listing:
            entries = list(listing)
    except OSError:
        return []
    removed = []
    for entry in entries:
        kind = next((item for item in kinds if entry.name.startswith(item)), None)
        if kind is None:
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if _remove_if_abandoned(entry.path, kind):
            removed.append(entry.path)
    return removed


def _claim_text(kind):
    claim = {
        "schema": CLAIM_SCHEMA,
        "owner": CLAIM_OWNER,
        "kind": kind,
        "pid": os.getpid(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return json.dumps(claim, sort_keys=True).encode("utf-8")


def _valid_claim(raw, kind):
    try:
        claim = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return (
        isinstance(claim, dict)
        and claim.get("schema") == CLAIM_SCHEMA
        and claim.get("owner") == CLAIM_OWNER
        and claim.get("kind") == kind
    )


def _remove_if_abandoned(path, kind):
    try:
        folder = _open_folder(path)
    except OSError:
        return False
    try:
        try:
            fd = os.open(CLAIM_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
        except OSError:
            return False
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                return False
            if not _ours(os.fstat(folder)) or not _ours(opened):
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            raw = os.read(fd, CLAIM_MAX_BYTES + 1)
            if len(raw) > CLAIM_MAX_BYTES or not _valid_claim(raw, kind):
                return False
            current = os.stat(CLAIM_NAME, dir_fd=folder, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                return False
            if not _empty_folder(folder):
                return False
        finally:
            os.close(fd)
    except OSError:
        return False
    finally:
        os.close(folder)
    return _remove_empty(path)


def _open_folder(path):
    """The folder itself, never what a link in its place points to."""

    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _ours(status):
    """Whether this process's account owns the folder or claim: no other account can make one so."""

    return status.st_uid == os.geteuid()


def _remove_created(folder, created, claimed):
    """Unlink the files this process created in ``folder``, SQLite's companions with them, the claim last.

    The claim goes only when nothing else is left: a name that is no longer a
    file, or anything someone added, keeps the folder and its proof standing
    for a later sweep.
    """

    try:
        for name in created:
            for suffix in ("", *SQLITE_COMPANIONS):
                try:
                    os.unlink(name + suffix, dir_fd=folder)
                except FileNotFoundError:
                    pass
        with os.scandir(folder) as listing:
            if any(entry.name != CLAIM_NAME for entry in listing):
                return False
        if claimed:
            os.unlink(CLAIM_NAME, dir_fd=folder)
    except OSError:
        return False
    return True


def _empty_folder(folder):
    """Remove an opened folder's files, its claim after everything else.

    No staging kind makes subfolders, so a directory in it is not a staged
    file and keeps the folder standing; nothing is removed recursively. A
    removal cut short leaves the claim with whatever is left, so a later
    sweep finishes it; at most an empty folder can stay behind.
    """

    try:
        with os.scandir(folder) as listing:
            entries = [entry for entry in listing if entry.name != CLAIM_NAME]
        if any(entry.is_dir(follow_symlinks=False) for entry in entries):
            return False
        for entry in entries:
            os.unlink(entry.name, dir_fd=folder)
        try:
            os.unlink(CLAIM_NAME, dir_fd=folder)
        except FileNotFoundError:
            pass
    except OSError:
        return False
    return True


def _close_quietly(fd):
    """Close ``fd``; Linux frees it even when close reports an error, so the error changes nothing."""

    try:
        os.close(fd)
    except OSError:
        pass


def _remove_empty(path):
    try:
        os.rmdir(path)
    except OSError:
        return False
    return True
