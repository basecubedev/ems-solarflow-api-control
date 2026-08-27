# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real filesystems for the image inspector to read.

Mounting is not available to these tests, and that is the point rather than a
limitation: the Pi 5 root uses 16 KiB ext4 blocks and no 4 KiB-page host kernel
will mount one, so the inspector reads the structures out of the image file
directly. These helpers build the real thing with mkfs -- including a 16 KiB
filesystem on a host whose kernel refuses to mount it -- so the inspector is
exercised against what an image actually contains.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from appliance import image_inspect

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

# From the inspector's own expectations, not typed out again: a hand-copied list
# is how a fixture comes to satisfy an inspector that was checking the same
# wrong thing.
UNITS = tuple(sorted(image_inspect.REQUIRED_UNITS.values()))

# What image-rpios writes for an ext4 root. systemd-remount-fs applies it, so
# this line is where the writable root is actually enforced.
FSTAB = (
    "/dev/disk/by-slot/system  /  ext4 rw,relatime,errors=remount-ro,commit=30 0 1\n"
    "/dev/disk/by-slot/boot  /boot/firmware  vfat defaults,ro,noatime,nofail 0 2\n"
)

CMDLINE = (
    "console=serial0,115200 root=/dev/disk/by-slot/system rootfstype=ext4 rw fsck.repair=yes"
)

# What a real image carries: the firmware talks on the same line the kernel
# later takes over, per board.
CONFIG = "arm_64bit=1\n\n[pi4]\nenable_uart=1\n\n[pi5]\nBOOT_UART=1\n\n[all]\nuart_2ndstage=1\n"


def populate_root(
    base,
    *,
    version=APPLIANCE_VERSION,
    build_id=BUILD_ID,
    package=image_inspect.PACKAGE_NAME,
    status="install ok installed",
    architecture="arm64",
    dpkg=True,
    fstab=FSTAB,
    units=UNITS,
):
    """A root carrying exactly what an appliance image has to carry."""

    base.mkdir(parents=True, exist_ok=True)
    for helper in image_inspect.RUNTIME_HELPERS:
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
                "image_layer": "image-rpios",
                # What dpkg answered inside the chroot at build time.
                "package": {
                    "name": package,
                    "version": version,
                    "architecture": architecture,
                    "status": status,
                },
            }
        )
    )

    templates = base / image_inspect.OPERATOR_TEMPLATE_DIR
    templates.mkdir(parents=True)
    for name in image_inspect.OPERATOR_CONFIG:
        (templates / name).write_text(f"# {name}\n")

    units_dir = base / image_inspect.UNIT_DIRECTORY
    units_dir.mkdir(parents=True, exist_ok=True)
    wants = base / image_inspect.WANTS_DIRECTORY
    wants.mkdir(parents=True, exist_ok=True)
    for unit in units:
        # A unit that names the program it runs, so the image check that the
        # program exists has something real to look for.
        program = {
            "ems-appliance-grow-root.service": "/usr/lib/ems-appliance-manager/grow-root.sh",
        }.get(unit, "/usr/bin/ems-appliance")
        (units_dir / unit).write_text(f"[Unit]\n[Service]\nExecStart={program}\n")
        (wants / unit).symlink_to(f"/usr/lib/systemd/system/{unit}")
    helper = base / "usr/lib/ems-appliance-manager/grow-root.sh"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("#!/bin/sh\n")

    (base / "etc/fstab").write_text(fstab)

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
    # one is FAT12, and the reader has to answer for whichever it meets.
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
