# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a built A/B image has to look like for the runtime to agree with it.

The layout is not this project's to declare — ``image-rota`` produces it — so
these tests state only what the runtime depends on: the GPT labels upstream's
slot mapper maps slots by, the filesystem types, and a partition identity that
is generated per build rather than pinned.

The inspector reads a GPT out of an image file, so the fixture builds one by
hand rather than mounting anything. What the inspector cannot see without a
mounted filesystem is asserted to be reported as NOT RUN, because a
partition-table check that reported PASS would be a false image validation.
"""

import struct
import uuid
from pathlib import Path

import pytest

from appliance import ab_image
from appliance.ab_image import FAIL, NOT_RUN, PASS

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
SECTOR = ab_image.SECTOR_SIZE

# One megabyte per partition is enough for a superblock at the right offset.
SIZE_MIB = 1

# Distinct per build, exactly as genimage produces them.
IDENTITIES = {
    "bootconfig": "11111111-1111-4111-8111-111111111111",
    "boot_a": "22222222-2222-4222-8222-222222222222",
    "boot_b": "33333333-3333-4333-8333-333333333333",
    "system_a": "44444444-4444-4444-8444-444444444444",
    "system_b": "55555555-5555-4555-8555-555555555555",
    "persistent": "66666666-6666-4666-8666-666666666666",
}
OTHER_BUILD = {
    label: identity.replace(identity[0], "a", 1) for label, identity in IDENTITIES.items()
}


def _guid_bytes(text):
    value = uuid.UUID(text)
    fields = value.fields
    return (
        struct.pack("<IHH", fields[0], fields[1], fields[2])
        + bytes([fields[3], fields[4]])
        + fields[5].to_bytes(6, "big")
    )


def build_image(path, *, partitions=ab_image.EXPECTED_PARTITIONS, identities=None, sizes=None):
    """A GPT image with image-rota's labels and real filesystem magics."""

    identities = identities or IDENTITIES
    sizes = sizes or {}
    cursor = 2048
    placed = []
    for label, fstype in partitions:
        sectors = int(sizes.get(label, SIZE_MIB)) * 1024 * 1024 // SECTOR
        placed.append((label, fstype, cursor, cursor + sectors - 1))
        cursor += sectors

    with open(path, "wb") as handle:
        handle.truncate((cursor + 2048) * SECTOR)

        header = bytearray(SECTOR)
        header[0:8] = ab_image.GPT_SIGNATURE
        struct.pack_into("<QII", header, 72, 2, 128, 128)
        handle.seek(SECTOR)
        handle.write(header)

        table = bytearray(128 * 128)
        for index, (label, fstype, first, last) in enumerate(placed):
            record = bytearray(128)
            type_guid = ab_image.TYPE_ESP_FAT if fstype == "vfat" else ab_image.TYPE_LINUX
            record[0:16] = _guid_bytes(type_guid)
            record[16:32] = _guid_bytes(identities[label])
            struct.pack_into("<QQ", record, 32, first, last)
            encoded = label.encode("utf-16-le")
            record[56 : 56 + len(encoded)] = encoded
            table[index * 128 : (index + 1) * 128] = record
        handle.seek(2 * SECTOR)
        handle.write(table)

        for label, fstype, first, _last in placed:
            offset = first * SECTOR
            if fstype == "vfat":
                handle.seek(offset + ab_image.FAT_SIGNATURE_OFFSET)
                handle.write(ab_image.FAT_SIGNATURE)
            else:
                handle.seek(offset + ab_image.EXT_SUPERBLOCK_OFFSET + ab_image.EXT_MAGIC_OFFSET)
                handle.write(ab_image.EXT_MAGIC)
    return path


@pytest.fixture
def image(tmp_path):
    return build_image(tmp_path / "appliance.img")


def results(findings):
    return {finding.check: finding.result for finding in findings}


# --- the contract ------------------------------------------------------------


def test_the_expected_layout_is_image_rotas_and_names_no_identity():
    labels = [label for label, _fstype in ab_image.EXPECTED_PARTITIONS]

    assert labels == ["bootconfig", "boot_a", "boot_b", "system_a", "system_b", "persistent"]
    assert "partuuid" not in ab_image.__doc__.lower()


def test_the_bootconfig_partition_is_the_first_fat_partition():
    """The firmware reads autoboot.txt from the first FAT partition."""

    fat = [label for label, fstype in ab_image.EXPECTED_PARTITIONS if fstype == "vfat"]

    assert fat[0] == "bootconfig"


# --- a synthetic image --------------------------------------------------------


def test_a_correct_image_passes_every_check_it_can_make(image):
    findings = ab_image.inspect(image)
    verdicts = results(findings)

    assert verdicts["partition_table"] == PASS
    assert verdicts["partition_count"] == PASS
    assert verdicts["partition_identity"] == PASS
    assert verdicts["slot_pairing"] == PASS
    for label, _fstype in ab_image.EXPECTED_PARTITIONS:
        assert verdicts[f"label:{label}"] == PASS, label
        assert verdicts[f"type:{label}"] == PASS, label
        assert verdicts[f"filesystem:{label}"] == PASS, label
    assert ab_image.summarise(findings)["counts"][FAIL] == 0


def test_the_checks_that_need_a_mounted_filesystem_are_reported_as_not_run(image):
    verdicts = results(ab_image.inspect(image))

    for check in ab_image.UNMOUNTED_CHECKS:
        assert verdicts[check] == NOT_RUN, check


def test_an_image_without_a_gpt_fails(tmp_path):
    empty = tmp_path / "empty.img"
    empty.write_bytes(b"\x00" * (4 * SECTOR))

    findings = ab_image.inspect(empty)

    assert findings[0].result == FAIL
    assert "GPT" in findings[0].detail


def test_an_image_missing_a_partition_fails(tmp_path):
    reduced = tuple(
        entry for entry in ab_image.EXPECTED_PARTITIONS if entry[0] != "system_b"
    )
    target = build_image(tmp_path / "short.img", partitions=reduced)

    verdicts = results(ab_image.inspect(target))

    assert verdicts["partition_count"] == FAIL


def test_a_label_the_slot_mapper_cannot_read_fails(tmp_path):
    """image-rota mandates the labels; without them slot mapping breaks."""

    renamed = tuple(
        (("system_x" if label == "system_a" else label), fstype)
        for label, fstype in ab_image.EXPECTED_PARTITIONS
    )
    identities = dict(IDENTITIES)
    identities["system_x"] = identities.pop("system_a")
    target = build_image(tmp_path / "mislabelled.img", partitions=renamed, identities=identities)

    verdicts = results(ab_image.inspect(target))

    assert verdicts["label:system_a"] == FAIL


def test_both_slots_claiming_one_identity_fails(tmp_path):
    identities = dict(IDENTITIES)
    identities["system_b"] = identities["system_a"]
    target = build_image(tmp_path / "shared.img", identities=identities)

    verdicts = results(ab_image.inspect(target))

    assert verdicts["partition_identity"] == FAIL


def test_two_slots_of_different_size_fail(tmp_path):
    target = build_image(tmp_path / "lopsided.img", sizes={"system_b": 2})

    verdicts = results(ab_image.inspect(target))

    assert verdicts["slot_pairing"] == FAIL


def test_a_boot_partition_without_a_fat_signature_fails(tmp_path):
    target = build_image(tmp_path / "nofat.img")
    partitions = ab_image.read_partitions(target)
    boot = next(item for item in partitions if item.label == "boot_a")
    with open(target, "r+b") as handle:
        handle.seek(boot.offset + ab_image.FAT_SIGNATURE_OFFSET)
        handle.write(b"\x00\x00")

    verdicts = results(ab_image.inspect(target))

    assert verdicts["filesystem:boot_a"] == FAIL


def test_a_system_partition_carrying_a_fat_filesystem_fails(tmp_path):
    target = build_image(tmp_path / "wrongfs.img")
    partitions = ab_image.read_partitions(target)
    system = next(item for item in partitions if item.label == "system_a")
    with open(target, "r+b") as handle:
        handle.seek(system.offset + ab_image.EXT_SUPERBLOCK_OFFSET + ab_image.EXT_MAGIC_OFFSET)
        handle.write(b"\x00\x00")
        handle.seek(system.offset + ab_image.FAT_SIGNATURE_OFFSET)
        handle.write(ab_image.FAT_SIGNATURE)

    verdicts = results(ab_image.inspect(target))

    assert verdicts["filesystem:system_a"] == FAIL


# --- two builds ---------------------------------------------------------------


def test_two_builds_do_not_reuse_a_partition_identity(tmp_path):
    """Two appliance media on one bus must be distinguishable."""

    first = build_image(tmp_path / "first.img")
    second = build_image(tmp_path / "second.img", identities=OTHER_BUILD)

    assert ab_image.compare_identities(first, second).result == PASS


def test_a_rebuild_that_reused_the_identities_is_a_failure(tmp_path):
    first = build_image(tmp_path / "first.img")
    second = build_image(tmp_path / "second.img")

    finding = ab_image.compare_identities(first, second)

    assert finding.result == FAIL
    assert "reused" in finding.detail


def test_the_summary_is_json_serialisable(image):
    import json

    summary = ab_image.summarise(ab_image.inspect(image))

    assert json.loads(json.dumps(summary))["result"] in (PASS, FAIL, NOT_RUN)
