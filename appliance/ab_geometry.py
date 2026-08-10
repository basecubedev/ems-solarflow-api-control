# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where a partition ends, and where the disk it lives on ends.

The first-boot growth helper used to decide "this partition already fills the
medium" from ``disk_bytes - partition_bytes <= slack``. For the *last* partition
of an A/B image that ignores the whole occupied prefix — six image-rota
partitions and roughly 17 GB of it — so a persistent partition that had already
been grown to the end of a 32 GB card still looked several gigabytes short. A
power cut between the partition growth and the marker therefore produced a medium
that retried on every boot, was told the partition could not be grown, and failed
the boot path forever.

So the question is answered from real geometry instead: the partition's start
and length, the disk's length, and — where the GPT can be read — the last usable
LBA the table itself declares. Tolerance covers partition alignment and, only
when the table could not be read, the GPT backup structures at the end of the
disk. Nothing here infers a size from another size.

Read-only, and sysfs first: the numbers come from the kernel's own view of the
block layer, so there is no output format to parse loosely and no partitioning
tool in the path of a component that answers requests.
"""

import struct
from dataclasses import dataclass
from pathlib import Path

# sysfs reports `start` and `size` in 512-byte units whatever the device's
# logical block size is. GPT LBAs are in logical blocks. The two are converted
# into each other here and nowhere else.
SYSFS_SECTOR = 512

GPT_SIGNATURE = b"EFI PART"
GPT_HEADER_LBA = 1
GPT_LAST_USABLE_OFFSET = 48

# One mebibyte of alignment grain: the boot helper's growth tool aligns a grown
# partition to it, so a partition that reaches the end of the medium still leaves
# up to this much tail.
ALIGNMENT_TOLERANCE_SECTORS = 2 * 1024 * 1024 // SYSFS_SECTOR

# Only used when the GPT could not be read: a backup header plus a 128-entry
# array of 128 bytes, the layout every tool in this project writes.
GPT_BACKUP_RESERVE_LBA = 33

FROM_GPT = "gpt_last_usable_lba"
FROM_DISK_END = "disk_end_minus_gpt_backup_reserve"

# A logical block size the block layer cannot actually present is a parse
# failure, not a device property.
LOGICAL_BLOCK_SIZES = (512, 1024, 2048, 4096)


class GeometryError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PartitionGeometry:
    """One partition and the disk it ends on, in 512-byte sysfs sectors."""

    device: str
    disk: str
    number: int
    logical_block_size: int
    start_sector: int
    sectors: int
    disk_sectors: int
    usable_end_sector: int
    usable_end_source: str
    tolerance_sectors: int

    @property
    def end_sector(self):
        """The first sector after the partition."""

        return self.start_sector + self.sectors

    @property
    def tail_sectors(self):
        return max(0, self.usable_end_sector - self.end_sector)

    @property
    def size_bytes(self):
        return self.sectors * SYSFS_SECTOR

    @property
    def disk_bytes(self):
        return self.disk_sectors * SYSFS_SECTOR

    @property
    def tail_bytes(self):
        return self.tail_sectors * SYSFS_SECTOR

    @property
    def fills_disk(self):
        return self.tail_sectors <= self.tolerance_sectors

    def to_dict(self):
        return {
            "device": self.device,
            "disk": self.disk,
            "number": self.number,
            "logical_block_size": self.logical_block_size,
            "start_sector": self.start_sector,
            "sectors": self.sectors,
            "end_sector": self.end_sector,
            "disk_sectors": self.disk_sectors,
            "usable_end_sector": self.usable_end_sector,
            "usable_end_source": self.usable_end_source,
            "tail_sectors": self.tail_sectors,
            "tolerance_sectors": self.tolerance_sectors,
            "size_bytes": self.size_bytes,
            "disk_bytes": self.disk_bytes,
            "tail_bytes": self.tail_bytes,
            "fills_disk": self.fills_disk,
        }

    def to_lines(self):
        """``key=value`` for the boot-time shell helper, which has no JSON."""

        return [
            f"{key}={'yes' if value is True else 'no' if value is False else value}"
            for key, value in self.to_dict().items()
        ]


def _read_int(path, *, label):
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GeometryError("geometry_unreadable", f"{label} could not be read: {exc}")
    if not raw.isdigit():
        raise GeometryError("geometry_unreadable", f"{label} is {raw!r}, not a number")
    return int(raw)


def _partition_sysfs(device, *, sysfs):
    name = Path(str(device)).name
    if not name or "/" in name or name in (".", ".."):
        raise GeometryError("geometry_device_invalid", f"{device} does not name a partition")
    entry = Path(sysfs) / "class/block" / name
    if not entry.is_dir():
        raise GeometryError(
            "geometry_device_unknown", f"the block layer does not describe {device}"
        )
    resolved = entry.resolve()
    if not (resolved / "partition").is_file():
        raise GeometryError("geometry_not_a_partition", f"{device} is a whole disk, not a slice")
    return resolved


def _gpt_usable_end(disk_path, logical_block_size, *, opener=None):
    """The last usable LBA the disk's own GPT declares, as a 512-byte sector.

    ``None`` when the table cannot be read: a medium whose primary header is
    damaged is still growable, it simply loses the exact bound and falls back to
    a conservative reserve.
    """

    open_disk = opener or (lambda path: Path(path).open("rb"))
    try:
        with open_disk(disk_path) as handle:
            handle.seek(GPT_HEADER_LBA * logical_block_size)
            header = handle.read(logical_block_size)
    except OSError:
        return None
    if len(header) < GPT_LAST_USABLE_OFFSET + 8 or header[:8] != GPT_SIGNATURE:
        return None
    (last_usable_lba,) = struct.unpack_from("<Q", header, GPT_LAST_USABLE_OFFSET)
    if last_usable_lba <= 0:
        return None
    # The GPT names the last usable block inclusively; every sector count here
    # is exclusive of its end.
    return (last_usable_lba + 1) * logical_block_size // SYSFS_SECTOR


def read_geometry(device, *, sysfs="/sys", opener=None):
    """The real geometry of one partition, from the kernel and the GPT."""

    entry = _partition_sysfs(device, sysfs=sysfs)
    disk_entry = entry.parent
    disk = f"/dev/{disk_entry.name}"

    number = _read_int(entry / "partition", label=f"the partition number of {device}")
    start_sector = _read_int(entry / "start", label=f"the start sector of {device}")
    sectors = _read_int(entry / "size", label=f"the sector count of {device}")
    disk_sectors = _read_int(disk_entry / "size", label=f"the sector count of {disk}")

    logical_block_size = SYSFS_SECTOR
    queue = disk_entry / "queue/logical_block_size"
    if queue.is_file():
        logical_block_size = _read_int(queue, label=f"the logical block size of {disk}")
    if logical_block_size not in LOGICAL_BLOCK_SIZES:
        raise GeometryError(
            "geometry_unreadable",
            f"{disk} reports a logical block size of {logical_block_size}",
        )

    if sectors <= 0:
        raise GeometryError("geometry_unreadable", f"{device} is {sectors} sectors long")
    if disk_sectors <= 0:
        raise GeometryError("geometry_unreadable", f"{disk} is {disk_sectors} sectors long")
    if start_sector + sectors > disk_sectors:
        raise GeometryError(
            "geometry_inconsistent",
            f"{device} ends at sector {start_sector + sectors} of a {disk_sectors}-sector {disk}",
        )

    gpt_end = _gpt_usable_end(disk, logical_block_size, opener=opener)
    if gpt_end is not None and start_sector + sectors <= gpt_end <= disk_sectors:
        usable_end_sector = gpt_end
        usable_end_source = FROM_GPT
        tolerance = ALIGNMENT_TOLERANCE_SECTORS
    else:
        reserve = GPT_BACKUP_RESERVE_LBA * logical_block_size // SYSFS_SECTOR
        usable_end_sector = max(start_sector + sectors, disk_sectors - reserve)
        usable_end_source = FROM_DISK_END
        tolerance = ALIGNMENT_TOLERANCE_SECTORS + reserve

    return PartitionGeometry(
        device=str(device),
        disk=disk,
        number=number,
        logical_block_size=logical_block_size,
        start_sector=start_sector,
        sectors=sectors,
        disk_sectors=disk_sectors,
        usable_end_sector=usable_end_sector,
        usable_end_source=usable_end_source,
        tolerance_sectors=tolerance,
    )
