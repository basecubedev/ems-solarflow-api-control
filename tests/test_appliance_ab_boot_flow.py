# SPDX-License-Identifier: AGPL-3.0-or-later
"""The whole A/B flow, across simulated reboots.

Each test here is one complete journey rather than one function: stage, arm,
reboot into the trial, judge it, and either commit or come back. The property
they all check is the same one, from different directions — before a verified
commit the default boot slot is exactly what it was, and after one the old slot
is the recorded rollback candidate.

The firmware is modelled, not emulated. A reboot reads the selector the
appliance actually wrote and boots the partition it names, consuming the
one-shot tryboot flag once. Whether a real Raspberry Pi bootloader behaves that
way is the physical gate in docs/appliance/ab-hardware-validation.md.
"""

import pytest

from appliance.ab_health import RESULT_HEALTHY, RESULT_UNHEALTHY
from appliance.ab_state import SlotRecord
from appliance.os_update import OsUpdateError
from tests.helpers.appliance_ab import BootFlowSimulator, FakeSystemd
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
    simulator.state.record_known_good(
        SlotRecord(slot="A", release_version="1.4.0", build_id="20260801-1")
    )
    return simulator


def stage(pi):
    operation, plan = pi.plan_update(RELEASE_ID)
    assert plan["blockers"] == [], plan["blockers"]
    return operation, pi.confirm(operation)


# --- A default → trial B → healthy → commit B --------------------------------


def test_a_healthy_update_commits_the_new_slot(pi):
    stage(pi)

    assert pi.selector().default_partition == 2, "the default slot must not move yet"
    assert pi.selector().tryboot_partition == 3

    assert pi.reboot(trial=True, build_id=NEW_BUILD) == "B"
    report = pi.health().evaluate()
    assert report.result == RESULT_HEALTHY

    result = pi.health().commit()

    assert result["committed"] is True
    assert pi.selector().default_partition == 3
    assert pi.selector().tryboot_partition == 2
    assert pi.reboot() == "B"
    assert pi.fallbacks == []


def test_after_a_commit_the_old_slot_is_the_recorded_rollback_candidate(pi):
    stage(pi)
    pi.reboot(trial=True, build_id=NEW_BUILD)

    pi.health().commit()

    history = pi.state.slots()
    assert history.known_good_slot == "B"
    assert history.previous_slot == "A"
    assert history.record("A").build_id == "20260801-1"
    assert history.record("B").build_id == NEW_BUILD


# --- A default → trial B → crash → fallback A --------------------------------


def test_a_trial_that_never_reaches_its_health_check_falls_back(pi):
    stage(pi)
    pi.reboot(trial=True, build_id=NEW_BUILD)

    # The trial slot crashes: nothing commits, and the one-shot flag is spent.
    assert pi.reboot() == "A"

    assert pi.selector().default_partition == 2
    assert len(pi.fallbacks) == 1
    assert pi.fallbacks[0].target_slot == "B"
    assert pi.fallbacks[0].source_slot == "A"


def test_a_fallback_is_not_retried_on_the_next_boot(pi):
    stage(pi)
    pi.reboot(trial=True, build_id=NEW_BUILD)
    pi.reboot()

    pi.reboot()

    assert len(pi.fallbacks) == 1
    assert pi.state.pending() is None


# --- A default → trial B → unhealthy → fallback A ----------------------------


def test_an_unhealthy_trial_never_commits_and_returns_to_the_old_default(pi):
    stage(pi)
    pi.reboot(trial=True, build_id=NEW_BUILD)
    systemd = FakeSystemd(active=())
    verified = pi.health(systemd=systemd)
    report = verified.evaluate()
    assert report.result == RESULT_UNHEALTHY

    verified.abandon(report)

    assert pi.selector().default_partition == 2
    assert systemd.reboots == 1
    assert pi.reboot() == "A"
    assert pi.state.slots().known_good_slot == "A"


def test_an_unhealthy_trial_leaves_the_operation_recoverable_not_committed(pi):
    stage(pi)
    pi.reboot(trial=True, build_id=NEW_BUILD)
    verified = pi.health(systemd=FakeSystemd(active=()))

    verified.abandon(verified.evaluate())
    pi.reboot()

    assert pi.state.pending() is None
    assert pi.fallbacks[-1].last_health["result"] == RESULT_UNHEALTHY


# --- B default → trial A → healthy → commit A --------------------------------


def test_the_next_update_runs_in_the_other_direction(tmp_path, releases):
    pi = BootFlowSimulator(tmp_path, releases)
    pi.state.record_known_good(SlotRecord(slot="A", build_id="20260801-1"))
    stage(pi)
    pi.reboot(trial=True, build_id=NEW_BUILD)
    pi.health().commit()
    pi.reboot()
    pi.service.operations.finish(
        pi.service.operations.list()[0].operation_id, "succeeded"
    )
    assert pi.host.slot == "B"

    releases.publish(
        "ems-solarflow-appliance-1.6.0-arm64-ab",
        manifest_overrides={"release_version": "1.6.0", "build_id": "20260808-1"},
    )
    operation = pi.service.operations.create("ab.update")
    plan = pi.service.plan_update(operation, "ems-solarflow-appliance-1.6.0-arm64-ab")
    assert plan["blockers"] == [], plan["blockers"]
    assert plan["current_slot"] == "B"
    assert plan["target_slot"] == "A"
    pi.confirm(operation)

    assert pi.selector().default_partition == 3
    assert pi.selector().tryboot_partition == 2
    assert pi.reboot(trial=True, build_id="20260808-1") == "A"
    pi.health().commit()

    assert pi.selector().default_partition == 2
    assert pi.state.slots().known_good_slot == "A"
    assert pi.state.slots().previous_slot == "B"


# --- manual rollback through a trial boot ------------------------------------


def test_a_manual_rollback_trial_boots_the_previous_slot_and_commits_it(pi):
    stage(pi)
    pi.reboot(trial=True, build_id=NEW_BUILD)
    pi.health().commit()
    pi.reboot()
    pi.service.operations.finish(
        pi.service.operations.list()[0].operation_id, "succeeded"
    )
    assert pi.host.slot == "B"

    operation, plan = pi.plan_rollback()

    assert plan["blockers"] == [], plan["blockers"]
    assert plan["kind"] == "rollback"
    assert plan["target_slot"] == "A"
    assert plan["target_build_id"] == "20260801-1"

    pi.service.backend.calls.clear()
    result = pi.confirm(operation)

    # A rollback writes nothing: the previous known-good slot is already there.
    assert result["stage"] == "tryboot_requested"
    assert [call for call in pi.service.backend.calls if call[0] == "write"] == []
    assert pi.selector().default_partition == 3
    assert pi.selector().tryboot_partition == 2

    assert pi.reboot(trial=True, build_id="20260801-1") == "A"
    report = pi.health().evaluate()
    assert report.result == RESULT_HEALTHY
    pi.health().commit()

    assert pi.selector().default_partition == 2
    assert pi.state.slots().known_good_slot == "A"


def test_a_rollback_without_a_recorded_previous_slot_is_refused(tmp_path, releases):
    pi = BootFlowSimulator(tmp_path, releases)
    operation = pi.service.operations.create("ab.rollback")

    with pytest.raises(Exception) as caught:
        pi.service.plan_rollback(operation)

    assert getattr(caught.value, "code", "") == "no_previous_known_good_slot"


def test_a_rollback_never_targets_the_running_slot(tmp_path, releases):
    pi = BootFlowSimulator(tmp_path, releases)
    pi.state.record_known_good(SlotRecord(slot="B", build_id="b-build"))
    pi.state.record_known_good(SlotRecord(slot="A", build_id="a-build"), previous_slot="A")
    operation = pi.service.operations.create("ab.rollback")

    with pytest.raises(Exception) as caught:
        pi.service.plan_rollback(operation)

    assert getattr(caught.value, "code", "") == "rollback_target_is_active"


def test_an_unhealthy_rollback_trial_returns_to_the_slot_it_started_from(pi):
    stage(pi)
    pi.reboot(trial=True, build_id=NEW_BUILD)
    pi.health().commit()
    pi.reboot()
    pi.service.operations.finish(
        pi.service.operations.list()[0].operation_id, "succeeded"
    )
    operation, _plan = pi.plan_rollback()
    pi.confirm(operation)
    pi.reboot(trial=True, build_id="20260801-1")
    verified = pi.health(systemd=FakeSystemd(active=()))

    verified.abandon(verified.evaluate())

    assert pi.selector().default_partition == 3
    assert pi.reboot() == "B"
    assert pi.state.slots().known_good_slot == "B"


# --- power loss --------------------------------------------------------------


def test_power_loss_before_the_trial_reboot_leaves_the_default_slot(tmp_path, releases):
    """The selector is armed and the pending trial written, then power is cut."""

    pi = BootFlowSimulator(tmp_path, releases)
    pi.state.record_known_good(SlotRecord(slot="A", build_id="20260801-1"))
    stage(pi)

    assert pi.reboot() == "A"

    assert pi.selector().default_partition == 2
    assert len(pi.fallbacks) == 1


def test_power_loss_during_the_inactive_write_leaves_the_default_slot(tmp_path, releases):
    pi = BootFlowSimulator(tmp_path, releases)
    pi.state.record_known_good(SlotRecord(slot="A", build_id="20260801-1"))
    pi.service.backend.fail_write_after = 8
    operation, plan = pi.plan_update(RELEASE_ID)
    assert plan["blockers"] == []

    with pytest.raises(OsUpdateError):
        pi.confirm(operation)

    assert pi.selector().default_partition == 2
    assert pi.state.pending() is None
    assert pi.reboot() == "A"
    assert pi.fallbacks == []


def test_an_interrupted_write_never_leaves_a_bootable_rollback_candidate(tmp_path, releases):
    pi = BootFlowSimulator(tmp_path, releases)
    pi.state.record_known_good(SlotRecord(slot="B", build_id="old-b"))
    pi.state.record_known_good(SlotRecord(slot="A", build_id="20260801-1"), previous_slot="B")
    pi.service.backend.fail_write_after = 8
    operation, _plan = pi.plan_update(RELEASE_ID)

    with pytest.raises(OsUpdateError):
        pi.confirm(operation)

    assert pi.state.slots().record("B") is None
    assert pi.state.slots().previous_slot == ""
