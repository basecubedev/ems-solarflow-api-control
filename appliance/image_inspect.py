# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading a built appliance image, and checking it against its contract.

The layout itself is not described here. ``image-rpios`` owns the partition
table, and this module only states what the runtime depends on: the MBR labels,
the filesystem types, and what each partition has to contain.

The inspector reads the table out of an image **file**. It does not attach a
loop device, does not mount anything and needs no privileges, so it can run in
CI and on a developer machine.

Content is read the same way, through ``image_filesystems``. Mounting is not an
option a release gate can rely on: the Pi 5 root filesystem uses 16 KiB ext4
blocks, which no 4 KiB-page host kernel will mount, and mounting needs root and
a loop device besides. A check that cannot run is not a check that passed, so
the questions that decide whether an image is an appliance -- the package
version in the root, the enabled units, the absence of a shipped host key --
are answered out of the filesystem structures directly.
"""

import gzip
import json
import lzma
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from appliance import backup_ownership, image_filesystems, media_sizing
from appliance.image_shape import IMAGE

SECTOR_SIZE = 512

FAT_SIGNATURE_OFFSET = 0x1FE
FAT_SIGNATURE = b"\x55\xaa"
EXT_SUPERBLOCK_OFFSET = 0x400
EXT_MAGIC_OFFSET = 0x38
EXT_MAGIC = b"\x53\xef"


# What image-rpios writes: an MBR with a FAT boot partition and one ext4 root.
SINGLE_SLOT_PARTITIONS = (
    ("boot", "vfat"),
    ("root", "ext"),
)

# media_sizing is what the signed attestation states minimum_media_bytes from,
# and nothing read the built image back: a profile size that stopped reaching
# genimage was attested rather than detected. Exact equality on purpose -- a
# tolerance is what let the number drift in the first place.
DECLARED_PARTITION_BYTES = {
    "boot": media_sizing.BOOT_PARTITION_BYTES,
    "root": media_sizing.ROOT_PARTITION_BYTES,
}


class ImageError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ImagePartition:
    number: int
    partuuid: str
    type_guid: str
    label: str
    first_lba: int
    last_lba: int

    @property
    def offset(self):
        return self.first_lba * SECTOR_SIZE

    @property
    def size_bytes(self):
        return (self.last_lba - self.first_lba + 1) * SECTOR_SIZE

    def to_dict(self):
        return {
            "number": self.number,
            "partuuid": self.partuuid,
            "type_guid": self.type_guid,
            "label": self.label,
            "offset": self.offset,
            "size_bytes": self.size_bytes,
        }


# --- reading an image file --------------------------------------------------


MBR_TABLE_OFFSET = 446
MBR_ENTRY_SIZE = 16
MBR_ENTRY_COUNT = 4
MBR_SIGNATURE = b"\x55\xaa"

# An MBR carries no partition names. Upstream's genimage template assigns the
# boot partition type 0x0C (FAT32 LBA) and the root 0x83 (Linux), so that is
# what the name is taken from -- an entry of any other type is left unnamed
# rather than guessed at, because a wrong name here would inspect the wrong
# filesystem and report the answer as though it were about the right one.
MBR_PARTITION_LABELS = {0x0C: "boot", 0x83: "root"}


def read_mbr_partitions(path):
    """The MBR partition entries of an image file, without mounting anything."""

    target = Path(path)
    try:
        with target.open("rb") as handle:
            sector = handle.read(SECTOR_SIZE)
    except OSError as exc:
        raise ImageError("image_unreadable", f"{target} could not be read: {exc}")
    if len(sector) < SECTOR_SIZE or sector[510:512] != MBR_SIGNATURE:
        raise ImageError("image_not_mbr", f"{target} does not carry an MBR partition table")

    partitions = []
    for index in range(MBR_ENTRY_COUNT):
        start = MBR_TABLE_OFFSET + index * MBR_ENTRY_SIZE
        entry = sector[start : start + MBR_ENTRY_SIZE]
        kind = entry[4]
        first_lba, sectors = struct.unpack("<II", entry[8:16])
        if not kind or not sectors:
            continue
        partitions.append(
            ImagePartition(
                number=index + 1,
                partuuid="",
                type_guid=f"0x{kind:02x}",
                label=MBR_PARTITION_LABELS.get(kind, ""),
                first_lba=first_lba,
                last_lba=first_lba + sectors - 1,
            )
        )
    return partitions


def filesystem_signature(path, partition):
    """``vfat``, ``ext``, or ``""`` when the partition carries neither."""

    try:
        with Path(path).open("rb") as handle:
            handle.seek(partition.offset + EXT_SUPERBLOCK_OFFSET + EXT_MAGIC_OFFSET)
            if handle.read(2) == EXT_MAGIC:
                return "ext"
            handle.seek(partition.offset + FAT_SIGNATURE_OFFSET)
            if handle.read(2) == FAT_SIGNATURE:
                return "vfat"
    except OSError:
        return ""
    return ""


# --- inspection -------------------------------------------------------------


@dataclass
class Finding:
    """One answer, and whether a release may be cut without it.

    ``mandatory`` is the difference between "this check passed" and "this check
    did not run, and that is acceptable". Without it a release gate reading only
    pass and fail counts calls an inspection that never executed half its checks
    a passing inspection.
    """

    check: str
    result: str
    detail: str = ""
    mandatory: bool = True

    def to_dict(self):
        return {
            "check": self.check,
            "result": self.result,
            "detail": self.detail,
            "mandatory": self.mandatory,
        }


PASS = "pass"
FAIL = "fail"
NOT_RUN = "not_run"

# Checks a production image inspection may report as NOT RUN. Optionality is a
# policy decision recorded here, never something derived from whether a tool
# happened to be installed on the machine that ran the inspection.
OPTIONAL_CHECKS = frozenset()


APPLIANCE_BINARY = "usr/bin/ems-appliance"
WANTS_DIRECTORY = "etc/systemd/system/multi-user.target.wants"
# Where a unit is masked: a link to /dev/null under /etc, which systemd reads
# before anything under /usr/lib.
ETC_UNIT_DIRECTORY = "etc/systemd/system"

# The second network stack. NetworkManager (REQUIRED_UNITS) is the one DHCP
# client this appliance can steer, so these are masked in the image rather
# than left to whatever the base layer enabled: two clients on eth0 took two
# leases, and the address an owner bookmarked was the one that went away.
MASKED_NETWORK_UNITS = (
    "systemd-networkd.service",
    "systemd-networkd.socket",
    "systemd-networkd-wait-online.service",
)


# --- what the image has to contain ------------------------------------------

PACKAGE_NAME = "ems-appliance-manager"
DPKG_STATUS = "var/lib/dpkg/status"
BUILD_MARKER = "etc/ems-appliance-os-build"

# Files that belong to whoever runs the appliance. Shipping a packaged copy of
# one of these under /etc would put an operator edit and a package file at the
# same path, and dpkg is entitled to the second. The image must carry them only
# as templates, and ems-appliance-config-seed.service creates what is missing.
OPERATOR_CONFIG = ("appliance.conf", "allowed-images.conf")
OPERATOR_CONFIG_DIR = "etc/ems-appliance-manager"
OPERATOR_TEMPLATE_DIR = "usr/share/ems-appliance-manager"

UNIT_DIRECTORY = "usr/lib/systemd/system"
SSH_DIRECTORY = "etc/ssh"


# The units the root must carry enabled.
REQUIRED_UNITS = {
    "agent_service_enabled": "ems-appliance-agent.service",
    "web_service_enabled": "ems-appliance-web.service",
    # The medium an owner flashed is whatever they had; the root the build
    # sized is 8 GiB. Without this the rest of the card is unreachable, and
    # everything on this appliance writes to that root.
    "grow_root_service_enabled": "ems-appliance-grow-root.service",
    "config_seed_service_enabled": "ems-appliance-config-seed.service",
    # The image deletes the host keys the build chroot made; this unit is the
    # only thing that makes new ones (Debian's sshd-keygen.service is WantedBy
    # the ssh units alone, and this image ships ssh off). Without it sshd
    # cannot start, and backup-access reads the missing policy as a reason
    # to expire the backup account.
    "sshd_keys_service_enabled": "ems-appliance-sshd-keys.service",
    # Enabled by the postinst's offline fallback, never by the layer's
    # enable-units line. Without them the export root is never built and an
    # EMS installed after the first boot is, in the postinst's own words,
    # silently never published.
    "export_path_enabled": "ems-appliance-export.path",
    "export_service_enabled": "ems-appliance-export.service",
    # The archive watcher: re-grants the backup account's ACL when EMS writes
    # a new archive, so a backup made after boot is readable without one.
    "export_acl_path_enabled": "ems-appliance-export-acl.path",
    # Moves an Admin installed with `restart: "no"` to the policy that brings
    # it back after a reboot; without it such an Admin is missing after each.
    "admin_restart_policy_service_enabled": "ems-appliance-admin-restart-policy.service",
    # The one DHCP client. Debian's networkd units are masked beside this
    # (MASKED_NETWORK_UNITS); nmcli is the only interface this appliance can
    # take an address back on.
    "network_manager_enabled": "NetworkManager.service",
}


RUNTIME_HELPERS = (
    "usr/bin/ems-appliance",
    "usr/lib/ems-appliance-manager/setup-export-root.sh",
    "usr/lib/ems-appliance-manager/backup-account.sh",
    # Written by the postinst, not shipped by dpkg: without it a flashed image
    # cannot establish ownership of the account it carries.
    f"usr/lib/ems-appliance-manager/{backup_ownership.ACCOUNT_ORIGIN_NAME}",
)

# What the layer's rootfs overlay contributes. rpi-image-gen derives the overlay
# directory from the layer file's own stem, so a rename on either side stops it
# being applied and the build says nothing -- the layer file states exactly
# that. Each of these bounds a write to the SD card: unbounded Docker json-file
# logs and journald's default of 10% of the root outlive the card by years.
# tests/test_appliance_image.py keeps this equal to the overlay tree itself.
ROOT_OVERLAY_FILES = (
    "etc/docker/daemon.json",
    "etc/systemd/journald.conf.d/50-ems-appliance.conf",
    "etc/systemd/system.conf.d/50-ems-appliance-watchdog.conf",
)

# A board that never reaches the network cannot be asked anything, and the image
# ships no login account. The serial line is the only channel a first boot has,
# and it is where a root that will not mount says so.
SERIAL_CONSOLES = ("serial", "ttyAMA", "ttyS", "ttyUSB")
FIRMWARE_UART_SETTINGS = ("enable_uart=1", "BOOT_UART=1", "uart_2ndstage=1")

BOOT_KERNELS = ("kernel8.img", "kernel_2712.img", "kernel.img")
BOOT_INITRAMFS = ("initramfs8", "initramfs_2712", "initramfs")

# What each board's firmware loads, by rpi_image_gen.HARDWARE_PROFILES name.
# Any kernel used to pass for every board, and a Pi 4 kernel on a Pi 5 card
# does not boot. Read off built images: rpi3 and rpi4 carry kernel8.img, rpi5
# kernel_2712.img, and every image ships every device tree; the firmware picks
# the one its board and stepping need.
BOARD_BOOT_FILES = {
    "rpi3": ("kernel8.img", "initramfs8", ("bcm2710-rpi-3-b.dtb", "bcm2710-rpi-3-b-plus.dtb")),
    "rpi4": ("kernel8.img", "initramfs8", ("bcm2711-rpi-4-b.dtb",)),
    "rpi5": (
        "kernel_2712.img",
        "initramfs_2712",
        ("bcm2712-rpi-5-b.dtb", "bcm2712d0-rpi-5-b.dtb"),
    ),
}

# What makes /dev/disk/by-slot/system, the root the command line names: the
# rule upstream's image layer ships and the tagger it runs. Both have to be in
# the initramfs, because that is where the root is mounted from.
INITRAMFS_SLOT_FILES = ("udev/rules.d/99-rpi-05-image.rules", "bin/rpi-bootdev-tag")
INITRAMFS_LIMIT_BYTES = 512 * 1024 * 1024

# Enabled under sysinit.target: the board has no real-time clock.
TIMESYNC_WANTS = "etc/systemd/system/sysinit.target.wants/systemd-timesyncd.service"
MACHINE_ID = "etc/machine-id"
MACHINE_ID_UNSET = ("", "uninitialized")


PACKAGE_INSTALLED_STATUS = "install ok installed"
FROM_DPKG = "the dpkg database in the root"
FROM_BUILD_MARKER = "the build marker"


def _package_record(reader):
    """The dpkg status stanza for the Appliance Manager, if it is installed."""

    return _dpkg_records(reader)[0]


def _dpkg_records(reader):
    """``(our stanza, database present)`` from ``/var/lib/dpkg/status``.

    The two are separate answers: a root with no database at all and a database
    that is there and does not name this package are different facts, and only
    the second one is evidence of anything.
    """

    try:
        status = reader.read_text(DPKG_STATUS)
    except image_filesystems.FilesystemError:
        return {}, False
    for stanza in status.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            if line.startswith((" ", "\t")) or ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        if fields.get("Package") == PACKAGE_NAME:
            return fields, True
    return {}, True


def _package_evidence(reader, marker):
    """The exact package record this root carries, and where it came from.

    ``expected in observed`` used to answer the version question, which makes
    0.1.0 a match for 10.1.0 and 0.1.0-rc1. Every field below is compared for
    equality instead, so the record has to be exact rather than contained.
    """

    fields, database = _dpkg_records(reader)
    if database:
        return (
            {
                "name": fields.get("Package", ""),
                "version": fields.get("Version", ""),
                "architecture": fields.get("Architecture", ""),
                "status": fields.get("Status", ""),
            },
            FROM_DPKG,
        )
    recorded = marker.get("package")
    if isinstance(recorded, dict):
        return (
            {
                "name": str(recorded.get("name") or ""),
                "version": str(recorded.get("version") or ""),
                "architecture": str(recorded.get("architecture") or ""),
                "status": str(recorded.get("status") or ""),
            },
            FROM_BUILD_MARKER,
        )
    return None, ""


FSTAB = "etc/fstab"


def _fstab_root_mode(reader):
    """``(ok, detail)`` for the mount options the root declares for ``/``.

    A verdict, not a preference: a root that is read-only cannot be patched by
    apt, which is the only way this image is patched at all.
    """

    required, forbidden = "rw", "ro"
    try:
        fstab = reader.read_text(FSTAB)
    except image_filesystems.FilesystemError:
        return False, "the root carries no /etc/fstab"
    for line in fstab.splitlines():
        if line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 4 or fields[1] != "/":
            continue
        options = [option.strip() for option in fields[3].split(",")]
        if forbidden in options:
            return False, f"the root is mounted {forbidden}: {fields[3]}"
        if required not in options:
            return False, f"the root declares no {required}: {fields[3]}"
        return True, fields[3]
    return False, "the root's fstab has no entry for /"


def _missing_exec_paths(reader, unit):
    """Absolute programs a unit's Exec* lines name that the image does not hold.

    systemd allows a leading ``-``, ``+``, ``!`` or ``@`` on the command, and a
    relative first word is a systemd special rather than a path, so only an
    absolute one is a claim about a file this image must carry.
    """

    missing = []
    try:
        text = reader.read_text(f"{UNIT_DIRECTORY}/{unit}")
    except image_filesystems.FilesystemError:
        return [f"{unit} could not be read"]
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key.strip().startswith("Exec"):
            continue
        command = value.strip().split()
        if not command:
            continue
        program = command[0].lstrip("-+!@:")
        if not program.startswith("/"):
            continue
        relative = program.lstrip("/")
        if not (reader.is_file(relative) or reader.is_symlink(relative)):
            missing.append(f"{unit} runs {program}, which the image does not carry")
            continue
        # Present is not runnable. systemd resolves ExecStart with access(X_OK)
        # and fails the unit 203/EXEC when it comes back no, root included --
        # the failure the one booted appliance logged 255 times in 4h39m. A
        # symlink is followed by that call, so it is checked through the link.
        if not reader.is_executable(relative):
            missing.append(
                f"{unit} runs {program}, which the image carries without an execute bit; "
                "systemd would fail it with 203/EXEC"
            )
    return missing


def _root_content_findings(label, reader, *, appliance_version, build_id, architecture):
    findings = []

    def record(check, ok, detail):
        findings.append(Finding(f"{check}:{label}", PASS if ok else FAIL, detail))

    record(
        "package_installed",
        reader.is_file(RUNTIME_HELPERS[0]),
        RUNTIME_HELPERS[0],
    )

    marker = {}
    try:
        marker = json.loads(reader.read_text(BUILD_MARKER))
    except (image_filesystems.FilesystemError, ValueError):
        marker = {}

    # The record the image holds is proven from its dpkg database where it has
    # one, and otherwise from the build marker, which the build captured from
    # dpkg itself inside the chroot.
    evidence, source = _package_evidence(reader, marker)
    if evidence is None:
        for check in ("package_name", "package_status", "package_version",
                      "package_architecture"):
            record(check, False, "the root carries neither a dpkg database nor a "
                                 "package record in its build marker")
    else:
        record(
            "package_name",
            evidence["name"] == PACKAGE_NAME,
            f"{evidence['name'] or 'nothing'} in {source}",
        )
        record(
            "package_status",
            evidence["status"] == PACKAGE_INSTALLED_STATUS,
            f"{evidence['status'] or 'no status'} in {source}",
        )
        if appliance_version:
            record(
                "package_version",
                evidence["version"] == appliance_version,
                f"{evidence['version'] or 'none'} (release declares {appliance_version})",
            )
        else:
            findings.append(
                Finding(
                    f"package_version:{label}",
                    PASS,
                    evidence["version"] or "no version declared",
                )
            )
        if architecture:
            record(
                "package_architecture",
                evidence["architecture"] == architecture,
                f"{evidence['architecture'] or 'none'} (release declares {architecture})",
            )
        else:
            findings.append(
                Finding(
                    f"package_architecture:{label}",
                    PASS,
                    evidence["architecture"] or "no architecture declared",
                )
            )

    if not marker:
        record("build_marker", False, "the root carries no build marker")
    elif build_id and str(marker.get("build_id") or "") != build_id:
        record(
            "build_marker",
            False,
            f"the root reports build {marker.get('build_id') or 'none'}, "
            f"the release is {build_id}",
        )
    else:
        record("build_marker", True, str(marker.get("build_id") or "present"))

    shipped = sorted(
        name for name in OPERATOR_CONFIG if reader.is_file(f"{OPERATOR_CONFIG_DIR}/{name}")
    )
    record(
        "operator_config_not_shipped",
        not shipped,
        f"{', '.join(shipped)} would be reverted at every reboot"
        if shipped
        else "no packaged copy of an operator-owned file under a shared path",
    )
    absent = sorted(
        name for name in OPERATOR_CONFIG if not reader.is_file(f"{OPERATOR_TEMPLATE_DIR}/{name}")
    )
    record(
        "operator_config_templates",
        not absent,
        f"{OPERATOR_TEMPLATE_DIR} is missing {', '.join(absent)}"
        if absent
        else f"{len(OPERATOR_CONFIG)} templates to seed a fresh appliance from",
    )

    # Where the mount mode is actually enforced. The kernel command line decides
    # the initial mount; systemd-remount-fs then applies this line, so a root
    # whose fstab disagrees is that mode however it booted.
    record(
        "root_fstab_writable",
        *_fstab_root_mode(reader),
    )


    record(
        "timesyncd_enabled",
        reader.is_symlink(TIMESYNC_WANTS),
        "enabled under sysinit.target"
        if reader.is_symlink(TIMESYNC_WANTS)
        else "systemd-timesyncd is not enabled; a board without a clock never learns the time",
    )
    try:
        machine_id = reader.read_text(MACHINE_ID).strip()
    except image_filesystems.FilesystemError:
        machine_id = ""
    record(
        "machine_id_uninitialized",
        machine_id in MACHINE_ID_UNSET,
        "left for the first boot to make"
        if machine_id in MACHINE_ID_UNSET
        else "the image carries a machine-id, so every card flashed from it shares one",
    )

    unrunnable = []
    for check, unit in REQUIRED_UNITS.items():
        present = reader.is_file(f"{UNIT_DIRECTORY}/{unit}")
        enabled = reader.is_symlink(f"{WANTS_DIRECTORY}/{unit}")
        record(
            check,
            present and enabled,
            "installed and enabled"
            if present and enabled
            else ("installed but not enabled" if present else "not installed"),
        )
        if present:
            unrunnable.extend(_missing_exec_paths(reader, unit))

    # An enabled unit whose ExecStart is not in the image fails on the first
    # boot and nowhere earlier. "Installed and enabled" was the whole answer
    # before, and it is true of a unit that cannot run.
    record(
        "unit_programs_present",
        not unrunnable,
        "; ".join(unrunnable) if unrunnable else "every enabled unit has its program",
    )

    # One stack may configure the interface. Required unconditionally rather
    # than only when the unit file exists: the layer hook always writes the
    # three links, so their presence is exactly "the hook ran", and an image
    # where it did not is refused rather than excused.
    unmasked = []
    for unit in MASKED_NETWORK_UNITS:
        path = f"{ETC_UNIT_DIRECTORY}/{unit}"
        try:
            target = reader.readlink(path) if reader.is_symlink(path) else ""
        except image_filesystems.FilesystemError:
            target = ""
        if target != "/dev/null":
            unmasked.append(unit)
    record(
        "one_network_stack",
        not unmasked,
        f"a second DHCP client can claim the interface: {', '.join(unmasked)} not masked"
        if unmasked
        else "NetworkManager is the only stack that can configure the interface",
    )


    if not reader.is_dir(SSH_DIRECTORY):
        # listdir answers () for an empty directory and for a path it cannot
        # list at all, so a symlinked or unreadable /etc/ssh used to read as
        # proof that no host key ships. It is also not a shape this appliance
        # has: ems-appliance-sshd-keys.service refuses a symlinked /etc/ssh
        # and would never make the keys.
        record(
            "no_host_key_shipped",
            False,
            f"/{SSH_DIRECTORY} is not a directory this inspection can list, "
            "so whether the image ships a private host key cannot be answered",
        )
    else:
        keys = [name for name in reader.listdir(SSH_DIRECTORY) if name.startswith("ssh_host_")]
        private = [name for name in keys if not name.endswith(".pub")]
        record(
            "no_host_key_shipped",
            not private,
            f"the image carries {', '.join(private)}" if private else "no host key is shipped",
        )


    missing_helpers = [name for name in RUNTIME_HELPERS if not reader.is_file(name)]
    record(
        "runtime_helpers",
        not missing_helpers,
        f"missing: {', '.join(missing_helpers)}"
        if missing_helpers
        else f"{len(RUNTIME_HELPERS)} helpers present",
    )

    # Presence, not content: none of these paths exists in a stock Debian
    # root, so presence is exactly "the overlay was applied". The values in
    # them stay owned by the overlay files.
    missing_overlay = [name for name in ROOT_OVERLAY_FILES if not reader.is_file(name)]
    record(
        "rootfs_overlay_applied",
        not missing_overlay,
        f"the layer overlay was not applied: missing {', '.join(missing_overlay)}"
        if missing_overlay
        else f"{len(ROOT_OVERLAY_FILES)} overlay files present",
    )
    return findings


class InitramfsUnreadable(Exception):
    pass


def _cpio_names(data, offset, names):
    """Read one newc archive from ``offset``; the offset after its trailer."""

    while True:
        header = data[offset:offset + 110]
        if len(header) < 110 or header[:6] not in (b"070701", b"070702"):
            raise InitramfsUnreadable("a cpio header is cut short or malformed")
        try:
            size = int(header[54:62], 16)
            name_size = int(header[94:102], 16)
        except ValueError:
            raise InitramfsUnreadable("a cpio header carries no sizes")
        start = offset + 110
        name = data[start:start + name_size - 1].decode("utf-8", "replace")
        offset = start + name_size
        offset += -offset % 4
        if name == "TRAILER!!!":
            return offset
        names.add(name)
        offset += size
        offset += -offset % 4


def _decompress(data):
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:6] == b"\xfd7zXZ\x00":
        return lzma.decompress(data)
    if data[:4] == b"\x28\xb5\x2f\xfd":
        tool = shutil.which("zstd")
        if tool is None:
            raise InitramfsUnreadable("the main archive is zstd and zstd is not installed")
        result = subprocess.run(
            [tool, "-dc"], input=data, capture_output=True, timeout=120, check=False
        )
        if result.returncode != 0:
            raise InitramfsUnreadable("zstd could not decompress the main archive")
        return result.stdout
    raise InitramfsUnreadable(f"the main archive's compression ({data[:4].hex()}) is unknown")


def initramfs_names(data):
    """Every path in an initramfs: early uncompressed archives, then the main one."""

    names = set()
    offset = 0
    while offset < len(data):
        while offset < len(data) and data[offset] == 0:
            offset += 1
        if offset >= len(data):
            break
        if data[offset:offset + 6] in (b"070701", b"070702"):
            offset = _cpio_names(data, offset, names)
            continue
        main = _decompress(data[offset:])
        if len(main) > INITRAMFS_LIMIT_BYTES:
            raise InitramfsUnreadable("the main archive unpacks beyond any initramfs")
        position = 0
        while position < len(main) and main[position:position + 6] in (b"070701", b"070702"):
            position = _cpio_names(main, position, names)
            while position < len(main) and main[position] == 0:
                position += 1
        break
    return names


def _initramfs_finding(label, reader, initramfs):
    check = f"boot_initramfs_slot_rules:{label}"
    if not initramfs:
        return Finding(check, FAIL, "there is no initramfs to look in")
    missing = []
    for name in initramfs:
        try:
            names = initramfs_names(reader.read_bytes(name))
        except (InitramfsUnreadable, image_filesystems.FilesystemError, OSError, EOFError,
                lzma.LZMAError, subprocess.SubprocessError) as exc:
            return Finding(check, NOT_RUN, f"{name} could not be read: {exc}")
        missing.extend(
            f"{name} lacks {wanted}"
            for wanted in INITRAMFS_SLOT_FILES
            if not any(entry.endswith("/" + wanted) or entry == wanted for entry in names)
        )
    return Finding(
        check,
        FAIL if missing else PASS,
        "; ".join(missing) if missing else "the by-slot rule and rpi-bootdev-tag are in "
        + ", ".join(initramfs),
    )


def _board_fit(entries, profile):
    kernel, initramfs, trees = BOARD_BOOT_FILES[profile]
    missing = [name for name in (kernel, initramfs, *trees) if name not in entries]
    if missing:
        return False, f"{profile} boots {kernel}; the image lacks {', '.join(missing)}"
    return True, f"{kernel}, {initramfs}, {', '.join(trees)}"


def _boot_content_findings(label, reader, profile=""):
    findings = []

    def record(check, ok, detail):
        findings.append(Finding(f"{check}:{label}", PASS if ok else FAIL, detail))

    try:
        cmdline = reader.read_text("cmdline.txt").strip()
    except image_filesystems.FilesystemError:
        record("boot_cmdline", False, "there is no cmdline.txt")
        cmdline = ""
    if cmdline:
        # The root is named by the alias upstream's image layer
        # writes, never by a kernel device name: which name the card comes
        # up as depends on how the board booted.
        device = f"root={IMAGE.root_device}"
        record(
            "boot_cmdline",
            device in cmdline,
            device if device in cmdline else f"root is {cmdline[:120]}",
        )
        # The root has to be writable, and for the plainest of reasons: apt
        # is how this image is patched at all. The command line decides the
        # *initial* mount; /etc/fstab in the root decides what it stays, and
        # that is checked in the root findings.
        fields = cmdline.split()
        readonly = "ro" in fields
        writable = "rw" in fields
        record(
            "boot_writable_root",
            writable and not readonly,
            "ro overrides it"
            if readonly
            else ("rw" if writable else "the root is not asked to be writable"),
        )
        consoles = [field.split("=", 1)[1] for field in fields if field.startswith("console=")]
        serial = [name for name in consoles if name.split(",")[0].startswith(SERIAL_CONSOLES)]
        record(
            "boot_console",
            bool(serial),
            ", ".join(serial)
            or (f"only {', '.join(consoles)}" if consoles else "the kernel names no console"),
        )

    entries = reader.listdir("/")
    kernels = [name for name in BOOT_KERNELS if name in entries]
    initramfs = [name for name in BOOT_INITRAMFS if name in entries]
    blobs = [name for name in entries if name.endswith(".dtb")]
    record("boot_kernel", bool(kernels), ", ".join(kernels) or "no kernel image")
    record("boot_initramfs", bool(initramfs), ", ".join(initramfs) or "no initramfs")
    record("boot_device_tree", bool(blobs), f"{len(blobs)} device-tree blobs")
    if profile:
        record("boot_board_fit", *_board_fit(entries, profile))
        initramfs = [BOARD_BOOT_FILES[profile][1]]
    findings.append(_initramfs_finding(label, reader, [n for n in initramfs if n in entries]))
    record("boot_configuration", "config.txt" in entries, "config.txt")
    try:
        configuration = reader.read_text("config.txt").replace(" ", "")
    except image_filesystems.FilesystemError:
        configuration = ""
    asked = [setting for setting in FIRMWARE_UART_SETTINGS if setting in configuration]
    record(
        "boot_firmware_uart",
        bool(asked),
        ", ".join(asked) or "the firmware is not asked to use the serial line",
    )
    return findings


def inspect_contents(
    image_path, *, appliance_version="", build_id="", architecture="", profile=""
):
    """What the image actually contains, read without mounting anything.

    The boot partition and the root, in that order, each opened straight out of
    the image file. Named, ``profile`` holds the boot partition to that board.
    """

    if profile and profile not in BOARD_BOOT_FILES:
        return [Finding("image_contents", FAIL, f"{profile!r} is not a board this project builds")]

    try:
        partitions = {item.label: item for item in read_mbr_partitions(image_path)}
    except ImageError as exc:
        return [Finding("image_contents", FAIL, exc.message)]

    findings = []
    for label, _ in SINGLE_SLOT_PARTITIONS:
        partition = partitions.get(label)
        if partition is None:
            findings.append(
                Finding(f"partition_present:{label}", FAIL, "the image has no such partition")
            )
            continue
        try:
            reader = image_filesystems.open_partition(image_path, partition)
        except image_filesystems.FilesystemError as exc:
            findings.append(Finding(f"filesystem_readable:{label}", FAIL, exc.message))
            continue
        findings.append(
            Finding(
                f"filesystem_readable:{label}",
                PASS,
                f"{type(reader).__name__.replace('Reader', '').lower()}"
                + (f", {reader.block_size} byte blocks" if hasattr(reader, "block_size") else ""),
            )
        )
        if label == "boot":
            findings.extend(_boot_content_findings(label, reader, profile))
        else:
            findings.extend(
                _root_content_findings(
                    label,
                    reader,
                    appliance_version=appliance_version,
                    build_id=build_id,
                    architecture=architecture,
                )
            )
    return findings


def inspect(
    image_path,
    *,
    expected=None,
    appliance_version="",
    build_id="",
    architecture="",
    contents=True,
    profile="",
):
    """Check a built image against its contract, without booting it.

    ``image-rpios`` owns the partition table: an MBR with a FAT boot partition
    and one ext4 root. What is checked here is what the runtime depends on --
    the labels, the filesystem types, and then the content of both partitions.
    """

    if expected is None:
        expected = SINGLE_SLOT_PARTITIONS

    findings = []
    try:
        partitions = read_mbr_partitions(image_path)
    except ImageError as exc:
        return [Finding("partition_table", FAIL, exc.message)]

    findings.append(Finding("partition_table", PASS, f"{len(partitions)} MBR partitions"))
    if len(partitions) != len(expected):
        findings.append(
            Finding(
                "partition_count",
                FAIL,
                f"{IMAGE.image_layer} produces {len(expected)} "
                f"partitions, the image has {len(partitions)}",
            )
        )
        return findings
    findings.append(Finding("partition_count", PASS, str(len(expected))))

    for partition, (label, fstype) in zip(partitions, expected):
        if partition.label != label:
            findings.append(
                Finding(
                    f"label:{label}",
                    FAIL,
                    f"partition {partition.number} is labelled {partition.label!r}",
                )
            )
        else:
            findings.append(Finding(f"label:{label}", PASS, f"partition {partition.number}"))

        # An MBR names a one-byte type, not a GUID, and read_mbr_partitions
        # only produced this label because that byte said so -- checking it
        # again here would be checking the reader against itself.

        signature = filesystem_signature(image_path, partition)
        if not signature:
            findings.append(
                Finding(f"filesystem:{label}", FAIL, "no FAT or ext superblock at the offset")
            )
        elif signature != fstype:
            findings.append(
                Finding(f"filesystem:{label}", FAIL, f"found {signature}, expected {fstype}")
            )
        else:
            findings.append(Finding(f"filesystem:{label}", PASS, signature))

        declared = DECLARED_PARTITION_BYTES[label]
        findings.append(
            Finding(
                f"partition_size:{label}",
                PASS if partition.size_bytes == declared else FAIL,
                f"{partition.size_bytes} bytes, the profile declares {declared}",
            )
        )

    # Measured from the table rather than stat(): what a flash writes is what
    # the table describes, and the attested floor is a promise about that.
    written = (max(item.last_lba for item in partitions) + 1) * SECTOR_SIZE
    findings.append(
        Finding(
            "image_fits_minimum_media",
            PASS if written <= media_sizing.MINIMUM_MEDIA_BYTES else FAIL,
            f"{written} bytes written; the release attests a "
            f"{media_sizing.MINIMUM_MEDIA_BYTES} byte floor",
        )
    )

    if contents:
        findings.extend(
            inspect_contents(
                image_path,
                appliance_version=appliance_version,
                build_id=build_id,
                architecture=architecture,
                profile=profile,
            )
        )
    return findings


def summarise(findings):
    """The verdict, derived from the mandatory findings only.

    A failure outranks everything: an optional check that ran and disagreed has
    proven something wrong. Below that, a mandatory check that never executed
    makes the inspection incomplete rather than passing — "the image is good"
    and "nobody looked" were the same answer before, which is how an inspection
    with a skipped oracle reached a release gate as PASS.
    """

    counts = {PASS: 0, FAIL: 0, NOT_RUN: 0}
    mandatory = {PASS: 0, FAIL: 0, NOT_RUN: 0}
    for finding in findings:
        counts[finding.result] = counts.get(finding.result, 0) + 1
        if finding.mandatory:
            mandatory[finding.result] = mandatory.get(finding.result, 0) + 1

    skipped = [
        finding.check
        for finding in findings
        if finding.mandatory and finding.result == NOT_RUN
    ]
    if counts[FAIL]:
        result = FAIL
    elif skipped or not mandatory[PASS]:
        result = NOT_RUN
    else:
        result = PASS

    return {
        "result": result,
        "counts": counts,
        "mandatory": mandatory,
        "mandatory_not_run": skipped,
        "optional": sum(1 for finding in findings if not finding.mandatory),
        "findings": [finding.to_dict() for finding in findings],
    }
