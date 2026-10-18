# SPDX-License-Identifier: AGPL-3.0-or-later
"""File primitives the EMS's state files share: one lock across processes,
one way to replace a file whole.

``runtime-state.json`` and ``schedules.json`` are each written by the EMS,
its dashboard and emsctl. Import-side-effect-free.
"""

import contextlib
import fcntl
import logging
import os
import secrets
import threading
import time

from ems.logging_utils import log_event

LOCK_TIMEOUT_SECONDS = 2.0
STARTUP_LOCK_TIMEOUT_SECONDS = 30.0
STALE_TEMP_AGE_SECONDS = 3600
_LOCKS_UNAVAILABLE = set()


class FileLockTimeout(TimeoutError):
    """Another process held a lock longer than a writer waits for it."""


def lock_timed_out(exc):
    """Whether ``exc`` is, or was raised because of, a FileLockTimeout."""

    while exc is not None:
        if isinstance(exc, FileLockTimeout):
            return True
        exc = exc.__cause__
    return False


@contextlib.contextmanager
def exclusive_file_lock(path, timeout=None):
    """An exclusive flock on ``path``, shared by every process on the host.

    Waits at most ``timeout`` seconds, because the control loop is among the
    writers and a stalled emsctl must not stall it; then FileLockTimeout. A
    writer that cannot open the lock file -- a directory it may not create
    files in -- or whose filesystem has no flock -- an NFS mount without a
    lock daemon -- goes on without it, as every writer did before there was
    one.
    """

    timeout = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)
    except OSError as exc:
        _lock_unavailable(path, exc)
        yield
        return
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(f"{path} is held by another process") from None
                time.sleep(0.02)
            except OSError as exc:
                _lock_unavailable(path, exc)
                break
        yield
    finally:
        os.close(fd)


def _lock_unavailable(path, exc):
    if path not in _LOCKS_UNAVAILABLE:
        _LOCKS_UNAVAILABLE.add(path)
        log_event(logging.WARNING, "file_lock_unavailable", path=path, error=exc)


def file_signature(stat):
    """What tells one version of a file from the next, from its stat.

    Two replaces within one tick of the filesystem's coarse clock share an
    mtime; each brings its own inode, as every writer here replaces the file
    through a temp file of its own.
    """

    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def remove_stale_temp_files(path, *, now=None):
    """Remove ``<path>.<pid>.<thread>[.<token>].tmp`` files a killed writer left behind.

    Only names this module's writer makes, and only ones older than an hour:
    a write holds its temp file for milliseconds. Returns the names removed.
    """

    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + "."
    now = time.time() if now is None else now
    removed = []
    try:
        names = os.listdir(directory)
    except OSError:
        return removed
    for name in names:
        middle = name[len(prefix):-len(".tmp")] if name.startswith(prefix) and name.endswith(".tmp") else ""
        parts = middle.split(".")
        if not (len(parts) in (2, 3) and parts[0].isdigit() and parts[1].isdigit()):
            continue
        if len(parts) == 3 and not (len(parts[2]) == 8 and all(c in "0123456789abcdef" for c in parts[2])):
            continue
        candidate = os.path.join(directory, name)
        with contextlib.suppress(OSError):
            if now - os.lstat(candidate).st_mtime >= STALE_TEMP_AGE_SECONDS and os.path.isfile(candidate):
                os.remove(candidate)
                removed.append(name)
    return removed


def atomic_write_bytes(path, raw):
    """Replace ``path`` with ``raw`` through a temp file of this write's own.

    The EMS and the Admin write the same bind-mounted directory from separate
    PID namespaces, where both can be PID 1; the random token keeps their
    temp files apart, and ``x`` refuses a name that exists.
    """

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp_path, "xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_path)
        raise
