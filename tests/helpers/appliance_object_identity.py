# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ACL manifest object identity, produced exactly as the packaged scripts do.

``setup-export-root.sh`` writes it and ``postrm`` compares against it, both
through one ``stat`` format. Rebuilding it here rather than reimplementing it
keeps the harnesses honest about what the shell actually records, and lets a
test construct the one case a filesystem will not produce on demand: a
replacement object that inherited the recorded device and inode.
"""

import subprocess

STAT_FORMAT = "%d|%i|%F|%u|%g|%w|%z"
UNKNOWN_GENERATION = ("-", "?", "")


def object_identity(path):
    result = subprocess.run(
        ["stat", "-c", STAT_FORMAT, str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    raw = (result.stdout or "").splitlines()
    if result.returncode != 0 or not raw:
        return ""
    fields = raw[0].split("|")
    if len(fields) != 7:
        return ""
    device, inode, kind, uid, gid, birth, changed = fields
    generation = changed if birth in UNKNOWN_GENERATION else birth
    identity = ":".join([device, inode, _flatten(kind), uid, gid, _flatten(generation)])
    version = inode_version(path)
    return f"{identity}:v{version}" if version else identity


def inode_version(path):
    """The optional half of the identity, or "" wherever it cannot be read.

    ``lsattr`` is not a dependency of this package and no filesystem is obliged
    to answer it, so a host without it is a normal host and not a broken test
    environment. Feature detection here, exactly as the packaged shell does.
    """

    try:
        result = subprocess.run(
            ["lsattr", "-d", "-v", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    first = (result.stdout or "").split()
    return first[0] if first and first[0].isdigit() else ""


def _flatten(value):
    return value.replace(" ", "_").replace("\t", "_")


def identity_with_reused_inode(recorded, replacement):
    """The recorded identity as it reads once a replacement inherited the inode.

    Device and inode come from the object that is there now — exactly what a
    filesystem that hands a released inode straight back produces — while every
    other field still describes the object the manifest recorded.

    Creation timestamps have the resolution of a clock tick, so two objects made
    microseconds apart can carry the same one. That is a property of the host
    clock, not of the identity under test, and a test that silently degraded
    into "these two are the same object" would prove nothing. Where the two
    collide the recorded generation is made explicitly distinct, so the case
    exercised is always the intended one: the recorded device and inode, on an
    object this package never granted.
    """

    current = object_identity(replacement)
    if not recorded or not current:
        return recorded
    fresh = current.split(":", 2)
    original = recorded.split(":", 2)
    if len(fresh) < 3 or len(original) < 3:
        return recorded
    tail = original[2]
    if tail == fresh[2]:
        tail = f"{tail}-recorded-before-the-replacement"
    return f"{fresh[0]}:{fresh[1]}:{tail}"
