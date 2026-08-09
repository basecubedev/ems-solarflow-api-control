# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unpacking an OS update archive without trusting it.

An update artifact is an archive from outside this host. It is verified before
it is opened — the manifest's signature, then the archive's SHA-256 — but a
verified archive is still an archive, so extraction refuses everything that
would let a member decide where it lands or how large it gets.

Nothing here streams to a block device. The archive is extracted into a
root-owned staging directory on the persistent partition, every member's digest
is checked against the manifest there, and only then may the writer read the
files back. An archive written straight to a partition would have to be trusted
before it could be checked, which is the wrong order.
"""

import os
import shutil
import subprocess
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from appliance.commands import EXECUTABLES
from appliance.os_releases import ReleaseError, file_digest

# Python's tarfile learned zstd in 3.14 and the appliance runs 3.13, so a
# .tar.zst is streamed through the allowlisted binary. Streamed, not copied
# out: the staging partition holds one copy of a multi-gigabyte artifact.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

MAX_MEMBERS = 16
MAX_MEMBER_BYTES = 12 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024 * 1024
STAGING_MODE = 0o700
MEMBER_MODE = 0o600


class ArtifactError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StagedArtifact:
    directory: Path
    members: dict
    total_bytes: int

    def path(self, name):
        try:
            return self.members[name]
        except KeyError:
            raise ArtifactError("artifact_member_missing", f"{name} was not staged")

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "members": {name: str(path) for name, path in sorted(self.members.items())},
            "total_bytes": self.total_bytes,
        }


def _refuse(member, reason):
    raise ArtifactError(
        "artifact_member_refused", f"{member.name!r} was refused: {reason}"
    )


def _check_member(member, expected_names):
    if not member.isfile():
        _refuse(member, "an update archive holds regular files only")
    if member.issym() or member.islnk():
        _refuse(member, "links are never extracted")
    if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
        _refuse(member, "device nodes are never extracted")
    name = member.name
    if name.startswith("/") or name.startswith("\\"):
        _refuse(member, "an absolute path")
    normalised = os.path.normpath(name)
    if normalised.startswith("..") or ".." in Path(normalised).parts:
        _refuse(member, "a parent-directory traversal")
    if normalised != name or "/" in name:
        _refuse(member, "a nested path; every member sits at the archive root")
    if name not in expected_names:
        _refuse(member, "not a member this appliance knows how to write")
    if member.size > MAX_MEMBER_BYTES:
        _refuse(member, f"{member.size} bytes exceeds the {MAX_MEMBER_BYTES}-byte member limit")
    return name


def _is_zstd(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == ZSTD_MAGIC
    except OSError:
        return False


def _zstd_binary():
    for candidate in EXECUTABLES["zstd"]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise ArtifactError(
        "artifact_decompressor_missing",
        "zstd is not installed, so a .tar.zst update artifact cannot be read",
    )


@contextmanager
def open_archive(path):
    """Read an update archive, whatever rpi-image-gen compressed it with.

    The argument vector is fixed and the path comes from the release catalogue,
    never from a request. No shell is involved.
    """

    if not _is_zstd(path):
        with tarfile.open(path, mode="r:*") as archive:
            yield archive
        return

    process = subprocess.Popen(
        [_zstd_binary(), "-d", "-c", "--", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            yield archive
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        # A refused member leaves the decompressor writing into a closed pipe;
        # it has to be reaped either way, and never waited on indefinitely.
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def prepare_staging(directory):
    """A root-owned, empty staging directory on the persistent partition."""

    target = Path(directory)
    try:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        os.chmod(target, STAGING_MODE)
        if os.geteuid() == 0:
            os.chown(target, 0, 0)
    except OSError as exc:
        raise ArtifactError("staging_unusable", f"the staging directory is unusable: {exc}")
    return target


def extract(archive_path, staging_dir, release):
    """Extract exactly the members ``release`` declares, and verify each one."""

    expected = set(release.members)
    target = prepare_staging(staging_dir)
    members = {}
    total = 0

    try:
        with open_archive(archive_path) as archive:
            seen = 0
            while True:
                member = archive.next()
                if member is None:
                    break
                seen += 1
                if seen > MAX_MEMBERS:
                    raise ArtifactError(
                        "artifact_member_refused",
                        f"the archive holds more than {MAX_MEMBERS} members",
                    )
                name = _check_member(member, expected)
                if name in members:
                    raise ArtifactError(
                        "artifact_member_refused", f"{name} appears twice in the archive"
                    )
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise ArtifactError(
                        "artifact_too_large",
                        f"the archive expands past the {MAX_TOTAL_BYTES}-byte limit",
                    )
                destination = target / name
                source = archive.extractfile(member)
                if source is None:
                    _refuse(member, "it has no readable content")
                _write_member(source, destination, member.size)
                members[name] = destination
    except tarfile.TarError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise ArtifactError("artifact_unreadable", f"the update archive could not be read: {exc}")
    except (ArtifactError, ReleaseError):
        shutil.rmtree(target, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise ArtifactError("staging_unusable", f"the archive could not be staged: {exc}")

    missing = sorted(expected - set(members))
    if missing:
        shutil.rmtree(target, ignore_errors=True)
        raise ArtifactError(
            "artifact_member_missing", f"the archive is missing {', '.join(missing)}"
        )

    try:
        for name, path in members.items():
            observed = file_digest(path)
            declared = release.member(name).digest
            if observed != declared:
                raise ArtifactError(
                    "artifact_member_digest_mismatch",
                    f"{name} hashes to {observed}, the manifest declares {declared}",
                )
    except (ArtifactError, ReleaseError):
        shutil.rmtree(target, ignore_errors=True)
        raise

    _sync_directory(target)
    return StagedArtifact(directory=target, members=members, total_bytes=total)


def _write_member(source, destination, expected_size):
    """Copy a member out with an explicit size bound and a flush.

    The declared size is not trusted on its own: a stream that keeps producing
    bytes past it is a decompression bomb, so the copy stops at the bound and
    fails instead of filling the persistent partition.
    """

    written = 0
    handle = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, MEMBER_MODE)
    try:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            written += len(block)
            if written > expected_size:
                raise ArtifactError(
                    "artifact_member_refused",
                    f"{destination.name} produced more bytes than it declared",
                )
            os.write(handle, block)
        if written != expected_size:
            raise ArtifactError(
                "artifact_member_refused",
                f"{destination.name} produced {written} of {expected_size} declared bytes",
            )
        os.fsync(handle)
    finally:
        os.close(handle)
    return written


def _sync_directory(path):
    try:
        handle = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(handle)
    except OSError:
        return False
    finally:
        os.close(handle)
    return True


def discard(staging_dir):
    """Remove a staging directory. Idempotent; a partial one is never kept."""

    shutil.rmtree(Path(staging_dir), ignore_errors=True)
    return True
