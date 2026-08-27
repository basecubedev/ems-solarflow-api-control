# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read an image's filesystems without mounting them.

A release gate has to answer what is *inside* the image that will be flashed:
which package version the root carries, whether the units are enabled,
whether a host key was shipped by accident. Mounting is how that is normally
done, and it is not available here for two independent reasons.

The Pi 5 root filesystem uses 16 KiB ext4 blocks. A kernel can only mount an
ext4 filesystem whose block size is at most its page size, so on any ordinary
4 KiB-page x86 build host that mount fails — and a check that cannot run is not
a check that passed. Mounting also needs root and a loop device, which a build
container and an unprivileged CI runner do not have.

So the structures are read directly out of the image file: the ext4 superblock,
group descriptors, inodes and extent trees, and the FAT boot parameter block,
allocation table and directory entries. Nothing here writes, and nothing here
needs a privilege.

This is a deliberately small reader. It resolves paths, reads regular files and
follows symlinks; it does not implement inline data, journals or anything an
inspection does not need. A filesystem using a feature it cannot read says so
rather than reporting an absent file.
"""

import struct
from pathlib import Path

EXT_SUPERBLOCK_OFFSET = 0x400
EXT_MAGIC = 0xEF53

INCOMPAT_64BIT = 0x80
INCOMPAT_EXTENTS = 0x40
INCOMPAT_INLINE_DATA = 0x8000
INCOMPAT_META_BG = 0x10

EXTENT_MAGIC = 0xF30A
EXTENTS_FLAG = 0x80000

ROOT_INODE = 2

S_IFMT = 0o170000
S_IFDIR = 0o040000
S_IFREG = 0o100000
S_IFLNK = 0o120000

FAT_ATTR_LONG_NAME = 0x0F
FAT_ATTR_DIRECTORY = 0x10
FAT_ATTR_VOLUME_ID = 0x08

MAX_SYMLINK_HOPS = 8


class FilesystemError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class _Window:
    """A bounded read-only view of one partition inside an image file."""

    def __init__(self, path, offset=0, size=None):
        self.path = Path(path)
        self.offset = int(offset)
        self.size = int(size) if size is not None else None

    def read(self, position, length):
        if position < 0 or length < 0:
            raise FilesystemError("filesystem_read_out_of_range", "a negative read was requested")
        if self.size is not None and position + length > self.size:
            raise FilesystemError(
                "filesystem_read_out_of_range",
                f"a read of {length} bytes at {position} leaves the partition",
            )
        with self.path.open("rb") as handle:
            handle.seek(self.offset + position)
            data = handle.read(length)
        if len(data) != length:
            raise FilesystemError(
                "filesystem_truncated", f"the image ends before {position + length}"
            )
        return data


class Ext4Reader:
    """Enough of ext4 to answer what the image root contains.

    Block size is read from the superblock, so a 16 KiB filesystem is read the
    same way a 4 KiB one is. That is the whole point: the host kernel's page
    size decides what can be mounted, and decides nothing here.
    """

    def __init__(self, path, offset=0, size=None):
        self._window = _Window(path, offset, size)
        superblock = self._window.read(EXT_SUPERBLOCK_OFFSET, 1024)
        if struct.unpack_from("<H", superblock, 0x38)[0] != EXT_MAGIC:
            raise FilesystemError("filesystem_not_ext4", "there is no ext4 superblock here")

        log_block_size = struct.unpack_from("<I", superblock, 0x18)[0]
        if log_block_size > 16:
            raise FilesystemError("filesystem_not_ext4", "the superblock declares no block size")
        self.block_size = 1024 << log_block_size
        self.inodes_per_group = struct.unpack_from("<I", superblock, 0x28)[0]
        self.inode_size = struct.unpack_from("<H", superblock, 0x58)[0] or 128
        if not self.inodes_per_group or self.inode_size < 128:
            raise FilesystemError(
                "filesystem_not_ext4", "the superblock describes no usable inode table"
            )
        self.first_data_block = struct.unpack_from("<I", superblock, 0x14)[0]
        self.feature_incompat = struct.unpack_from("<I", superblock, 0x60)[0]
        self.blocks_count = struct.unpack_from("<I", superblock, 0x4)[0]
        self.volume_name = superblock[0x78:0x88].split(b"\x00")[0].decode("ascii", "replace")
        descriptor_size = struct.unpack_from("<H", superblock, 0xFE)[0]
        self.descriptor_size = descriptor_size if self.feature_incompat & INCOMPAT_64BIT else 32
        if self.descriptor_size < 32:
            self.descriptor_size = 32

        unsupported = []
        if self.feature_incompat & INCOMPAT_INLINE_DATA:
            unsupported.append("inline_data")
        if self.feature_incompat & INCOMPAT_META_BG:
            unsupported.append("meta_bg")
        if unsupported:
            raise FilesystemError(
                "filesystem_feature_unsupported",
                f"this reader does not implement {', '.join(unsupported)}",
            )

    # --- blocks and inodes ------------------------------------------------

    def _block(self, number, count=1):
        return self._window.read(number * self.block_size, self.block_size * count)

    def _group_descriptor(self, group):
        table_start = (self.first_data_block + 1) * self.block_size
        raw = self._window.read(table_start + group * self.descriptor_size, self.descriptor_size)
        inode_table = struct.unpack_from("<I", raw, 8)[0]
        if self.descriptor_size >= 64:
            inode_table |= struct.unpack_from("<I", raw, 40)[0] << 32
        return inode_table

    def _inode(self, number):
        group, index = divmod(number - 1, self.inodes_per_group)
        table = self._group_descriptor(group)
        raw = self._window.read(
            table * self.block_size + index * self.inode_size, self.inode_size
        )
        mode = struct.unpack_from("<H", raw, 0)[0]
        size = struct.unpack_from("<I", raw, 4)[0] | (
            struct.unpack_from("<I", raw, 108)[0] << 32 if mode & S_IFMT == S_IFREG else 0
        )
        return {
            "mode": mode,
            "size": size,
            "flags": struct.unpack_from("<I", raw, 32)[0],
            "blocks_lo": struct.unpack_from("<I", raw, 28)[0],
            "block": raw[40:100],
        }

    def _extent_blocks(self, data):
        """Every (file block, disk block, length) run of an extent tree node."""

        magic, entries, _max, depth = struct.unpack_from("<HHHH", data, 0)
        if magic != EXTENT_MAGIC:
            raise FilesystemError("filesystem_unreadable", "an extent node has no magic")
        runs = []
        for index in range(entries):
            entry = data[12 + index * 12 : 24 + index * 12]
            if depth == 0:
                file_block, length, start_hi, start_lo = struct.unpack("<IHHI", entry)
                # An uninitialised extent has the high bit of the length set.
                length &= 0x7FFF
                runs.append((file_block, (start_hi << 32) | start_lo, length))
            else:
                _file_block, leaf_lo, leaf_hi, _unused = struct.unpack("<IIHH", entry)
                child = self._block((leaf_hi << 32) | leaf_lo)
                runs.extend(self._extent_blocks(child))
        return runs

    def _read_inode_data(self, inode):
        size = inode["size"]
        if not inode["flags"] & EXTENTS_FLAG:
            raise FilesystemError(
                "filesystem_feature_unsupported",
                "this reader implements extent-mapped files only",
            )
        chunks = {}
        for file_block, disk_block, length in self._extent_blocks(inode["block"]):
            for step in range(length):
                chunks[file_block + step] = disk_block + step
        data = bytearray()
        for index in range(0, (size + self.block_size - 1) // self.block_size):
            disk_block = chunks.get(index)
            if disk_block is None:
                data.extend(b"\x00" * self.block_size)
            else:
                data.extend(self._block(disk_block))
        return bytes(data[:size])

    # --- the directory tree -----------------------------------------------

    def _entries(self, inode):
        raw = self._read_inode_data(inode)
        entries = {}
        position = 0
        while position + 8 <= len(raw):
            number, rec_len, name_len, file_type = struct.unpack_from("<IHBB", raw, position)
            if rec_len < 8:
                break
            if number:
                name = raw[position + 8 : position + 8 + name_len].decode("utf-8", "replace")
                if name not in (".", ".."):
                    entries[name] = (number, file_type)
            position += rec_len
        return entries

    def _resolve(self, path, hops=0):
        if hops > MAX_SYMLINK_HOPS:
            raise FilesystemError("filesystem_symlink_loop", f"{path} loops through symlinks")
        number = ROOT_INODE
        inode = self._inode(number)
        parts = [part for part in str(path).split("/") if part not in ("", ".")]
        for index, part in enumerate(parts):
            if inode["mode"] & S_IFMT != S_IFDIR:
                return None
            entry = self._entries(inode).get(part)
            if entry is None:
                return None
            number = entry[0]
            inode = self._inode(number)
            if inode["mode"] & S_IFMT == S_IFLNK and index < len(parts) - 1:
                target = self._link_target(inode)
                prefix = "/".join(parts[:index])
                base = target if target.startswith("/") else f"/{prefix}/{target}"
                inode = self._resolve(f"{base}/{'/'.join(parts[index + 1:])}", hops + 1)
                return inode
        return inode

    def _link_target(self, inode):
        if inode["size"] < 60 and inode["blocks_lo"] == 0:
            return inode["block"][: inode["size"]].decode("utf-8", "replace")
        return self._read_inode_data(inode).decode("utf-8", "replace")

    # --- what an inspection asks ------------------------------------------

    def exists(self, path):
        return self._resolve(path) is not None

    def is_file(self, path):
        inode = self._resolve(path)
        return bool(inode) and inode["mode"] & S_IFMT == S_IFREG

    def is_dir(self, path):
        inode = self._resolve(path)
        return bool(inode) and inode["mode"] & S_IFMT == S_IFDIR

    def is_symlink(self, path):
        """Whether the final component itself is a link, without following it."""

        parent, _, name = str(path).rstrip("/").rpartition("/")
        inode = self._resolve(parent or "/")
        if inode is None or inode["mode"] & S_IFMT != S_IFDIR:
            return False
        entry = self._entries(inode).get(name)
        if entry is None:
            return False
        return self._inode(entry[0])["mode"] & S_IFMT == S_IFLNK

    def readlink(self, path):
        parent, _, name = str(path).rstrip("/").rpartition("/")
        inode = self._resolve(parent or "/")
        entry = self._entries(inode).get(name) if inode else None
        if entry is None:
            raise FilesystemError("filesystem_path_missing", f"{path} does not exist")
        return self._link_target(self._inode(entry[0]))

    def listdir(self, path):
        inode = self._resolve(path)
        if inode is None or inode["mode"] & S_IFMT != S_IFDIR:
            return ()
        return tuple(sorted(self._entries(inode)))

    def read_bytes(self, path):
        inode = self._resolve(path)
        if inode is None:
            raise FilesystemError("filesystem_path_missing", f"{path} does not exist")
        if inode["mode"] & S_IFMT == S_IFLNK:
            return self._link_target(inode).encode("utf-8")
        if inode["mode"] & S_IFMT != S_IFREG:
            raise FilesystemError("filesystem_not_a_file", f"{path} is not a regular file")
        return self._read_inode_data(inode)

    def read_text(self, path):
        return self.read_bytes(path).decode("utf-8", "replace")


class FatReader:
    """Enough of FAT12/16/32 to read a Raspberry Pi boot partition."""

    def __init__(self, path, offset=0, size=None):
        self._window = _Window(path, offset, size)
        boot = self._window.read(0, 512)
        self.bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
        self.sectors_per_cluster = boot[13]
        reserved = struct.unpack_from("<H", boot, 14)[0]
        self.fat_count = boot[16]
        self.root_entries = struct.unpack_from("<H", boot, 17)[0]
        total_16 = struct.unpack_from("<H", boot, 19)[0]
        fat_size_16 = struct.unpack_from("<H", boot, 22)[0]
        total_32 = struct.unpack_from("<I", boot, 32)[0]
        fat_size_32 = struct.unpack_from("<I", boot, 36)[0]

        if not self.bytes_per_sector or not self.sectors_per_cluster or not self.fat_count:
            raise FilesystemError("filesystem_not_fat", "there is no FAT boot parameter block here")

        self.fat_size = fat_size_16 or fat_size_32
        self.total_sectors = total_16 or total_32
        self.fat_start = reserved * self.bytes_per_sector
        root_sectors = (self.root_entries * 32 + self.bytes_per_sector - 1) // self.bytes_per_sector
        self.root_start = self.fat_start + self.fat_count * self.fat_size * self.bytes_per_sector
        self.data_start = self.root_start + root_sectors * self.bytes_per_sector
        self.cluster_size = self.sectors_per_cluster * self.bytes_per_sector

        data_sectors = self.total_sectors - (
            reserved + self.fat_count * self.fat_size + root_sectors
        )
        clusters = data_sectors // self.sectors_per_cluster if self.sectors_per_cluster else 0
        self.bits = 12 if clusters < 4085 else (16 if clusters < 65525 else 32)
        self.root_cluster = struct.unpack_from("<I", boot, 44)[0] if self.bits == 32 else 0

    def _cluster(self, number):
        return self._window.read(
            self.data_start + (number - 2) * self.cluster_size, self.cluster_size
        )

    def _next_cluster(self, number):
        if self.bits == 32:
            raw = self._window.read(self.fat_start + number * 4, 4)
            value = struct.unpack("<I", raw)[0] & 0x0FFFFFFF
            return None if value >= 0x0FFFFFF8 else value
        if self.bits == 16:
            raw = self._window.read(self.fat_start + number * 2, 2)
            value = struct.unpack("<H", raw)[0]
            return None if value >= 0xFFF8 else value
        position = self.fat_start + number + number // 2
        raw = self._window.read(position, 2)
        value = struct.unpack("<H", raw)[0]
        value = (value >> 4) if number % 2 else (value & 0x0FFF)
        return None if value >= 0xFF8 else value

    def _chain(self, first):
        data = bytearray()
        cluster = first
        seen = set()
        while cluster and cluster >= 2 and cluster not in seen:
            seen.add(cluster)
            data.extend(self._cluster(cluster))
            cluster = self._next_cluster(cluster)
        return bytes(data)

    def _root_directory(self):
        if self.bits == 32:
            return self._chain(self.root_cluster)
        return self._window.read(self.root_start, self.root_entries * 32)

    def _parse_directory(self, raw):
        entries = {}
        long_name = []
        for position in range(0, len(raw) - 31, 32):
            entry = raw[position : position + 32]
            if entry[0] == 0x00:
                break
            if entry[0] == 0xE5:
                long_name = []
                continue
            attributes = entry[11]
            if attributes == FAT_ATTR_LONG_NAME:
                chars = entry[1:11] + entry[14:26] + entry[28:32]
                long_name.insert(0, chars.decode("utf-16-le", "ignore").split("\x00")[0])
                continue
            if attributes & FAT_ATTR_VOLUME_ID:
                long_name = []
                continue
            name = "".join(long_name) if long_name else self._short_name(entry)
            long_name = []
            cluster = struct.unpack_from("<H", entry, 26)[0] | (
                struct.unpack_from("<H", entry, 20)[0] << 16
            )
            entries[name] = {
                "cluster": cluster,
                "size": struct.unpack_from("<I", entry, 28)[0],
                "directory": bool(attributes & FAT_ATTR_DIRECTORY),
            }
        return entries

    @staticmethod
    def _short_name(entry):
        # Byte 12 carries the case flags a DOS name has instead of a long name:
        # cmdline.txt is stored as CMDLINE TXT with both of them set.
        flags = entry[12]
        stem = entry[0:8].decode("ascii", "replace").rstrip()
        suffix = entry[8:11].decode("ascii", "replace").rstrip()
        if flags & 0x08:
            stem = stem.lower()
        if flags & 0x10:
            suffix = suffix.lower()
        return f"{stem}.{suffix}" if suffix else stem

    def _entry(self, path):
        parts = [part for part in str(path).split("/") if part not in ("", ".")]
        entries = self._parse_directory(self._root_directory())
        found = None
        for index, part in enumerate(parts):
            match = next(
                (value for name, value in entries.items() if name.lower() == part.lower()), None
            )
            if match is None:
                return None
            found = match
            if index < len(parts) - 1:
                if not match["directory"]:
                    return None
                entries = self._parse_directory(self._chain(match["cluster"]))
        return found

    def exists(self, path):
        return self._entry(path) is not None

    def is_file(self, path):
        entry = self._entry(path)
        return bool(entry) and not entry["directory"]

    def listdir(self, path="/"):
        if str(path).strip("/") == "":
            return tuple(sorted(self._parse_directory(self._root_directory())))
        entry = self._entry(path)
        if entry is None or not entry["directory"]:
            return ()
        return tuple(sorted(self._parse_directory(self._chain(entry["cluster"]))))

    def read_bytes(self, path):
        entry = self._entry(path)
        if entry is None:
            raise FilesystemError("filesystem_path_missing", f"{path} does not exist")
        if entry["directory"]:
            raise FilesystemError("filesystem_not_a_file", f"{path} is a directory")
        return self._chain(entry["cluster"])[: entry["size"]]

    def read_text(self, path):
        return self.read_bytes(path).decode("utf-8", "replace")


def open_partition(image_path, partition):
    """The reader for whatever filesystem this partition actually carries."""

    for reader in (Ext4Reader, FatReader):
        try:
            return reader(image_path, partition.offset, partition.size_bytes)
        except FilesystemError:
            continue
    raise FilesystemError(
        "filesystem_unrecognised",
        f"partition {partition.label or partition.number} carries neither ext4 nor FAT",
    )
