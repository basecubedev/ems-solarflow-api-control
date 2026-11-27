# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Refuse every write a test makes into the checkout's own ``data/``.

``data/`` holds what a developer's local EMS and Admin keep: the deployment
marker, the Guided Setup workflow, the state database. A test that resolved a
default path instead of a temporary one overwrote them, and under parallel
workers two such tests share the file.

A Python audit hook sees each write before it happens, from any thread of the
test's own process, refuses it with ``CheckoutDataWrite`` and remembers it, so
``tests/conftest.py`` fails the test even where the code under test caught the
refusal. ``_PATH_ARGUMENTS`` names, per audit event, where its paths and their
directory descriptors sit and whether the event follows a symbolic link in the
last component: removing or renaming a link judges the link, writing through
it judges its target. Changing a file's owner or times is judged on the name
given, as the audit event does not say whether it follows a link. A subprocess
the test starts is not seen, and a relative name opened through a directory
descriptor is read against the working directory, as the audit event for
``open`` does not carry the descriptor.

Import-side-effect-free: ``install()`` adds the hook.
"""

import os
import sys
from pathlib import Path

ROOT = os.path.realpath(Path(__file__).resolve().parents[1] / "data")

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_PATH_ARGUMENTS = {
    "os.mkdir": ((0, 2, False),),
    "os.rename": ((0, 2, False), (1, 3, False)),
    "os.link": ((1, 3, False),),
    "os.symlink": ((1, 2, False),),
    "os.remove": ((0, 1, False),),
    "os.rmdir": ((0, 1, False),),
    "shutil.rmtree": ((0, 1, False),),
    "os.truncate": ((0, None, True),),
    "os.chmod": ((0, 2, True),),
    "os.chown": ((0, 3, False),),
    "os.utime": ((0, 3, False),),
    "sqlite3.connect": ((0, None, True),),
}

_refused = []
_installed = []


class CheckoutDataWrite(AssertionError):
    """A test wrote into the checkout's own ``data/``."""


def install():
    """Add the audit hook once per process."""

    if not _installed:
        sys.addaudithook(_audit)
        _installed.append(True)


def take():
    """The writes refused since the last call, as ``(event, path)``; forgets them."""

    found = _refused[:]
    del _refused[: len(found)]
    return found


def _audit(event, args):
    if event == "open":
        if not _opens_for_writing(args):
            return
        paths = ((args[0], None, True),)
    elif event in _PATH_ARGUMENTS:
        paths = tuple(
            (args[index], _argument(args, folder), follows)
            for index, folder, follows in _PATH_ARGUMENTS[event]
            if index < len(args)
        )
    else:
        return
    for path, dir_fd, follows in paths:
        resolved = _inside_root(path, dir_fd, follows)
        if resolved is not None:
            _refused.append((event, resolved))
            raise CheckoutDataWrite(
                f"{event} {resolved}: a test must not write into the checkout's data/; "
                "use tmp_path, EMS_ADMIN_DATA_DIR, EMS_INSTALL_DIR or isolated_install_root"
            )


def _opens_for_writing(args):
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    if isinstance(mode, str):
        return any(letter in mode for letter in "wax+")
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _argument(args, index):
    return args[index] if index is not None and index < len(args) else None


def _inside_root(path, dir_fd=None, follows=True):
    if path is None or isinstance(path, int):
        return None
    try:
        text = os.fsdecode(os.fspath(path))
    except (TypeError, ValueError):
        return None
    if text == ":memory:" or text.startswith("file:"):
        return None
    try:
        if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
            base = os.readlink(f"/proc/self/fd/{dir_fd}")
        else:
            base = os.getcwd()
        joined = os.path.join(base, text).rstrip(os.sep) or os.sep
        parent, name = os.path.split(joined)
        if follows or name in ("", ".", ".."):
            resolved = os.path.realpath(joined)
        else:
            resolved = os.path.join(os.path.realpath(parent), name)
    except OSError:
        return None
    if resolved == ROOT or resolved.startswith(ROOT + os.sep):
        return resolved
    return None
