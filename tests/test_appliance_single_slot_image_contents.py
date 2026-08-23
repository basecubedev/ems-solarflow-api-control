# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inspecting a built single-slot image, without mounting it.

The A/B inspector is not simply a stricter version of this one: several of its
checks would be *wrong* here. A root that is read-only cannot be patched by
apt, which is the only way this variant is patched at all, and a layout
descriptor would tell the runtime it has slots that do not exist.

So the risk this module exists for is a variant-aware inspector that reports
"not applicable" as "fine". Every check that does not apply must come back NOT
RUN and be counted as not having run — an inspection of a single-slot image
must never read as a clean A/B inspection, and the other way round.
"""

import json
import struct
from pathlib import Path

import pytest

from appliance import ab_image, image_variants
from appliance.ab_image import FAIL, NOT_RUN, PASS
from tests.test_appliance_ab_image_contents import (
    APPLIANCE_VERSION,
    BUILD_ID,
    CONFIG,
    make_ext4,
    make_fat,
    populate_root,
    requires_mkfs,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

SECTOR = ab_image.SECTOR_SIZE
SINGLE = image_variants.VARIANT_SINGLE

# What image-rpios writes for an ext4 root, and what the appliance layer then
# adds to the kernel command line.
FSTAB = (
    "/dev/disk/by-slot/system  /  ext4 rw,relatime,errors=remount-ro,commit=30 0 1\n"
    "/dev/disk/by-slot/boot  /boot/firmware  vfat defaults,rw,noatime,errors=remount-ro 0 2\n"
)
CMDLINE = (
    "console=serial0,115200 root=/dev/disk/by-slot/system rootfstype=ext4 rw fsck.repair=yes"
)

SINGLE_UNITS = ("ems-appliance-agent.service", "ems-appliance-web.service")


def populate_single_root(base, *, fstab=FSTAB, units=SINGLE_UNITS, layout_descriptor=False):
    """A root as the single-slot layer lays one out."""

    populate_root(base, fstab=fstab)

    # populate_root builds an A/B root. Undo exactly the parts that are the A/B
    # layout, so what is left is what this variant actually ships.
    descriptor = base / ab_image.LAYOUT_DESCRIPTOR
    if descriptor.is_file() and not layout_descriptor:
        descriptor.unlink()

    marker = base / ab_image.BUILD_MARKER
    payload = json.loads(marker.read_text())
    payload["image_layer"] = image_variants.variant(SINGLE).image_layer
    payload.pop("layout_id", None)
    marker.write_text(json.dumps(payload))

    wants = base / ab_image.WANTS_DIRECTORY
    wants.mkdir(parents=True, exist_ok=True)
    for entry in list(wants.iterdir()):
        entry.unlink()
    units_dir = base / ab_image.UNIT_DIRECTORY
    units_dir.mkdir(parents=True, exist_ok=True)
    for unit in units:
        (units_dir / unit).write_text("[Unit]\n")
        (wants / unit).symlink_to(f"/usr/lib/systemd/system/{unit}")
    return base


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
        offset = ab_image.MBR_TABLE_OFFSET + index * ab_image.MBR_ENTRY_SIZE
        sector[offset] = bootable
        sector[offset + 4] = kind
        sector[offset + 8 : offset + 16] = struct.pack("<II", start, count)
    sector[510:512] = ab_image.MBR_SIGNATURE

    with target.open("wb") as handle:
        handle.write(sector)
        for path, start in ((boot, first), (root, root_start)):
            handle.seek(start * SECTOR)
            handle.write(path.read_bytes())
    return target


def build_single_image(tmp_path, **overrides):
    root_tree = tmp_path / "root"
    populate_single_root(root_tree, **overrides)
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
    return ab_image.inspect_contents(
        image,
        variant=SINGLE,
        appliance_version=APPLIANCE_VERSION,
        build_id=BUILD_ID,
        architecture="arm64",
        **kwargs,
    )


def by_check(findings):
    return {finding.check: finding for finding in findings}


# --- the partition table -----------------------------------------------------


def test_an_mbr_is_read_where_a_gpt_reader_would_refuse_the_image(single_image):
    with pytest.raises(ab_image.ImageError) as error:
        ab_image.read_partitions(single_image)
    assert error.value.code == "image_not_gpt"

    partitions = ab_image.read_mbr_partitions(single_image)

    assert [item.label for item in partitions] == ["boot", "root"]
    assert partitions[0].offset == 8 * 1024 * 1024


def test_a_partition_of_an_unexpected_type_is_left_unnamed(tmp_path, single_image):
    """A wrong name would inspect one filesystem and answer for another."""

    data = bytearray(single_image.read_bytes())
    data[ab_image.MBR_TABLE_OFFSET + 4] = 0x07
    target = tmp_path / "odd.img"
    target.write_bytes(bytes(data))

    labels = [item.label for item in ab_image.read_mbr_partitions(target)]

    assert labels == ["", "root"]


# --- the verdict -------------------------------------------------------------


@requires_mkfs
def test_every_applicable_check_passes_for_a_correctly_built_image(single_image):
    failed = [finding.to_dict() for finding in contents(single_image) if finding.result == FAIL]

    assert failed == []


@requires_mkfs
def test_the_ab_only_checks_are_reported_as_not_run_and_never_as_passing(single_image):
    """"Does not apply" and "passed" must not reach a gate as the same word."""

    findings = by_check(contents(single_image))

    for check in (
        "mount_points_present:root",
        "persistence_configuration:root",
        "shared_activations:root",
        "slot_generators:root",
        "machine_id_policy:root",
        "service_drop_ins:root",
    ):
        assert findings[check].result == NOT_RUN, findings[check].to_dict()


@requires_mkfs
def test_a_correct_image_passes_without_the_skips_making_it_incomplete(single_image):
    """A check that cannot apply is not a missing oracle.

    It still must not read as a pass -- it is counted, named and reported NOT
    RUN with the reason -- but it is not mandatory, because an inspection that
    can never pass by construction is one nobody reads.
    """

    summary = ab_image.summarise(contents(single_image))

    assert summary["result"] == PASS
    assert summary["counts"][FAIL] == 0
    assert summary["counts"][NOT_RUN] > 0
    assert summary["mandatory_not_run"] == []


@requires_mkfs
def test_a_skipped_check_is_still_visible_as_not_run_in_the_report(single_image):
    """Counted and named, so a reader can see what was not answered."""

    reported = {
        entry["check"]: entry
        for entry in ab_image.summarise(contents(single_image))["findings"]
    }
    entry = reported["shared_activations:root"]

    assert entry["result"] == NOT_RUN
    assert entry["mandatory"] is False
    assert "no second slot" in entry["detail"]


@requires_mkfs
def test_the_writable_root_is_what_is_actually_asserted(single_image):
    findings = by_check(contents(single_image))

    assert findings["root_fstab_writable:root"].result == PASS
    assert findings["boot_writable_root:boot"].result == PASS
    assert "root_fstab_readonly:root" not in findings
    assert "boot_readonly_root:boot" not in findings


@requires_mkfs
def test_a_read_only_root_fails_rather_than_passing_an_ab_check(tmp_path):
    """The exact image the A/B inspector would call correct."""

    image = build_single_image(
        tmp_path,
        fstab="/dev/disk/by-slot/system  /  ext4 ro,relatime 0 1\n",
    )

    findings = by_check(contents(image))

    assert findings["root_fstab_writable:root"].result == FAIL
    assert "ro" in findings["root_fstab_writable:root"].detail


@requires_mkfs
def test_a_layout_descriptor_on_a_single_slot_image_is_a_failure(tmp_path):
    """It would tell the runtime it has slots, and every A/B path would act."""

    image = build_single_image(tmp_path, layout_descriptor=True)

    findings = by_check(contents(image))

    assert findings["layout_descriptor_absent:root"].result == FAIL


@requires_mkfs
def test_a_root_that_ships_a_host_private_key_is_refused_here_too(tmp_path):
    """Variant-independent: a private key in a public artefact is compromised."""

    root_tree = tmp_path / "root"
    populate_single_root(root_tree)
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
    for check in ("health_service_enabled:root", "persistence_service_enabled:root"):
        assert check not in findings
