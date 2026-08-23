# SPDX-License-Identifier: AGPL-3.0-or-later
"""What has to be unchanged between planning a slot write and performing one.

Confirmation proves the operator saw a plan. These guards prove the *disk* is
still the one that plan was made against: between the two, a medium can be
swapped, a slot can be committed from elsewhere, or the layout can change under
the appliance. Three of them had never been executed, and the failure they
prevent is writing an OS image onto the wrong partition.
"""

import pytest

from appliance import ab_layout
from appliance.os_update import OsUpdateError, WriteAuthority

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


class _Manifest:
    def __init__(self, layout_id):
        self.layout_id = layout_id
        self.slot_schema_version = 1


class _Layout:
    def __init__(self, **fields):
        self.mode = ab_layout.MODE_AB
        self.may_mutate = True
        self.drift = []
        self.manifest = _Manifest(fields.get("layout_id", "ems-appliance-rota-v1"))
        self.active_slot = fields.get("active_slot", "A")
        self.inactive_slot = fields.get("inactive_slot", "B")
        self.device = fields.get("device", "/dev/mmcblk0")


def authority(**overrides):
    values = {
        "layout_id": "ems-appliance-rota-v1",
        "slot_schema_version": 1,
        "persistent_schema_version": 3,
        "device": "/dev/mmcblk0",
        "active_slot": "A",
        "target_slot": "B",
        "boot_device": "/dev/mmcblk0p3",
        "root_device": "/dev/mmcblk0p5",
        "boot_partuuid": "aaaa-0003",
        "root_partuuid": "aaaa-0005",
        "release_id": "r1",
        "build_id": "20260808-1",
        "artifact_digest": "sha256:" + "a" * 64,
        "boot_digest": "sha256:" + "b" * 64,
        "rootfs_digest": "sha256:" + "c" * 64,
        "boot_expanded_digest": "sha256:" + "d" * 64,
        "rootfs_expanded_digest": "sha256:" + "e" * 64,
        "boot_expanded_size": 1,
        "rootfs_expanded_size": 1,
        "boot_encoding": "sparse",
        "rootfs_encoding": "sparse",
        "hardware_profile": "rpi5",
    }
    values.update(overrides)
    return WriteAuthority(**values)


class _Service:
    """The revalidation on its own, with the disk it sees injected."""

    def __init__(self, layout):
        from appliance.os_update import OsUpdateService

        self._layout = layout
        self._revalidate = OsUpdateService._revalidate.__get__(self)
        self.inspector = type("I", (), {"assert_no_leak": lambda _s: None})()
        self.probe = object()

    def run(self, auth, monkeypatch):
        monkeypatch.setattr(ab_layout, "discover", lambda _probe: self._layout)
        return self._revalidate(auth)


def refusal(layout, monkeypatch, **overrides):
    with pytest.raises(OsUpdateError) as error:
        _Service(layout).run(authority(**overrides), monkeypatch)
    return error.value.code


# --- the three guards nothing executed ---------------------------------------


def test_a_layout_that_changed_after_the_plan_is_refused(monkeypatch):
    layout = _Layout(layout_id="some-other-layout")

    assert refusal(layout, monkeypatch) == "layout_changed"


def test_a_running_slot_that_changed_after_the_plan_is_refused(monkeypatch):
    """The appliance rebooted into the other slot between plan and apply, so
    the target is now the slot it is running from."""

    layout = _Layout(active_slot="B", inactive_slot="A")

    assert refusal(layout, monkeypatch) == "active_slot_changed"


def test_a_storage_device_that_changed_after_the_plan_is_refused(monkeypatch):
    """The medium was swapped. Writing the plan now writes it to another disk."""

    layout = _Layout(device="/dev/sda")

    assert refusal(layout, monkeypatch) == "device_changed"


def test_drift_is_refused_before_any_field_is_compared(monkeypatch):
    layout = _Layout()
    layout.may_mutate = False
    layout.drift = ["the selector points at partition 3"]

    assert refusal(layout, monkeypatch) == "layout_drift"
