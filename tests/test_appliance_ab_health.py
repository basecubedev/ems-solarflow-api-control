# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a trial slot has to prove, and what happens when it cannot.

The commit is the one moment the appliance stops being able to fall back
automatically, so the authority for it is checked from every side here: only a
booted slot commits itself, only under tryboot, only when the pending operation
names it, and only after every required health gate passed. A trial that cannot
prove those things becomes manual_action_required — never a commit, and never a
silent pass.
"""

import pytest

from appliance import ab_docker_health, ab_health
from appliance.ab_boot import parse_selector
from appliance.ab_health import (
    RESULT_HEALTHY,
    RESULT_MANUAL_ACTION_REQUIRED,
    RESULT_NOT_A_TRIAL,
    RESULT_UNHEALTHY,
    AbHealthError,
)
from appliance.ab_state import AbStateStore, PendingTrial, SlotRecord
from tests.helpers.appliance_ab import (
    ApplianceAbHost,
    FakeDocker,
    FakeSystemd,
    PARTUUIDS,
    build_health_service,
)

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]


@pytest.fixture
def host(tmp_path):
    """An appliance that booted slot B under tryboot after an update wrote it."""

    host = ApplianceAbHost(tmp_path, slot="B", tryboot=True)
    host.write_os_build(
        {
            "release_version": "1.5.0",
            "build_id": "20260807-1",
            "layout_id": "ems-appliance-rota-v1",
            "persistent_schema_version": 1,
            "slot_schema_version": 1,
        }
    )
    return host


@pytest.fixture
def state(host):
    store = AbStateStore(host.ab_state_dir)
    store.ensure()
    return store


def pending(**overrides):
    values = {
        "operation_id": "op-1",
        "source_slot": "A",
        "target_slot": "B",
        "target_release": "ems-solarflow-appliance-1.5.0-arm64-ab",
        "target_build_id": "20260807-1",
        "artifact_digest": "sha256:" + "c" * 64,
        "expected_boot_partition": 3,
        "expected_root_partuuid": PARTUUIDS["system_b"],
        "trial_requested_at": 1000.0,
        "release_version": "1.5.0",
        "boot_digest": "sha256:" + "d" * 64,
        "rootfs_digest": "sha256:" + "e" * 64,
    }
    values.update(overrides)
    return PendingTrial(**values)


def service(host, state, **kwargs):
    kwargs.setdefault("time_fn", lambda: 1100.0)
    return build_health_service(host, state, **kwargs)


class Clock:
    """A clock only the settle loop moves, so no test waits on real time."""

    def __init__(self, start=1100.0):
        self.now = start
        self.slept = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


class ColdStartDocker(FakeDocker):
    """An Admin container that answers only from the nth probe onward."""

    def __init__(self, *, probes_before_ready=3, **kwargs):
        super().__init__(**kwargs)
        self.probes = 0
        self.probes_before_ready = probes_before_ready

    def admin_runtime(self, expected_digest):
        self.probes += 1
        if self.probes < self.probes_before_ready:
            return ab_docker_health.failed(
                "admin_http_unhealthy", "the Admin container is still starting"
            )
        return ab_docker_health.passed("the Admin container answers")


# --- trial detection ---------------------------------------------------------


def test_a_trial_boot_with_a_matching_pending_operation_is_healthy(host, state):
    state.set_pending(pending())

    report = service(host, state).evaluate()

    assert report.result == RESULT_HEALTHY
    assert report.slot == "B"
    assert report.tryboot is True
    assert report.operation_id == "op-1"


def test_an_ordinary_boot_is_not_a_trial(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="A", tryboot=False)
    store = AbStateStore(host.ab_state_dir)
    store.ensure()

    report = service(host, store).evaluate()

    assert report.result == RESULT_NOT_A_TRIAL
    assert report.tryboot is False


def test_a_tryboot_without_a_pending_operation_needs_an_operator(host, state):
    report = service(host, state).evaluate()

    assert report.result == RESULT_MANUAL_ACTION_REQUIRED
    assert any("no A/B operation is pending" in reason for reason in report.reasons)


def test_a_trial_of_another_slot_than_the_pending_one_needs_an_operator(host, state):
    state.set_pending(pending(target_slot="A", expected_boot_partition=2))

    report = service(host, state).evaluate()

    assert report.result == RESULT_MANUAL_ACTION_REQUIRED


def test_a_slot_reporting_another_build_than_the_trial_wrote_needs_an_operator(host, state):
    state.set_pending(pending(target_build_id="some-other-build"))

    report = service(host, state).evaluate()

    assert report.result == RESULT_MANUAL_ACTION_REQUIRED
    assert any("this slot reports build" in reason for reason in report.reasons)


def test_a_trial_whose_root_filesystem_is_not_the_target_needs_an_operator(host, state):
    state.set_pending(pending(expected_root_partuuid=PARTUUIDS["system_a"]))

    report = service(host, state).evaluate()

    assert report.result == RESULT_MANUAL_ACTION_REQUIRED


def test_an_already_committed_trial_is_not_committed_twice(host, state):
    state.set_pending(pending(committed=True))

    report = service(host, state).evaluate()

    assert report.result == RESULT_MANUAL_ACTION_REQUIRED


# --- health gates ------------------------------------------------------------


def failed_gates(report):
    return {gate.name for gate in report.gates if gate.required and not gate.passed}


def test_a_missing_persistent_partition_fails_the_trial(host, state):
    state.set_pending(pending())
    host.unmount("/persistent")

    report = service(host, state).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert "persistent_partition" in failed_gates(report)


def test_a_shared_path_that_fell_back_to_the_rootfs_fails_the_trial(host, state):
    state.set_pending(pending())
    host.unmount("/opt/ems-solarflow")

    report = service(host, state).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert "persistent_paths" in failed_gates(report)


def test_a_lost_ems_installation_fails_the_trial(host, state):
    state.set_pending(pending())
    (host.root / "opt/ems-solarflow/config").rmdir()

    report = service(host, state).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert "ems_data_present" in failed_gates(report)


def test_a_stopped_appliance_agent_fails_the_trial(host, state):
    state.set_pending(pending())

    report = service(host, state, systemd=FakeSystemd(active=())).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert "unit_active:ems-appliance-agent.service" in failed_gates(report)


def test_an_unusable_agent_socket_fails_the_trial(host, state):
    state.set_pending(pending())

    report = service(host, state, agent_socket=lambda: False).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert "agent_socket" in failed_gates(report)


def test_a_failing_verify_install_fails_the_trial(host, state):
    state.set_pending(pending())

    report = service(host, state, install_check=lambda: False).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert "verify_install" in failed_gates(report)


def test_a_missing_host_identity_fails_the_trial(host, state):
    state.set_pending(pending())
    (host.root / "persistent/common/etc/machine-id").unlink()

    report = service(host, state).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert "machine_identity" in failed_gates(report)


def test_a_slot_that_invented_its_own_machine_identity_fails_the_trial(host, state):
    """One physical appliance stays one machine across a slot switch."""

    state.set_pending(pending())
    (host.root / "etc/machine-id").write_text("ffffffffffffffffffffffffffffffff\n")

    report = service(host, state).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert "machine_identity" in failed_gates(report)


def test_an_unreachable_docker_daemon_fails_the_trial_when_docker_is_configured(host, state):
    state.set_pending(pending())

    report = service(host, state, docker=FakeDocker(daemon=False)).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert "docker_usable" in failed_gates(report)


def test_a_slot_without_a_recoverable_admin_runtime_fails_the_trial(host, state):
    """A slot an operator cannot reach the Admin console from is not known-good."""

    state.set_pending(pending())

    report = service(host, state, docker=FakeDocker(admin=False)).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert "admin_runtime" in failed_gates(report)


def test_a_host_without_docker_configured_is_not_failed_for_it(host, state):
    """An appliance with no container runtime has no Admin runtime to recover."""

    state.set_pending(pending())

    report = service(host, state, docker=None).evaluate()

    assert report.result == RESULT_HEALTHY


def test_a_trial_that_exceeded_its_window_is_unhealthy(host, state):
    state.set_pending(pending())
    host.set_uptime(9_999.0)

    report = service(host, state, health_window_seconds=200).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert any("window" in reason for reason in report.reasons)


def test_the_window_is_measured_from_this_boot_not_across_the_reboot(host, state):
    """The board has no RTC. After a cold boot the clock can sit behind the
    stamp the trial was requested with, and a wall-clock difference is then
    negative: the window never expires and a stuck trial stays pending."""

    state.set_pending(pending(trial_requested_at=5_000_000.0))
    host.set_uptime(9_999.0)

    report = service(host, state, time_fn=lambda: 1_000.0, health_window_seconds=200).evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert any("window" in reason for reason in report.reasons)


def test_a_long_pre_reboot_stamp_does_not_shorten_the_window(host, state):
    state.set_pending(pending(trial_requested_at=1.0))
    host.set_uptime(30.0)

    report = service(
        host, state, time_fn=lambda: 5_000_000.0, health_window_seconds=200
    ).evaluate()

    assert report.result == RESULT_HEALTHY


def test_the_window_outlasts_every_unit_ordered_before_the_health_check():
    """A window shorter than that chain rolls back slots that are simply slow."""

    import re
    from pathlib import Path as _Path

    from appliance import config as appliance_config

    units = _Path(__file__).resolve().parents[1] / "packaging" / "appliance" / "systemd"

    def start_timeout(name):
        match = re.search(
            r"^TimeoutStartSec=(\d+)", (units / name).read_text(encoding="utf-8"), re.M
        )
        assert match, f"{name} declares no TimeoutStartSec"
        return int(match.group(1))

    budget = start_timeout("ems-appliance-persistence.service") + start_timeout(
        "ems-appliance-slot-bootstrap.service"
    )

    assert appliance_config.DEFAULT_AB_HEALTH_WINDOW > budget
    assert ab_health.DEFAULT_HEALTH_WINDOW_SECONDS > budget

    # systemd must not kill the verdict before the settle budget is spent:
    # a slot abandoned for being slow is the failure this whole file guards.
    assert start_timeout("ems-appliance-ab-health.service") > ab_health.DEFAULT_SETTLE_SECONDS


def test_the_health_window_is_never_shorter_than_the_documented_floor(host, state):
    verified = service(host, state, health_window_seconds=5)

    assert verified.health_window_seconds == ab_health.MIN_HEALTH_WINDOW_SECONDS


# --- commit ------------------------------------------------------------------


def test_a_healthy_trial_commits_itself_and_keeps_the_old_slot_as_fallback(host, state):
    state.set_pending(pending())
    verified = service(host, state)

    result = verified.commit()

    assert result["committed"] is True
    assert result["slot"] == "B"
    assert result["previous_slot"] == "A"
    selector = parse_selector(host.selector_path().read_text(encoding="utf-8"))
    assert selector.default_partition == 3
    assert selector.tryboot_partition == 2


def test_a_commit_records_the_slot_its_build_and_its_digests(host, state):
    state.set_pending(pending())

    service(host, state).commit()

    history = state.slots()
    assert history.known_good_slot == "B"
    assert history.previous_slot == "A"
    record = history.record("B")
    assert record.build_id == "20260807-1"
    assert record.release_version == "1.5.0"
    assert record.artifact_digest.startswith("sha256:")
    assert record.health["result"] == RESULT_HEALTHY


def test_a_commit_marks_the_pending_trial_committed(host, state):
    state.set_pending(pending())

    service(host, state).commit()

    assert state.pending().committed is True


@pytest.mark.parametrize(
    "break_it",
    [
        lambda h, s: s.set_pending(pending(target_slot="A", expected_boot_partition=2)),
        lambda h, s: h.unmount("/persistent"),
        lambda h, s: h.boot_slot("B", tryboot=False),
    ],
    ids=["wrong_slot", "no_persistence", "not_a_trial"],
)
def test_an_unauthorised_commit_never_touches_the_selector(host, state, break_it):
    state.set_pending(pending())
    before = host.selector_path().read_text(encoding="utf-8")
    break_it(host, state)

    with pytest.raises(AbHealthError) as caught:
        service(host, state).commit()

    assert caught.value.code == "commit_not_authorised"
    assert host.selector_path().read_text(encoding="utf-8") == before


def test_a_slot_that_is_not_the_trial_slot_cannot_commit_it(tmp_path):
    """Slot A must never commit a trial of slot B on its behalf."""

    host = ApplianceAbHost(tmp_path, slot="A", tryboot=False)
    store = AbStateStore(host.ab_state_dir)
    store.ensure()
    store.set_pending(pending())
    before = host.selector_path().read_text(encoding="utf-8")

    with pytest.raises(AbHealthError):
        service(host, store).commit()

    assert host.selector_path().read_text(encoding="utf-8") == before


# --- health failure ----------------------------------------------------------


def test_an_unhealthy_trial_asks_for_an_ordinary_reboot_and_changes_nothing(host, state):
    state.set_pending(pending())
    systemd = FakeSystemd(active=())
    verified = service(host, state, systemd=systemd)
    report = verified.evaluate()
    before = host.selector_path().read_text(encoding="utf-8")

    outcome = verified.abandon(report)

    assert outcome["rebooted"] is True
    assert systemd.reboots == 1
    assert host.selector_path().read_text(encoding="utf-8") == before
    assert state.pending().committed is False


def test_an_unhealthy_trial_records_what_it_saw(host, state):
    state.set_pending(pending())
    verified = service(host, state, systemd=FakeSystemd(active=()))

    verified.abandon(verified.evaluate())

    fallback = state.last_fallback()
    assert fallback.operation_id == "op-1"
    assert fallback.target_slot == "B"
    assert fallback.last_health["result"] == RESULT_UNHEALTHY


def test_a_reboot_that_is_refused_becomes_manual_action_required(host, state):
    state.set_pending(pending())
    systemd = FakeSystemd(active=())
    systemd.reboot_ok = False
    verified = service(host, state, systemd=systemd)

    with pytest.raises(AbHealthError) as caught:
        verified.abandon(verified.evaluate())

    assert caught.value.code == "manual_action_required"


# --- automatic fallback ------------------------------------------------------


def test_the_source_slot_booting_with_an_uncommitted_trial_is_a_fallback(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="A", tryboot=False)
    store = AbStateStore(host.ab_state_dir)
    store.ensure()
    store.set_pending(pending())

    record = ab_health.classify_fallback(host.probe(), store, time_fn=lambda: 2000.0)

    assert record is not None
    assert record.source_slot == "A"
    assert record.target_slot == "B"
    assert record.target_build_id == "20260807-1"
    assert record.observed_at == 2000.0


def test_a_fallback_is_never_retried_automatically(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="A", tryboot=False)
    store = AbStateStore(host.ab_state_dir)
    store.ensure()
    store.set_pending(pending())

    ab_health.classify_fallback(host.probe(), store)

    assert store.pending() is None
    assert ab_health.classify_fallback(host.probe(), store) is None


def test_a_committed_trial_is_not_a_fallback(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="B", tryboot=False)
    store = AbStateStore(host.ab_state_dir)
    store.ensure()
    store.set_pending(pending(committed=True))

    assert ab_health.classify_fallback(host.probe(), store) is None


def test_booting_the_target_slot_normally_is_not_a_fallback(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="B", tryboot=False)
    host.write_selector(default=3, trial=2)
    store = AbStateStore(host.ab_state_dir)
    store.ensure()
    store.set_pending(pending())

    assert ab_health.classify_fallback(host.probe(), store) is None


def test_a_trial_boot_is_not_yet_a_fallback(host, state):
    state.set_pending(pending())

    assert ab_health.classify_fallback(host.probe(), state) is None


def test_a_fallback_names_the_current_known_good_slot(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="A", tryboot=False)
    store = AbStateStore(host.ab_state_dir)
    store.ensure()
    store.record_known_good(SlotRecord(slot="A", build_id="20260801-1"))
    store.set_pending(pending())

    record = ab_health.classify_fallback(host.probe(), store)

    assert record.known_good_slot == "A"


# --- settling a cold-started deployment --------------------------------------


def test_a_cold_started_deployment_is_given_time_before_it_is_abandoned(host, state):
    """The regression: the gates fire seconds after `compose up -d` returned.

    A single sample taken then says nothing about the slot, only that the
    containers have not finished starting. Rolling back on it means an OS update
    can never succeed on a board slower than the builder.
    """

    state.set_pending(pending())
    clock = Clock()
    docker = ColdStartDocker(probes_before_ready=3)

    report = service(
        host, state, docker=docker, time_fn=clock.time
    ).evaluate_settled(sleep=clock.sleep)

    assert report.result == RESULT_HEALTHY
    assert docker.probes == 3
    assert clock.slept > 0


def test_a_deployment_that_never_answers_is_still_abandoned(host, state):
    state.set_pending(pending())
    clock = Clock()

    report = service(
        host, state, docker=FakeDocker(admin=False), time_fn=clock.time
    ).evaluate_settled(sleep=clock.sleep)

    assert report.result == RESULT_UNHEALTHY
    assert clock.slept >= ab_health.DEFAULT_SETTLE_SECONDS


def test_settling_never_outlives_the_health_window(host, state):
    """The window is the outer bound; settling may not spend past it."""

    state.set_pending(pending())
    host.set_uptime(150.0)
    clock = Clock()

    report = service(
        host,
        state,
        docker=FakeDocker(admin=False),
        time_fn=clock.time,
        health_window_seconds=200,
    ).evaluate_settled(sleep=clock.sleep)

    assert report.result == RESULT_UNHEALTHY
    assert clock.slept <= 50


def test_an_exceeded_window_is_a_verdict_and_is_not_retried(host, state):
    """Every gate passed; only the window failed. Retrying just burns it."""

    state.set_pending(pending())
    host.set_uptime(9_999.0)
    clock = Clock()

    report = service(
        host, state, time_fn=clock.time, health_window_seconds=200
    ).evaluate_settled(sleep=clock.sleep)

    assert report.result == RESULT_UNHEALTHY
    assert any("window" in reason for reason in report.reasons)
    assert clock.slept == 0


def test_a_boot_that_is_not_a_trial_is_not_retried(tmp_path):
    host = ApplianceAbHost(tmp_path, slot="A", tryboot=False)
    store = AbStateStore(host.ab_state_dir)
    store.ensure()
    clock = Clock()

    report = service(host, store, time_fn=clock.time).evaluate_settled(sleep=clock.sleep)

    assert report.result == RESULT_NOT_A_TRIAL
    assert clock.slept == 0
