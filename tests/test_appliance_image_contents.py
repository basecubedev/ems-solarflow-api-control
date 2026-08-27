# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inspecting a built image, without mounting it.

Mounting is not available and that is the point rather than a limitation: the
Pi 5 root uses 16 KiB ext4 blocks, which no 4 KiB-page host kernel will mount,
and mounting needs root and a loop device besides. A check that cannot run is
not a check that passed, so what decides whether an image is an appliance is
read out of the filesystem structures directly.

These tests build the real thing with mkfs -- including a 16 KiB filesystem on a
host whose kernel refuses to mount it -- and prove the content checks see what
is really there, and fail when it is not.
"""

import struct

import pytest

from appliance import image_inspect
from appliance.image_inspect import FAIL, PASS
from tests.helpers.appliance_image import (
    APPLIANCE_VERSION,
    BUILD_ID,
    CONFIG,
    make_ext4,
    make_fat,
    populate_root,
    requires_mkfs,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

SECTOR = image_inspect.SECTOR_SIZE

# What image-rpios writes for an ext4 root, and what the appliance layer then
# adds to the kernel command line.
FSTAB = (
    "/dev/disk/by-slot/system  /  ext4 rw,relatime,errors=remount-ro,commit=30 0 1\n"
    "/dev/disk/by-slot/boot  /boot/firmware  vfat defaults,rw,noatime,errors=remount-ro 0 2\n"
)
CMDLINE = (
    "console=serial0,115200 root=/dev/disk/by-slot/system rootfstype=ext4 rw fsck.repair=yes"
)

REQUIRED_UNITS = tuple(sorted(image_inspect.REQUIRED_UNITS.values()))


def assemble_mbr(target, boot, root):
    """One image file with an MBR naming a FAT boot and an ext4 root."""

    first = 8 * 1024 * 1024 // SECTOR
    boot_sectors = (boot.stat().st_size + SECTOR - 1) // SECTOR
    root_start = first + boot_sectors
    root_sectors = (root.stat().st_size + SECTOR - 1) // SECTOR

    sector = bytearray(SECTOR)
    for index, (kind, start, count, bootable) in enumerate(
        ((0x0C, first, boot_sectors, 0x80), (0x83, root_start, root_sectors, 0x00))
    ):
        offset = image_inspect.MBR_TABLE_OFFSET + index * image_inspect.MBR_ENTRY_SIZE
        sector[offset] = bootable
        sector[offset + 4] = kind
        sector[offset + 8 : offset + 16] = struct.pack("<II", start, count)
    sector[510:512] = image_inspect.MBR_SIGNATURE

    with target.open("wb") as handle:
        handle.write(sector)
        for path, start in ((boot, first), (root, root_start)):
            handle.seek(start * SECTOR)
            handle.write(path.read_bytes())
    return target


def build_single_image(tmp_path, **overrides):
    root_tree = tmp_path / "root"
    populate_root(root_tree, **{k: v for k, v in overrides.items() if k != "cmdline"})
    root = make_ext4(tmp_path / "root.ext4", root_tree)
    boot = make_fat(
        tmp_path / "boot.vfat",
        {
            "cmdline.txt": overrides.get("cmdline", CMDLINE),
            "config.txt": CONFIG,
            "kernel8.img": b"kernel",
            "initramfs8": b"initramfs",
            "bcm2712-rpi-5-b.dtb": b"dtb",
        },
    )
    return assemble_mbr(tmp_path / "appliance.img", boot, root)


@pytest.fixture
def single_image(tmp_path):
    return build_single_image(tmp_path)


def contents(image, **kwargs):
    return image_inspect.inspect_contents(
        image,
        appliance_version=APPLIANCE_VERSION,
        build_id=BUILD_ID,
        architecture="arm64",
        **kwargs,
    )


def by_check(findings):
    return {finding.check: finding for finding in findings}


# --- the partition table -----------------------------------------------------


@requires_mkfs
def test_a_partition_of_an_unexpected_type_is_left_unnamed(tmp_path, single_image):
    """A wrong name would inspect one filesystem and answer for another."""

    data = bytearray(single_image.read_bytes())
    data[image_inspect.MBR_TABLE_OFFSET + 4] = 0x07
    target = tmp_path / "odd.img"
    target.write_bytes(bytes(data))

    labels = [item.label for item in image_inspect.read_mbr_partitions(target)]

    assert labels == ["", "root"]


# --- the verdict -------------------------------------------------------------


@requires_mkfs
def test_every_applicable_check_passes_for_a_correctly_built_image(single_image):
    failed = [finding.to_dict() for finding in contents(single_image) if finding.result == FAIL]

    assert failed == []


@requires_mkfs
def test_the_writable_root_is_what_is_actually_asserted(single_image):
    findings = by_check(contents(single_image))

    assert findings["root_fstab_writable:root"].result == PASS
    assert findings["boot_writable_root:boot"].result == PASS
    assert "root_fstab_readonly:root" not in findings
    assert "boot_readonly_root:boot" not in findings


@requires_mkfs
def test_a_root_that_ships_a_host_private_key_is_refused_here_too(tmp_path):
    """Variant-independent: a private key in a public artefact is compromised."""

    root_tree = tmp_path / "root"
    populate_root(root_tree)
    (root_tree / "etc/ssh").mkdir(parents=True, exist_ok=True)
    (root_tree / "etc/ssh/ssh_host_ed25519_key").write_text("PRIVATE")
    root = make_ext4(tmp_path / "root.ext4", root_tree)
    boot = make_fat(
        tmp_path / "boot.vfat",
        {"cmdline.txt": CMDLINE, "config.txt": CONFIG, "kernel8.img": b"k",
         "initramfs8": b"i", "bcm2712-rpi-5-b.dtb": b"d"},
    )
    image = assemble_mbr(tmp_path / "appliance.img", boot, root)

    findings = by_check(contents(image))

    assert findings["no_host_key_shipped:root"].result == FAIL


@requires_mkfs
def test_the_units_a_single_slot_host_can_run_are_the_ones_required(single_image):
    findings = by_check(contents(single_image))

    assert findings["agent_service_enabled:root"].result == PASS
    assert findings["web_service_enabled:root"].result == PASS
    assert findings["grow_root_service_enabled:root"].result == PASS
    for check in ("health_service_enabled:root", "persistence_service_enabled:root"):
        assert check not in findings


# --- the whole inspection ----------------------------------------------------


@requires_mkfs
def test_the_whole_inspection_of_a_correct_single_slot_image_passes(single_image):
    findings = image_inspect.inspect(single_image, contents=True,
                                appliance_version=APPLIANCE_VERSION, build_id=BUILD_ID,
                                architecture="arm64")

    summary = image_inspect.summarise(findings)

    assert summary["counts"][FAIL] == 0, [
        entry for entry in summary["findings"] if entry["result"] == FAIL
    ]
    assert summary["result"] == PASS
    assert summary["mandatory_not_run"] == []


@requires_mkfs
def test_an_enabled_unit_whose_program_is_absent_is_a_failure(tmp_path):
    """"Installed and enabled" is true of a unit that cannot run.

    The growth unit is the first one in this project whose ExecStart is a file
    of its own rather than the appliance binary, so an image that enabled it
    without shipping the script would have failed on the first boot and
    nowhere earlier.
    """

    root_tree = tmp_path / "root"
    populate_root(root_tree)
    # The unit stays enabled and keeps naming its program; only the program is
    # gone -- which is exactly the image a packaging mistake would produce.
    (root_tree / "usr/lib/ems-appliance-manager/grow-root.sh").unlink()
    root = make_ext4(tmp_path / "root.ext4", root_tree)
    boot = make_fat(
        tmp_path / "boot.vfat",
        {"cmdline.txt": CMDLINE, "config.txt": CONFIG, "kernel8.img": b"k",
         "initramfs8": b"i", "bcm2712-rpi-5-b.dtb": b"d"},
    )
    image = assemble_mbr(tmp_path / "appliance.img", boot, root)

    findings = by_check(contents(image))

    assert findings["unit_programs_present:root"].result == FAIL
    assert "grow-root.sh" in findings["unit_programs_present:root"].detail


@requires_mkfs
def test_a_unit_whose_program_is_present_passes(single_image):
    findings = by_check(contents(single_image))

    assert findings["unit_programs_present:root"].result == PASS
