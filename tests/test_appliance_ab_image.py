# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a built A/B image has to look like for the runtime to agree with it.

The layout is not this project's to declare — ``image-rota`` produces it — so
these tests state only what the runtime depends on: the GPT labels upstream's
slot mapper maps slots by, the filesystem types, and a partition identity that
is generated per build rather than pinned.

The inspector reads a GPT out of an image file, so the fixture builds one by
hand rather than mounting anything — a complete one, both headers and every
CRC, because the inspector validates those structures.

These are the structural cases. The fixture's partitions carry a filesystem
magic and nothing else, so they are inspected with ``contents=False``: what an
image actually contains is proven in test_appliance_ab_image_contents.py
against filesystems mkfs really made.
"""

import struct
import uuid
import zlib
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


DISK_GUID = "77777777-7777-4777-8777-777777777777"


def _gpt_header(*, current, backup, first_usable, last_usable, entries_lba, entries_crc,
                disk_guid=DISK_GUID):
    """A complete GPT header, CRCs included, the way a real one is written."""

    header = bytearray(SECTOR)
    header[0:8] = ab_image.GPT_SIGNATURE
    struct.pack_into("<IIII", header, 8, 0x00010000, 92, 0, 0)
    struct.pack_into("<QQQQ", header, 24, current, backup, first_usable, last_usable)
    header[56:72] = _guid_bytes(disk_guid)
    struct.pack_into("<QIII", header, 72, entries_lba, 128, 128, entries_crc)
    struct.pack_into("<I", header, 16, zlib.crc32(bytes(header[:92])) & 0xFFFFFFFF)
    return bytes(header)


def build_image(path, *, partitions=ab_image.EXPECTED_PARTITIONS, identities=None, sizes=None):
    """A GPT image with image-rota's labels and real filesystem magics.

    The GPT is complete — both headers, both entry arrays and every CRC —
    because the inspector validates those structures and a fixture that only
    looked like a partition table could not tell a valid image from a broken
    one.
    """

    identities = identities or IDENTITIES
    sizes = sizes or {}
    cursor = 2048
    placed = []
    for label, fstype in partitions:
        sectors = int(sizes.get(label, SIZE_MIB)) * 1024 * 1024 // SECTOR
        placed.append((label, fstype, cursor, cursor + sectors - 1))
        cursor += sectors

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
    entries_crc = zlib.crc32(bytes(table)) & 0xFFFFFFFF

    total_sectors = cursor + 2048
    backup_lba = total_sectors - 1
    backup_entries_lba = backup_lba - 32
    last_usable = backup_entries_lba - 1

    with open(path, "wb") as handle:
        handle.truncate(total_sectors * SECTOR)

        handle.seek(SECTOR)
        handle.write(
            _gpt_header(
                current=1,
                backup=backup_lba,
                first_usable=34,
                last_usable=last_usable,
                entries_lba=2,
                entries_crc=entries_crc,
            )
        )
        handle.seek(2 * SECTOR)
        handle.write(table)

        handle.seek(backup_entries_lba * SECTOR)
        handle.write(table)
        handle.seek(backup_lba * SECTOR)
        handle.write(
            _gpt_header(
                current=backup_lba,
                backup=1,
                first_usable=34,
                last_usable=last_usable,
                entries_lba=backup_entries_lba,
                entries_crc=entries_crc,
            )
        )

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
    findings = ab_image.inspect(image, contents=False)
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
    verdicts = results(ab_image.inspect(image, contents=False))

    for check in ab_image.UNMOUNTED_CHECKS:
        assert verdicts[check] == NOT_RUN, check


def test_an_image_without_a_gpt_fails(tmp_path):
    empty = tmp_path / "empty.img"
    empty.write_bytes(b"\x00" * (4 * SECTOR))

    findings = ab_image.inspect(empty, contents=False)

    assert findings[0].result == FAIL
    assert "GPT" in findings[0].detail


def test_an_image_missing_a_partition_fails(tmp_path):
    reduced = tuple(
        entry for entry in ab_image.EXPECTED_PARTITIONS if entry[0] != "system_b"
    )
    target = build_image(tmp_path / "short.img", partitions=reduced)

    verdicts = results(ab_image.inspect(target, contents=False))

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

    verdicts = results(ab_image.inspect(target, contents=False))

    assert verdicts["label:system_a"] == FAIL


def test_both_slots_claiming_one_identity_fails(tmp_path):
    identities = dict(IDENTITIES)
    identities["system_b"] = identities["system_a"]
    target = build_image(tmp_path / "shared.img", identities=identities)

    verdicts = results(ab_image.inspect(target, contents=False))

    assert verdicts["partition_identity"] == FAIL


def test_two_slots_of_different_size_fail(tmp_path):
    target = build_image(tmp_path / "lopsided.img", sizes={"system_b": 2})

    verdicts = results(ab_image.inspect(target, contents=False))

    assert verdicts["slot_pairing"] == FAIL


def test_a_boot_partition_without_a_fat_signature_fails(tmp_path):
    target = build_image(tmp_path / "nofat.img")
    partitions = ab_image.read_partitions(target)
    boot = next(item for item in partitions if item.label == "boot_a")
    with open(target, "r+b") as handle:
        handle.seek(boot.offset + ab_image.FAT_SIGNATURE_OFFSET)
        handle.write(b"\x00\x00")

    verdicts = results(ab_image.inspect(target, contents=False))

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

    verdicts = results(ab_image.inspect(target, contents=False))

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

    summary = ab_image.summarise(ab_image.inspect(image, contents=False))

    assert json.loads(json.dumps(summary))["result"] in (PASS, FAIL, NOT_RUN)


# --- the mounted tier ------------------------------------------------------


UNITS = ("ems-appliance-ab-health", "ems-appliance-slot-bootstrap")
SHARED_MOUNTS = (
    r"etc-ems\x2dappliance\x2dmanager.mount",
    r"etc-NetworkManager-system\x2dconnections.mount",
    r"opt-ems\x2dsolarflow.mount",
    r"var-lib-ems\x2dappliance\x2dmanager.mount",
    r"var-lib-ems\x2dappliance\x2dos\x2dupdate.mount",
    r"var-log-ems\x2dappliance\x2dmanager.mount",
)


def build_root(base):
    """A slot root as the packaged image lays one out."""

    (base / "usr/bin").mkdir(parents=True)
    (base / "usr/bin/ems-appliance").write_text("#!/usr/bin/python3\n")
    wants = base / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True)
    for unit in UNITS:
        (wants / f"{unit}.service").symlink_to(f"/usr/lib/systemd/system/{unit}.service")
    shared = base / "etc/systemd/system/local-fs.target.wants"
    shared.mkdir(parents=True)
    for name in SHARED_MOUNTS:
        (shared / name).symlink_to(f"/run/systemd/generator/{name}")
    return base


@pytest.fixture
def roots(tmp_path):
    return build_root(tmp_path / "a"), build_root(tmp_path / "b")


def result_of(findings, check):
    return next(finding for finding in findings if finding.check == check).result


def test_a_correctly_built_pair_of_roots_passes_the_mounted_checks(roots, tmp_path):
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "cmdline.txt").write_text("console=serial0 root=/dev/disk/by-slot/active/system rw\n")

    findings = ab_image.inspect_mounted(system_a=roots[0], system_b=roots[1], boot=boot)

    for check in ab_image.UNMOUNTED_CHECKS:
        assert result_of(findings, check) == PASS, check


def test_a_slot_the_package_never_reached_is_a_failure(roots):
    (roots[1] / "usr/bin/ems-appliance").unlink()

    findings = ab_image.inspect_mounted(system_a=roots[0], system_b=roots[1])

    assert result_of(findings, "package_present_in_both_roots") == FAIL


def test_a_missing_shared_activation_is_a_failure(roots):
    """Five of six binds still lose every write to the sixth at a slot switch."""

    (roots[0] / "etc/systemd/system/local-fs.target.wants" / SHARED_MOUNTS[2]).unlink()

    findings = ab_image.inspect_mounted(system_a=roots[0], system_b=roots[1])

    finding = next(f for f in findings if f.check == "slot_shared_configuration_present")
    assert finding.result == FAIL
    assert "opt-ems" in finding.detail


def test_a_unit_that_is_installed_but_not_enabled_is_a_failure(roots):
    (roots[1] / "etc/systemd/system/multi-user.target.wants/ems-appliance-ab-health.service").unlink()

    findings = ab_image.inspect_mounted(system_a=roots[0], system_b=roots[1])

    assert result_of(findings, "health_service_enabled") == FAIL


def test_a_cmdline_naming_a_fixed_partition_is_a_failure(roots, tmp_path):
    """A hard-coded root device boots the same slot whichever one is active."""

    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "cmdline.txt").write_text("root=PARTUUID=deadbeef-04 rw\n")

    findings = ab_image.inspect_mounted(system_a=roots[0], system_b=roots[1], boot=boot)

    assert result_of(findings, "slot_cmdline_selects_the_active_slot") == FAIL


def test_an_unmounted_boot_leaves_the_cmdline_check_not_run(roots):
    findings = ab_image.inspect_mounted(system_a=roots[0], system_b=roots[1])

    assert result_of(findings, "slot_cmdline_selects_the_active_slot") == NOT_RUN


def test_a_shipped_host_key_pair_is_a_failure(roots):
    """An image that ships a host key gives every appliance the same identity.

    The fix that stopped the build shipping one is a build-time change; this
    is the check that can see it in the artefact. It cannot be done by
    grepping the raw image, because every OpenSSH binary carries the same
    header as a string constant.
    """

    ssh = roots[0] / "etc/ssh"
    ssh.mkdir(parents=True)
    (ssh / "ssh_host_ed25519_key").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")

    findings = ab_image.inspect_mounted(system_a=roots[0], system_b=roots[1])

    finding = next(f for f in findings if f.check == "no_host_key_shipped")
    assert finding.result == FAIL
    assert "ssh_host_ed25519_key" in finding.detail


def test_a_public_host_key_is_not_a_shipped_secret(roots):
    """A .pub beside no secret is not an identity anyone can impersonate."""

    ssh = roots[0] / "etc/ssh"
    ssh.mkdir(parents=True)
    (ssh / "ssh_host_ed25519_key.pub").write_text("ssh-ed25519 AAAA\n")

    findings = ab_image.inspect_mounted(system_a=roots[0], system_b=roots[1])

    assert result_of(findings, "no_host_key_shipped") == PASS


def test_an_image_with_no_host_keys_passes(roots):
    findings = ab_image.inspect_mounted(system_a=roots[0], system_b=roots[1])

    assert result_of(findings, "no_host_key_shipped") == PASS


# --- the GPT structures themselves -------------------------------------------


def test_a_correct_image_has_both_gpt_headers_and_both_entry_arrays(image):
    verdicts = results(ab_image.verify_gpt(image))

    assert verdicts["gpt_primary_header"] == PASS
    assert verdicts["gpt_backup_header"] == PASS
    assert verdicts["gpt_primary_entries"] == PASS
    assert verdicts["gpt_backup_entries"] == PASS
    assert verdicts["gpt_headers_agree"] == PASS
    assert verdicts["gpt_partition_ranges"] == PASS
    assert verdicts["gpt_no_overlap"] == PASS


def test_a_damaged_backup_gpt_is_a_failure(image):
    """The fallback every repair tool reaches for is not cosmetic."""

    size = image.stat().st_size
    with open(image, "r+b") as handle:
        handle.seek(size - SECTOR)
        handle.write(b"\x00" * SECTOR)

    verdicts = results(ab_image.verify_gpt(image))

    assert verdicts["gpt_primary_header"] == PASS
    assert verdicts["gpt_backup_header"] == FAIL


def test_a_header_that_does_not_match_its_crc_is_a_failure(image):
    with open(image, "r+b") as handle:
        handle.seek(SECTOR + 24)
        handle.write(struct.pack("<Q", 99))

    verdicts = results(ab_image.verify_gpt(image))

    assert verdicts["gpt_primary_header"] == FAIL


def test_an_entry_array_that_was_edited_after_the_crc_is_a_failure(image):
    with open(image, "r+b") as handle:
        handle.seek(2 * SECTOR + 56)
        handle.write("evil".encode("utf-16-le"))

    verdicts = results(ab_image.verify_gpt(image))

    assert verdicts["gpt_primary_entries"] == FAIL


def test_overlapping_partitions_are_a_failure(tmp_path):
    target = tmp_path / "overlap.img"
    build_image(target)
    partitions = ab_image.read_partitions(target)
    second = partitions[1]
    with open(target, "r+b") as handle:
        # Move boot_a back so that it starts inside bootconfig, and repair the
        # CRCs so that only the overlap itself is wrong.
        handle.seek(2 * SECTOR + 128 + 32)
        handle.write(struct.pack("<QQ", partitions[0].first_lba + 8, second.last_lba))
        handle.seek(2 * SECTOR)
        table = handle.read(128 * 128)
        handle.seek(SECTOR)
        header = bytearray(handle.read(SECTOR))
        struct.pack_into("<I", header, 88, zlib.crc32(table) & 0xFFFFFFFF)
        struct.pack_into("<I", header, 16, 0)
        struct.pack_into("<I", header, 16, zlib.crc32(bytes(header[:92])) & 0xFFFFFFFF)
        handle.seek(SECTOR)
        handle.write(bytes(header))

    verdicts = results(ab_image.verify_gpt(target))

    assert verdicts["gpt_primary_entries"] == PASS
    assert verdicts["gpt_no_overlap"] == FAIL


def test_a_partition_reaching_past_the_end_of_the_disk_is_a_failure(tmp_path):
    target = build_image(tmp_path / "past-the-end.img")
    with open(target, "r+b") as handle:
        handle.seek(2 * SECTOR + 5 * 128 + 40)
        handle.write(struct.pack("<Q", target.stat().st_size // SECTOR + 1000))
        handle.seek(2 * SECTOR)
        table = handle.read(128 * 128)
        handle.seek(SECTOR)
        header = bytearray(handle.read(SECTOR))
        struct.pack_into("<I", header, 88, zlib.crc32(table) & 0xFFFFFFFF)
        struct.pack_into("<I", header, 16, 0)
        struct.pack_into("<I", header, 16, zlib.crc32(bytes(header[:92])) & 0xFFFFFFFF)
        handle.seek(SECTOR)
        handle.write(bytes(header))

    verdicts = results(ab_image.verify_gpt(target))

    assert verdicts["gpt_partition_ranges"] == FAIL


# --- mandatory and optional findings ----------------------------------------
#
# The verdict used to be "FAIL if anything failed, otherwise PASS if anything
# passed". A check that never ran therefore cost nothing: an inspection whose
# independent GPT oracle was not installed, or whose content checks were never
# requested, reached a release gate as PASS. Optionality is a policy recorded on
# the finding now, and it is never derived from whether a tool was installed.


def test_a_mandatory_check_that_did_not_run_leaves_the_inspection_incomplete():
    summary = ab_image.summarise(
        [
            ab_image.Finding("looked", PASS, ""),
            ab_image.Finding("never_looked", NOT_RUN, "the tool is not installed"),
        ]
    )

    assert summary["result"] == NOT_RUN
    assert summary["mandatory_not_run"] == ["never_looked"]
    assert summary["counts"][PASS] == 1


def test_an_optional_check_that_did_not_run_does_not_block_a_release():
    summary = ab_image.summarise(
        [
            ab_image.Finding("looked", PASS, ""),
            ab_image.Finding("second_opinion", NOT_RUN, "", mandatory=False),
        ]
    )

    assert summary["result"] == PASS
    assert summary["mandatory_not_run"] == []
    assert summary["optional"] == 1


def test_an_optional_check_that_ran_and_disagreed_still_fails_the_release():
    """Optional is about being allowed not to run, never about being ignored."""

    summary = ab_image.summarise(
        [
            ab_image.Finding("looked", PASS, ""),
            ab_image.Finding("second_opinion", FAIL, "the table is corrupt", mandatory=False),
        ]
    )

    assert summary["result"] == FAIL


def test_a_failure_outranks_an_incomplete_inspection():
    summary = ab_image.summarise(
        [
            ab_image.Finding("broken", FAIL, ""),
            ab_image.Finding("never_looked", NOT_RUN, ""),
        ]
    )

    assert summary["result"] == FAIL


def test_an_inspection_that_proved_nothing_is_not_a_pass():
    assert ab_image.summarise([])["result"] == NOT_RUN
    assert (
        ab_image.summarise([ab_image.Finding("only_optional", PASS, "", mandatory=False)])["result"]
        == NOT_RUN
    )


def test_every_finding_carries_its_own_mandatory_flag(image):
    for finding in ab_image.inspect(image, contents=False):
        assert isinstance(finding.mandatory, bool), finding.check
        assert finding.to_dict()["mandatory"] is finding.mandatory


def test_a_partition_table_only_inspection_is_incomplete_rather_than_passing(image):
    """"The image is good" and "nobody read the image" are different answers."""

    summary = ab_image.summarise(ab_image.inspect(image, contents=False))

    assert summary["result"] == NOT_RUN
    assert set(summary["mandatory_not_run"]) == set(ab_image.UNMOUNTED_CHECKS)


def test_optional_checks_are_named_rather_than_discovered():
    assert isinstance(ab_image.OPTIONAL_CHECKS, frozenset)
