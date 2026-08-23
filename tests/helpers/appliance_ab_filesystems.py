# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real filesystem images for the update-artifact fixtures.

An expanded ``boot`` member is a FAT filesystem and an expanded ``system``
member is ext4. Testing the writer against arbitrary bytes proves the digests
agree; it does not prove the thing on the partition is a filesystem at all,
which is exactly the failure a sparse container produces.

``mformat`` builds a genuine FAT here whenever mtools is installed. ext4 needs
``mke2fs``; where that is missing the builder produces a superblock-only image,
which is enough for type detection and explicitly not enough for a mount. What
cannot be built is reported through :func:`unavailable`, never silently
downgraded.
"""

import os
import shutil
import struct
import subprocess
import tempfile

EXT4_SUPER_MAGIC = 0xEF53
EXT4_MAGIC_OFFSET = 0x438

FAT_TOOL = "mformat"
EXT4_TOOL = "mke2fs"


def unavailable(kind):
    """Why a real filesystem of ``kind`` cannot be built here, or nothing."""

    if kind == "vfat":
        return None if shutil.which(FAT_TOOL) else f"{FAT_TOOL} (mtools) is not installed"
    if kind == "ext4":
        return None if shutil.which(EXT4_TOOL) else f"{EXT4_TOOL} (e2fsprogs) is not installed"
    return f"{kind} is not a filesystem this helper builds"


def fat_image(size, *, label="BOOT", files=None, real_only=False):
    """A real FAT filesystem, or a plausible boot sector when mtools is absent.

    ``real_only`` refuses the substitute, for the assertions that only mean
    something against a filesystem this helper did not write by hand.
    """

    reason = unavailable("vfat")
    if reason:
        if real_only:
            raise SyntheticFilesystem(reason)
        return _fat_boot_sector(size, label=label)
    handle, path = tempfile.mkstemp(suffix=".vfat")
    os.close(handle)
    try:
        with open(path, "wb") as target:
            target.truncate(size)
        subprocess.run(
            [shutil.which(FAT_TOOL), "-i", path, "-v", label, "::"],
            check=True,
            capture_output=True,
            timeout=60,
            env={"PATH": "/usr/bin:/bin", "MTOOLS_SKIP_CHECK": "1"},
        )
        for name, payload in (files or {}).items():
            _copy_into_fat(path, name, payload)
        with open(path, "rb") as source:
            return source.read()
    finally:
        os.unlink(path)


def _copy_into_fat(image, name, payload):
    handle, staged = tempfile.mkstemp()
    try:
        with os.fdopen(handle, "wb") as target:
            target.write(payload)
        subprocess.run(
            [shutil.which("mcopy"), "-i", image, staged, f"::/{name}"],
            check=True,
            capture_output=True,
            timeout=60,
            env={"PATH": "/usr/bin:/bin", "MTOOLS_SKIP_CHECK": "1"},
        )
    finally:
        os.unlink(staged)


def _fat_boot_sector(size, *, label):
    """Enough of a FAT16 boot sector for ``file`` and a type probe to agree."""

    sector = bytearray(b"\x00" * 512)
    sector[0:3] = b"\xeb\x3c\x90"
    sector[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", sector, 11, 512)
    sector[13] = 4
    struct.pack_into("<H", sector, 14, 4)
    sector[16] = 2
    struct.pack_into("<H", sector, 17, 512)
    struct.pack_into("<H", sector, 19, min(size // 512, 0xFFFF))
    sector[21] = 0xF8
    struct.pack_into("<H", sector, 22, 32)
    sector[43:54] = label.ljust(11).encode("ascii")[:11]
    sector[54:62] = b"FAT16   "
    sector[510:512] = b"\x55\xaa"
    return bytes(sector) + b"\x00" * (size - 512)


class SyntheticFilesystem(RuntimeError):
    """A caller asked for a real filesystem and this host cannot build one."""


def ext4_image(size, *, label="SYSTEM", real_only=False):
    """A real ext4 filesystem, or a superblock the type probe recognises.

    ``real_only`` is for the assertions that are only worth anything against a
    real one: degrading silently there turns a filesystem check into a
    round-trip of bytes this helper wrote itself.
    """

    reason = unavailable("ext4")
    if reason:
        if real_only:
            raise SyntheticFilesystem(reason)
        return _ext4_superblock(size, label=label)
    handle, path = tempfile.mkstemp(suffix=".ext4")
    os.close(handle)
    try:
        with open(path, "wb") as target:
            target.truncate(size)
        subprocess.run(
            [shutil.which(EXT4_TOOL), "-q", "-t", "ext4", "-L", label, "-F", path],
            check=True,
            capture_output=True,
            timeout=120,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        with open(path, "rb") as source:
            return source.read()
    finally:
        os.unlink(path)


def _ext4_superblock(size, *, label):
    image = bytearray(b"\x00" * size)
    struct.pack_into("<H", image, EXT4_MAGIC_OFFSET, EXT4_SUPER_MAGIC)
    struct.pack_into("<I", image, 0x400 + 0x18, 2)
    image[0x400 + 0x78 : 0x400 + 0x78 + 16] = label.ljust(16, "\x00").encode("ascii")[:16]
    return bytes(image)


def filesystem_of(blob):
    """The filesystem a raw image holds, by magic. Nothing is mounted."""

    if len(blob) > EXT4_MAGIC_OFFSET + 2:
        magic = struct.unpack_from("<H", blob, EXT4_MAGIC_OFFSET)[0]
        if magic == EXT4_SUPER_MAGIC:
            return "ext4"
    if len(blob) > 512 and blob[510:512] == b"\x55\xaa" and blob[0:1] in (b"\xeb", b"\xe9"):
        return "vfat"
    return ""
