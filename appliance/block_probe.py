# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the block layer reports, and what image this host was flashed from.

Two questions the first-boot growth helper has to answer before it repartitions
anything: which partition is mounted at ``/``, and whether this host carries a
build marker written by an image this project produced. Both are read here, and
both fail loudly rather than emptily -- "no partitions" and "cannot see the
partitions" must not be the same answer to a caller deciding whether to write
to a disk.

``root`` makes the whole set testable against a fixture tree; ``runner`` is the
allowlisted command runner used for the block layer.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from appliance.image_shape import OS_BUILD_MARKER, marker_is_ours

LSBLK_COLUMNS = "PATH,TYPE,SIZE,PARTUUID,PARTLABEL,FSTYPE,MOUNTPOINT,PKNAME,PARTN"

# The record packaging/appliance/bin/grow-root.sh publishes in the state
# directory once the root fills its medium.
GROW_MARKER_NAME = ".root-grown"
MIB = 1024 * 1024


class BlockProbeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


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


def _flatten_lsblk(entries):
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        yield entry
        yield from _flatten_lsblk(entry.get("children") or [])


class BlockProbe:
    """The block layer and the build marker, read read-only."""

    def __init__(self, *, root="/", runner=None):
        self.root = Path(root)
        self.runner = runner

    def _read_text(self, relative):
        try:
            return (self.root / relative).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return ""

    def os_build(self):
        """The build marker the image wrote into its own root, or an empty map."""

        try:
            return json.loads(self._read_text(OS_BUILD_MARKER) or "{}")
        except ValueError:
            return {}

    def block_partitions(self):
        """Every partition the block layer reports, from ``lsblk --json``.

        An unavailable or unparsable block layer is an error, never an empty
        result.
        """

        if self.runner is None or not self.runner.available("lsblk"):
            raise BlockProbeError("block_layer_unavailable", "lsblk is not available on this host")
        result = self.runner.run(
            "lsblk", ["--json", "--bytes", "--paths", "--output", LSBLK_COLUMNS], timeout=30
        )
        if not result.ok:
            raise BlockProbeError(
                "block_layer_unavailable", "lsblk could not list the block devices"
            )
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError:
            raise BlockProbeError("block_layer_unavailable", "lsblk did not return valid JSON")
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

    def root_partition(self):
        """The partition mounted at ``/``, or ``None``.

        Asked of the block layer rather than resolved out of ``/proc``: the
        mount source is whatever string was passed to mount, and on this image
        that is a by-slot alias rather than the kernel name sysfs is keyed by.
        """

        return next((item for item in self.block_partitions() if item.mountpoint == "/"), None)

    def root_medium(self, *, state_dir, opener=None):
        """The medium the root lives on, and what the first boot grew of it.

        Only a host flashed from an appliance image grows its root, so any
        other host is answered without being measured. On an image, a
        question the block layer cannot answer raises rather than reading as
        a filled medium.
        """

        from appliance import media_sizing, partition_geometry

        if not marker_is_ours(self.os_build()):
            return {"appliance_image": False}
        root = self.root_partition()
        if root is None:
            raise BlockProbeError("root_partition_unknown", "no partition is mounted at /")
        try:
            geometry = partition_geometry.read_geometry(
                root.path, sysfs=str(self.root / "sys"), opener=opener
            )
        except partition_geometry.GeometryError as error:
            raise BlockProbeError(error.code, error.message)
        return {
            "appliance_image": True,
            "available": True,
            "medium_mb": geometry.disk_bytes // MIB,
            "root_mb": geometry.size_bytes // MIB,
            "unused_mb": geometry.tail_bytes // MIB,
            "fills_medium": geometry.fills_disk,
            "medium_supported": media_sizing.media_is_supported(geometry.disk_bytes),
            "grown": _grow_record(Path(state_dir) / GROW_MARKER_NAME),
        }


def _grow_record(path):
    """The outcome and time grow-root.sh recorded, or ``None`` before it has."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return {"outcome": fields.get("outcome", ""), "grown_at": fields.get("grown_at", "")}
