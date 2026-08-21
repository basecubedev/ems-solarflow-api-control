# SPDX-License-Identifier: AGPL-3.0-or-later
"""What is inside the image that will actually be flashed.

The strict gate ran the image inspector without ``--mount``, so five checks —
the slot cmdline, the package in both roots, the shared-slot configuration and
two enabled services — were reported NOT RUN while the gate itself passed. The
image content was inferred from the source files that went into the build, not
read out of the artefact.

Mounting is not the fix. The Pi 5 root filesystem uses 16 KiB ext4 blocks and no
4 KiB-page host kernel will mount one, so on an ordinary x86 build host the
mount-based path could not run at all; it also needs root and a loop device,
which CI does not have. The structures are therefore read directly out of the
image.

These tests build real filesystems with mkfs — including a 16 KiB one, on a
host whose kernel refuses to mount it — and prove that the content checks see
what is really there, and fail when it is not.
"""

import json
import shutil
import struct
import subprocess
import uuid
import zlib
from pathlib import Path

import pytest

from appliance import ab_filesystems, ab_image
from appliance.ab_image import FAIL, PASS

pytestmark = [pytest.mark.integration, pytest.mark.simulation]

SECTOR = ab_image.SECTOR_SIZE
APPLIANCE_VERSION = "0.1.0"
BUILD_ID = "20260809120000"

MKFS_EXT4 = shutil.which("mkfs.ext4") or "/usr/sbin/mkfs.ext4"
MKFS_VFAT = shutil.which("mkfs.vfat") or "/usr/sbin/mkfs.vfat"

requires_mkfs = pytest.mark.skipif(
    not Path(MKFS_EXT4).exists()
    or not Path(MKFS_VFAT).exists()
    or shutil.which("mcopy") is None,
    reason="mkfs.ext4, mkfs.vfat and mcopy are required to build real filesystems",
)

UNITS = (
    "ems-appliance-ab-health.service",
    "ems-appliance-slot-bootstrap.service",
    "ems-appliance-persistence.service",
    "ems-appliance-host-identity.service",
)

AUTOBOOT = """[all]
tryboot_a_b=1
boot_partition=2

[tryboot]
boot_partition=3
"""

# What image-rota writes for an ext4 root. systemd-remount-fs applies it, so
# this line is where the read-only root is actually enforced.
FSTAB = (
    "/dev/disk/by-slot/active/system / ext4 ro,relatime,commit=30 0 1\n"
    "/dev/disk/by-slot/active/boot /boot/firmware vfat defaults,ro,noatime,nofail 0 2\n"
)

CMDLINE = (
    "console=serial0,115200 root=/dev/disk/by-slot/active/system rootfstype=ext4 ro fsck.repair=yes"
)

# What a real image carries: the firmware talks on the same line the kernel
# later takes over, per board.
CONFIG = "arm_64bit=1\n\n[pi4]\nenable_uart=1\n\n[pi5]\nBOOT_UART=1\n\n[all]\nuart_2ndstage=1\n"


def populate_root(
    base,
    *,
    version=APPLIANCE_VERSION,
    build_id=BUILD_ID,
    package=ab_image.PACKAGE_NAME,
    status="install ok installed",
    architecture="arm64",
    dpkg=True,
    fstab=FSTAB,
):
    """A slot root carrying exactly what an appliance image has to carry."""

    base.mkdir(parents=True, exist_ok=True)
    for helper in ab_image.RUNTIME_HELPERS:
        path = base / helper
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")

    if dpkg:
        (base / "var/lib/dpkg").mkdir(parents=True)
        (base / "var/lib/dpkg/status").write_text(
            "Package: bash\nStatus: install ok installed\nVersion: 5.2\n\n"
            f"Package: {package}\n"
            f"Status: {status}\n"
            f"Version: {version}\n"
            f"Architecture: {architecture}\n\n"
        )

    (base / "etc/ems-appliance-manager").mkdir(parents=True)
    (base / "etc/ems-appliance-os-build").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "release_version": version,
                "architecture": architecture,
                # What dpkg answered inside the chroot at build time. On a
                # real image /var is bound per slot, so this is the only
                # package record the slot root itself carries.
                "package": {
                    "name": package,
                    "version": version,
                    "architecture": architecture,
                    "status": status,
                },
            }
        )
    )
    (base / "etc/ems-appliance-manager/ab-layout.json").write_text(
        json.dumps({"schema_version": 2, "layout_id": "ems-appliance-rota-v1"})
    )

    shared = base / "etc/rpi-image-gen/slot-shared.d"
    shared.mkdir(parents=True)
    (shared / "50-ems-appliance.conf").write_text(
        "Version=1\n"
        + "".join(
            f"Path={path}\n"
            for path in (
                "/opt/ems-solarflow",
                "/var/lib/ems-appliance-manager",
                "/var/log/ems-appliance-manager",
                "/etc/ems-appliance-manager",
                "/var/lib/ems-appliance-os-update",
                "/etc/NetworkManager/system-connections",
            )
        )
    )

    units = base / "usr/lib/systemd/system"
    units.mkdir(parents=True)
    for unit in (*UNITS, ab_image.MACHINE_ID_UNIT):
        (units / unit).write_text("[Unit]\n")

    wants = base / ab_image.WANTS_DIRECTORY
    wants.mkdir(parents=True)
    for unit in UNITS:
        (wants / unit).symlink_to(f"/usr/lib/systemd/system/{unit}")

    activation = base / ab_image.SHARED_ACTIVATION_DIRECTORY
    activation.mkdir(parents=True)
    for mount in ab_image.SHARED_ACTIVATIONS:
        (activation / mount).symlink_to(f"/run/systemd/generator/{mount}")

    generators = base / "usr/lib/systemd/system-generators"
    generators.mkdir(parents=True)
    for generator in ab_image.SLOT_GENERATORS:
        (base / generator).write_text("#!/bin/sh\n")

    for path, unit in ab_image.SERVICE_DROP_INS.items():
        (base / path).parent.mkdir(parents=True, exist_ok=True)
        (base / path).write_text(f"[Unit]\nRequires={unit}\nAfter={unit}\n")

    (base / "etc/fstab").write_text(fstab)

    for mountpoint in ab_image.MOUNT_POINTS:
        (base / mountpoint.lstrip("/")).mkdir(parents=True, exist_ok=True)

    (base / "etc/ssh").mkdir(parents=True)
    (base / "etc/ssh/sshd_config").write_text("Port 22\n")
    return base


def make_ext4(path, source, *, size_mib=24, block_size=16384):
    subprocess.run(
        [MKFS_EXT4, "-q", "-F", "-b", str(block_size), "-d", str(source), str(path),
         f"{size_mib}M"],
        capture_output=True,
        check=True,
    )
    return path


def make_fat(path, files, *, size_kib=4096):
    # The FAT width is mkfs's choice: a Pi boot partition is FAT32 and a small
    # bootconfig is FAT12, and the reader has to answer for whichever it meets.
    subprocess.run(
        [MKFS_VFAT, "-C", str(path), str(size_kib)], capture_output=True, check=True
    )
    for name, content in files.items():
        staged = path.parent / name
        staged.write_bytes(content if isinstance(content, bytes) else content.encode())
        subprocess.run(
            ["mcopy", "-i", str(path), str(staged), f"::{name}"], capture_output=True, check=True
        )
    return path


def _guid_bytes(text):
    fields = uuid.UUID(text).fields
    return (
        struct.pack("<IHH", fields[0], fields[1], fields[2])
        + bytes([fields[3], fields[4]])
        + fields[5].to_bytes(6, "big")
    )


def assemble(target, blobs):
    """One GPT image holding the six real filesystems, image-rota's order."""

    placed = []
    cursor = 2048
    for label, fstype, blob in blobs:
        sectors = (blob.stat().st_size + SECTOR - 1) // SECTOR
        placed.append((label, fstype, blob, cursor, cursor + sectors - 1))
        cursor += sectors

    table = bytearray(128 * 128)
    for index, (label, fstype, _blob, first, last) in enumerate(placed):
        record = bytearray(128)
        record[0:16] = _guid_bytes(
            ab_image.TYPE_ESP_FAT if fstype == "vfat" else ab_image.TYPE_LINUX
        )
        record[16:32] = _guid_bytes(f"{index + 1:08d}-0000-4000-8000-000000000000")
        struct.pack_into("<QQ", record, 32, first, last)
        encoded = label.encode("utf-16-le")
        record[56 : 56 + len(encoded)] = encoded
        table[index * 128 : (index + 1) * 128] = record
    entries_crc = zlib.crc32(bytes(table)) & 0xFFFFFFFF

    total = cursor + 2048
    backup_lba = total - 1
    backup_entries = backup_lba - 32

    def header(current, backup, entries_lba):
        raw = bytearray(SECTOR)
        raw[0:8] = ab_image.GPT_SIGNATURE
        struct.pack_into("<IIII", raw, 8, 0x00010000, 92, 0, 0)
        struct.pack_into("<QQQQ", raw, 24, current, backup, 34, backup_entries - 1)
        raw[56:72] = _guid_bytes("77777777-7777-4777-8777-777777777777")
        struct.pack_into("<QIII", raw, 72, entries_lba, 128, 128, entries_crc)
        struct.pack_into("<I", raw, 16, zlib.crc32(bytes(raw[:92])) & 0xFFFFFFFF)
        return bytes(raw)

    with open(target, "wb") as handle:
        handle.truncate(total * SECTOR)
        handle.seek(SECTOR)
        handle.write(header(1, backup_lba, 2))
        handle.seek(2 * SECTOR)
        handle.write(table)
        handle.seek(backup_entries * SECTOR)
        handle.write(table)
        handle.seek(backup_lba * SECTOR)
        handle.write(header(backup_lba, 1, backup_entries))
        for _label, _fstype, blob, first, _last in placed:
            handle.seek(first * SECTOR)
            handle.write(blob.read_bytes())
    return target


def build_appliance_image(tmp_path, **overrides):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    root = populate_root(work / "root", **overrides.get("root", {}))
    if "mutate" in overrides:
        overrides["mutate"](root)
    system = make_ext4(work / "system.ext4", root)
    # image-rota writes one bit-for-bit identical pair.
    system_b = work / "system_b.ext4"
    system_b.write_bytes(system.read_bytes())

    boot = make_fat(
        work / "boot.fat",
        {
            "cmdline.txt": overrides.get("cmdline", CMDLINE),
            "config.txt": overrides.get("config", CONFIG),
            "kernel8.img": b"\x00" * 1024,
            "initramfs8": b"\x00" * 1024,
            "bcm2712-rpi-5-b.dtb": b"\x00" * 512,
        },
    )
    boot_b = work / "boot_b.fat"
    boot_b.write_bytes(boot.read_bytes())

    bootconfig = make_fat(
        work / "bootconfig.fat",
        {"autoboot.txt": overrides.get("autoboot", AUTOBOOT)},
        size_kib=1024,
    )
    persistent = make_ext4(work / "persistent.ext4", work / "empty", size_mib=8, block_size=4096)

    return assemble(
        tmp_path / "appliance.img",
        [
            ("bootconfig", "vfat", bootconfig),
            ("boot_a", "vfat", boot),
            ("boot_b", "vfat", boot_b),
            ("system_a", "ext", system),
            ("system_b", "ext", system_b),
            ("persistent", "ext", persistent),
        ],
    )


@pytest.fixture
def appliance_image(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    return build_appliance_image(tmp_path)


def contents(image, **kwargs):
    kwargs.setdefault("appliance_version", APPLIANCE_VERSION)
    kwargs.setdefault("build_id", BUILD_ID)
    return {
        finding.check: (finding.result, finding.detail)
        for finding in ab_image.inspect_contents(image, **kwargs)
    }


def flip_byte(image, label, position=4096):
    partition = next(
        item for item in ab_image.read_partitions(image) if item.label == label
    )
    with open(image, "r+b") as handle:
        handle.seek(partition.offset + position)
        original = handle.read(1)
        handle.seek(partition.offset + position)
        handle.write(bytes([original[0] ^ 0xFF]))


@requires_mkfs
def test_a_sixteen_kibibyte_root_is_read_without_mounting_it(appliance_image):
    """The host kernel cannot mount this filesystem. The gate still reads it."""

    partition = next(
        item for item in ab_image.read_partitions(appliance_image) if item.label == "system_a"
    )
    reader = ab_filesystems.open_partition(appliance_image, partition)

    assert isinstance(reader, ab_filesystems.Ext4Reader)
    assert reader.block_size == 16384
    assert reader.is_file("usr/bin/ems-appliance")
    assert reader.is_symlink(f"{ab_image.WANTS_DIRECTORY}/{UNITS[0]}")


@requires_mkfs
def test_every_content_check_passes_for_a_correctly_built_image(appliance_image):
    found = contents(appliance_image)

    failures = {check: detail for check, (result, detail) in found.items() if result != PASS}
    assert failures == {}
    assert found["package_version:system_b"][0] == PASS
    assert found["bootconfig_autoboot"][0] == PASS


@requires_mkfs
def test_both_roots_and_both_boot_partitions_are_inspected(appliance_image):
    found = contents(appliance_image)

    for label in ("system_a", "system_b"):
        assert f"package_installed:{label}" in found
        assert f"no_host_key_shipped:{label}" in found
    for label in ("boot_a", "boot_b"):
        assert f"boot_cmdline:{label}" in found


@requires_mkfs
def test_a_package_missing_from_the_second_root_is_a_failure(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(tmp_path)
    # Rebuild system_b without the package, the way a half-installed build
    # would leave it.
    work = tmp_path / "work"
    stripped = populate_root(work / "stripped")
    (stripped / "usr/bin/ems-appliance").unlink()
    make_ext4(work / "system_b.ext4", stripped)
    partition = next(
        item for item in ab_image.read_partitions(image) if item.label == "system_b"
    )
    with open(image, "r+b") as handle:
        handle.seek(partition.offset)
        handle.write((work / "system_b.ext4").read_bytes())

    found = contents(image)

    assert found["package_installed:system_a"][0] == PASS
    assert found["package_installed:system_b"][0] == FAIL


@requires_mkfs
def test_a_wrong_package_version_is_a_failure(appliance_image):
    found = contents(appliance_image, appliance_version="9.9.9")

    assert found["package_version:system_a"][0] == FAIL
    assert "0.1.0" in found["package_version:system_a"][1]


# --- the package record is compared exactly ---------------------------------
#
# `expected in observed` answered the version question, so 0.1.0 matched
# 10.1.0, 0.1.0-rc1 and 20.1.0-beta alike. Every field is compared for equality
# now, and each of the four is its own finding.


def image_with_package(tmp_path, **package):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    return build_appliance_image(tmp_path, root=package)


@requires_mkfs
def test_the_expected_version_matches_only_itself(appliance_image):
    found = contents(appliance_image, appliance_version=APPLIANCE_VERSION)

    assert found["package_version:system_a"][0] == PASS
    assert found["package_version:system_b"][0] == PASS


@requires_mkfs
@pytest.mark.parametrize("installed", ["10.1.0", "0.1.0-rc1", "20.1.0", "0.1.10"])
def test_a_version_that_merely_contains_the_expected_one_is_a_failure(tmp_path, installed):
    """The reproduction: 0.1.0 is not 10.1.0, and substring matching says it is."""

    image = image_with_package(tmp_path, version=installed)

    found = contents(image, appliance_version="0.1.0")

    assert found["package_version:system_a"][0] == FAIL, installed
    assert installed in found["package_version:system_a"][1]


@requires_mkfs
def test_another_package_under_the_expected_version_is_a_failure(tmp_path):
    image = image_with_package(tmp_path, package="ems-appliance-manager-dev")

    found = contents(image)

    assert found["package_name:system_a"][0] == FAIL
    assert found["package_name:system_b"][0] == FAIL


@requires_mkfs
def test_a_package_that_is_unpacked_but_not_configured_is_a_failure(tmp_path):
    image = image_with_package(tmp_path, status="install ok unpacked")

    found = contents(image)

    assert found["package_status:system_a"][0] == FAIL
    assert "unpacked" in found["package_status:system_a"][1]


@requires_mkfs
def test_a_package_built_for_another_architecture_is_a_failure(tmp_path):
    image = image_with_package(tmp_path, architecture="amd64")

    found = contents(image, architecture="arm64")

    assert found["package_architecture:system_a"][0] == FAIL
    assert "amd64" in found["package_architecture:system_a"][1]


@requires_mkfs
def test_a_correct_package_passes_every_field(appliance_image):
    found = contents(appliance_image, architecture="arm64")

    for check in ("package_name", "package_status", "package_version", "package_architecture"):
        assert found[f"{check}:system_a"][0] == PASS, check
        assert found[f"{check}:system_b"][0] == PASS, check


# --- and from the build marker, which is all a real slot root has -----------


@requires_mkfs
def test_a_slot_root_without_a_dpkg_database_is_read_from_the_build_marker(tmp_path):
    """image-rota binds /var per slot, so the real image carries no database."""

    image = image_with_package(tmp_path, dpkg=False)

    found = contents(image, architecture="arm64")

    assert found["package_name:system_a"][0] == PASS
    assert found["package_version:system_a"][0] == PASS
    assert found["package_architecture:system_a"][0] == PASS
    assert found["package_status:system_a"][0] == PASS
    assert "build marker" in found["package_version:system_a"][1] \
        or "0.1.0" in found["package_version:system_a"][1]


@requires_mkfs
def test_a_marker_recorded_version_is_still_compared_exactly(tmp_path):
    image = image_with_package(tmp_path, dpkg=False, version="10.1.0")

    found = contents(image, appliance_version="0.1.0")

    assert found["package_version:system_a"][0] == FAIL


@requires_mkfs
def test_a_root_with_neither_source_proves_nothing(tmp_path):
    def strip(root):
        (root / "etc/ems-appliance-os-build").write_text(json.dumps({"build_id": BUILD_ID}))

    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(tmp_path, root={"dpkg": False}, mutate=strip)

    found = contents(image)

    assert found["package_name:system_a"][0] == FAIL
    assert found["package_version:system_a"][0] == FAIL


@requires_mkfs
def test_a_dpkg_database_without_this_package_is_not_answered_from_the_marker(tmp_path):
    """A database that is there and does not name us is a different fact."""

    image = image_with_package(tmp_path, package="something-else")

    found = contents(image)

    assert found["package_name:system_a"][0] == FAIL
    assert "the dpkg database" in found["package_name:system_a"][1]


@requires_mkfs
def test_a_shipped_host_key_is_a_failure(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(
        tmp_path,
        mutate=lambda root: (root / "etc/ssh/ssh_host_ed25519_key").write_text("PRIVATE\n"),
    )

    found = contents(image)

    assert found["no_host_key_shipped:system_a"][0] == FAIL
    assert "ssh_host_ed25519_key" in found["no_host_key_shipped:system_a"][1]


# The appliance ships no login account, so a board that never reaches the
# network cannot be asked anything. The serial line is the only channel left,
# and it is the one that carries the A/B root resolution's own refusal. It comes
# from the generator's defaults, which is exactly why it is asserted here: a
# generator bump that dropped it would take the only first-boot diagnosis with
# it, and nothing would say so.


@requires_mkfs
def test_both_slots_narrate_their_boot_on_a_serial_line(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(tmp_path)

    found = contents(image)

    assert found["boot_console:boot_a"][0] == PASS
    assert found["boot_console:boot_b"][0] == PASS


@requires_mkfs
def test_a_boot_that_only_talks_to_a_screen_is_a_failure(tmp_path):
    """tty1 is not a diagnosis: a box that never comes up has nothing attached."""

    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(
        tmp_path, cmdline="console=tty1 root=/dev/disk/by-slot/active/system ro"
    )

    found = contents(image)

    assert found["boot_console:boot_a"][0] == FAIL


@requires_mkfs
def test_a_boot_with_no_console_at_all_is_a_failure(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(
        tmp_path, cmdline="root=/dev/disk/by-slot/active/system ro"
    )

    found = contents(image)

    assert found["boot_console:boot_a"][0] == FAIL


@requires_mkfs
def test_the_firmware_is_asked_to_speak_before_the_kernel_does(tmp_path):
    """A failure earlier than the kernel is the one a serial line answers that
    nothing else can."""

    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(tmp_path)

    found = contents(image)

    assert found["boot_firmware_uart:boot_a"][0] == PASS


@requires_mkfs
def test_a_firmware_that_stays_silent_is_a_failure(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(tmp_path, config="arm_64bit=1\n")

    found = contents(image)

    assert found["boot_firmware_uart:boot_a"][0] == FAIL


@requires_mkfs
def test_a_boot_partition_that_pins_a_fixed_root_is_a_failure(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(tmp_path, cmdline="console=serial0 root=/dev/mmcblk0p4 ro")

    found = contents(image)

    assert found["boot_cmdline:boot_a"][0] == FAIL
    assert found["boot_cmdline:boot_b"][0] == FAIL


@requires_mkfs
def test_a_writable_root_is_a_failure(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(
        tmp_path, cmdline="console=serial0 root=/dev/disk/by-slot/active/system rw"
    )

    found = contents(image)

    assert found["boot_readonly_root:boot_a"][0] == FAIL


@requires_mkfs
def test_a_bootconfig_without_the_tryboot_selector_is_a_failure(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    image = build_appliance_image(tmp_path, autoboot="[all]\nboot_partition=2\n")

    found = contents(image)

    assert found["bootconfig_autoboot"][0] == FAIL
    assert found["bootconfig_sections"][0] == FAIL


@requires_mkfs
def test_one_changed_byte_in_the_second_root_breaks_the_slot_pairing(appliance_image):
    partitions = ab_image.read_partitions(appliance_image)
    before = ab_image._slot_pairing_finding(appliance_image, partitions)
    flip_byte(appliance_image, "system_b", position=1024 * 1024)

    after = ab_image._slot_pairing_finding(appliance_image, partitions)

    assert before.result == PASS
    assert after.result == FAIL
    assert "hashes to" in after.detail


@requires_mkfs
def test_one_changed_byte_in_the_second_boot_partition_breaks_the_boot_pairing(appliance_image):
    partitions = ab_image.read_partitions(appliance_image)
    before = ab_image._boot_pairing_finding(appliance_image, partitions)
    flip_byte(appliance_image, "boot_b", position=8192)

    after = ab_image._boot_pairing_finding(appliance_image, partitions)

    assert before.result == PASS
    assert after.result == FAIL


@requires_mkfs
def test_the_whole_inspection_of_a_correct_image_has_no_failure_and_no_hidden_skip(
    appliance_image,
):
    findings = ab_image.inspect(
        appliance_image, appliance_version=APPLIANCE_VERSION, build_id=BUILD_ID
    )
    summary = ab_image.summarise(findings)

    assert summary["counts"][FAIL] == 0, [
        f.to_dict() for f in findings if f.result == FAIL
    ]
    assert summary["counts"]["not_run"] == 0, [
        f.to_dict() for f in findings if f.result == "not_run"
    ]


@requires_mkfs
def test_a_service_that_could_start_against_a_fallback_directory_is_a_failure(tmp_path):
    """NetworkManager without the persistence ordering is the reproduction.

    /etc/NetworkManager/system-connections is a shared bind. Upstream's
    generator guards each bind with a condition and fails open, so a missing
    persistent source leaves the empty slot-local directory in place — and
    NetworkManager starting against that comes up with no profiles, looks
    healthy, and writes new ones where the next slot switch discards them.
    """

    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    drop_in = "etc/systemd/system/NetworkManager.service.d/50-ems-appliance-persistence.conf"
    image = build_appliance_image(
        tmp_path, mutate=lambda root: (root / drop_in).unlink()
    )

    found = contents(image)

    assert found["service_drop_ins:system_a"][0] == FAIL
    assert "NetworkManager" in found["service_drop_ins:system_a"][1]


@requires_mkfs
def test_a_drop_in_that_only_orders_and_does_not_require_is_a_failure(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/empty").mkdir()
    drop_in = "etc/systemd/system/NetworkManager.service.d/50-ems-appliance-persistence.conf"
    image = build_appliance_image(
        tmp_path,
        mutate=lambda root: (root / drop_in).write_text(
            "[Unit]\nAfter=ems-appliance-persistence.service\n"
        ),
    )

    found = contents(image)

    assert found["service_drop_ins:system_a"][0] == FAIL
    assert "does not require" in found["service_drop_ins:system_a"][1]


def test_the_docker_daemon_configuration_is_placed_in_the_image():
    """Container logs land on the shared persistent partition, so an appliance
    without rotation fills the partition its own updates need. /etc is
    slot-local and read-only at runtime, so this can only be baked in."""

    import json

    root = Path(__file__).resolve().parents[1]
    overlay = (
        root
        / "packaging/appliance/image/layer/ems-appliance.rootfs-overlay"
        / "etc/docker/daemon.json"
    )

    assert overlay.is_file(), "the Docker daemon configuration reaches no image"
    payload = json.loads(overlay.read_text(encoding="utf-8"))
    assert payload["log-opts"]["max-size"]
    assert payload["log-opts"]["max-file"]


def test_the_package_never_rewrites_a_hosts_docker_configuration():
    """On a plain host /etc/docker/daemon.json is the operator's file."""

    root = Path(__file__).resolve().parents[1]
    for script in ("postinst", "prerm", "postrm"):
        text = (root / "packaging/appliance/debian" / script).read_text(encoding="utf-8")
        assert "daemon.json" not in text, script
