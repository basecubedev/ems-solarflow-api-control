# SPDX-License-Identifier: AGPL-3.0-or-later
"""Proving the freshly written slot before the firmware is pointed at it.

A read-back digest says the medium holds the bytes that were sent to it. It
says nothing about whether those bytes are a filesystem, whether the filesystem
carries the release it claims to, or whether the appliance inside it would come
back up. Only mounting it answers that, and it has to happen before the selector
is armed — after the reboot the appliance is already running whatever is there.

The other half of the contract is cleanup. A mount that could not be undone is
recorded, and a recorded leak blocks the next update, because the following
write would otherwise open a partition the kernel still has mounted.
"""

import json
from types import SimpleNamespace

import pytest

from appliance import ab_inspect, ab_persistence
from appliance.ab_inspect import FAIL, PASS, InactiveSlotInspector, InspectionError
from appliance.commands import CommandResult, RecordingRunner

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

RELEASE = SimpleNamespace(
    release_version="1.5.0",
    build_id="20260807-1",
    appliance_manager_version="0.9.0",
)

AUTHORITY = SimpleNamespace(
    target_slot="B",
    boot_device="/dev/mmcblk0p3",
    root_device="/dev/mmcblk0p5",
)


class MountingRunner(RecordingRunner):
    """A runner whose mount and umount succeed without touching the kernel.

    The fixture populates the mount directory directly, which is what the real
    mount would have made visible there.
    """

    def __init__(self, *, mount_ok=True, umount_ok=True):
        super().__init__()
        self.mount_ok = mount_ok
        self.umount_ok = umount_ok

    def run(self, tool, args=(), *, timeout=None, input_text=None, check=False):
        self.calls.append((tool, tuple(args), input_text))
        ok = self.mount_ok if tool == "mount" else self.umount_ok
        return CommandResult(tool, tuple(args), 0 if ok else 1, "", "")


def build_slot(root, *, build_id="20260807-1", release_version="1.5.0", **overrides):
    """A mount directory holding what a correctly written slot would hold."""

    rootfs = root / "run/ems-appliance-manager/ab-inspect/root"
    boot = root / "run/ems-appliance-manager/ab-inspect/boot"
    for directory in (rootfs, boot):
        directory.mkdir(parents=True, exist_ok=True)

    marker = {
        "release_version": release_version,
        "build_id": build_id,
        "layout_id": "ems-appliance-rota-v1",
        "appliance_manager_version": overrides.get("appliance_manager_version", "0.9.0"),
    }
    (rootfs / "etc").mkdir(parents=True, exist_ok=True)
    (rootfs / "etc/ems-appliance-os-build").write_text(json.dumps(marker), encoding="utf-8")

    (rootfs / "etc/ems-appliance-manager").mkdir(parents=True, exist_ok=True)
    (rootfs / "etc/ems-appliance-manager/ab-layout.json").write_text(
        json.dumps({"schema_version": 2, "layout_id": "ems-appliance-rota-v1"}), encoding="utf-8"
    )

    conf = rootfs / ab_persistence.SLOT_SHARED_CONF_DIR.lstrip("/")
    conf.mkdir(parents=True, exist_ok=True)
    (conf / ab_persistence.SLOT_SHARED_CONF_NAME).write_text(
        ab_persistence.slot_shared_conf(), encoding="utf-8"
    )

    (rootfs / "usr/bin").mkdir(parents=True, exist_ok=True)
    (rootfs / "usr/bin/ems-appliance").write_text("#!/bin/sh\n", encoding="utf-8")

    units = rootfs / ab_inspect.UNIT_DIRECTORY
    enabled = rootfs / ab_inspect.ENABLED_DIRECTORY
    units.mkdir(parents=True, exist_ok=True)
    enabled.mkdir(parents=True, exist_ok=True)
    for unit in (ab_inspect.HEALTH_UNIT, ab_inspect.BOOTSTRAP_UNIT, ab_inspect.PERSISTENCE_UNIT):
        (units / unit).write_text("[Unit]\n", encoding="utf-8")
        (enabled / unit).write_text("[Unit]\n", encoding="utf-8")

    (rootfs / "lib/modules/6.12.0-rpi").mkdir(parents=True, exist_ok=True)

    (boot / "cmdline.txt").write_text(
        f"console=serial0,115200 {ab_inspect.EXPECTED_ROOT} ro\n", encoding="utf-8"
    )
    (boot / "config.txt").write_text("arm_64bit=1\n", encoding="utf-8")
    (boot / "kernel8.img").write_bytes(b"kernel")
    (boot / "initramfs8").write_bytes(b"initramfs")
    (boot / "bcm2711-rpi-4-b.dtb").write_bytes(b"dtb")
    return rootfs, boot


@pytest.fixture
def inspector(tmp_path):
    build_slot(tmp_path)
    return InactiveSlotInspector(runner=MountingRunner(), root=tmp_path)


def verdicts(report):
    return {finding.check: finding.result for finding in report.findings}


# --- a correctly written slot -------------------------------------------------


def test_a_correctly_written_slot_passes_and_is_unmounted(tmp_path, inspector):
    report = inspector.inspect(AUTHORITY, RELEASE, appliance_version="0.9.0")

    assert report.ok
    assert report.cleaned
    assert report.leaked == ()
    assert verdicts(report)["root_mountable"] == PASS
    assert verdicts(report)["boot_mountable"] == PASS


def test_both_filesystems_are_mounted_read_only(tmp_path, inspector):
    inspector.inspect(AUTHORITY, RELEASE)

    mounts = [call for call in inspector.runner.calls if call[0] == "mount"]

    assert len(mounts) == 2
    for _tool, args, _input in mounts:
        assert args[0] == "-o"
        assert "ro" in args[1].split(",")
        assert "noexec" in args[1].split(",")


def test_every_mount_is_undone(tmp_path, inspector):
    inspector.inspect(AUTHORITY, RELEASE)

    unmounts = [call for call in inspector.runner.calls if call[0] == "umount"]

    assert len(unmounts) == 2


def test_only_the_devices_the_authority_names_are_mounted(tmp_path, inspector):
    """No mount source ever comes from anywhere but the revalidated plan."""

    inspector.inspect(AUTHORITY, RELEASE)

    sources = [args[2] for tool, args, _ in inspector.runner.calls if tool == "mount"]

    assert set(sources) == {AUTHORITY.root_device, AUTHORITY.boot_device}


# --- what the inspection refuses ---------------------------------------------


def test_a_slot_carrying_another_build_is_refused(tmp_path):
    build_slot(tmp_path, build_id="something-else")
    inspector = InactiveSlotInspector(runner=MountingRunner(), root=tmp_path)

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert not report.ok
    assert verdicts(report)["os_build_marker"] == FAIL


def test_a_slot_carrying_another_release_version_is_refused(tmp_path):
    build_slot(tmp_path, release_version="9.9.9")
    inspector = InactiveSlotInspector(runner=MountingRunner(), root=tmp_path)

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert verdicts(report)["os_build_marker"] == FAIL


def test_a_slot_without_the_appliance_package_is_refused(tmp_path, inspector):
    (tmp_path / "run/ems-appliance-manager/ab-inspect/root/usr/bin/ems-appliance").unlink()

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert verdicts(report)["appliance_package"] == FAIL


def test_a_slot_carrying_another_appliance_version_is_refused(tmp_path):
    build_slot(tmp_path, appliance_manager_version="0.1.0")
    inspector = InactiveSlotInspector(runner=MountingRunner(), root=tmp_path)

    report = inspector.inspect(AUTHORITY, RELEASE, appliance_version="0.9.0")

    assert verdicts(report)["appliance_package"] == FAIL


@pytest.mark.parametrize(
    "unit",
    [ab_inspect.HEALTH_UNIT, ab_inspect.BOOTSTRAP_UNIT, ab_inspect.PERSISTENCE_UNIT],
)
def test_a_slot_whose_units_are_installed_but_not_enabled_is_refused(tmp_path, inspector, unit):
    rootfs = tmp_path / "run/ems-appliance-manager/ab-inspect/root"
    (rootfs / ab_inspect.ENABLED_DIRECTORY / unit).unlink()

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert verdicts(report)[f"unit_installed:{unit}"] == FAIL


def test_a_slot_that_declares_no_shared_paths_is_refused(tmp_path, inspector):
    """It would boot, look healthy, and lose everything at the next switch."""

    rootfs = tmp_path / "run/ems-appliance-manager/ab-inspect/root"
    (rootfs / ab_persistence.SLOT_SHARED_CONF_DIR.lstrip("/") /
     ab_persistence.SLOT_SHARED_CONF_NAME).unlink()

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert verdicts(report)["persistence_declared"] == FAIL


def test_a_slot_declaring_only_some_shared_paths_is_refused(tmp_path, inspector):
    rootfs = tmp_path / "run/ems-appliance-manager/ab-inspect/root"
    target = (
        rootfs / ab_persistence.SLOT_SHARED_CONF_DIR.lstrip("/")
        / ab_persistence.SLOT_SHARED_CONF_NAME
    )
    target.write_text("Version=1\nPath=/opt/ems-solarflow\n", encoding="utf-8")

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert verdicts(report)["persistence_declared"] == FAIL


def test_a_rootfs_that_names_a_slot_is_refused(tmp_path, inspector):
    """image-rota writes one identical pair; a slot-pinned rootfs is a defect."""

    rootfs = tmp_path / "run/ems-appliance-manager/ab-inspect/root"
    (rootfs / "etc/ems-appliance-slot").write_text("A\n", encoding="utf-8")

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert verdicts(report)["slot_agnostic_rootfs"] == FAIL


def test_a_slot_without_kernel_modules_is_refused(tmp_path, inspector):
    import shutil

    shutil.rmtree(tmp_path / "run/ems-appliance-manager/ab-inspect/root/lib/modules")

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert verdicts(report)["kernel_modules"] == FAIL


def test_a_boot_filesystem_selecting_a_fixed_root_is_refused(tmp_path, inspector):
    """A cmdline naming one partition would pin the payload to one slot."""

    boot = tmp_path / "run/ems-appliance-manager/ab-inspect/boot"
    (boot / "cmdline.txt").write_text("root=PARTUUID=0000-0004 ro\n", encoding="utf-8")

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert verdicts(report)["boot_cmdline"] == FAIL


@pytest.mark.parametrize("asset", ["kernel8.img", "initramfs8", "bcm2711-rpi-4-b.dtb"])
def test_a_boot_filesystem_missing_a_boot_asset_is_refused(tmp_path, inspector, asset):
    (tmp_path / "run/ems-appliance-manager/ab-inspect/boot" / asset).unlink()

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert not report.ok


def test_a_filesystem_that_cannot_be_mounted_is_refused(tmp_path):
    build_slot(tmp_path)
    inspector = InactiveSlotInspector(runner=MountingRunner(mount_ok=False), root=tmp_path)

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert not report.ok
    assert verdicts(report)["root_mountable"] == FAIL


# --- leaks --------------------------------------------------------------------


def test_a_mount_that_could_not_be_undone_is_recorded_as_a_leak(tmp_path):
    build_slot(tmp_path)
    inspector = InactiveSlotInspector(runner=MountingRunner(umount_ok=False), root=tmp_path)

    report = inspector.inspect(AUTHORITY, RELEASE)

    assert not report.ok
    assert not report.cleaned
    assert len(report.leaked) == 2


def test_a_recorded_leak_blocks_the_next_inspection(tmp_path):
    build_slot(tmp_path)
    leaking = InactiveSlotInspector(runner=MountingRunner(umount_ok=False), root=tmp_path)
    leaking.inspect(AUTHORITY, RELEASE)

    healthy = InactiveSlotInspector(runner=MountingRunner(), root=tmp_path)

    with pytest.raises(InspectionError) as caught:
        healthy.inspect(AUTHORITY, RELEASE)

    assert caught.value.code == "inspection_mount_leaked"


def test_a_clean_run_leaves_no_leak_marker(tmp_path, inspector):
    inspector.inspect(AUTHORITY, RELEASE)

    inspector.assert_no_leak()


def test_the_report_is_serialisable(tmp_path, inspector):
    payload = json.loads(json.dumps(inspector.inspect(AUTHORITY, RELEASE).to_dict()))

    assert payload["target_slot"] == "B"
    assert payload["ok"] is True
