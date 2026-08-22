# SPDX-License-Identifier: AGPL-3.0-or-later
"""What has to hold when the happy path does not happen.

Three separate questions, all of which decide whether an appliance an operator
cannot physically reach is still reachable afterwards:

- every way one service of the recorded application can fail to come back, and
  whether the slot commits anyway. It must not, it must fall back to the
  previous known-good slot, and the shared user data must be untouched either
  way. A stable reason has to say which one failed.

- a crash at each ordering point of the update. Before the OS commit the
  previous slot stays the fallback; after the selector commit the selector is
  authoritative and recovery reconciles the operation record against it. What
  must never exist is two slots both recorded as known-good.

- the seed on the persistent partition, which is bounded storage. It is pruned
  on the paths that end a trial and never on the path that still needs it.

Rollback is the case worth being explicit about: rolling the OS slot back does
not roll the EMS deployment back. Config, data and the compose file are shared,
so slot A returning after slot B was active reconstructs against the *current*
deployment. That is the correct behaviour and the reason the fingerprint is
re-verified rather than assumed.
"""

import pytest

from appliance import ab_bootstrap, ab_health
from appliance.ab_state import FallbackRecord, SlotRecord
from tests.helpers.appliance_deployment import (
    ADMIN_CONTAINER,
    ADMIN_REFERENCE,
    EMS_CONTAINER,
    INFLUX_CONTAINER,
    INFLUX_REFERENCE,
    Image,
    TrialAppliance,
    bootstrap_service,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]


def unhealthy(url, timeout):
    return 502, ""


def seed_of(appliance, role):
    return appliance.store.seed_directory / f"{role}.tar"


def failed_gates(report):
    return [gate.name for gate in report.gates if gate.required and not gate.passed]


# --- phase 28: the service-level failure matrix ------------------------------


def test_a_missing_admin_seed_with_no_registry_does_not_commit(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    seed_of(appliance, ab_bootstrap.ROLE_ADMIN).unlink()
    appliance.target.registry.pop(ADMIN_REFERENCE)

    report = appliance.reconstruct()
    health = appliance.health()

    assert not report.ok
    assert report.code == ab_bootstrap.RECONSTRUCTION_INCOMPLETE
    assert health.result == ab_health.RESULT_UNHEALTHY
    assert "admin_runtime" in failed_gates(health)


def test_a_missing_influx_seed_with_no_registry_does_not_commit(tmp_path):
    appliance = TrialAppliance(tmp_path, influx=True)
    record = appliance.capture()
    appliance.arm(record)
    seed_of(appliance, ab_bootstrap.ROLE_INFLUXDB).unlink()
    appliance.target.registry.pop(INFLUX_REFERENCE)

    appliance.reconstruct()
    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert "influxdb_runtime" in failed_gates(health)


def test_a_registry_that_is_unavailable_is_survived_by_the_seed(tmp_path):
    """No WAN is not a broken slot. The seed is what makes that true."""

    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.target.registry.clear()

    report = appliance.reconstruct()
    health = appliance.health()

    assert report.ok, report.problems
    assert appliance.target.pulled == []
    assert health.result == ab_health.RESULT_HEALTHY, health.reasons


def test_an_unhealthy_admin_endpoint_does_not_commit(tmp_path):
    appliance = TrialAppliance(tmp_path)

    _report, health = appliance.trial(http_probe=unhealthy)

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert "admin_runtime" in failed_gates(health)


def test_an_unhealthy_ems_container_does_not_commit(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    appliance.target.containers[EMS_CONTAINER]["health"] = "unhealthy"

    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert "ems_runtime" in failed_gates(health)


def test_an_unhealthy_influx_container_does_not_commit(tmp_path):
    appliance = TrialAppliance(tmp_path, influx=True)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    appliance.target.containers[INFLUX_CONTAINER]["health"] = "unhealthy"

    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert "influxdb_runtime" in failed_gates(health)


def test_an_image_for_the_wrong_platform_does_not_commit(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    seed_of(appliance, ab_bootstrap.ROLE_ADMIN).unlink()
    appliance.target.registry[ADMIN_REFERENCE] = Image(
        ADMIN_REFERENCE, ADMIN_REFERENCE.partition("@")[2], architecture="amd64"
    )

    report = appliance.reconstruct()
    health = appliance.health()

    assert not report.ok
    assert health.result == ab_health.RESULT_UNHEALTHY


def test_compose_drift_does_not_commit_and_touches_no_container(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.deployment.mutate_compose()

    report = appliance.reconstruct()
    health = appliance.health()

    assert report.code == ab_bootstrap.DEPLOYMENT_AUTHORITY_DRIFT
    assert appliance.target.mutations == []
    assert health.result == ab_health.RESULT_UNHEALTHY
    assert "deployment_authority" in failed_gates(health)


def test_environment_drift_does_not_commit(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.deployment.mutate_environment()

    appliance.reconstruct()
    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert "deployment_authority" in failed_gates(health)


def test_persistence_drift_does_not_commit(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    appliance.host.unmount("/opt/ems-solarflow")
    appliance.host.apply_mounts()

    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert "persistent_paths" in failed_gates(health)


def test_no_failure_path_ever_touches_the_shared_user_data(tmp_path):
    appliance = TrialAppliance(tmp_path)
    config = appliance.host.root / "opt/ems-solarflow/config/config.json"
    config.write_text('{"devices": []}', encoding="utf-8")
    record = appliance.capture()
    appliance.arm(record)
    appliance.deployment.mutate_compose()

    appliance.reconstruct()
    appliance.health()

    assert config.read_text(encoding="utf-8") == '{"devices": []}'
    assert (appliance.host.root / "opt/ems-solarflow/data").is_dir()


def test_every_failure_reports_one_stable_reason(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    appliance.target.remove_container(ADMIN_CONTAINER)

    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    for reason in health.reasons:
        assert reason.startswith("health gate failed: ")


# --- phase 22: what the seed costs, and when it is discarded -----------------


def test_the_seed_is_bounded_to_one_generation(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    stale = appliance.store.seed_directory / "old-role.tar"
    stale.write_bytes(b"an archive from a deployment that no longer exists")

    bootstrap_service(appliance.source, appliance.store, appliance.deployment).seed(record)

    assert not stale.exists()
    assert {path.name for path in appliance.store.seed_directory.iterdir()} == {
        f"{entry.role}.tar" for entry in record.images
    }


def test_the_seed_size_is_reported_for_technical_status(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.capture()

    service = bootstrap_service(appliance.source, appliance.store, appliance.deployment)

    assert service.seed_bytes() > 0
    assert service.seed_state() == ab_bootstrap.SEED_READY


def test_an_incomplete_seed_is_reported_rather_than_assumed_complete(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    seed_of(appliance, ab_bootstrap.ROLE_EMS).unlink()

    service = bootstrap_service(appliance.source, appliance.store, appliance.deployment)

    assert service.seed_state() == ab_bootstrap.SEED_INCOMPLETE


def test_the_seed_a_pending_trial_needs_is_never_discarded_by_a_new_capture(tmp_path):
    """Re-capturing the same deployment must not leave the trial unable to load."""

    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)

    appliance.capture()

    for entry in record.images:
        assert seed_of(appliance, entry.role).is_file()
    assert appliance.reconstruct().ok


def test_discarding_the_seed_is_explicit_and_complete(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    service = bootstrap_service(appliance.source, appliance.store, appliance.deployment)

    service.discard_seed()

    assert not appliance.store.seed_directory.exists()
    assert service.seed_bytes() == 0
    assert service.seed_state() == ab_bootstrap.SEED_INCOMPLETE


# --- phase 20/21: rolling the OS back is not rolling the deployment back -----


def test_a_rollback_trial_reconstructs_against_the_current_shared_deployment(tmp_path):
    """Slot A returns after slot B was active. The deployment is B's.

    OS slot rollback is not EMS configuration rollback and not a database
    rollback: config, data and the compose file are shared, so the slot that
    comes back reconstructs whatever the appliance is deployed as now.
    """

    appliance = TrialAppliance(tmp_path, slot="A")
    appliance.state.record_known_good(
        SlotRecord(slot="B", release_version="1.5.0", build_id="20260807-1"),
        previous_slot="A",
    )
    # The EMS deployment moved on while slot B was the active one.
    appliance.deployment.mutate_compose()
    appliance.source.compose_file = str(appliance.deployment.compose_file)

    record = appliance.capture()
    appliance.arm(record, operation_id="op-rollback", source_slot="B", target_slot="A")
    report = appliance.reconstruct()
    health = appliance.health()

    assert report.ok, report.problems
    assert health.result == ab_health.RESULT_HEALTHY, health.reasons
    assert appliance.store.read().compose.sha256 == record.compose.sha256


def test_a_rollback_never_restores_an_older_compose_file(tmp_path):
    appliance = TrialAppliance(tmp_path, slot="A")
    first = appliance.capture()
    appliance.deployment.mutate_compose()
    second = bootstrap_service(
        appliance.source, appliance.store, appliance.deployment
    ).record_running_runtime()

    assert first.compose.sha256 != second.compose.sha256
    assert appliance.store.read().compose.sha256 == second.compose.sha256


def test_a_rollback_planned_against_an_older_deployment_is_refused(tmp_path):
    """The plan is bound to a deployment. A changed one needs a new plan."""

    appliance = TrialAppliance(tmp_path, slot="A")
    record = appliance.capture()
    appliance.arm(record, operation_id="op-rollback", source_slot="B", target_slot="A")
    appliance.deployment.mutate_compose()

    report = appliance.reconstruct()
    health = appliance.health()

    assert report.code == ab_bootstrap.DEPLOYMENT_AUTHORITY_DRIFT
    assert health.result == ab_health.RESULT_UNHEALTHY


# --- phase 29: crashing between the ordering points --------------------------


def test_a_crash_after_the_capture_leaves_the_record_readable(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()

    # The power goes here: the record and its seeds are on the medium, nothing
    # is armed, the appliance boots the slot it was already running.
    assert appliance.store.read().fingerprint == record.fingerprint
    assert appliance.state.pending() is None


def test_a_crash_during_the_seed_write_leaves_a_seed_that_is_never_loaded(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    seed = seed_of(appliance, ab_bootstrap.ROLE_EMS)
    seed.write_bytes(seed.read_bytes()[: len(seed.read_bytes()) // 2])

    report = appliance.reconstruct()

    assert str(seed) not in appliance.target.loaded
    outcome = next(item for item in report.outcomes if item.role == ab_bootstrap.ROLE_EMS)
    assert outcome.source == ab_bootstrap.SOURCE_REGISTRY


def test_a_crash_after_the_seed_before_the_pending_trial_is_an_ordinary_boot(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.capture()

    # No pending trial, so a boot of the source slot classifies as nothing at
    # all: the operator plans again, and the seed is simply already there.
    assert appliance.state.pending() is None
    assert appliance.store.read().images


def test_a_crash_after_admin_before_ems_does_not_commit(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    appliance.target.remove_container(EMS_CONTAINER)

    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert "ems_runtime" in failed_gates(health)


def test_a_crash_after_every_service_is_healthy_before_the_commit_falls_back(tmp_path):
    """The tryboot flag is one-shot: nothing committed means nothing changed."""

    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    assert appliance.health().result == ab_health.RESULT_HEALTHY

    pending = appliance.state.pending()
    assert not pending.committed
    assert appliance.state.slots().known_good_slot == ""


def test_recovery_never_records_two_known_good_slots(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.state.record_known_good(SlotRecord(slot="A"), previous_slot="")
    appliance.state.record_known_good(SlotRecord(slot="B"), previous_slot="A")

    history = appliance.state.slots()

    assert history.known_good_slot == "B"
    assert history.previous_slot == "A"
    assert history.known_good_slot != history.previous_slot


def test_a_fallback_is_recorded_against_the_operation_that_caused_it(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)

    appliance.state.record_fallback(
        FallbackRecord(
            operation_id="op-1",
            source_slot="A",
            target_slot="B",
            target_release="ems-solarflow-appliance-1.5.0-rpi5-arm64-ab",
            target_build_id="20260807-1",
            observed_at=1200.0,
        )
    )

    last = appliance.state.last_fallback()
    assert last.operation_id == "op-1"
    assert not last.acknowledged
