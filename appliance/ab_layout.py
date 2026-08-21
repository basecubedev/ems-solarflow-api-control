# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only A/B slot and partition discovery.

Nothing here mutates anything. What it produces is the authority every mutating
A/B step is bound to: which slot is running, which slot is inactive, and which
block devices those two slots actually are.

The rule that shapes the module: **no single signal decides a slot.** The
firmware says which partition it booted, the kernel command line says which root
filesystem it was told to use, the mount table says what is mounted, the block
layer says which partition carries which GPT label, upstream's slot mapper
publishes ``/dev/disk/by-slot`` symlinks, and the layout descriptor says what the
image was supposed to look like. They must agree. A disagreement is
``layout_drift``, which disables every A/B mutation — it is never resolved by
picking the signal that would let the update proceed.

Identity is by GPT ``PARTLABEL``, which ``image-rota`` mandates and generates,
never by a PARTUUID this project pinned. Partition numbers and PARTUUIDs are
discovered from the running medium. Because a second appliance medium carries
the same labels, discovery first proves which physical device the firmware
booted and then considers only that device's partitions.
"""

import json
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from appliance.ab_boot import SelectorError, read_selector

LAYOUT_MANIFEST_NAME = "ab-layout.json"
LAYOUT_SCHEMA_VERSION = 2
OS_BUILD_MARKER = "etc/ems-appliance-os-build"

SLOT_A = "A"
SLOT_B = "B"
SLOTS = (SLOT_A, SLOT_B)

MODE_UNSUPPORTED = "unsupported"
MODE_SINGLE_SLOT = "single_slot"
MODE_AB = "ab"

REASON_SUPPORTED = "ab_layout_present"
REASON_NOT_PRESENT = "ab_layout_not_present"
REASON_MANIFEST_UNSUPPORTED = "ab_layout_manifest_unsupported"
REASON_DRIFT = "layout_drift"
REASON_BLOCK_LAYER_UNAVAILABLE = "block_layer_unavailable"

BOOTLOADER_PARTITION = "proc/device-tree/chosen/bootloader/partition"
BOOTLOADER_TRYBOOT = "proc/device-tree/chosen/bootloader/tryboot"
BOOTLOADER_BOOT_MODE = "proc/device-tree/chosen/bootloader/boot-mode"
CMDLINE = "proc/cmdline"
HOST_MOUNTINFO = "proc/1/mountinfo"
MOUNTINFO = "proc/self/mountinfo"

SLOT_LINK_ROLES = ("active/boot", "active/system", "other/boot", "other/system", "persistent")

# The boot modes upstream's slot mapper supports, and the device each names.
BOOT_MODE_DEVICES = {1: "mmcblk0", 6: "nvme0n1"}

LSBLK_COLUMNS = "PATH,TYPE,SIZE,PARTUUID,PARTLABEL,FSTYPE,MOUNTPOINT,PKNAME,PARTN"


class LayoutError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PartitionSpec:
    """One partition as the image build labelled it."""

    label: str
    fstype: str = ""

    def to_dict(self):
        return {"label": self.label, "fstype": self.fstype}


@dataclass(frozen=True)
class SlotSpec:
    slot: str
    boot: PartitionSpec
    root: PartitionSpec

    def to_dict(self):
        return {"slot": self.slot, "boot": self.boot.to_dict(), "root": self.root.to_dict()}


@dataclass(frozen=True)
class LayoutManifest:
    """``/etc/ems-appliance-manager/ab-layout.json``, written at image-build time."""

    schema_version: int
    layout_id: str
    slot_schema_version: int
    persistent_schema_version: int
    bootconfig: PartitionSpec
    persist: PartitionSpec
    slots: dict
    image_layer: str = "image-rota"
    image_layer_version: str = ""
    slot_device_prefix: str = "/dev/disk/by-slot"
    selector_mountpoint: str = "/bootfs"
    persist_mountpoint: str = "/persistent"
    boot_mountpoint: str = "/boot/firmware"
    shared_root: str = "/persistent/shared"
    machine_id_source: str = "/persistent/common/etc/machine-id"

    def slot(self, name):
        try:
            return self.slots[name]
        except KeyError:
            raise LayoutError("unknown_slot", f"{name!r} is not a slot of this layout")

    def partitions(self):
        entries = [self.bootconfig, self.persist]
        for spec in self.slots.values():
            entries.extend((spec.boot, spec.root))
        return tuple(entries)

    def slot_link(self, role):
        return f"{self.slot_device_prefix.rstrip('/')}/{role}"

    @property
    def persistent_alias(self):
        return self.slot_link("persistent")

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "layout_id": self.layout_id,
            "slot_schema_version": self.slot_schema_version,
            "persistent_schema_version": self.persistent_schema_version,
            "image_layer": self.image_layer,
            "image_layer_version": self.image_layer_version,
            "slot_device_prefix": self.slot_device_prefix,
            "bootconfig": self.bootconfig.to_dict(),
            "persist": self.persist.to_dict(),
            "slots": {name: spec.to_dict() for name, spec in sorted(self.slots.items())},
            "selector_mountpoint": self.selector_mountpoint,
            "persist_mountpoint": self.persist_mountpoint,
            "boot_mountpoint": self.boot_mountpoint,
            "shared_root": self.shared_root,
            "machine_id_source": self.machine_id_source,
        }


@dataclass(frozen=True)
class BlockPartition:
    """One partition as the block layer reports it right now."""

    path: str
    partuuid: str
    size_bytes: int
    parent: str
    number: int = 0
    label: str = ""
    fstype: str = ""
    mountpoint: str = ""

    def to_dict(self):
        return {
            "path": self.path,
            "partuuid": self.partuuid,
            "size_bytes": self.size_bytes,
            "parent": self.parent,
            "number": self.number,
            "label": self.label,
            "fstype": self.fstype,
            "mountpoint": self.mountpoint,
        }


@dataclass(frozen=True)
class SlotDevices:
    """A slot bound to the block devices it actually is."""

    slot: str
    boot: BlockPartition
    root: BlockPartition

    @property
    def boot_partition_number(self):
        return self.boot.number

    def to_dict(self):
        return {
            "slot": self.slot,
            "boot": self.boot.to_dict(),
            "root": self.root.to_dict(),
            "boot_partition_number": self.boot_partition_number,
        }


@dataclass(frozen=True)
class LayoutStatus:
    """What A/B discovery concluded, and what it is allowed to authorise."""

    mode: str
    ab_supported: bool
    reason: str
    manifest: LayoutManifest = None
    active_slot: str = ""
    inactive_slot: str = ""
    tryboot: bool = False
    boot_partition: int = 0
    root_partuuid: str = ""
    device: str = ""
    selector: object = None
    slots: dict = field(default_factory=dict)
    selector_device: BlockPartition = None
    persist_device: BlockPartition = None
    drift: tuple = ()
    os_build: dict = field(default_factory=dict)

    @property
    def may_mutate(self):
        return self.ab_supported and not self.drift

    def slot_devices(self, name):
        try:
            return self.slots[name]
        except KeyError:
            raise LayoutError("unknown_slot", f"{name!r} is not a slot of this layout")

    def to_dict(self):
        return {
            "mode": self.mode,
            "ab_supported": self.ab_supported,
            "reason": self.reason,
            "active_slot": self.active_slot,
            "inactive_slot": self.inactive_slot,
            "tryboot": self.tryboot,
            "boot_partition": self.boot_partition,
            "device": self.device,
            "selector": self.selector.to_dict() if self.selector else None,
            "slots": {name: spec.to_dict() for name, spec in sorted(self.slots.items())},
            "drift": list(self.drift),
            "may_mutate": self.may_mutate,
            "os_build": dict(self.os_build),
            "layout_id": self.manifest.layout_id if self.manifest else "",
            "image_layer": self.manifest.image_layer if self.manifest else "",
            "slot_schema_version": self.manifest.slot_schema_version if self.manifest else 0,
            "persistent_schema_version": (
                self.manifest.persistent_schema_version if self.manifest else 0
            ),
        }


# --- manifest ---------------------------------------------------------------


def _partition_spec(payload, *, label):
    if not isinstance(payload, dict):
        raise LayoutError("layout_manifest_invalid", f"{label} is not an object")
    name = str(payload.get("label") or "").strip()
    if not name:
        raise LayoutError("layout_manifest_invalid", f"{label} has no GPT label")
    return PartitionSpec(label=name, fstype=str(payload.get("fstype") or ""))


def parse_layout_manifest(payload):
    if not isinstance(payload, dict):
        raise LayoutError("layout_manifest_invalid", "the layout descriptor is not an object")
    version = payload.get("schema_version")
    if version != LAYOUT_SCHEMA_VERSION:
        raise LayoutError(
            "layout_manifest_unsupported",
            f"layout descriptor schema {version!r} is not schema {LAYOUT_SCHEMA_VERSION}",
        )
    raw_slots = payload.get("slots")
    if not isinstance(raw_slots, dict) or set(raw_slots) != set(SLOTS):
        raise LayoutError("layout_manifest_invalid", "the layout descriptor needs slots A and B")
    slots = {}
    for name in SLOTS:
        entry = raw_slots[name]
        if not isinstance(entry, dict):
            raise LayoutError("layout_manifest_invalid", f"slot {name} is not an object")
        slots[name] = SlotSpec(
            slot=name,
            boot=_partition_spec(entry.get("boot"), label=f"slot {name} boot"),
            root=_partition_spec(entry.get("root"), label=f"slot {name} root"),
        )

    manifest = LayoutManifest(
        schema_version=LAYOUT_SCHEMA_VERSION,
        layout_id=str(payload.get("layout_id") or ""),
        slot_schema_version=int(payload.get("slot_schema_version") or 0),
        persistent_schema_version=int(payload.get("persistent_schema_version") or 0),
        bootconfig=_partition_spec(payload.get("bootconfig"), label="the bootconfig partition"),
        persist=_partition_spec(payload.get("persist"), label="the persistent partition"),
        slots=slots,
        image_layer=str(payload.get("image_layer") or "image-rota"),
        image_layer_version=str(payload.get("image_layer_version") or ""),
        slot_device_prefix=str(payload.get("slot_device_prefix") or "/dev/disk/by-slot"),
        selector_mountpoint=str(payload.get("selector_mountpoint") or "/bootfs"),
        persist_mountpoint=str(payload.get("persist_mountpoint") or "/persistent"),
        boot_mountpoint=str(payload.get("boot_mountpoint") or "/boot/firmware"),
        shared_root=str(payload.get("shared_root") or "/persistent/shared"),
        machine_id_source=str(
            payload.get("machine_id_source") or "/persistent/common/etc/machine-id"
        ),
    )
    if not manifest.layout_id:
        raise LayoutError("layout_manifest_invalid", "the layout descriptor has no layout_id")
    _require_distinct_labels(manifest)
    return manifest


def _require_distinct_labels(manifest):
    labels = [spec.label.lower() for spec in manifest.partitions()]
    if len(set(labels)) != len(labels):
        raise LayoutError(
            "layout_manifest_invalid", "two entries of the layout claim the same GPT label"
        )


def read_layout_manifest(path):
    """Return the descriptor, or ``None`` when this host has no A/B layout."""

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        return parse_layout_manifest(json.loads(raw))
    except ValueError:
        raise LayoutError("layout_manifest_invalid", "the layout descriptor is not valid JSON")


# --- host signals -----------------------------------------------------------


class LayoutProbe:
    """Every independent signal discovery cross-checks, and nothing else.

    ``root`` makes the whole set testable against a fixture tree; ``runner`` is
    the allowlisted command runner used for the block layer.
    """

    def __init__(self, *, root="/", runner=None, manifest_path=None):
        self.root = Path(root)
        self.runner = runner
        self.manifest_path = (
            Path(manifest_path)
            if manifest_path is not None
            else self.root / "etc" / "ems-appliance-manager" / LAYOUT_MANIFEST_NAME
        )

    def _read_text(self, relative):
        try:
            return (self.root / relative).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return ""

    def _read_bytes(self, relative):
        try:
            return (self.root / relative).read_bytes()
        except (OSError, ValueError):
            return b""

    def manifest(self):
        return read_layout_manifest(self.manifest_path)

    def bootloader_partition(self):
        """The partition number the firmware booted, or ``0`` when unknown.

        The device tree publishes it as a big-endian 32-bit cell.
        """

        return _device_tree_cell(self._read_bytes(BOOTLOADER_PARTITION))

    def boot_mode(self):
        """The firmware's boot mode, which names the medium it booted from."""

        return _device_tree_cell(self._read_bytes(BOOTLOADER_BOOT_MODE))

    def tryboot(self):
        """Whether the firmware booted this slot as a one-shot trial."""

        raw = self._read_bytes(BOOTLOADER_TRYBOOT)
        if not raw:
            return None
        return bool(_device_tree_cell(raw))

    def cmdline_root(self):
        for token in self._read_text(CMDLINE).split():
            if token.startswith("root="):
                return token[len("root=") :].strip()
        return ""

    def os_build(self):
        try:
            return json.loads(self._read_text(OS_BUILD_MARKER) or "{}")
        except ValueError:
            return {}

    def slot_links(self, manifest):
        """Upstream's ``/dev/disk/by-slot`` symlinks, as device paths.

        The link text names a device on the booted medium, so it is read
        without resolving it against the probe root — resolving would rewrite
        that answer under a fixture. udev writes every ``/dev`` symlink
        relative to its own directory, so a relative target is joined back onto
        the link's own location in the booted namespace, never onto the root.
        """

        links = {}
        base = manifest.slot_device_prefix.rstrip("/")
        prefix = self.root / base.strip("/")
        for role in SLOT_LINK_ROLES:
            target = prefix / role
            try:
                text = str(Path(target).readlink())
            except (OSError, ValueError):
                continue
            if not os.path.isabs(text):
                text = os.path.normpath(os.path.join(os.path.dirname(f"{base}/{role}"), text))
            links[role] = text
        return links

    def mounts(self):
        raw = self._read_text(HOST_MOUNTINFO) or self._read_text(MOUNTINFO)
        table = {}
        for line in raw.splitlines():
            fields = line.split(" ")
            if len(fields) < 6:
                continue
            try:
                separator = fields.index("-", 6)
            except ValueError:
                continue
            table[fields[4].replace("\\040", " ")] = {
                "options": frozenset(fields[5].split(",")),
                "fstype": fields[separator + 1] if len(fields) > separator + 1 else "",
                "source": fields[separator + 2] if len(fields) > separator + 2 else "",
                "root": fields[3].replace("\\040", " "),
            }
        return table

    def block_partitions(self):
        """Every partition the block layer reports, from ``lsblk --json``.

        An unavailable or unparsable block layer is an error, never an empty
        result: "no partitions" and "cannot see the partitions" must not be the
        same answer to a caller deciding whether to write to a disk.
        """

        if self.runner is None or not self.runner.available("lsblk"):
            raise LayoutError("block_layer_unavailable", "lsblk is not available on this host")
        result = self.runner.run(
            "lsblk", ["--json", "--bytes", "--paths", "--output", LSBLK_COLUMNS], timeout=30
        )
        if not result.ok:
            raise LayoutError("block_layer_unavailable", "lsblk could not list the block devices")
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError:
            raise LayoutError("block_layer_unavailable", "lsblk did not return valid JSON")
        partitions = []
        for entry in _flatten_lsblk(payload.get("blockdevices") or []):
            if str(entry.get("type") or "") != "part":
                continue
            partitions.append(
                BlockPartition(
                    path=str(entry.get("path") or ""),
                    partuuid=str(entry.get("partuuid") or "").strip().lower(),
                    size_bytes=int(entry.get("size") or 0),
                    parent=str(entry.get("pkname") or ""),
                    number=int(entry.get("partn") or 0),
                    label=str(entry.get("partlabel") or ""),
                    fstype=str(entry.get("fstype") or ""),
                    mountpoint=str(entry.get("mountpoint") or ""),
                )
            )
        return partitions

    def selector(self, manifest):
        path = self.root / str(manifest.selector_mountpoint).lstrip("/") / "autoboot.txt"
        return read_selector(path)


def _device_tree_cell(raw):
    if len(raw) >= 4:
        return int(struct.unpack(">I", raw[:4])[0])
    text = raw.decode("utf-8", "ignore").strip().strip("\x00")
    return int(text) if text.isdigit() else 0


def _flatten_lsblk(entries):
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        yield entry
        yield from _flatten_lsblk(entry.get("children") or [])


# --- discovery --------------------------------------------------------------


def discover(probe):
    """The single entry point: what is this host, and may A/B act on it?"""

    try:
        manifest = probe.manifest()
    except LayoutError as exc:
        return LayoutStatus(
            mode=MODE_UNSUPPORTED,
            ab_supported=False,
            reason=REASON_MANIFEST_UNSUPPORTED,
            drift=(exc.message,),
        )
    if manifest is None:
        return LayoutStatus(mode=MODE_SINGLE_SLOT, ab_supported=False, reason=REASON_NOT_PRESENT)

    try:
        partitions = probe.block_partitions()
    except LayoutError as exc:
        return LayoutStatus(
            mode=MODE_AB,
            ab_supported=False,
            reason=REASON_BLOCK_LAYER_UNAVAILABLE,
            manifest=manifest,
            drift=(exc.message,),
        )

    drift = []
    booted = probe.bootloader_partition()
    booted_partition, booted_problem = _booted_partition(probe, partitions, booted)
    if booted_problem:
        drift.append(booted_problem)

    device = booted_partition.parent if booted_partition else ""
    medium = [entry for entry in partitions if entry.parent == device] if device else []
    block, duplicates = _by_label(medium)
    for label in duplicates:
        drift.append(f"the booted medium carries {label} more than once")

    slots = {}
    for name in SLOTS:
        spec = manifest.slot(name)
        boot = block.get(spec.boot.label.lower())
        root = block.get(spec.root.label.lower())
        if boot is None:
            drift.append(f"slot {name} boot partition {spec.boot.label} is not on the booted medium")
            continue
        if root is None:
            drift.append(f"slot {name} root partition {spec.root.label} is not on the booted medium")
            continue
        slots[name] = SlotDevices(slot=name, boot=boot, root=root)

    devices = {}
    for spec, role in ((manifest.bootconfig, "bootconfig"), (manifest.persist, "persist")):
        entry = block.get(spec.label.lower())
        if entry is None:
            drift.append(f"the {role} partition {spec.label} is not on the booted medium")
        else:
            devices[role] = entry

    active = booted_partition.label.lower() if booted_partition else ""
    active = _slot_of_label(manifest, active)
    if booted_partition is not None and not active:
        drift.append(
            f"the firmware booted {booted_partition.path} labelled "
            f"{booted_partition.label!r}, which is not a slot boot partition"
        )

    selector = None
    try:
        selector = probe.selector(manifest)
    except SelectorError as exc:
        drift.append(f"the boot selector is unusable: {exc.message}")

    mounts = probe.mounts()
    links = probe.slot_links(manifest)
    if active and active in slots:
        drift.extend(_mount_drift(manifest, slots[active], mounts))
        drift.extend(_cmdline_drift(manifest, slots[active], probe.cmdline_root()))
        drift.extend(_link_drift(manifest, slots, active, links))

    tryboot = bool(probe.tryboot())
    if selector is not None and active and active in slots:
        expected_boot = slots[active].boot.number
        chosen = selector.tryboot_partition if tryboot else selector.default_partition
        if expected_boot and chosen != expected_boot:
            drift.append(
                f"the selector points at partition {chosen}, but partition {expected_boot} booted"
            )

    inactive = _other_slot(active) if active else ""
    supported = not drift
    return LayoutStatus(
        mode=MODE_AB,
        ab_supported=supported,
        reason=REASON_SUPPORTED if supported else REASON_DRIFT,
        manifest=manifest,
        active_slot=active,
        inactive_slot=inactive,
        tryboot=tryboot,
        boot_partition=booted,
        root_partuuid=slots[active].root.partuuid if active and active in slots else "",
        device=device,
        selector=selector,
        slots=slots,
        selector_device=devices.get("bootconfig"),
        persist_device=devices.get("persist"),
        drift=tuple(drift),
        os_build=probe.os_build(),
    )


def _booted_partition(probe, partitions, number):
    """The partition the firmware booted, disambiguated by the booted medium.

    Two appliance media on one bus carry the same GPT labels and can carry the
    same partition numbers, so the boot mode decides which medium is meant
    before any label is read. Without it an ambiguous match is drift, never a
    guess: guessing here would write an update onto the wrong disk.
    """

    if not number:
        return None, "the firmware did not report which partition it booted"

    mode = probe.boot_mode()
    expected = BOOT_MODE_DEVICES.get(mode)
    if expected:
        wanted = f"/dev/{expected}p{number}"
        for entry in partitions:
            if entry.path == wanted:
                return entry, ""
        return None, f"the firmware booted {wanted}, which the block layer does not report"

    candidates = [entry for entry in partitions if entry.number == number]
    if len(candidates) == 1:
        return candidates[0], ""
    if not candidates:
        return None, f"the firmware booted partition {number}, which is not on any device"
    return None, (
        f"the firmware booted partition {number} and boot mode {mode or 'unknown'} does not "
        "identify the medium; more than one attached device matches"
    )


def _by_label(partitions):
    table, duplicates = {}, set()
    for entry in partitions:
        label = entry.label.strip().lower()
        if not label:
            continue
        if label in table:
            duplicates.add(label)
            continue
        table[label] = entry
    return table, sorted(duplicates)


def _slot_of_label(manifest, label):
    for name in SLOTS:
        if manifest.slot(name).boot.label.lower() == label:
            return name
    return ""


def _other_slot(name):
    return SLOT_B if name == SLOT_A else SLOT_A


def _aliases(manifest, role, device):
    """The names one partition legitimately answers to in the mount table."""

    return {device.path, manifest.slot_link(role)} - {""}


def _mount_drift(manifest, active, mounts):
    problems = []
    root_mount = mounts.get("/")
    if root_mount is None:
        problems.append("the mount table does not describe the root filesystem")
    elif root_mount["source"] and root_mount["source"] not in _aliases(
        manifest, "active/system", active.root
    ):
        problems.append(
            f"/ is mounted from {root_mount['source']}, but slot {active.slot} owns "
            f"{active.root.path}"
        )
    boot_mount = mounts.get(manifest.boot_mountpoint)
    if boot_mount is None:
        problems.append(f"{manifest.boot_mountpoint} is not mounted")
    elif boot_mount["source"] and boot_mount["source"] not in _aliases(
        manifest, "active/boot", active.boot
    ):
        problems.append(
            f"{manifest.boot_mountpoint} is mounted from {boot_mount['source']}, but slot "
            f"{active.slot} owns {active.boot.path}"
        )
    return problems


def _cmdline_drift(manifest, active, root):
    """``image-rota`` boots ``root=/dev/disk/by-slot/active/system``."""

    if not root:
        return ["the kernel command line does not name a root filesystem"]
    text = root.strip().strip('"')
    if text.upper().startswith("PARTUUID="):
        observed = text[len("PARTUUID=") :].strip().lower()
        if active.root.partuuid and observed != active.root.partuuid:
            return [
                f"the kernel was told to use root {observed}, but slot {active.slot} owns "
                f"{active.root.partuuid}"
            ]
        return []
    if text not in _aliases(manifest, "active/system", active.root):
        return [
            f"the kernel was told to use root {text}, but slot {active.slot} owns "
            f"{active.root.path}"
        ]
    return []


def _link_drift(manifest, slots, active, links):
    """Upstream's slot mapper must agree about which slot is which."""

    del manifest
    problems = []
    inactive = _other_slot(active)
    expected = {
        "active/boot": slots.get(active).boot.path if active in slots else "",
        "active/system": slots.get(active).root.path if active in slots else "",
        "other/boot": slots.get(inactive).boot.path if inactive in slots else "",
        "other/system": slots.get(inactive).root.path if inactive in slots else "",
    }
    for role, wanted in expected.items():
        observed = links.get(role)
        if not observed or not wanted:
            continue
        if observed != wanted:
            problems.append(
                f"{role} resolves to {observed}, but the layout says {wanted}"
            )
    return problems


# --- inactive-slot authority ------------------------------------------------


def prove_inactive_slot(status, mounts):
    """Refuse everything that would make the inactive slot unsafe to write.

    The inactive slot is never "the other partition number". It is the slot the
    layout descriptor labels, whose devices are not the active ones, are not
    mounted writable anywhere, and sit on the booted physical device.
    """

    if not status.may_mutate:
        raise LayoutError("layout_drift", "the A/B layout could not be proven on this host")
    if not status.inactive_slot:
        raise LayoutError("inactive_slot_unknown", "the inactive slot could not be identified")

    active = status.slot_devices(status.active_slot)
    inactive = status.slot_devices(status.inactive_slot)

    if inactive.boot.path == active.boot.path or inactive.root.path == active.root.path:
        raise LayoutError(
            "inactive_slot_is_active", "the inactive slot resolves to the running slot's devices"
        )
    root_source = (mounts.get("/") or {}).get("source")
    if inactive.root.path == root_source:
        raise LayoutError(
            "inactive_slot_is_active", "the inactive root filesystem is the running root filesystem"
        )
    if {inactive.boot.parent, inactive.root.parent} != {active.boot.parent}:
        raise LayoutError(
            "inactive_slot_foreign_device",
            "the inactive slot is not on the same physical device as the running slot",
        )
    for device in (inactive.boot, inactive.root):
        for target, record in mounts.items():
            if record.get("source") != device.path:
                continue
            if "ro" not in record.get("options", frozenset()):
                raise LayoutError(
                    "inactive_slot_mounted",
                    f"{device.path} is mounted writable at {target}",
                )
    return inactive
