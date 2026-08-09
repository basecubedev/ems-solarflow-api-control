# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an operator keeps when the appliance changes root filesystems.

This is the test that would have caught the failure A/B exists to avoid: an
update that boots perfectly and has quietly lost the EMS configuration, the
appliance password, the SSH host identity or the WLAN profile, because those
lived in the slot that was just replaced.

The shared partition is modelled as a real directory tree and the bind mounts as
real mount records, so "did this survive" is answered by reading the file after
the slot switch rather than by trusting the contract.
"""

import json

import pytest

from appliance import ab_persistence
from appliance.ab_state import SlotRecord
from tests.helpers.appliance_ab import (
    PERSIST_MOUNT,
    SLOT_PREFIX,
    BootFlowSimulator,
)
from tests.helpers.appliance_ab_artifacts import ReleaseDirectory

pytestmark = [pytest.mark.unit, pytest.mark.simulation]

RELEASE_ID = "ems-solarflow-appliance-1.5.0-arm64-ab"
NEW_BUILD = "20260807-1"


@pytest.fixture
def releases(tmp_path):
    directory = ReleaseDirectory(tmp_path)
    directory.publish(RELEASE_ID, manifest_overrides={"build_id": NEW_BUILD})
    return directory


@pytest.fixture
def pi(tmp_path, releases):
    simulator = BootFlowSimulator(tmp_path, releases)
    simulator.state.record_known_good(SlotRecord(slot="A", build_id="20260801-1"))
    return simulator


# The operator-visible state each shared path stands for.
OPERATOR_STATE = {
    "persist/ems/config/config.json": '{"devices": [{"name": "inverter"}]}\n',
    "persist/ems/data/runtime-state.json": '{"winter_mode": true}\n',
    "persist/ems/backups/backup-2026-08-01.tar.gz": "an EMS backup\n",
    "persist/appliance/lib/web/auth/auth.json": '{"password_hash": "argon2"}\n',
    "persist/appliance/lib/agent/known-good/admin.json": '{"admin_version": "v0.8.0"}\n',
    "persist/appliance/etc/appliance.conf": "[appliance]\nweb_port = 8080\n",
    "persist/host/ssh/ssh_host_ed25519_key.pub": "ssh-ed25519 AAAAhostkey\n",
    "persist/network/system-connections/home.nmconnection": "[wifi]\nssid=home\n",
}


def seed(pi):
    for relative, payload in OPERATOR_STATE.items():
        path = pi.host.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    return OPERATOR_STATE


def switch_to_slot_b(pi):
    operation, plan = pi.plan_update(RELEASE_ID)
    assert plan["blockers"] == [], plan["blockers"]
    pi.confirm(operation)
    pi.reboot(trial=True, build_id=NEW_BUILD)
    pi.health().commit()
    return pi.reboot()


# --- data survives the switch ------------------------------------------------


def test_every_operator_visible_file_survives_a_slot_switch(pi):
    seed(pi)

    assert switch_to_slot_b(pi) == "B"

    for relative, payload in OPERATOR_STATE.items():
        assert (pi.host.root / relative).read_text(encoding="utf-8") == payload, relative


def test_the_ems_configuration_is_reachable_at_its_normal_path_after_the_switch(pi):
    seed(pi)
    switch_to_slot_b(pi)

    mounts = pi.host.mounts()
    entry = mounts["/opt/ems-solarflow"]

    assert entry["source"] == f"{SLOT_PREFIX}/persistent"
    assert entry["root"] == "/shared/opt/ems-solarflow"


def test_the_appliance_password_store_is_shared_not_slot_local(pi):
    seed(pi)
    switch_to_slot_b(pi)

    report = ab_persistence.verify(pi.host.discover(), pi.host.mounts())
    auth = next(
        entry for entry in report.paths if entry["target"] == "/var/lib/ems-appliance-manager"
    )

    assert auth["shared"] is True
    assert auth["source"].startswith(PERSIST_MOUNT)


def test_the_ssh_host_identity_is_shared_without_sharing_etc_ssh(pi):
    """The keys survive the switch; the distro's sshd configuration does not."""

    seed(pi)
    switch_to_slot_b(pi)

    report = ab_persistence.verify(pi.host.discover(), pi.host.mounts())
    targets = {item["target"] for item in report.paths}
    entry = next(
        item for item in report.paths if item["target"] == "/var/lib/ems-appliance-manager"
    )

    assert "/etc/ssh" not in targets
    assert ab_persistence.SSH_HOST_KEY_DIRECTORY.startswith(entry["target"] + "/")
    assert entry["shared"] is True


def test_the_network_profiles_are_shared(pi):
    seed(pi)
    switch_to_slot_b(pi)

    report = ab_persistence.verify(pi.host.discover(), pi.host.mounts())
    entry = next(
        item
        for item in report.paths
        if item["target"] == "/etc/NetworkManager/system-connections"
    )

    assert entry["shared"] is True


def test_the_ab_state_itself_survives_the_switch(pi):
    """The slot history has to be readable from the slot it now describes."""

    seed(pi)
    switch_to_slot_b(pi)

    history = pi.state.slots()
    assert history.known_good_slot == "B"
    assert history.previous_slot == "A"
    assert history.record("A").build_id == "20260801-1"


def test_data_written_in_the_new_slot_survives_a_rollback(pi):
    seed(pi)
    switch_to_slot_b(pi)
    written_in_b = pi.host.root / "persist/ems/data/written-in-slot-b.json"
    written_in_b.write_text('{"from": "slot B"}\n', encoding="utf-8")
    pi.service.operations.finish(pi.service.operations.list()[0].operation_id, "succeeded")

    operation, plan = pi.plan_rollback()
    assert plan["blockers"] == [], plan["blockers"]
    pi.confirm(operation)
    pi.reboot(trial=True, build_id="20260801-1")
    pi.health().commit()
    assert pi.reboot() == "A"

    assert written_in_b.read_text(encoding="utf-8") == '{"from": "slot B"}\n'
    for relative, payload in OPERATOR_STATE.items():
        assert (pi.host.root / relative).read_text(encoding="utf-8") == payload, relative


# --- what must not be shared -------------------------------------------------


def test_the_package_database_is_not_shared(pi):
    """A shared dpkg database would make one slot depend on the other's packages."""

    mounts = pi.host.mounts()

    assert "/var/lib/dpkg" not in mounts
    assert "/var/lib/dpkg" in ab_persistence.SLOT_LOCAL_PATHS


def test_docker_engine_state_is_not_shared(pi):
    mounts = pi.host.mounts()

    assert "/var/lib/docker" not in mounts


def test_the_ems_installation_is_shared_independently_of_docker(pi):
    """Application data survives even though the engine's store does not."""

    seed(pi)
    switch_to_slot_b(pi)

    assert (pi.host.root / "persist/ems/config/config.json").is_file()
    assert "/var/lib/docker" not in pi.host.mounts()


# --- fail closed -------------------------------------------------------------


def test_an_update_is_refused_while_the_shared_partition_is_missing(pi):
    seed(pi)
    pi.host.unmount(PERSIST_MOUNT)
    pi.service.probe = pi.host.probe()

    operation = pi.service.operations.create("ab.update")
    plan = pi.service.plan_update(operation, RELEASE_ID)

    assert "persistence_unavailable" in {entry["code"] for entry in plan["blockers"]}


def test_a_trial_slot_without_the_shared_partition_never_commits(pi):
    seed(pi)
    operation, _plan = pi.plan_update(RELEASE_ID)
    pi.confirm(operation)
    pi.reboot(trial=True, build_id=NEW_BUILD)
    pi.host.unmount(PERSIST_MOUNT)

    report = pi.health().evaluate()

    assert report.healthy is False
    assert pi.selector().default_partition == 2


def test_the_verifier_survives_json(pi):
    report = ab_persistence.verify(pi.host.discover(), pi.host.mounts())

    assert json.loads(json.dumps(report.to_dict()))["ok"] is True
