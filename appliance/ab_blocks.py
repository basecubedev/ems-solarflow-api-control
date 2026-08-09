# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writing to a block device, and the fake that makes it testable.

This is the only place in the appliance that opens a partition for writing. Two
rules hold everywhere in it:

- The device path is never a caller-supplied string. It comes from
  ``ab_layout`` discovery, which derived it from the root-owned layout manifest
  and the block layer, and it is re-checked against the operation record
  immediately before the descriptor is opened.
- Nothing is believed until it has been read back. A write that returned
  success, a flush that returned success and a device that reports the right
  size still prove nothing about what is on the medium.

``FakeBlockBackend`` models the failure modes real storage has — short writes,
EIO, a flush that fails, a read-back that differs, a device that disappears —
so the destructive path is covered without a real ``/dev/mmcblk*``. Production
never accepts a fake or a browser-provided device: ``RealBlockBackend`` opens
only paths that came out of layout discovery, and the loop-device tier is gated
behind an explicit opt-in (see ``ab_block_guard``).
"""

import hashlib
import os
from dataclasses import dataclass, field

CHUNK = 4 * 1024 * 1024


class BlockError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WriteResult:
    path: str
    bytes_written: int
    digest: str
    verified: bool

    def to_dict(self):
        return {
            "path": self.path,
            "bytes_written": self.bytes_written,
            "digest": self.digest,
            "verified": self.verified,
        }


class RealBlockBackend:
    """Open, write, flush and read back a real partition."""

    def size(self, path):
        try:
            handle = os.open(str(path), os.O_RDONLY)
        except OSError as exc:
            raise BlockError("block_device_unavailable", f"{path} could not be opened: {exc}")
        try:
            return os.lseek(handle, 0, os.SEEK_END)
        except OSError as exc:
            raise BlockError("block_device_unavailable", f"{path} has no readable size: {exc}")
        finally:
            os.close(handle)

    def write(self, path, source, *, expected_bytes):
        """Write ``source`` onto ``path`` and flush it to the medium.

        ``O_EXCL`` on a block device is what makes this operation the owner of
        the partition: the kernel refuses it while the device is mounted or
        another opener holds it exclusively.
        """

        written = 0
        digest = hashlib.sha256()
        try:
            handle = os.open(str(path), os.O_WRONLY | os.O_EXCL)
        except OSError as exc:
            raise BlockError(
                "block_device_busy",
                f"{path} could not be opened for exclusive writing: {exc}",
            )
        try:
            with open(str(source), "rb") as reader:
                while True:
                    block = reader.read(CHUNK)
                    if not block:
                        break
                    offset = 0
                    while offset < len(block):
                        try:
                            count = os.write(handle, block[offset:])
                        except OSError as exc:
                            raise BlockError("block_write_failed", f"{path}: {exc}")
                        if count <= 0:
                            raise BlockError(
                                "block_write_short", f"{path} accepted no bytes at offset {written}"
                            )
                        offset += count
                        written += count
                    digest.update(block)
            try:
                os.fsync(handle)
            except OSError as exc:
                raise BlockError("block_flush_failed", f"{path} could not be flushed: {exc}")
        finally:
            os.close(handle)

        if written != expected_bytes:
            raise BlockError(
                "block_write_short",
                f"{path} received {written} of {expected_bytes} bytes",
            )
        return f"sha256:{digest.hexdigest()}"

    def read_digest(self, path, length):
        """The digest of the first ``length`` bytes actually on the medium."""

        digest = hashlib.sha256()
        remaining = int(length)
        try:
            handle = os.open(str(path), os.O_RDONLY)
        except OSError as exc:
            raise BlockError("block_device_unavailable", f"{path} could not be reopened: {exc}")
        try:
            while remaining > 0:
                try:
                    block = os.read(handle, min(CHUNK, remaining))
                except OSError as exc:
                    raise BlockError("block_read_failed", f"{path}: {exc}")
                if not block:
                    raise BlockError(
                        "block_read_short",
                        f"{path} ended {remaining} bytes before the written length",
                    )
                digest.update(block)
                remaining -= len(block)
        finally:
            os.close(handle)
        return f"sha256:{digest.hexdigest()}"

    def flush_device(self, path):
        try:
            handle = os.open(str(path), os.O_RDONLY)
        except OSError:
            return False
        try:
            os.fsync(handle)
        except OSError:
            return False
        finally:
            os.close(handle)
        return True


class FakeBlockBackend:
    """A block layer that can fail the way real storage fails.

    Every mode here has been the cause of a real corrupted update somewhere: a
    short write, an EIO halfway through, a flush that lied, a read-back that
    differs from what was written, a device that vanished mid-operation, and a
    partition too small for the image. Production never accepts this backend.
    """

    def __init__(self, *, sizes=None):
        self.sizes = dict(sizes or {})
        self.contents = {}
        self.calls = []
        self.short_write_after = None
        self.fail_write_after = None
        self.fail_flush = False
        self.corrupt_readback = False
        self.disappear_before_readback = False
        self.busy = set()
        self.opened_exclusive = []

    # --- scripting -------------------------------------------------------

    def set_size(self, path, size):
        self.sizes[str(path)] = int(size)
        return self

    def mark_busy(self, path):
        self.busy.add(str(path))
        return self

    def size(self, path):
        key = str(path)
        if key not in self.sizes:
            raise BlockError("block_device_unavailable", f"{key} is not a known device")
        return self.sizes[key]

    def write(self, path, source, *, expected_bytes):
        key = str(path)
        self.calls.append(("write", key))
        if key in self.busy:
            raise BlockError("block_device_busy", f"{key} is held by another opener")
        if key not in self.sizes:
            raise BlockError("block_device_unavailable", f"{key} is not a known device")
        self.opened_exclusive.append(key)

        payload = open(str(source), "rb").read()
        if self.fail_write_after is not None and self.fail_write_after < len(payload):
            self.contents[key] = payload[: self.fail_write_after]
            raise BlockError("block_write_failed", f"{key}: simulated EIO")
        if self.short_write_after is not None and self.short_write_after < len(payload):
            self.contents[key] = payload[: self.short_write_after]
            raise BlockError(
                "block_write_short",
                f"{key} received {self.short_write_after} of {expected_bytes} bytes",
            )
        self.contents[key] = payload
        if len(payload) != expected_bytes:
            raise BlockError(
                "block_write_short", f"{key} received {len(payload)} of {expected_bytes} bytes"
            )
        if self.fail_flush:
            raise BlockError("block_flush_failed", f"{key} could not be flushed")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def read_digest(self, path, length):
        key = str(path)
        self.calls.append(("read", key))
        if self.disappear_before_readback:
            raise BlockError("block_device_unavailable", f"{key} disappeared")
        payload = self.contents.get(key)
        if payload is None:
            raise BlockError("block_read_failed", f"{key} has never been written")
        if len(payload) < length:
            raise BlockError(
                "block_read_short", f"{key} ended {length - len(payload)} bytes early"
            )
        payload = payload[:length]
        if self.corrupt_readback:
            payload = b"\x00" + payload[1:]
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def flush_device(self, path):
        self.calls.append(("flush", str(path)))
        return not self.fail_flush


# --- the destructive-test guard ---------------------------------------------

ENV_OPT_IN = "EMS_APPLIANCE_AB_BLOCK_WRITE"
ENV_ALLOWLIST = "EMS_APPLIANCE_AB_BLOCK_ALLOWLIST"

# Never acceptable merely because they exist. A development host's own storage
# is exactly what an accidental block write destroys.
NEVER_WRITABLE = (
    "/dev/mmcblk0",
    "/dev/nvme0n1",
    "/dev/sda",
    "/dev/vda",
    "/dev/hda",
)


@dataclass(frozen=True)
class BlockWriteGuard:
    """Everything that must hold before a test may write a real block device."""

    opted_in: bool = False
    root: bool = False
    allowlisted: bool = False
    unmounted: bool = False
    not_system_storage: bool = False
    reasons: tuple = field(default_factory=tuple)

    @property
    def permitted(self):
        return (
            self.opted_in
            and self.root
            and self.allowlisted
            and self.unmounted
            and self.not_system_storage
            and not self.reasons
        )

    def to_dict(self):
        return {
            "permitted": self.permitted,
            "opted_in": self.opted_in,
            "root": self.root,
            "allowlisted": self.allowlisted,
            "unmounted": self.unmounted,
            "not_system_storage": self.not_system_storage,
            "reasons": list(self.reasons),
        }


def ab_block_guard(path, *, environ=None, mounts=None, euid=None):
    """May a destructive block write run against ``path`` right now?

    Every condition must hold. Default test runs use image files, loop devices
    or the fake backend and never reach this.
    """

    environ = os.environ if environ is None else environ
    euid = os.geteuid() if euid is None else euid
    mounts = mounts or {}
    target = str(path)
    reasons = []

    opted_in = str(environ.get(ENV_OPT_IN, "")).strip() == "1"
    if not opted_in:
        reasons.append(f"{ENV_OPT_IN}=1 was not set")

    root = euid == 0
    if not root:
        reasons.append("the caller is not root")

    allowlist = [
        item.strip()
        for item in str(environ.get(ENV_ALLOWLIST, "")).split(",")
        if item.strip()
    ]
    allowlisted = target in allowlist
    if not allowlisted:
        reasons.append(f"{target} is not in {ENV_ALLOWLIST}")

    unmounted = not any(record.get("source") == target for record in mounts.values())
    if not unmounted:
        reasons.append(f"{target} is mounted")

    not_system_storage = not any(
        target == device or target.startswith(device) for device in NEVER_WRITABLE
    )
    if not not_system_storage:
        reasons.append(f"{target} is system or development storage and is never written")

    return BlockWriteGuard(
        opted_in=opted_in,
        root=root,
        allowlisted=allowlisted,
        unmounted=unmounted,
        not_system_storage=not_system_storage,
        reasons=tuple(reasons),
    )
