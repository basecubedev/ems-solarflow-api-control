# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where a partition ends, measured rather than inferred.

The growth helper used to ask ``disk_bytes - partition_bytes <= slack``. The
persistent partition is the last of six, so that subtraction counted the entire
occupied prefix — around 17 GB of image — as unused tail, and a partition that
had already been grown to the end of a 32 GB card still read as gigabytes short.
A power cut between growpart and the marker then produced a medium that retried
on every boot, got NOCHANGE, and failed for ever.

Everything below is about the numbers themselves: the start, the length, the
disk's length, and the last usable LBA the GPT declares. The fallback used when
a table cannot be read is bounded and named, so a reader can tell which of the
two answered.
"""

import struct

import pytest

from appliance import ab_geometry

pytestmark = [pytest.mark.unit, pytest.mark.system_build]

SECTOR = ab_geometry.SYSFS_SECTOR
MIB = 1024 * 1024

DISK_SECTORS = 32 * 1024 * MIB // SECTOR
PREFIX_SECTORS = 17 * 1024 * MIB // SECTOR
IMAGED_SECTORS = 8 * 1024 * MIB // SECTOR


def build_sysfs(
    tmp_path,
    *,
    disk_sectors=DISK_SECTORS,
    start_sector=PREFIX_SECTORS,
    sectors=IMAGED_SECTORS,
    logical_block_size=512,
    disk="mmcblk0",
    partition="mmcblk0p6",
    number=6,
):
    """A sysfs tree shaped exactly like the kernel's, for one partition."""

    sysfs = tmp_path / "sys"
    disk_dir = sysfs / "block" / disk
    partition_dir = disk_dir / partition
    partition_dir.mkdir(parents=True)
    (disk_dir / "queue").mkdir()
    (disk_dir / "queue" / "logical_block_size").write_text(f"{logical_block_size}\n")
    (disk_dir / "size").write_text(f"{disk_sectors}\n")
    (partition_dir / "partition").write_text(f"{number}\n")
    (partition_dir / "start").write_text(f"{start_sector}\n")
    (partition_dir / "size").write_text(f"{sectors}\n")

    class_block = sysfs / "class" / "block"
    class_block.mkdir(parents=True)
    (class_block / partition).symlink_to(partition_dir)
    (class_block / disk).symlink_to(disk_dir)
    return sysfs


def gpt_opener(last_usable_lba, *, logical_block_size=512, signature=ab_geometry.GPT_SIGNATURE):
    """A disk whose primary GPT header declares ``last_usable_lba``."""

    header = bytearray(logical_block_size)
    header[0:8] = signature
    struct.pack_into("<Q", header, ab_geometry.GPT_LAST_USABLE_OFFSET, last_usable_lba)
    payload = bytes(logical_block_size) + bytes(header)

    class _Disk:
        def __init__(self):
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def seek(self, offset):
            self.offset = offset

        def read(self, length):
            return payload[self.offset : self.offset + length]

    return lambda _path: _Disk()


def unreadable_opener(_path):
    raise OSError("no such device")


# --- the defect -------------------------------------------------------------


def test_a_grown_last_partition_is_not_read_as_short(tmp_path):
    """The regression: the occupied prefix is not unused tail."""

    grown = DISK_SECTORS - 33 - PREFIX_SECTORS
    sysfs = build_sysfs(tmp_path, sectors=grown)

    geometry = ab_geometry.read_geometry(
        "/dev/mmcblk0p6", sysfs=sysfs, opener=gpt_opener(DISK_SECTORS - 34)
    )

    assert geometry.fills_disk
    assert geometry.tail_sectors == 0
    # What the old check would have computed, for contrast: 17 GiB of prefix.
    assert geometry.disk_bytes - geometry.size_bytes > 17 * 1024 * MIB - MIB


def test_an_ungrown_partition_still_reports_its_real_tail(tmp_path):
    sysfs = build_sysfs(tmp_path)

    geometry = ab_geometry.read_geometry(
        "/dev/mmcblk0p6", sysfs=sysfs, opener=gpt_opener(DISK_SECTORS - 34)
    )

    assert not geometry.fills_disk
    # 32 GiB less the 17 GiB prefix and the 8 GiB partition, less the 33
    # sectors the GPT keeps for its backup.
    assert geometry.tail_bytes == 7 * 1024 * MIB - 33 * SECTOR
    assert geometry.end_sector == PREFIX_SECTORS + IMAGED_SECTORS


# --- where the usable end comes from ----------------------------------------


def test_the_gpt_last_usable_lba_is_the_bound_when_it_can_be_read(tmp_path):
    sysfs = build_sysfs(tmp_path)

    geometry = ab_geometry.read_geometry(
        "/dev/mmcblk0p6", sysfs=sysfs, opener=gpt_opener(DISK_SECTORS - 34)
    )

    assert geometry.usable_end_source == ab_geometry.FROM_GPT
    assert geometry.usable_end_sector == DISK_SECTORS - 33


def test_a_disk_whose_table_cannot_be_read_falls_back_to_a_named_reserve(tmp_path):
    sysfs = build_sysfs(tmp_path)

    geometry = ab_geometry.read_geometry(
        "/dev/mmcblk0p6", sysfs=sysfs, opener=unreadable_opener
    )

    assert geometry.usable_end_source == ab_geometry.FROM_DISK_END
    assert geometry.usable_end_sector == DISK_SECTORS - ab_geometry.GPT_BACKUP_RESERVE_LBA
    # The fallback tolerance has to absorb the reserve it could not read.
    assert geometry.tolerance_sectors > ab_geometry.ALIGNMENT_TOLERANCE_SECTORS


def test_a_table_that_is_not_a_gpt_falls_back_rather_than_trusting_it(tmp_path):
    sysfs = build_sysfs(tmp_path)

    geometry = ab_geometry.read_geometry(
        "/dev/mmcblk0p6",
        sysfs=sysfs,
        opener=gpt_opener(DISK_SECTORS - 34, signature=b"NOTAGPT!"),
    )

    assert geometry.usable_end_source == ab_geometry.FROM_DISK_END


def test_a_gpt_that_claims_more_than_the_disk_holds_is_not_believed(tmp_path):
    sysfs = build_sysfs(tmp_path)

    geometry = ab_geometry.read_geometry(
        "/dev/mmcblk0p6", sysfs=sysfs, opener=gpt_opener(DISK_SECTORS * 2)
    )

    assert geometry.usable_end_source == ab_geometry.FROM_DISK_END


def test_a_gpt_that_ends_before_the_partition_does_is_not_believed(tmp_path):
    sysfs = build_sysfs(tmp_path)

    geometry = ab_geometry.read_geometry(
        "/dev/mmcblk0p6", sysfs=sysfs, opener=gpt_opener(PREFIX_SECTORS)
    )

    assert geometry.usable_end_source == ab_geometry.FROM_DISK_END


def test_a_four_kibibyte_disk_converts_its_lbas_into_sysfs_sectors(tmp_path):
    """GPT counts logical blocks; sysfs always counts 512-byte sectors."""

    sysfs = build_sysfs(tmp_path, logical_block_size=4096)

    geometry = ab_geometry.read_geometry(
        "/dev/mmcblk0p6",
        sysfs=sysfs,
        opener=gpt_opener(DISK_SECTORS // 8 - 6, logical_block_size=4096),
    )

    assert geometry.logical_block_size == 4096
    assert geometry.usable_end_sector == (DISK_SECTORS // 8 - 5) * 8


# --- refusals ---------------------------------------------------------------


def test_a_device_the_block_layer_does_not_describe_is_an_error(tmp_path):
    sysfs = build_sysfs(tmp_path)

    with pytest.raises(ab_geometry.GeometryError) as error:
        ab_geometry.read_geometry("/dev/mmcblk9p9", sysfs=sysfs)

    assert error.value.code == "geometry_device_unknown"


def test_a_whole_disk_is_not_a_partition(tmp_path):
    sysfs = build_sysfs(tmp_path)

    with pytest.raises(ab_geometry.GeometryError) as error:
        ab_geometry.read_geometry("/dev/mmcblk0", sysfs=sysfs)

    assert error.value.code == "geometry_not_a_partition"


def test_a_device_name_that_is_a_path_is_refused(tmp_path):
    sysfs = build_sysfs(tmp_path)

    with pytest.raises(ab_geometry.GeometryError) as error:
        ab_geometry.read_geometry("/dev/../etc/passwd/", sysfs=sysfs)

    assert error.value.code in {"geometry_device_invalid", "geometry_device_unknown"}


def test_a_sysfs_value_that_is_not_a_number_is_an_error_not_a_zero(tmp_path):
    sysfs = build_sysfs(tmp_path)
    (sysfs / "block/mmcblk0/mmcblk0p6/size").write_text("unknown\n")

    with pytest.raises(ab_geometry.GeometryError) as error:
        ab_geometry.read_geometry("/dev/mmcblk0p6", sysfs=sysfs)

    assert error.value.code == "geometry_unreadable"


def test_a_partition_that_ends_past_its_disk_is_refused(tmp_path):
    sysfs = build_sysfs(tmp_path, sectors=DISK_SECTORS)

    with pytest.raises(ab_geometry.GeometryError) as error:
        ab_geometry.read_geometry("/dev/mmcblk0p6", sysfs=sysfs)

    assert error.value.code == "geometry_inconsistent"


def test_a_logical_block_size_no_device_presents_is_refused(tmp_path):
    sysfs = build_sysfs(tmp_path)
    (sysfs / "block/mmcblk0/queue/logical_block_size").write_text("777\n")

    with pytest.raises(ab_geometry.GeometryError) as error:
        ab_geometry.read_geometry("/dev/mmcblk0p6", sysfs=sysfs)

    assert error.value.code == "geometry_unreadable"


# --- what the boot helper consumes ------------------------------------------


def test_the_shell_helper_gets_every_field_as_one_key_per_line(tmp_path):
    sysfs = build_sysfs(tmp_path)

    geometry = ab_geometry.read_geometry(
        "/dev/mmcblk0p6", sysfs=sysfs, opener=gpt_opener(DISK_SECTORS - 34)
    )
    lines = dict(line.split("=", 1) for line in geometry.to_lines())

    assert lines["fills_disk"] == "no"
    assert lines["device"] == "/dev/mmcblk0p6"
    assert lines["disk"] == "/dev/mmcblk0"
    assert lines["number"] == "6"
    assert int(lines["tail_bytes"]) == geometry.tail_bytes
    assert int(lines["size_bytes"]) == geometry.size_bytes
    assert set(lines) == set(geometry.to_dict())
