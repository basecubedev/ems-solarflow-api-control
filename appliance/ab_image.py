# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading a built A/B image, and checking it against the image-rota contract.

The layout itself is not described here. ``image-rota`` owns the partition
table, and this module only states what the runtime depends on: the GPT labels
``rpi-ab-slot-mapper`` maps slots by, the filesystem types, and the fact that
each partition carries its own generated identity.

The inspector reads the GPT out of an image **file**. It does not attach a loop
device, does not mount anything and needs no privileges, so it can run in CI and
on a developer machine.

Content is read the same way, through ``ab_filesystems``. Mounting is not an
option a release gate can rely on: the Pi 5 root filesystem uses 16 KiB ext4
blocks, which no 4 KiB-page host kernel will mount, and mounting needs root and
a loop device besides. A check that cannot run is not a check that passed, so
the questions that decide whether an image is an appliance — the package
version in *both* roots, the enabled units, the shared-path activations, the
absence of a shipped host key — are answered out of the filesystem structures
directly.
"""

import hashlib
import json
import struct
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path

from appliance import ab_filesystems, ab_persistence, backup_ownership, image_variants

SECTOR_SIZE = 512
GPT_SIGNATURE = b"EFI PART"
GPT_HEADER_LBA = 1

FAT_SIGNATURE_OFFSET = 0x1FE
FAT_SIGNATURE = b"\x55\xaa"
EXT_SUPERBLOCK_OFFSET = 0x400
EXT_MAGIC_OFFSET = 0x38
EXT_MAGIC = b"\x53\xef"

TYPE_ESP_FAT = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
TYPE_LINUX = "0fc63daf-8483-4772-8e79-3d69d8477de4"

# What image-rota produces, in order. The labels are the contract: upstream's
# slot mapper derives the active slot from the boot partition's PARTLABEL.
EXPECTED_PARTITIONS = (
    ("bootconfig", "vfat"),
    ("boot_a", "vfat"),
    ("boot_b", "vfat"),
    ("system_a", "ext"),
    ("system_b", "ext"),
    ("persistent", "ext"),
)

SLOT_LABELS = {"A": ("boot_a", "system_a"), "B": ("boot_b", "system_b")}

# What image-rpios writes: an MBR with a FAT boot partition and one ext4 root.
SINGLE_SLOT_PARTITIONS = (
    ("boot", "vfat"),
    ("root", "ext"),
)


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


def _guid(raw):
    """A GPT mixed-endian GUID as its canonical lowercase text form."""

    first, second, third = struct.unpack("<IHH", raw[:8])
    return str(
        uuid.UUID(fields=(first, second, third, raw[8], raw[9], int.from_bytes(raw[10:16], "big")))
    )


def read_partitions(path):
    """The GPT partition entries of an image file, without mounting anything."""

    target = Path(path)
    try:
        with target.open("rb") as handle:
            handle.seek(GPT_HEADER_LBA * SECTOR_SIZE)
            header = handle.read(SECTOR_SIZE)
            if header[:8] != GPT_SIGNATURE:
                raise ImageError("image_not_gpt", f"{target} does not carry a GPT partition table")
            entries_lba, entry_count, entry_size = struct.unpack("<QII", header[72:88])
            handle.seek(entries_lba * SECTOR_SIZE)
            raw = handle.read(entry_count * entry_size)
    except OSError as exc:
        raise ImageError("image_unreadable", f"{target} could not be read: {exc}")

    partitions = []
    for index in range(entry_count):
        entry = raw[index * entry_size : (index + 1) * entry_size]
        if len(entry) < 128 or entry[:16] == b"\x00" * 16:
            continue
        first_lba, last_lba = struct.unpack("<QQ", entry[32:48])
        label = entry[56:128].decode("utf-16-le", "ignore").rstrip("\x00")
        partitions.append(
            ImagePartition(
                number=index + 1,
                partuuid=_guid(entry[16:32]),
                type_guid=_guid(entry[:16]),
                label=label,
                first_lba=first_lba,
                last_lba=last_lba,
            )
        )
    return partitions


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
    """The MBR partition entries of an image file, without mounting anything.

    The single-slot image is not a GPT image, so ``read_partitions`` above
    would refuse it outright. Same shape out, so everything downstream reads
    one kind of partition.
    """

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


@dataclass(frozen=True)
class GptHeader:
    location: str
    valid: bool
    detail: str
    current_lba: int = 0
    backup_lba: int = 0
    first_usable_lba: int = 0
    last_usable_lba: int = 0
    entries_lba: int = 0
    entry_count: int = 0
    entry_size: int = 0
    entries_crc: int = 0
    disk_guid: str = ""


def _read_header(handle, lba, image_size, location):
    handle.seek(lba * SECTOR_SIZE)
    raw = handle.read(SECTOR_SIZE)
    if len(raw) < 92 or raw[:8] != GPT_SIGNATURE:
        return GptHeader(location, False, f"there is no GPT header at LBA {lba}")
    header_size = struct.unpack_from("<I", raw, 12)[0]
    if not 92 <= header_size <= SECTOR_SIZE:
        return GptHeader(location, False, f"the header declares {header_size} bytes")
    stored_crc = struct.unpack_from("<I", raw, 16)[0]
    computed = zlib.crc32(raw[:16] + b"\x00\x00\x00\x00" + raw[20:header_size]) & 0xFFFFFFFF
    if stored_crc != computed:
        return GptHeader(location, False, "the header CRC does not match the header")
    current_lba, backup_lba, first_usable, last_usable = struct.unpack_from("<QQQQ", raw, 24)
    disk_guid = _guid(raw[56:72])
    entries_lba, entry_count, entry_size, entries_crc = struct.unpack_from("<QIII", raw, 72)
    if last_usable * SECTOR_SIZE > image_size:
        return GptHeader(
            location, False, f"the last usable LBA {last_usable} is past the end of the image"
        )
    return GptHeader(
        location,
        True,
        f"disk {disk_guid}",
        current_lba=current_lba,
        backup_lba=backup_lba,
        first_usable_lba=first_usable,
        last_usable_lba=last_usable,
        entries_lba=entries_lba,
        entry_count=entry_count,
        entry_size=entry_size,
        entries_crc=entries_crc,
        disk_guid=disk_guid,
    )


def verify_gpt(image_path):
    """Both GPT headers, both entry arrays, and the ranges they describe.

    An invalid backup GPT is not cosmetic: it is what firmware and every repair
    tool fall back to when the primary is damaged, and a medium whose fallback
    is wrong will come back as a different disk.
    """

    findings = []
    target = Path(image_path)
    try:
        image_size = target.stat().st_size
        with target.open("rb") as handle:
            primary = _read_header(handle, GPT_HEADER_LBA, image_size, "primary")
            findings.append(
                Finding("gpt_primary_header", PASS if primary.valid else FAIL, primary.detail)
            )
            backup_lba = primary.backup_lba if primary.valid else image_size // SECTOR_SIZE - 1
            backup = _read_header(handle, backup_lba, image_size, "backup")
            findings.append(
                Finding("gpt_backup_header", PASS if backup.valid else FAIL, backup.detail)
            )

            for header in (primary, backup):
                if not header.valid:
                    findings.append(
                        Finding(
                            f"gpt_{header.location}_entries",
                            NOT_RUN,
                            "the header it belongs to is not valid",
                        )
                    )
                    continue
                handle.seek(header.entries_lba * SECTOR_SIZE)
                raw = handle.read(header.entry_count * header.entry_size)
                observed = zlib.crc32(raw) & 0xFFFFFFFF
                findings.append(
                    Finding(
                        f"gpt_{header.location}_entries",
                        PASS if observed == header.entries_crc else FAIL,
                        f"{header.entry_count} entries of {header.entry_size} bytes"
                        if observed == header.entries_crc
                        else "the partition entry array does not match its CRC",
                    )
                )

            if primary.valid and backup.valid:
                agreed = (
                    primary.disk_guid == backup.disk_guid
                    and primary.entries_crc == backup.entries_crc
                    and primary.first_usable_lba == backup.first_usable_lba
                    and primary.last_usable_lba == backup.last_usable_lba
                    and primary.current_lba == backup.backup_lba
                    and primary.backup_lba == backup.current_lba
                )
                findings.append(
                    Finding(
                        "gpt_headers_agree",
                        PASS if agreed else FAIL,
                        "primary and backup describe one disk"
                        if agreed
                        else "the two GPT headers describe different disks",
                    )
                )
            else:
                findings.append(
                    Finding("gpt_headers_agree", NOT_RUN, "one of the two headers is not valid")
                )
    except OSError as exc:
        return [Finding("gpt_primary_header", FAIL, f"{target} could not be read: {exc}")]

    if not primary.valid:
        return findings

    partitions = sorted(read_partitions(image_path), key=lambda item: item.first_lba)
    problems = []
    for partition in partitions:
        if partition.first_lba < primary.first_usable_lba:
            problems.append(f"{partition.label} starts before the first usable LBA")
        if partition.last_lba > primary.last_usable_lba:
            problems.append(f"{partition.label} ends after the last usable LBA")
        if partition.last_lba < partition.first_lba:
            problems.append(f"{partition.label} ends before it starts")
        if partition.last_lba * SECTOR_SIZE + SECTOR_SIZE > image_size:
            problems.append(f"{partition.label} ends past the end of the image")
    findings.append(
        Finding(
            "gpt_partition_ranges",
            FAIL if problems else PASS,
            "; ".join(problems) if problems else f"{len(partitions)} partitions inside the disk",
        )
    )

    overlaps = [
        f"{first.label} and {second.label}"
        for first, second in zip(partitions, partitions[1:])
        if second.first_lba <= first.last_lba
    ]
    findings.append(
        Finding(
            "gpt_no_overlap",
            FAIL if overlaps else PASS,
            "; ".join(overlaps) if overlaps else "no partition overlaps another",
        )
    )
    return findings


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


def identities(path):
    """Every partition identity in one image, for comparing two builds."""

    return {partition.partuuid for partition in read_partitions(path)}


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

# Reported rather than omitted, so a partition-table check is never read as
# image validation. ``inspect_mounted`` answers these from mounted slot roots.
UNMOUNTED_CHECKS = (
    "slot_cmdline_selects_the_active_slot",
    "package_present_in_both_roots",
    "slot_shared_configuration_present",
    "health_service_enabled",
    "slot_bootstrap_service_enabled",
)

APPLIANCE_BINARY = "usr/bin/ems-appliance"
ENABLED_UNITS = {
    "health_service_enabled": "ems-appliance-ab-health.service",
    "slot_bootstrap_service_enabled": "ems-appliance-slot-bootstrap.service",
}
WANTS_DIRECTORY = "etc/systemd/system/multi-user.target.wants"
SHARED_ACTIVATION_DIRECTORY = "etc/systemd/system/local-fs.target.wants"


def _mount_unit_name(path):
    """The .mount unit systemd generates for a path, by its own escaping rules.

    Derived rather than written out. The list used to be typed by hand and was
    one short: /var/lib/ems-backup was missing, so an image that never
    activated the backup bind passed inspection, and every backup it wrote
    would have been lost at the next slot switch.
    """

    escaped = []
    for index, character in enumerate(path.strip("/")):
        if character == "/":
            escaped.append("-")
        elif (character.isascii() and character.isalnum()) or character == "_" or (
            character == "." and index
        ):
            escaped.append(character)
        else:
            escaped.append(f"\\x{ord(character):02x}")
    return "".join(escaped) + ".mount"


SHARED_ACTIVATIONS = tuple(
    sorted(_mount_unit_name(shared.target) for shared in ab_persistence.SHARED_PATHS)
)
SLOT_ROOT_DEVICE = image_variants.variant(image_variants.VARIANT_AB).root_device


@dataclass(frozen=True)
class RootExpectations:
    """What a root filesystem of one image variant has to contain.

    The checks an image is subjected to are not a matter of taste: an A/B slot
    root that is writable is broken, and a single-slot root that is not is
    unpatchable. Reporting the inapplicable half as passing would let an
    inspection of the wrong kind of image read as a clean one, so a check that
    does not apply is reported NOT RUN and counts as not having run.
    """

    variant: str
    root_device: str
    fstab_readonly: bool
    layout_descriptor: bool
    shared_persistence: bool
    slot_generators: bool
    machine_id_policy: bool
    service_drop_ins: bool
    units: dict


# --- what the image has to contain ------------------------------------------

PACKAGE_NAME = "ems-appliance-manager"
DPKG_STATUS = "var/lib/dpkg/status"
BUILD_MARKER = "etc/ems-appliance-os-build"
LAYOUT_DESCRIPTOR = "etc/ems-appliance-manager/ab-layout.json"
SLOT_SHARED_CONF = "etc/rpi-image-gen/slot-shared.d/50-ems-appliance.conf"
UNIT_DIRECTORY = "usr/lib/systemd/system"
SYSTEM_ROOTS = ("system_a", "system_b")
BOOT_PARTITIONS = ("boot_a", "boot_b")

ROOT_UNITS = {
    "health_service_enabled": "ems-appliance-ab-health.service",
    "slot_bootstrap_service_enabled": "ems-appliance-slot-bootstrap.service",
    "persistence_service_enabled": "ems-appliance-persistence.service",
    "host_identity_service_enabled": "ems-appliance-host-identity.service",
}

# The units a single-slot root must carry enabled. It has no slot to bootstrap,
# no trial to judge and no persistence to prove, so what is left is the pair
# that is the appliance.
SINGLE_SLOT_UNITS = {
    "agent_service_enabled": "ems-appliance-agent.service",
    "web_service_enabled": "ems-appliance-web.service",
}

ROOT_EXPECTATIONS = {
    image_variants.VARIANT_AB: RootExpectations(
        variant=image_variants.VARIANT_AB,
        root_device=image_variants.variant(image_variants.VARIANT_AB).root_device,
        fstab_readonly=True,
        layout_descriptor=True,
        shared_persistence=True,
        slot_generators=True,
        machine_id_policy=True,
        service_drop_ins=True,
        units=ROOT_UNITS,
    ),
    image_variants.VARIANT_SINGLE: RootExpectations(
        variant=image_variants.VARIANT_SINGLE,
        root_device=image_variants.variant(image_variants.VARIANT_SINGLE).root_device,
        fstab_readonly=False,
        layout_descriptor=False,
        shared_persistence=False,
        slot_generators=False,
        machine_id_policy=False,
        service_drop_ins=False,
        units=SINGLE_SLOT_UNITS,
    ),
}


# Upstream's own generators. Without them the shared binds and the per-slot
# /var policy are never generated, and every write on the appliance is lost at
# the next slot switch.
SLOT_GENERATORS = (
    "usr/lib/systemd/system-generators/slot-shared-generator",
    "usr/lib/systemd/system-generators/slot-perst-generator",
)
MACHINE_ID_UNIT = "machine-id-sync.service"

# Two services must not start against a slot-local fallback: sshd would offer
# an identity nobody can vouch for, and NetworkManager would come up with no
# profiles and write new ones somewhere the next slot switch discards.
SERVICE_DROP_INS = {
    "etc/systemd/system/ssh.service.d/50-ems-appliance-host-identity.conf": (
        "ems-appliance-host-identity.service"
    ),
    "etc/systemd/system/NetworkManager.service.d/50-ems-appliance-persistence.conf": (
        "ems-appliance-persistence.service"
    ),
}

RUNTIME_HELPERS = (
    "usr/bin/ems-appliance",
    "usr/lib/ems-appliance-manager/setup-export-root.sh",
    "usr/lib/ems-appliance-manager/backup-account.sh",
    # Written by the postinst, not shipped by dpkg: without it a flashed image
    # cannot establish ownership of the account it carries.
    f"usr/lib/ems-appliance-manager/{backup_ownership.ACCOUNT_ORIGIN_NAME}",
)

# A board that never reaches the network cannot be asked anything, and the image
# ships no login account. The serial line is the only channel a first boot has,
# and it is where the A/B root resolution reports its own refusal.
SERIAL_CONSOLES = ("serial", "ttyAMA", "ttyS", "ttyUSB")
FIRMWARE_UART_SETTINGS = ("enable_uart=1", "BOOT_UART=1", "uart_2ndstage=1")

BOOT_KERNELS = ("kernel8.img", "kernel_2712.img", "kernel.img")
BOOT_INITRAMFS = ("initramfs8", "initramfs_2712", "initramfs")

AUTOBOOT_FILE = "autoboot.txt"
TRYBOOT_SETTING = "tryboot_a_b=1"


PACKAGE_INSTALLED_STATUS = "install ok installed"
FROM_DPKG = "the dpkg database in the slot root"
FROM_BUILD_MARKER = "the build marker"


def _package_record(reader):
    """The dpkg status stanza for the Appliance Manager, if it is installed."""

    return _dpkg_records(reader)[0]


def _dpkg_records(reader):
    """``(our stanza, database present)`` from ``/var/lib/dpkg/status``.

    The two are separate answers. image-rota binds ``/var`` per slot, so a slot
    root legitimately carries no database at all; a database that *is* there and
    does not name this package is a different fact entirely.
    """

    try:
        status = reader.read_text(DPKG_STATUS)
    except ab_filesystems.FilesystemError:
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
    """The exact package record this slot root carries, and where it came from.

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

# Every directory that has to exist before anything is mounted onto it. Derived
# from the persistence contract rather than repeated, so a new shared path
# cannot be declared without its mount point being required too.
MOUNT_POINTS = (
    ab_persistence.PERSISTENT_MOUNTPOINT,
    *(shared.target for shared in ab_persistence.SHARED_PATHS),
)


def _fstab_root_mode(reader, *, readonly):
    """``(ok, detail)`` for the mount options the root declares for ``/``.

    Both directions are a real verdict, not a preference. An A/B slot root that
    is writable loses every write at the next slot switch; a single-slot root
    that is read-only cannot be patched by apt, which is the only way that
    variant is patched at all.
    """

    required, forbidden = ("ro", "rw") if readonly else ("rw", "ro")
    try:
        fstab = reader.read_text(FSTAB)
    except ab_filesystems.FilesystemError:
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


def _root_content_findings(label, reader, *, appliance_version, build_id, architecture,
                           expectations=None):
    expectations = expectations or ROOT_EXPECTATIONS[image_variants.VARIANT_AB]
    findings = []

    def record(check, ok, detail):
        findings.append(Finding(f"{check}:{label}", PASS if ok else FAIL, detail))

    def skip(check, detail):
        """A check this variant has nothing to answer with.

        Never PASS: "the image is good" and "nobody looked" reaching a release
        gate as the same answer is exactly what summarise() exists to stop. But
        not mandatory either, because a check that cannot apply to this image
        is not a missing oracle -- it is a question about a mechanism this
        variant does not have, and the detail says which. A mandatory NOT RUN
        would make every single-slot inspection incomplete by construction, and
        an inspection that can never pass is one nobody reads.
        """

        findings.append(Finding(f"{check}:{label}", NOT_RUN, detail, mandatory=False))

    record(
        "package_installed",
        reader.is_file(RUNTIME_HELPERS[0]),
        RUNTIME_HELPERS[0],
    )

    marker = {}
    try:
        marker = json.loads(reader.read_text(BUILD_MARKER))
    except (ab_filesystems.FilesystemError, ValueError):
        marker = {}

    # image-rota binds /var per slot, so the slot root carries no dpkg
    # database: it is created on the persistent partition at first boot. The
    # record the image actually holds is therefore proven from the build marker,
    # which the build captured from dpkg itself inside the chroot, and from a
    # dpkg database directly where the slot root still carries one.
    evidence, source = _package_evidence(reader, marker)
    if evidence is None:
        for check in ("package_name", "package_status", "package_version",
                      "package_architecture"):
            record(check, False, "the slot root carries neither a dpkg database nor a "
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
        record("build_marker", False, "the slot root carries no build marker")
    elif build_id and str(marker.get("build_id") or "") != build_id:
        record(
            "build_marker",
            False,
            f"the slot root reports build {marker.get('build_id') or 'none'}, "
            f"the release is {build_id}",
        )
    else:
        record("build_marker", True, str(marker.get("build_id") or "present"))

    present = reader.is_file(LAYOUT_DESCRIPTOR)
    if expectations.layout_descriptor:
        record("layout_descriptor", present, LAYOUT_DESCRIPTOR)
    else:
        # Not "not applicable": a single-slot image carrying this would tell the
        # runtime it has slots, and every A/B path would then act on a host that
        # has none. Its absence is the mechanism, so its presence is a failure.
        record(
            "layout_descriptor_absent",
            not present,
            f"{LAYOUT_DESCRIPTOR} would claim slots this image does not have"
            if present
            else "no layout descriptor, as a single-slot image requires",
        )

    # Where the mount mode is actually enforced. The kernel command line decides
    # the initial mount; systemd-remount-fs then applies this line, so a root
    # whose fstab disagrees is that mode however it booted.
    record(
        "root_fstab_readonly" if expectations.fstab_readonly else "root_fstab_writable",
        *_fstab_root_mode(reader, readonly=expectations.fstab_readonly),
    )

    # And what a read-only root then requires: every mount point already in the
    # image. systemd creates a missing one only where it can write, so a shared
    # path with no directory here is a bind that never happens on hardware.
    if expectations.shared_persistence:
        absent = [path for path in MOUNT_POINTS if not reader.is_dir(path.lstrip("/"))]
        record(
            "mount_points_present",
            not absent,
            f"no directory for: {', '.join(absent)}"
            if absent
            else f"{len(MOUNT_POINTS)} mount points present",
        )
    else:
        skip("mount_points_present", "a writable root creates its own directories")

    if expectations.shared_persistence:
        try:
            declared = {
                line.split("=", 1)[1].strip()
                for line in reader.read_text(SLOT_SHARED_CONF).splitlines()
                if line.startswith("Path=")
            }
        except ab_filesystems.FilesystemError:
            declared = set()
        record(
            "persistence_configuration",
            len(declared) >= len(SHARED_ACTIVATIONS),
            f"{len(declared)} shared paths declared"
            if declared
            else "the slot root declares no shared paths and would lose every write",
        )

        absent = [
            activation
            for activation in SHARED_ACTIVATIONS
            if not reader.is_symlink(f"{SHARED_ACTIVATION_DIRECTORY}/{activation}")
        ]
        record(
            "shared_activations",
            not absent,
            f"not activated: {', '.join(absent)}"
            if absent
            else f"{len(SHARED_ACTIVATIONS)} shared paths activated",
        )
    else:
        reason = "there is no second slot for a write to be lost to"
        skip("persistence_configuration", reason)
        skip("shared_activations", reason)

    for check, unit in expectations.units.items():
        present = reader.is_file(f"{UNIT_DIRECTORY}/{unit}")
        enabled = reader.is_symlink(f"{WANTS_DIRECTORY}/{unit}")
        record(
            check,
            present and enabled,
            "installed and enabled"
            if present and enabled
            else ("installed but not enabled" if present else "not installed"),
        )

    if expectations.slot_generators:
        missing_generators = [name for name in SLOT_GENERATORS if not reader.exists(name)]
        record(
            "slot_generators",
            not missing_generators,
            f"missing: {', '.join(missing_generators)}"
            if missing_generators
            else "shared and per-slot generators present",
        )
    else:
        skip("slot_generators", "image-rpios generates no slot binds")

    # Upstream ships it as a unit in /etc; a vendor unit in /usr/lib is the
    # other legitimate place for it.
    if expectations.machine_id_policy:
        machine_id = [
            directory
            for directory in (UNIT_DIRECTORY, "etc/systemd/system")
            if reader.exists(f"{directory}/{MACHINE_ID_UNIT}")
        ]
        record(
            "machine_id_policy",
            bool(machine_id),
            f"{MACHINE_ID_UNIT} in {', '.join(machine_id)}" if machine_id else MACHINE_ID_UNIT,
        )
    else:
        skip("machine_id_policy", "one root keeps its own machine id")

    keys = [name for name in reader.listdir("etc/ssh") if name.startswith("ssh_host_")]
    private = [name for name in keys if not name.endswith(".pub")]
    record(
        "no_host_key_shipped",
        not private,
        f"the image carries {', '.join(private)}" if private else "no host key is shipped",
    )

    if expectations.service_drop_ins:
        unordered = []
        for path, required_unit in SERVICE_DROP_INS.items():
            try:
                text = reader.read_text(path)
            except ab_filesystems.FilesystemError:
                unordered.append(f"{path} is missing")
                continue
            if f"Requires={required_unit}" not in text or f"After={required_unit}" not in text:
                unordered.append(f"{path} does not require {required_unit}")
        record(
            "service_drop_ins",
            not unordered,
            "; ".join(unordered) if unordered else f"{len(SERVICE_DROP_INS)} drop-ins ordered",
        )
    else:
        # Both drop-ins order a service behind a unit that only exists to make an
        # A/B slot switch non-destructive. Shipping them here would order sshd
        # and NetworkManager behind units that never run.
        skip("service_drop_ins", "neither drop-in guards a mechanism this image has")

    # Checked either way: these are the appliance, not the layout.
    missing_helpers = [name for name in RUNTIME_HELPERS if not reader.is_file(name)]
    record(
        "runtime_helpers",
        not missing_helpers,
        f"missing: {', '.join(missing_helpers)}"
        if missing_helpers
        else f"{len(RUNTIME_HELPERS)} helpers present",
    )
    return findings


def _boot_content_findings(label, reader, *, expectations=None):
    expectations = expectations or ROOT_EXPECTATIONS[image_variants.VARIANT_AB]
    findings = []

    def record(check, ok, detail):
        findings.append(Finding(f"{check}:{label}", PASS if ok else FAIL, detail))

    try:
        cmdline = reader.read_text("cmdline.txt").strip()
    except ab_filesystems.FilesystemError:
        record("boot_cmdline", False, "there is no cmdline.txt")
        cmdline = ""
    if cmdline:
        # Both variants mount their root through a by-slot name, but not the
        # same one: the A/B image is pointed at whichever slot is active, the
        # single-slot image at the only one there is.
        device = f"root={expectations.root_device}"
        record(
            "boot_cmdline",
            device in cmdline,
            device if device in cmdline else f"root is {cmdline[:120]}",
        )
        # A read-only root is what makes an A/B slot reproducible: everything
        # that has to survive is a shared bind, and everything else is
        # discarded at the next boot. A single-slot root is the opposite, and
        # for the same kind of reason: apt is how it is patched at all. The
        # command line decides the *initial* mount; /etc/fstab in the root
        # decides what it stays, and that is checked in the root findings.
        fields = cmdline.split()
        readonly = "ro" in fields
        writable = "rw" in fields
        if expectations.fstab_readonly:
            record(
                "boot_readonly_root",
                readonly and not writable,
                "rw overrides it"
                if writable
                else ("ro" if readonly else "the root is not read-only"),
            )
        else:
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
    record("boot_configuration", "config.txt" in entries, "config.txt")
    try:
        configuration = reader.read_text("config.txt").replace(" ", "")
    except ab_filesystems.FilesystemError:
        configuration = ""
    asked = [setting for setting in FIRMWARE_UART_SETTINGS if setting in configuration]
    record(
        "boot_firmware_uart",
        bool(asked),
        ", ".join(asked) or "the firmware is not asked to use the serial line",
    )
    return findings


def _bootconfig_findings(reader):
    findings = []
    try:
        autoboot = reader.read_text(AUTOBOOT_FILE)
    except ab_filesystems.FilesystemError:
        return [
            Finding("bootconfig_autoboot", FAIL, f"the bootconfig partition has no {AUTOBOOT_FILE}")
        ]
    text = autoboot.replace(" ", "").lower()
    findings.append(
        Finding(
            "bootconfig_autoboot",
            PASS if TRYBOOT_SETTING in text else FAIL,
            TRYBOOT_SETTING if TRYBOOT_SETTING in text else autoboot.strip()[:120],
        )
    )
    # The selector is these two sections: what the firmware boots normally, and
    # what it boots once when a tryboot switch is armed. A missing [tryboot]
    # section is an image that can never roll back.
    sections = {line.strip().lower() for line in autoboot.splitlines()}
    findings.append(
        Finding(
            "bootconfig_sections",
            PASS if {"[all]", "[tryboot]"} <= sections else FAIL,
            ", ".join(sorted(sections & {"[all]", "[tryboot]"})) or "no boot sections",
        )
    )
    return findings


# What each variant's image is made of, and in which order it is inspected.
VARIANT_PARTITIONS = {
    image_variants.VARIANT_AB: ("bootconfig", *BOOT_PARTITIONS, *SYSTEM_ROOTS),
    image_variants.VARIANT_SINGLE: ("boot", "root"),
}
VARIANT_ROOT_LABELS = {
    image_variants.VARIANT_AB: frozenset(SYSTEM_ROOTS),
    image_variants.VARIANT_SINGLE: frozenset({"root"}),
}
VARIANT_BOOT_LABELS = {
    image_variants.VARIANT_AB: frozenset(BOOT_PARTITIONS),
    image_variants.VARIANT_SINGLE: frozenset({"boot"}),
}


def inspect_contents(image_path, *, variant=image_variants.VARIANT_AB, appliance_version="",
                     build_id="", architecture=""):
    """What the image actually contains, read without mounting anything.

    For the A/B image: both slot roots and both boot partitions, because an
    update writes the *other* slot, and content present in only one of them is
    an appliance that stops being an appliance at the first slot switch. For
    the single-slot image there is one of each, and the expectations of what
    they must contain differ with it.
    """

    expectations = ROOT_EXPECTATIONS[variant]
    reader_for_table = (
        read_partitions if variant == image_variants.VARIANT_AB else read_mbr_partitions
    )
    try:
        partitions = {item.label: item for item in reader_for_table(image_path)}
    except ImageError as exc:
        return [Finding("image_contents", FAIL, exc.message)]

    findings = []
    for label in VARIANT_PARTITIONS[variant]:
        partition = partitions.get(label)
        if partition is None:
            findings.append(Finding(f"partition_present:{label}", FAIL, "the image has no such partition"))
            continue
        try:
            reader = ab_filesystems.open_partition(image_path, partition)
        except ab_filesystems.FilesystemError as exc:
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
        if label == "bootconfig":
            findings.extend(_bootconfig_findings(reader))
        elif label in VARIANT_BOOT_LABELS[variant]:
            findings.extend(_boot_content_findings(label, reader, expectations=expectations))
        else:
            findings.extend(
                _root_content_findings(
                    label,
                    reader,
                    appliance_version=appliance_version,
                    build_id=build_id,
                    architecture=architecture,
                    expectations=expectations,
                )
            )
    return findings


def inspect_mounted(*, system_a, system_b, boot=None):
    """Check what only a mounted slot root can answer.

    Both roots are checked, not just the one that happens to boot first: an
    update writes the *other* slot, so a package or an enablement present in
    only one of them is an appliance that stops being an appliance at the
    first slot switch.
    """

    roots = {"system_a": Path(system_a), "system_b": Path(system_b)}
    findings = []

    missing = [name for name, root in roots.items() if not (root / APPLIANCE_BINARY).is_file()]
    findings.append(
        Finding("package_present_in_both_roots", FAIL, f"no {APPLIANCE_BINARY} in {', '.join(missing)}")
        if missing
        else Finding("package_present_in_both_roots", PASS, "both slot roots carry the package")
    )

    absent = sorted(
        f"{name}:{activation}"
        for name, root in roots.items()
        for activation in SHARED_ACTIVATIONS
        if not (root / SHARED_ACTIVATION_DIRECTORY / activation).is_symlink()
    )
    findings.append(
        Finding("slot_shared_configuration_present", FAIL, f"not activated: {', '.join(absent)}")
        if absent
        else Finding(
            "slot_shared_configuration_present",
            PASS,
            f"{len(SHARED_ACTIVATIONS)} shared paths activated in both roots",
        )
    )

    for check, unit in ENABLED_UNITS.items():
        unenabled = [
            name
            for name, root in roots.items()
            if not (root / WANTS_DIRECTORY / unit).is_symlink()
        ]
        findings.append(
            Finding(check, FAIL, f"{unit} is not enabled in {', '.join(unenabled)}")
            if unenabled
            else Finding(check, PASS, f"{unit} enabled in both roots")
        )

    shipped = sorted(
        f"{name}:{key.name}"
        for name, root in roots.items()
        for key in sorted((root / "etc/ssh").glob("ssh_host_*_key"))
        if key.is_file()
    )
    findings.append(
        Finding("no_host_key_shipped", FAIL, f"the image carries {', '.join(shipped)}")
        if shipped
        else Finding("no_host_key_shipped", PASS, "neither slot root carries a host key")
    )

    findings.append(_cmdline_finding(boot))
    return findings


def _cmdline_finding(boot):
    check = "slot_cmdline_selects_the_active_slot"
    if boot is None:
        return Finding(check, NOT_RUN, "no boot partition was mounted")
    cmdline = Path(boot) / "cmdline.txt"
    if not cmdline.is_file():
        return Finding(check, FAIL, f"{cmdline} is missing")
    text = cmdline.read_text(encoding="utf-8", errors="replace").strip()
    if SLOT_ROOT_DEVICE not in text:
        # A fixed root= boots whichever partition it names, so the selector
        # would switch slots while the kernel kept mounting the old one.
        return Finding(check, FAIL, f"root is not the active slot: {text[:120]}")
    return Finding(check, PASS, SLOT_ROOT_DEVICE)


def inspect(
    image_path,
    *,
    variant=image_variants.VARIANT_AB,
    expected=None,
    appliance_version="",
    build_id="",
    architecture="",
    contents=True,
):
    """Check a built image against its variant's contract, without booting it.

    The two variants do not merely expect different partitions, they expect a
    different kind of table: image-rota owns a six-partition GPT, image-rpios an
    MBR with two. So the checks that are about the GPT -- the independent sgdisk
    oracle, the header pair, the slot and boot pairings, the PARTUUID
    uniqueness -- have nothing to read on a single-slot image. They are reported
    as not having run, with the reason, and never as passing.
    """

    is_ab = variant == image_variants.VARIANT_AB
    if expected is None:
        expected = EXPECTED_PARTITIONS if is_ab else SINGLE_SLOT_PARTITIONS
    table = "GPT" if is_ab else "MBR"

    findings = []
    try:
        partitions = (read_partitions if is_ab else read_mbr_partitions)(image_path)
    except ImageError as exc:
        return [Finding("partition_table", FAIL, exc.message)]

    findings.append(Finding("partition_table", PASS, f"{len(partitions)} {table} partitions"))
    if len(partitions) != len(expected):
        findings.append(
            Finding(
                "partition_count",
                FAIL,
                f"{image_variants.variant(variant).image_layer} produces {len(expected)} "
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
        if is_ab:
            wanted_type = TYPE_ESP_FAT if fstype == "vfat" else TYPE_LINUX
            if partition.type_guid.lower() != wanted_type:
                findings.append(
                    Finding(f"type:{label}", FAIL, f"image has type {partition.type_guid}")
                )
            else:
                findings.append(Finding(f"type:{label}", PASS, partition.type_guid))

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

    if is_ab:
        observed = [partition.partuuid for partition in partitions]
        if len(set(observed)) != len(observed):
            findings.append(
                Finding("partition_identity", FAIL, "two partitions claim the same PARTUUID")
            )
        else:
            findings.append(
                Finding("partition_identity", PASS, f"{len(observed)} distinct PARTUUIDs")
            )

        findings.append(_slot_pairing_finding(image_path, partitions))
        findings.append(_boot_pairing_finding(image_path, partitions))
        findings.extend(verify_gpt(image_path))
    else:
        # Reported rather than omitted, so a reader can see that this image was
        # not asked a question it has no answer to -- and cannot mistake a
        # shorter report for a cleaner one.
        reason = "an MBR image carries no GPT and no second slot"
        for check in ("partition_identity", "slot_pairing", "boot_pairing", "gpt_header_pair"):
            findings.append(Finding(check, NOT_RUN, reason, mandatory=False))

    if contents:
        findings.extend(
            inspect_contents(
                image_path,
                variant=variant,
                appliance_version=appliance_version,
                build_id=build_id,
                architecture=architecture,
            )
        )
    else:
        for check in UNMOUNTED_CHECKS:
            findings.append(
                Finding(check, NOT_RUN, "content inspection was not requested")
            )
    return findings


def partition_digest(image_path, partition, *, chunk=4 * 1024 * 1024):
    """The SHA-256 of exactly one partition's payload, GPT metadata excluded."""

    digest = hashlib.sha256()
    remaining = partition.size_bytes
    with Path(image_path).open("rb") as handle:
        handle.seek(partition.offset)
        while remaining > 0:
            block = handle.read(min(chunk, remaining))
            if not block:
                break
            remaining -= len(block)
            digest.update(block)
    if remaining:
        raise ImageError(
            "image_truncated",
            f"{image_path} ends {remaining} bytes before the end of {partition.label}",
        )
    return f"sha256:{digest.hexdigest()}"


def _pairing_finding(image_path, partitions, check, first, second):
    """image-rota writes one bit-for-bit identical slot pair.

    Identical content with distinct partition identities is the property: the
    slots differ only in which one the firmware booted. Equal *size* was the
    check, and a slot whose every byte had been replaced is exactly the same
    size as the slot it replaced.
    """

    by_label = {partition.label: partition for partition in partitions}
    a = by_label.get(first)
    b = by_label.get(second)
    if a is None or b is None:
        return Finding(check, NOT_RUN, f"both {first} and {second} are needed")
    if a.partuuid == b.partuuid:
        return Finding(check, FAIL, "both slots claim one PARTUUID")
    if a.size_bytes != b.size_bytes:
        return Finding(
            check, FAIL, f"{first} is {a.size_bytes} bytes, {second} is {b.size_bytes}"
        )
    try:
        digest_a = partition_digest(image_path, a)
        digest_b = partition_digest(image_path, b)
    except ImageError as exc:
        return Finding(check, FAIL, exc.message)
    if digest_a != digest_b:
        return Finding(
            check,
            FAIL,
            f"{first} hashes to {digest_a[:23]}..., {second} to {digest_b[:23]}...",
        )
    return Finding(check, PASS, f"distinct identities, identical payload {digest_a[:23]}...")


def _slot_pairing_finding(image_path, partitions):
    return _pairing_finding(image_path, partitions, "slot_pairing", "system_a", "system_b")


def _boot_pairing_finding(image_path, partitions):
    return _pairing_finding(image_path, partitions, "boot_pairing", "boot_a", "boot_b")


def compare_identities(first, second):
    """Two independently built images must not share a partition identity."""

    shared = identities(first) & identities(second)
    if shared:
        return Finding(
            "identity_uniqueness",
            FAIL,
            f"{len(shared)} identities are reused between the two images",
        )
    return Finding("identity_uniqueness", PASS, "no identity is reused between builds")


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
