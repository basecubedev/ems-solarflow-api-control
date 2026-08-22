# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which slot is running, and may anything be written to the other one.

Every destructive step of an A/B update is bound to the answers here, so the
module has one job: never guess. A signal that disagrees with the others is
``layout_drift`` and disables mutation, and a host without an A/B layout is a
supported single-slot appliance rather than a broken A/B one.
"""

import json
import os
from dataclasses import replace

import pytest

from appliance import ab_layout
from appliance.ab_layout import LayoutError
from tests.helpers.appliance_ab import (
    DEVICE,
    PARTUUIDS,
    ApplianceAbHost,
    device_of,
    layout_manifest,
    lsblk_payload,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


@pytest.fixture
def host(tmp_path):
    return ApplianceAbHost(tmp_path)


# --- single slot and unsupported hosts ---------------------------------------


def test_a_host_without_a_layout_manifest_is_a_single_slot_appliance(host):
    host.remove_layout_manifest()

    status = host.discover()

    assert status.mode == ab_layout.MODE_SINGLE_SLOT
    assert status.ab_supported is False
    assert status.reason == ab_layout.REASON_NOT_PRESENT
    assert status.may_mutate is False


def test_a_layout_manifest_of_another_schema_is_unsupported(host):
    payload = layout_manifest() | {"schema_version": 99}
    host.write_layout_manifest(payload)

    status = host.discover()

    assert status.mode == ab_layout.MODE_UNSUPPORTED
    assert status.reason == ab_layout.REASON_MANIFEST_UNSUPPORTED


def test_a_layout_manifest_missing_a_slot_is_refused(host):
    payload = layout_manifest()
    del payload["slots"]["B"]
    host.write_layout_manifest(payload)

    status = host.discover()

    assert status.ab_supported is False
    assert status.mode == ab_layout.MODE_UNSUPPORTED


def test_two_layout_entries_may_not_claim_one_label(host):
    """image-rota mandates unique PARTLABELs; slot mapping depends on them."""

    payload = layout_manifest()
    payload["slots"]["B"]["root"]["label"] = payload["slots"]["A"]["root"]["label"]
    host.write_layout_manifest(payload)

    status = host.discover()

    assert status.ab_supported is False
    assert "same GPT label" in " ".join(status.drift)


def test_a_block_layer_that_cannot_be_read_is_never_an_empty_layout(host):
    status = host.discover(available=False)

    assert status.reason == ab_layout.REASON_BLOCK_LAYER_UNAVAILABLE
    assert status.may_mutate is False


# --- proving the active slot -------------------------------------------------


def test_a_healthy_appliance_proves_slot_a(host):
    status = host.discover()

    assert status.mode == ab_layout.MODE_AB
    assert status.ab_supported is True
    assert status.active_slot == "A"
    assert status.inactive_slot == "B"
    assert status.tryboot is False
    assert status.drift == ()
    assert status.may_mutate is True
    assert status.device == DEVICE


def test_a_healthy_appliance_proves_slot_b(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="B")
    host.write_selector(default=3, trial=2)

    status = host.discover()

    assert status.active_slot == "B"
    assert status.inactive_slot == "A"
    assert status.drift == ()


def test_a_trial_boot_is_reported_as_such(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="B", tryboot=True)

    status = host.discover()

    assert status.tryboot is True
    assert status.active_slot == "B"
    assert status.drift == ()


def test_the_slot_comes_from_the_firmware_not_from_the_mount_table(host):
    """The firmware booted partition 3; nothing may report slot A anyway."""

    host.boot_slot("B")
    host.write_selector(default=3, trial=2)
    host.mount_defaults()

    status = host.discover()

    assert status.active_slot == "B"


# --- drift -------------------------------------------------------------------


def test_a_root_partuuid_that_belongs_to_the_other_slot_is_drift(host):
    host._write("proc/cmdline", f"root=PARTUUID={PARTUUIDS['system_b']} ro\n")

    status = host.discover()

    assert status.ab_supported is False
    assert status.reason == ab_layout.REASON_DRIFT
    assert any("slot A owns" in problem for problem in status.drift)


def test_a_root_mounted_from_another_device_is_drift(host):
    host.mount("/", "/dev/sda9")

    status = host.discover()

    assert status.ab_supported is False
    assert any("/ is mounted from /dev/sda9" in problem for problem in status.drift)


def test_a_boot_partition_the_layout_does_not_know_is_drift(host):
    host.boot_partition_directly(6)

    status = host.discover()

    assert status.ab_supported is False
    assert any("not a slot boot partition" in problem for problem in status.drift)


def test_a_booted_partition_the_block_layer_does_not_report_is_drift(host):
    host.boot_partition_directly(7)

    status = host.discover()

    assert status.ab_supported is False
    assert any("does not report" in problem for problem in status.drift)


def test_a_selector_pointing_at_another_slot_than_the_one_that_booted_is_drift(host):
    host.write_selector(default=3, trial=2)

    status = host.discover()

    assert status.ab_supported is False
    assert any("selector points at partition 3" in problem for problem in status.drift)


def test_a_missing_partition_is_drift(host):
    host.set_block_devices(lsblk_payload(omit=("system_b",)))

    status = host.discover()

    assert status.ab_supported is False
    assert any("slot B root partition" in problem for problem in status.drift)


def test_a_slot_that_is_not_on_the_booted_medium_is_drift(host):
    payload = lsblk_payload()
    payload["blockdevices"][0]["children"][4]["pkname"] = "/dev/sda"
    host.set_block_devices(payload)

    status = host.discover()

    assert status.ab_supported is False
    assert any("not on the booted medium" in problem for problem in status.drift)


def test_a_second_appliance_medium_does_not_change_the_booted_slot(host):
    """Another appliance's card carries the same labels; it must be ignored."""

    host.attach_second_medium()

    status = host.discover()

    assert status.ab_supported is True
    assert status.device == DEVICE
    assert status.slot_devices("B").root.path == f"{DEVICE}p5"


def test_two_partitions_with_one_label_on_the_booted_medium_are_drift(host):
    payload = lsblk_payload()
    payload["blockdevices"][0]["children"][4]["partlabel"] = "system_a"
    host.set_block_devices(payload)

    status = host.discover()

    assert status.ab_supported is False
    assert any("more than once" in problem for problem in status.drift)


def test_an_unidentifiable_boot_medium_is_drift_not_a_guess(host):
    """Without a boot mode an ambiguous partition number must not be resolved."""

    host.attach_second_medium()
    host.set_boot_mode(0)

    status = host.discover()

    assert status.ab_supported is False
    assert any("does not identify the medium" in problem for problem in status.drift)


def test_an_unparsable_selector_is_drift_not_a_default(host):
    host.write_selector(default=2, trial=3, text="[all]\nboot_partition=2\n")

    status = host.discover()

    assert status.ab_supported is False
    assert any("boot selector is unusable" in problem for problem in status.drift)


def test_a_missing_boot_mount_is_drift(host):
    host.unmount("/boot/firmware")

    status = host.discover()

    assert status.ab_supported is False
    assert any("/boot/firmware is not mounted" in problem for problem in status.drift)


# --- proving the inactive slot -----------------------------------------------


def test_the_inactive_slot_is_the_one_the_layout_names(host):
    status = host.discover()

    inactive = ab_layout.prove_inactive_slot(status, host.mounts())

    assert inactive.slot == "B"
    assert inactive.boot.path == f"{DEVICE}p3"
    assert inactive.root.path == f"{DEVICE}p5"


def test_a_drifted_layout_never_yields_an_inactive_slot(host):
    host.boot_partition_directly(7)
    status = host.discover()

    with pytest.raises(LayoutError) as caught:
        ab_layout.prove_inactive_slot(status, host.mounts())

    assert caught.value.code == "layout_drift"


def test_an_inactive_root_mounted_writable_is_refused(host):
    status = host.discover()
    mounts = host.mount("/mnt/other", f"{DEVICE}p5", options=("rw",)).mounts()

    with pytest.raises(LayoutError) as caught:
        ab_layout.prove_inactive_slot(status, mounts)

    assert caught.value.code == "inactive_slot_mounted"


def test_an_inactive_root_mounted_read_only_is_allowed(host):
    status = host.discover()
    mounts = host.mount("/mnt/verify", f"{DEVICE}p5", options=("ro",)).mounts()

    assert ab_layout.prove_inactive_slot(status, mounts).slot == "B"


def test_an_inactive_slot_that_is_the_running_root_is_refused(host):
    status = host.discover()
    mounts = dict(host.mounts())
    mounts["/"] = dict(mounts["/"]) | {"source": f"{DEVICE}p5"}

    with pytest.raises(LayoutError) as caught:
        ab_layout.prove_inactive_slot(status, mounts)

    assert caught.value.code == "inactive_slot_is_active"


def test_an_inactive_slot_on_another_device_is_refused(host):
    """prove_inactive_slot is the last gate before a partition is opened."""

    status = host.discover()
    foreign = status.slot_devices("B")
    status = ab_layout.LayoutStatus(
        mode=status.mode,
        ab_supported=True,
        reason=ab_layout.REASON_SUPPORTED,
        manifest=status.manifest,
        active_slot="A",
        inactive_slot="B",
        slots={
            "A": status.slot_devices("A"),
            "B": ab_layout.SlotDevices(
                slot="B",
                boot=replace(foreign.boot, parent="/dev/sda"),
                root=replace(foreign.root, parent="/dev/sda"),
            ),
        },
    )

    with pytest.raises(LayoutError) as caught:
        ab_layout.prove_inactive_slot(status, host.mounts())

    assert caught.value.code == "inactive_slot_foreign_device"


# --- the status projection ---------------------------------------------------


def test_the_status_dictionary_names_no_secret_and_survives_json(host):
    payload = host.discover().to_dict()

    assert json.loads(json.dumps(payload))["active_slot"] == "A"
    assert payload["may_mutate"] is True
    assert payload["layout_id"]
    assert payload["selector"]["default_partition"] == 2
    assert payload["selector"]["tryboot_partition"] == 3


def test_relative_slot_links_are_read_as_the_devices_they_name(host):
    """udev writes ``/dev`` symlinks relative to their own directory.

    Comparing that text against an absolute device path reports drift on every
    boot of a real appliance and disables all A/B mutation.
    """

    link = host.root / "dev/disk/by-slot/active/system"
    assert not os.path.isabs(os.readlink(link))

    probe = ab_layout.LayoutProbe(root=host.root, runner=host.runner())
    links = probe.slot_links(probe.manifest())

    assert links["active/system"] == device_of("system_a")
    assert links["persistent"] == device_of("persistent")


# --- the firmware has to be able to do tryboot at all -------------------------


def test_a_bootloader_older_than_tryboot_is_named(tmp_path):
    """Without tryboot the firmware ignores the selector and boots the default.

    The appliance then writes the slot, asks for a trial, gets an ordinary boot
    of the old slot, and reports "fallback observed" -- correct, and for a
    reason no diagnostic names.
    """

    host = ApplianceAbHost(tmp_path, slot="A")
    host.set_bootloader_build_date(ab_layout.MINIMUM_BOOTLOADER_BUILD_DATE - 86_400)

    assert ab_layout.bootloader_too_old(host.probe()) is True


def test_a_current_bootloader_is_accepted(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="A")
    host.set_bootloader_build_date(ab_layout.MINIMUM_BOOTLOADER_BUILD_DATE + 86_400)

    assert ab_layout.bootloader_too_old(host.probe()) is False


def test_an_unreadable_bootloader_date_is_unknown_not_a_refusal(tmp_path):
    """A probe that cannot read the firmware must not block every appliance."""

    host = ApplianceAbHost(tmp_path, slot="A")

    assert ab_layout.bootloader_too_old(host.probe()) is None


def test_a_usb_booted_appliance_is_disambiguated_by_its_bus(tmp_path):
    """Boot mode 4 names a bus, not a device: USB enumerates in attach order.

    The hardware procedure asks the tester to attach a second appliance medium,
    which is exactly when an undisambiguated partition number goes ambiguous.
    """

    from appliance.ab_layout import BOOT_MODE_USB, _booted_partition

    class _Probe:
        def boot_mode(self):
            return BOOT_MODE_USB

    class _Entry:
        def __init__(self, path, parent, number):
            self.path, self.parent, self.number = path, parent, number

    partitions = [
        _Entry("/dev/mmcblk0p3", "/dev/mmcblk0", 3),
        _Entry("/dev/sda3", "/dev/sda", 3),
    ]

    entry, problem = _booted_partition(_Probe(), partitions, 3)

    assert problem == ""
    assert entry.path == "/dev/sda3"
