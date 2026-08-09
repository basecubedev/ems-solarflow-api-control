# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading a built A/B image, and checking it against the image-rota contract.

The layout itself is not described here. ``image-rota`` owns the partition
table, and this module only states what the runtime depends on: the GPT labels
``rpi-ab-slot-mapper`` maps slots by, the filesystem types, and the fact that
each partition carries its own generated identity.

The inspector reads the GPT out of an image **file**. It does not attach a loop
device, does not mount anything and needs no privileges, so it can run in CI and
on a developer machine. What it therefore cannot check — that a filesystem
mounts, that a package is installed inside it — is reported as not inspected
rather than as a pass.
"""

import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

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

# What image-rota's genimage template produces, in order. The labels are the
# contract: upstream's slot mapper derives the active slot from the boot
# partition's PARTLABEL, and a missing or duplicated label breaks slot mapping
# outright rather than degrading.
EXPECTED_PARTITIONS = (
    ("bootconfig", "vfat"),
    ("boot_a", "vfat"),
    ("boot_b", "vfat"),
    ("system_a", "ext"),
    ("system_b", "ext"),
    ("persistent", "ext"),
)

SLOT_LABELS = {"A": ("boot_a", "system_a"), "B": ("boot_b", "system_b")}


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
    check: str
    result: str
    detail: str = ""

    def to_dict(self):
        return {"check": self.check, "result": self.result, "detail": self.detail}


PASS = "pass"
FAIL = "fail"
NOT_RUN = "not_run"

# Only a mounted filesystem answers these, and this inspector deliberately does
# not mount. Reporting them keeps a partition-table check from being read as
# image validation; scripts/appliance-inspect-rpi-ab-image.sh runs the mounted
# tier when the host can.
UNMOUNTED_CHECKS = (
    "slot_cmdline_selects_the_active_slot",
    "package_present_in_both_roots",
    "slot_shared_configuration_present",
    "health_service_enabled",
    "slot_bootstrap_service_enabled",
)


def inspect(image_path, *, expected=EXPECTED_PARTITIONS):
    """Check a built image against the image-rota contract, without booting it."""

    findings = []
    try:
        partitions = read_partitions(image_path)
    except ImageError as exc:
        return [Finding("partition_table", FAIL, exc.message)]

    findings.append(Finding("partition_table", PASS, f"{len(partitions)} GPT partitions"))
    if len(partitions) != len(expected):
        findings.append(
            Finding(
                "partition_count",
                FAIL,
                f"image-rota produces {len(expected)} partitions, the image has {len(partitions)}",
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

    observed = [partition.partuuid for partition in partitions]
    if len(set(observed)) != len(observed):
        findings.append(
            Finding("partition_identity", FAIL, "two partitions claim the same PARTUUID")
        )
    else:
        findings.append(Finding("partition_identity", PASS, f"{len(observed)} distinct PARTUUIDs"))

    findings.append(_slot_pairing_finding(image_path, partitions))

    for check in UNMOUNTED_CHECKS:
        findings.append(
            Finding(check, NOT_RUN, "needs a mounted filesystem; run the loop-device inspector")
        )
    return findings


def _slot_pairing_finding(image_path, partitions):
    """image-rota writes one bit-for-bit identical slot pair.

    Identical content with distinct partition identities is the property: the
    slots differ only in which one the firmware booted.
    """

    by_label = {partition.label: partition for partition in partitions}
    try:
        a = by_label["system_a"]
        b = by_label["system_b"]
    except KeyError:
        return Finding("slot_pairing", NOT_RUN, "both system partitions are needed")
    if a.partuuid == b.partuuid:
        return Finding("slot_pairing", FAIL, "both slots claim one PARTUUID")
    if a.size_bytes != b.size_bytes:
        return Finding(
            "slot_pairing", FAIL, f"slot A is {a.size_bytes} bytes, slot B is {b.size_bytes}"
        )
    return Finding("slot_pairing", PASS, "distinct identities, equal size")


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
    counts = {PASS: 0, FAIL: 0, NOT_RUN: 0}
    for finding in findings:
        counts[finding.result] = counts.get(finding.result, 0) + 1
    return {
        "result": FAIL if counts[FAIL] else (PASS if counts[PASS] else NOT_RUN),
        "counts": counts,
        "findings": [finding.to_dict() for finding in findings],
    }
