# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mount the freshly written inactive slot, prove it, and unmount it.

A read-back digest proves the medium holds the bytes that were sent to it. It
does not prove those bytes are a filesystem, that the filesystem carries the
release it claims to, or that the appliance inside it would come back up. Only
mounting it answers that, and it has to happen before the selector is armed:
after the reboot the appliance is already running whatever was written.

Two rules shape the module:

- The device paths come from the revalidated write authority, never from a
  request, and are mounted ``ro,nosuid,nodev,noexec`` under a fixed root-owned
  directory. Nothing here accepts a caller-supplied mount source or target.
- Every mount is undone on every path. A mount that could not be undone is
  recorded as a leak, and a leak blocks further update mutation, because the
  next write would otherwise open a partition the kernel still has mounted.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from appliance import ab_persistence

MOUNT_ROOT = "/run/ems-appliance-manager/ab-inspect"
LEAK_MARKER = "inspection-mount-leaked"

MOUNT_OPTIONS = "ro,nosuid,nodev,noexec"

PASS = "pass"
FAIL = "fail"
NOT_RUN = "not_run"

HEALTH_UNIT = "ems-appliance-ab-health.service"
BOOTSTRAP_UNIT = "ems-appliance-slot-bootstrap.service"
PERSISTENCE_UNIT = "ems-appliance-persistence.service"

UNIT_DIRECTORY = "usr/lib/systemd/system"
ENABLED_DIRECTORY = "etc/systemd/system/multi-user.target.wants"

# image-rota resolves the root filesystem in the initramfs, so every slot's
# cmdline names the alias rather than a partition.
EXPECTED_ROOT = "root=/dev/disk/by-slot/active/system"

KERNELS = ("kernel8.img", "kernel_2712.img", "kernel.img")
INITRAMFS = ("initramfs8", "initramfs_2712", "initramfs")

# A rootfs that names one slot could not be the other slot's filesystem, and
# image-rota builds one bit-for-bit identical pair. Any of these is a defect.
SLOT_PINNED_MARKERS = ("etc/ems-appliance-slot", "etc/ems-appliance-manager/slot")


class InspectionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Finding:
    check: str
    result: str
    detail: str = ""

    def to_dict(self):
        return {"check": self.check, "result": self.result, "detail": self.detail}


@dataclass(frozen=True)
class InspectionReport:
    target_slot: str
    findings: tuple = ()
    cleaned: bool = True
    leaked: tuple = field(default_factory=tuple)

    @property
    def ok(self):
        return self.cleaned and not any(item.result == FAIL for item in self.findings)

    @property
    def problems(self):
        return tuple(
            f"{item.check}: {item.detail}" for item in self.findings if item.result == FAIL
        )

    def to_dict(self):
        return {
            "target_slot": self.target_slot,
            "ok": self.ok,
            "cleaned": self.cleaned,
            "leaked": list(self.leaked),
            "problems": list(self.problems),
            "findings": [item.to_dict() for item in self.findings],
        }


class InactiveSlotInspector:
    """Prove the written slot is a bootable appliance, then leave no mounts."""

    def __init__(self, *, runner, root="/", mount_root=MOUNT_ROOT):
        self.runner = runner
        self.root = Path(root)
        self.mount_root = str(mount_root)

    # --- mounting ---------------------------------------------------------

    def _path(self, relative):
        return self.root / str(relative).lstrip("/")

    def _leak_marker(self):
        return self._path(self.mount_root) / LEAK_MARKER

    def assert_no_leak(self):
        """A previous inspection that could not unmount blocks the next write."""

        if self._leak_marker().exists():
            raise InspectionError(
                "inspection_mount_leaked",
                "a previous inactive-slot inspection left a mount behind; "
                "reboot the appliance before staging another update",
            )

    def _mount(self, device, target):
        if self.runner is None or not self.runner.available("mount"):
            raise InspectionError(
                "inspection_mount_unavailable",
                "mount is not available, so the written slot cannot be inspected",
            )
        result = self.runner.run(
            "mount", ["-o", MOUNT_OPTIONS, str(device), str(target)], timeout=60
        )
        if not result.ok:
            raise InspectionError(
                "inspection_mount_failed",
                f"the written filesystem on {device} could not be mounted read-only",
            )
        return True

    def _unmount(self, target):
        if self.runner is None or not self.runner.available("umount"):
            return False
        return bool(self.runner.run("umount", [str(target)], timeout=60).ok)

    def inspect(self, authority, release, *, appliance_version=""):
        """Mount both written filesystems, check them, and always clean up."""

        self.assert_no_leak()
        base = self._path(self.mount_root)
        base.mkdir(parents=True, exist_ok=True)
        findings = []
        leaked = []

        for role, device, checker in (
            ("root", authority.root_device, self._root_findings),
            ("boot", authority.boot_device, self._boot_findings),
        ):
            target = base / role
            target.mkdir(parents=True, exist_ok=True)
            try:
                self._mount(device, target)
            except InspectionError as exc:
                findings.append(Finding(f"{role}_mountable", FAIL, exc.message))
                continue
            findings.append(Finding(f"{role}_mountable", PASS, str(device)))
            try:
                findings.extend(
                    checker(target, authority, release, appliance_version=appliance_version)
                )
            finally:
                if not self._unmount(target):
                    leaked.append(str(target))

        if leaked:
            self._leak_marker().parent.mkdir(parents=True, exist_ok=True)
            self._leak_marker().write_text("\n".join(leaked) + "\n", encoding="utf-8")

        return InspectionReport(
            target_slot=authority.target_slot,
            findings=tuple(findings),
            cleaned=not leaked,
            leaked=tuple(leaked),
        )

    # --- the written root filesystem --------------------------------------

    def _root_findings(self, mountpoint, authority, release, *, appliance_version=""):
        findings = [self._os_build_finding(mountpoint, authority, release)]
        findings.append(self._layout_finding(mountpoint))
        findings.append(self._slot_agnostic_finding(mountpoint))
        findings.append(self._slot_shared_finding(mountpoint))
        findings.append(self._package_finding(mountpoint, appliance_version))
        for unit in (HEALTH_UNIT, BOOTSTRAP_UNIT, PERSISTENCE_UNIT):
            findings.append(self._unit_finding(mountpoint, unit))
        findings.append(self._modules_finding(mountpoint))
        return findings

    def _os_build_finding(self, mountpoint, authority, release):
        path = mountpoint / "etc/ems-appliance-os-build"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Finding("os_build_marker", FAIL, "the written slot carries no build marker")
        build_id = str(payload.get("build_id") or "")
        version = str(payload.get("release_version") or "")
        if build_id != release.build_id:
            return Finding(
                "os_build_marker",
                FAIL,
                f"the written slot reports build {build_id or 'none'}, the release is "
                f"{release.build_id}",
            )
        if version != release.release_version:
            return Finding(
                "os_build_marker",
                FAIL,
                f"the written slot reports version {version or 'none'}, the release is "
                f"{release.release_version}",
            )
        del authority
        return Finding("os_build_marker", PASS, f"{version} ({build_id})")

    def _layout_finding(self, mountpoint):
        path = mountpoint / "etc/ems-appliance-manager/ab-layout.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Finding("layout_descriptor", FAIL, "the written slot carries no A/B layout")
        from appliance.ab_layout import LAYOUT_SCHEMA_VERSION

        if payload.get("schema_version") != LAYOUT_SCHEMA_VERSION:
            return Finding(
                "layout_descriptor",
                FAIL,
                f"the written layout is schema {payload.get('schema_version')!r}, "
                f"this appliance implements {LAYOUT_SCHEMA_VERSION}",
            )
        return Finding("layout_descriptor", PASS, str(payload.get("layout_id") or ""))

    def _slot_agnostic_finding(self, mountpoint):
        """Neither slot's filesystem may name a slot; upstream pairs them."""

        for marker in SLOT_PINNED_MARKERS:
            if (mountpoint / marker).exists():
                return Finding(
                    "slot_agnostic_rootfs",
                    FAIL,
                    f"{marker} pins this filesystem to one slot",
                )
        return Finding("slot_agnostic_rootfs", PASS, "no slot-pinned marker")

    def _slot_shared_finding(self, mountpoint):
        path = (
            mountpoint
            / ab_persistence.SLOT_SHARED_CONF_DIR.lstrip("/")
            / ab_persistence.SLOT_SHARED_CONF_NAME
        )
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return Finding(
                "persistence_declared",
                FAIL,
                "the written slot declares no shared paths and would lose every write",
            )
        declared = {
            line.split("=", 1)[1].strip() for line in text.splitlines() if line.startswith("Path=")
        }
        required = {shared.target for shared in ab_persistence.SHARED_PATHS if shared.required}
        missing = sorted(required - declared)
        if missing:
            return Finding(
                "persistence_declared", FAIL, f"not declared: {', '.join(missing)}"
            )
        return Finding("persistence_declared", PASS, f"{len(declared)} shared paths")

    def _package_finding(self, mountpoint, appliance_version):
        binary = mountpoint / "usr/bin/ems-appliance"
        if not binary.exists():
            return Finding(
                "appliance_package", FAIL, "the Appliance Manager is not installed in this slot"
            )
        if not appliance_version:
            return Finding("appliance_package", PASS, "installed")
        marker = mountpoint / "etc/ems-appliance-os-build"
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Finding("appliance_package", PASS, "installed; version not declared")
        observed = str(payload.get("appliance_manager_version") or "")
        if observed and observed != appliance_version:
            return Finding(
                "appliance_package",
                FAIL,
                f"the written slot carries Appliance Manager {observed}, the release declares "
                f"{appliance_version}",
            )
        return Finding("appliance_package", PASS, observed or "installed")

    def _unit_finding(self, mountpoint, unit):
        if not (mountpoint / UNIT_DIRECTORY / unit).exists():
            return Finding(f"unit_installed:{unit}", FAIL, "the unit is not in this slot")
        if not (mountpoint / ENABLED_DIRECTORY / unit).exists():
            return Finding(f"unit_installed:{unit}", FAIL, "the unit is installed but not enabled")
        return Finding(f"unit_installed:{unit}", PASS, "installed and enabled")

    def _modules_finding(self, mountpoint):
        modules = mountpoint / "lib/modules"
        if not modules.is_dir() or not any(modules.iterdir()):
            return Finding("kernel_modules", FAIL, "the written slot carries no kernel modules")
        return Finding(
            "kernel_modules", PASS, ", ".join(sorted(item.name for item in modules.iterdir()))
        )

    # --- the written boot filesystem --------------------------------------

    def _boot_findings(self, mountpoint, authority, release, *, appliance_version=""):
        del authority, release, appliance_version
        findings = [self._cmdline_finding(mountpoint)]
        findings.append(self._asset_finding(mountpoint, "kernel", KERNELS))
        findings.append(self._asset_finding(mountpoint, "initramfs", INITRAMFS))
        findings.append(
            Finding(
                "boot_configuration",
                PASS if (mountpoint / "config.txt").exists() else FAIL,
                "config.txt",
            )
        )
        findings.append(self._device_tree_finding(mountpoint))
        return findings

    def _cmdline_finding(self, mountpoint):
        try:
            text = (mountpoint / "cmdline.txt").read_text(encoding="utf-8")
        except OSError:
            return Finding("boot_cmdline", FAIL, "the written boot filesystem has no cmdline.txt")
        if EXPECTED_ROOT not in text:
            return Finding(
                "boot_cmdline",
                FAIL,
                f"cmdline.txt does not select the active slot with {EXPECTED_ROOT}",
            )
        return Finding("boot_cmdline", PASS, EXPECTED_ROOT)

    def _asset_finding(self, mountpoint, name, candidates):
        found = [item for item in candidates if (mountpoint / item).exists()]
        if not found:
            return Finding(
                f"boot_{name}", FAIL, f"none of {', '.join(candidates)} is in the boot filesystem"
            )
        return Finding(f"boot_{name}", PASS, ", ".join(found))

    def _device_tree_finding(self, mountpoint):
        blobs = sorted(item.name for item in mountpoint.glob("*.dtb"))
        if not blobs:
            return Finding("boot_device_tree", FAIL, "the boot filesystem carries no device tree")
        return Finding("boot_device_tree", PASS, f"{len(blobs)} device-tree blobs")
