# SPDX-License-Identifier: AGPL-3.0-or-later
"""What must survive a slot switch, and how a host proves it will.

A shared path that silently fell back to the active root filesystem looks
completely healthy: the directory exists, the services start, everything works
— until the first slot switch, which loses every byte written since the image
was flashed. So the verifier never accepts existence as evidence; it requires
each required path to be backed by the persistent partition.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from appliance import ab_persistence
from tests.helpers.appliance_ab import (
    DEFAULT_OS_BUILD,
    DEVICE,
    PERSIST_MOUNT,
    SLOT_PREFIX,
    ApplianceAbHost,
)

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


@pytest.fixture
def host(tmp_path):
    return ApplianceAbHost(tmp_path)


def verify(host):
    status = host.discover()
    return ab_persistence.verify(status, host.mounts())


# --- the contract itself -----------------------------------------------------


def test_the_ems_installation_is_shared():
    targets = {shared.target for shared in ab_persistence.SHARED_PATHS}

    assert "/opt/ems-solarflow" in targets


def test_appliance_authentication_and_operation_state_are_shared():
    targets = {shared.target for shared in ab_persistence.SHARED_PATHS}

    assert "/var/lib/ems-appliance-manager" in targets
    assert "/etc/ems-appliance-manager" in targets


def test_the_backup_accounts_keys_are_shared():
    """The operator's SSH backup key lives in that home. On a slot-local /var it
    is gone at the next slot switch, and remote backup access with it."""

    targets = {shared.target for shared in ab_persistence.SHARED_PATHS}

    assert "/var/lib/ems-backup" in targets


def test_the_backup_home_is_declared_to_the_generator_that_mounts_it():
    conf = ab_persistence.slot_shared_conf()

    assert "Path=/var/lib/ems-backup" in conf


def test_network_profiles_are_shared():
    targets = {shared.target for shared in ab_persistence.SHARED_PATHS}

    assert "/etc/NetworkManager/system-connections" in targets


def test_host_identity_is_shared_without_sharing_the_distro_ssh_configuration():
    """Machine identity survives; each slot keeps its own sshd package state."""

    targets = {shared.target for shared in ab_persistence.SHARED_PATHS}

    assert "/etc/ssh" not in targets
    assert "/etc/ssh" in ab_persistence.SLOT_LOCAL_PATHS
    assert any(
        ab_persistence.SSH_HOST_KEY_DIRECTORY.startswith(target.rstrip("/") + "/")
        for target in targets
    )


def test_the_machine_identity_comes_from_the_shared_partition():
    identity = ab_persistence.contract()["machine_identity"]

    assert identity["source"] == ab_persistence.MACHINE_ID_SOURCE
    assert identity["owner"] == "rpi-image-gen"
    assert identity["stable_across_slots"] is True


def test_the_package_database_and_system_libraries_are_slot_local():
    for path in ("/var/lib/dpkg", "/usr", "/lib/modules", "/boot/firmware"):
        assert path in ab_persistence.SLOT_LOCAL_PATHS, path


def test_docker_engine_state_is_slot_local_by_decision():
    """A rollback must never hand a newer content store to an older engine."""

    assert "/var/lib/docker" in ab_persistence.SLOT_LOCAL_PATHS
    assert "/var/lib/docker" not in {
        shared.target for shared in ab_persistence.SHARED_PATHS
    }


def test_no_shared_path_is_a_parent_of_a_slot_local_path():
    for shared in ab_persistence.SHARED_PATHS:
        for local in ab_persistence.SLOT_LOCAL_PATHS:
            if local == "/":
                continue
            assert not local.startswith(shared.target.rstrip("/") + "/"), (shared.target, local)


def test_the_contract_is_serialisable_data():
    contract = ab_persistence.contract()

    assert contract["schema_version"] == ab_persistence.PERSISTENT_SCHEMA_VERSION
    assert {entry["target"] for entry in contract["shared"]}
    assert "/var/lib/docker" in contract["slot_local"]


# --- the verifier ------------------------------------------------------------


def test_a_healthy_appliance_verifies(host):
    report = verify(host)

    assert report.ok is True
    assert report.state == ab_persistence.STATE_OK
    assert report.mountpoint == PERSIST_MOUNT
    # image-rota mounts it through upstream's slot alias; the kernel reports
    # back whatever string was passed, so both names are the same partition.
    assert report.source == f"{SLOT_PREFIX}/persistent"
    assert all(entry["shared"] for entry in report.paths)


def test_a_missing_persistent_partition_fails_closed(host):
    host.unmount(PERSIST_MOUNT)

    report = verify(host)

    assert report.ok is False
    assert report.state == ab_persistence.STATE_MISSING
    assert "not mounted" in report.problems[0]


def test_a_persistent_partition_of_another_identity_fails(host):
    host.mount(PERSIST_MOUNT, "/dev/sda1")

    report = verify(host)

    assert report.ok is False
    assert report.state == ab_persistence.STATE_IDENTITY_MISMATCH


def test_a_read_only_persistent_partition_fails(host):
    host.mount(PERSIST_MOUNT, f"{DEVICE}p6", options=("ro", "relatime"))

    report = verify(host)

    assert report.ok is False
    assert report.state == ab_persistence.STATE_OPTIONS_UNEXPECTED


@pytest.mark.parametrize(
    "target",
    [
        "/opt/ems-solarflow",
        "/var/lib/ems-appliance-manager",
        "/etc/ems-appliance-manager",
        "/var/lib/ems-appliance-os-update",
    ],
)
def test_a_required_path_that_fell_back_to_the_root_filesystem_fails(host, target):
    host.unmount(target)

    report = verify(host)

    assert report.ok is False
    assert report.state == ab_persistence.STATE_PATH_NOT_SHARED
    assert any(target in problem for problem in report.problems)


def test_a_required_path_backed_by_the_wrong_device_fails(host):
    host.mount("/opt/ems-solarflow", "/dev/sda1", subtree="/shared/opt/ems-solarflow")

    report = verify(host)

    assert report.ok is False
    assert any("not from the persistent partition" in problem for problem in report.problems)


def test_a_required_path_exposing_the_wrong_subtree_fails(host):
    host.mount("/opt/ems-solarflow", f"{DEVICE}p6", subtree="/appliance/lib")

    report = verify(host)

    assert report.ok is False
    assert any("exposes /appliance/lib" in problem for problem in report.problems)


def test_an_image_declaring_another_persistent_schema_fails(host):
    from tests.helpers.appliance_ab import layout_manifest

    host.write_layout_manifest(layout_manifest() | {"persistent_schema_version": 7})

    report = verify(host)

    assert report.ok is False
    assert any("persistent schema 7" in problem for problem in report.problems)


def test_a_host_without_a_layout_reports_missing_rather_than_ok(host):
    host.remove_layout_manifest()
    status = host.discover()

    report = ab_persistence.verify(status, host.mounts())

    assert report.ok is False
    assert report.state == ab_persistence.STATE_MISSING


def test_the_report_survives_json(host):
    import json

    payload = json.loads(json.dumps(verify(host).to_dict()))

    assert payload["ok"] is True
    assert payload["problems"] == []


def test_the_resolved_device_path_is_accepted_as_well_as_the_slot_alias(host):
    """A persistent partition mounted by its real path is the same partition."""

    host.mount(PERSIST_MOUNT, f"{DEVICE}p6", options=("rw", "noatime"))

    report = verify(host)

    assert report.ok is True


# --- activating what upstream generates ---------------------------------------


def test_every_declared_path_has_an_activation_link_in_the_image():
    """Upstream generates six mount units and activates one of them.

    The units themselves are correct; only the pull-in is missing, so the image
    ships the wants entries by name. systemd resolves a wants entry by its file
    name, which is why a link into the generator directory is enough.
    """

    overlay = (
        ROOT
        / "packaging/appliance/image/layer/ems-appliance.rootfs-overlay"
        / "etc/systemd/system/local-fs.target.wants"
    )
    shipped = {path.name: os.readlink(path) for path in overlay.iterdir()}

    assert set(shipped) == set(ab_persistence.shared_mount_units())
    for unit, target in shipped.items():
        assert target == f"{ab_persistence.GENERATOR_UNIT_DIR}/{unit}"


GENERATOR_SCRIPT = (
    "image/gpt/ab_userdata/device/rootfs-overlay"
    "/usr/lib/systemd/system-generators/slot-shared-generator"
)
DISCOVERY_PATHS = (Path("/usr/share/rpi-image-gen"), Path("/opt/rpi-image-gen"))


def slot_mount_gate(tmp_path, *, with_checkout):
    """A project root laid out the way the gate's own discovery expects."""

    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/appliance-verify-slot-mounts.sh", root / "scripts")
    (root / "appliance").symlink_to(ROOT / "appliance")
    if with_checkout:
        generator = tmp_path / "rpi-image-gen" / GENERATOR_SCRIPT
        generator.parent.mkdir(parents=True)
        generator.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        generator.chmod(0o755)
    return subprocess.run(
        ["sh", str(root / "scripts/appliance-verify-slot-mounts.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "EMS_RPI_IMAGE_GEN": ""},
    )


def test_the_slot_mount_gate_finds_a_sibling_checkout_without_being_told(tmp_path):
    """A release run passes no --rpi-image-gen, and reported NOT RUN because of it.

    Its sibling gate discovered the same checkout in the same run, so the six
    shipped wants links went unproven while the report looked clean.
    """

    completed = slot_mount_gate(tmp_path, with_checkout=True)

    assert "rpi_image_gen_unavailable" not in completed.stderr


def test_the_slot_mount_gate_still_refuses_when_there_is_no_checkout(tmp_path):
    for path in DISCOVERY_PATHS:
        if path.is_dir():
            pytest.skip(f"{path} would satisfy discovery on this host")

    completed = slot_mount_gate(tmp_path, with_checkout=False)

    assert completed.returncode == 3
    assert "rpi_image_gen_unavailable" in completed.stderr


def test_the_persistence_unit_orders_itself_after_every_shared_mount():
    """RequiresMountsFor is the second, project-owned guarantee."""

    unit = (ROOT / "packaging/appliance/systemd/ems-appliance-persistence.service").read_text(
        encoding="utf-8"
    )
    declared = " ".join(
        line.partition("=")[2].strip()
        for line in unit.splitlines()
        if line.startswith("RequiresMountsFor=")
    )

    for shared in ab_persistence.SHARED_PATHS:
        assert shared.target in declared.split(), shared.target


def test_the_escaper_matches_systemd_for_every_shared_path():
    if shutil.which("systemd-escape") is None:
        pytest.skip("systemd-escape is not installed")

    for shared in ab_persistence.SHARED_PATHS:
        expected = subprocess.run(
            ["systemd-escape", "--path", shared.target],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert ab_persistence.escape_path(shared.target) == expected


@pytest.mark.parametrize(
    "path,unit",
    [
        ("/", "-"),
        ("/opt", "opt"),
        ("/a-b", "a\\x2db"),
        ("/.hidden", "\\x2ehidden"),
        ("/a/b c", "a-b\\x20c"),
    ],
)
def test_the_escaper_follows_the_systemd_rules(path, unit):
    assert ab_persistence.escape_path(path) == unit


def test_the_binds_are_measured_against_the_partition_the_mountpoint_uses(host):
    """One partition, one identity — for the mountpoint and for its binds.

    A real A/B guest mounted ``/persistent`` and all six shared paths from the
    same partition, and this said the mountpoint was fine and every bind was
    foreign. The layout descriptor named no resolvable device there, so the
    mountpoint check had nothing to compare against and skipped it while the
    bind check went on comparing against an alias set the running system had
    never used. Both must ask the same question of the same partition.
    """

    real = f"{DEVICE}p6"
    host.mount(PERSIST_MOUNT, real, options=("rw", "noatime"))
    for shared in ab_persistence.SHARED_PATHS:
        host.mount(shared.target, real, subtree=ab_persistence.shared_subtree(shared))

    report = ab_persistence.verify(host.discover(lsblk_ok=False), host.mounts())

    assert report.ok is True, report.problems
    assert report.state == ab_persistence.STATE_OK
    assert all(entry["shared"] for entry in report.paths)


def test_a_bind_from_another_partition_is_still_refused_without_a_resolved_device(host):
    """The tolerance above may not become a blanket acceptance."""

    real = f"{DEVICE}p6"
    host.mount(PERSIST_MOUNT, real, options=("rw", "noatime"))
    host.mount("/opt/ems-solarflow", "/dev/sda1", subtree="/shared/opt/ems-solarflow")

    report = ab_persistence.verify(host.discover(lsblk_ok=False), host.mounts())

    assert report.ok is False
    assert report.state == ab_persistence.STATE_PATH_NOT_SHARED
    assert any("/dev/sda1" in problem for problem in report.problems)


def test_a_mountpoint_of_another_identity_does_not_bless_its_binds(host):
    """A wrong partition cannot make itself the authority for its own binds."""

    host.mount(PERSIST_MOUNT, "/dev/sda1")
    for shared in ab_persistence.SHARED_PATHS:
        host.mount(shared.target, "/dev/sda1", subtree=ab_persistence.shared_subtree(shared))

    report = ab_persistence.verify(host.discover(), host.mounts())

    assert report.ok is False
    assert report.state == ab_persistence.STATE_IDENTITY_MISMATCH


def test_the_verifier_is_not_gated_on_a_file_inside_what_it_verifies():
    """The layout descriptor lives under /etc/ems-appliance-manager, which is
    one of the shared binds this unit exists to prove. Conditioning on it means
    a skipped bind silently skips the verifier too."""

    from appliance import ab_persistence

    root = Path(__file__).resolve().parents[1]
    unit = (
        root / "packaging/appliance/systemd/ems-appliance-persistence.service"
    ).read_text(encoding="utf-8")

    condition = [
        line.split("=", 1)[1]
        for line in unit.splitlines()
        if line.startswith("ConditionPathExists=")
    ]

    assert condition, "the unit states no condition"
    for path in condition:
        for shared in ab_persistence.SHARED_PATHS:
            assert not path.startswith(shared.target.rstrip("/") + "/"), path
            assert path != shared.target, path


# --- the image that has no persistence contract ------------------------------
#
# A single-slot image ships one writable root and no persistent partition, so
# there is nothing here for it to prove. It still writes the build marker the
# whole product reads for provenance, and that marker is what the unit's
# ConditionPathExists= is anchored on -- so without a positive answer here the
# verifier would refuse the boot of an image that is working exactly as built.


def test_a_single_slot_image_is_not_asked_to_prove_a_persistence_contract(host):
    host.remove_layout_manifest()
    host.write_os_build(dict(DEFAULT_OS_BUILD) | {"image_layer": "image-rpios"})

    report = verify(host)

    assert report.ok is True
    assert report.state == ab_persistence.STATE_NOT_APPLICABLE
    assert report.problems == ()


def test_an_ab_image_that_lost_its_layout_descriptor_still_fails_closed(host):
    """The descriptor lives inside a bind this unit verifies.

    Losing it is precisely the failure the unit exists to catch, so the marker
    naming an A/B image must not be softened by the single-slot answer above.
    """

    host.remove_layout_manifest()
    host.write_os_build(dict(DEFAULT_OS_BUILD) | {"image_layer": "image-rota"})

    report = verify(host)

    assert report.ok is False
    assert report.state == ab_persistence.STATE_MISSING


@pytest.mark.parametrize(
    "marker",
    [
        DEFAULT_OS_BUILD,
        dict(DEFAULT_OS_BUILD) | {"image_layer": ""},
        dict(DEFAULT_OS_BUILD) | {"image_layer": "image-somethingelse"},
    ],
    ids=["field-absent", "field-empty", "layer-unknown"],
)
def test_a_marker_that_does_not_name_a_known_image_fails_closed(host, marker):
    """Only a positive, recognised statement may lift the check.

    A marker written before this field was read says nothing about slots, and
    an appliance that says nothing is the one that most needs verifying.
    """

    host.remove_layout_manifest()
    host.write_os_build(marker)

    report = verify(host)

    assert report.ok is False
    assert report.state == ab_persistence.STATE_MISSING


def test_a_host_with_no_build_marker_at_all_fails_closed(host):
    host.remove_layout_manifest()
    (host.root / "etc/ems-appliance-os-build").unlink()

    report = verify(host)

    assert report.ok is False
    assert report.state == ab_persistence.STATE_MISSING


def test_every_shared_path_has_an_activation_the_inspector_checks():
    """The inspector's list was written by hand and was one short.

    /var/lib/ems-backup was missing from it, so an image that shipped without
    that .wants link passed inspection: the bind would never have been
    activated, and every backup written to it would have been lost at the first
    slot switch. The list is derived now, and this is what keeps it derived.
    """

    from appliance import ab_image

    expected = {shared.target for shared in ab_persistence.SHARED_PATHS}

    assert len(ab_image.SHARED_ACTIVATIONS) == len(expected)
    assert "var-lib-ems\\x2dbackup.mount" in ab_image.SHARED_ACTIVATIONS


def test_the_shipped_image_activates_exactly_the_paths_the_contract_declares():
    """Derivation is only useful if it matches the bytes the image carries."""

    from pathlib import Path

    from appliance import ab_image

    overlay = (
        Path(__file__).resolve().parents[1]
        / "packaging/appliance/image/layer/ems-appliance.rootfs-overlay"
        / ab_image.SHARED_ACTIVATION_DIRECTORY
    )
    shipped = sorted(entry.name for entry in overlay.iterdir())

    assert shipped == sorted(ab_image.SHARED_ACTIVATIONS)
