# SPDX-License-Identifier: AGPL-3.0-or-later
"""The authority gaps an independent review reproduced against the A/B stack.

Each test here states a property the appliance already claims to have and that
the implementation did not hold. They are grouped by the claim, not by module,
because every one of them is the same failure shape: something was proven at one
moment and then trusted at a later one, across a step that can change it.

    plan → confirmation → destructive write → trial reboot → commit

The deployment an operator confirmed, the Admin image that was actually running,
the tree a builder read, the archive a reviewer received and the directory entry
a pending trial lives in are all authorities. Every one of them was captured or
checked on one side of a step and assumed on the other.

Nothing here needs hardware. The block layer, the Docker engine and the firmware
are fakes; ``git``, ``ssh-keygen`` and ``tar`` are real, because they are what is
under test in the provenance, host-identity and bundle sections.
"""

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from appliance import (
    ab_bootstrap,
    ab_docker_health,
    ab_state,
    build_authority,
    host_identity,
    os_update,
    source_bundle,
)
from appliance.ab_state import AbStateError, AbStateStore, PendingTrial
from appliance.host_identity import HostIdentityService, private_key_name
from appliance.os_update import OsUpdateError
from tests.helpers.appliance_deployment import (
    ADMIN_CONTAINER,
    ADMIN_DIGEST,
    ADMIN_REFERENCE,
    ADMIN_REPOSITORY,
    EMS_CONTAINER,
    EMS_DIGEST,
    EMS_REFERENCE,
    INFLUX_CONTAINER,
    INFLUX_DIGEST,
    INFLUX_REFERENCE,
    EmsDeployment,
    Image,
    PlannedAppliance,
    bootstrap_service,
    source_engine,
    target_engine,
    trial_health,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

RELEASE_ID = "ems-solarflow-appliance-1.5.0-arm64-ab"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required to prove a tracked tree"
)
requires_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="ssh-keygen is not installed"
)


def block_writes(appliance):
    return [path for kind, path in appliance.service.backend.calls if kind == "write"]


# --- finding 1: the confirmed plan has to name the deployment ----------------


def test_a_plan_binds_the_deployment_it_was_made_against(tmp_path):
    appliance = PlannedAppliance(tmp_path)

    _operation, payload = appliance.plan()

    authority = payload[os_update.CONFIRMED_AUTHORITY_FIELD]
    assert authority["os_write"]["target_slot"] == "B"
    assert authority["deployment_fingerprint"].startswith("sha256:")
    assert authority["deployment_schema"] == ab_bootstrap.RECORD_VERSION


def test_the_persisted_target_carries_the_confirmed_authority(tmp_path):
    appliance = PlannedAppliance(tmp_path)

    operation, payload = appliance.plan()

    stored = appliance.target(operation)[os_update.CONFIRMED_AUTHORITY_FIELD]
    assert stored == payload[os_update.CONFIRMED_AUTHORITY_FIELD]
    assert stored["deployment_fingerprint"] == appliance.store.read().fingerprint


def test_the_confirmation_hash_covers_the_deployment_fingerprint(tmp_path):
    """The operator confirmed a deployment, not only a pair of partitions."""

    from appliance import operation_schema

    appliance = PlannedAppliance(tmp_path)
    operation, payload = appliance.plan()
    record = appliance.service.operations.get(operation.operation_id)
    sealed = operation_schema.seal(record, payload)
    appliance.service.operations.update_target(operation.operation_id, sealed)

    tampered = appliance.service.operations.get(operation.operation_id)
    authority = dict(tampered.requested_target[os_update.CONFIRMED_AUTHORITY_FIELD])
    authority["deployment_fingerprint"] = "sha256:" + "0" * 64
    tampered.requested_target[os_update.CONFIRMED_AUTHORITY_FIELD] = authority

    with pytest.raises(operation_schema.OperationSchemaError):
        operation_schema.verify_authority(tampered)


def test_a_compose_edit_after_confirmation_fails_before_the_first_byte(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    appliance.deployment.mutate_compose()

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code == "deployment_authority_drift"
    assert block_writes(appliance) == []
    record = appliance.service.operations.get(operation.operation_id)
    assert record.result["inactive_slot_untouched"] is True
    assert record.result["default_slot_unchanged"] is True
    assert record.result["replan_required"] is True


def test_an_environment_edit_after_confirmation_fails_before_the_first_byte(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    appliance.deployment.mutate_environment()

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code == "deployment_authority_drift"
    assert block_writes(appliance) == []


def test_a_replaced_admin_image_after_confirmation_fails_before_the_write(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    replacement = Image(
        f"{ADMIN_REPOSITORY}@sha256:{'9' * 64}", "sha256:" + "9" * 64
    )
    appliance.engine.add_image(replacement)
    appliance.engine.add_container(ADMIN_CONTAINER, replacement, health="healthy")

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code == "deployment_authority_drift"
    assert block_writes(appliance) == []


def test_the_slot_history_is_untouched_by_a_pre_write_drift_refusal(tmp_path):
    """A refusal before the write may not destroy a rollback candidate."""

    appliance = PlannedAppliance(tmp_path)
    appliance.service.state.record_known_good(
        ab_state.SlotRecord(slot="B", build_id="20260801-1"), previous_slot="A"
    )
    operation, _payload = appliance.plan()
    appliance.deployment.mutate_compose()

    with pytest.raises(OsUpdateError):
        appliance.confirm_and_run(operation)

    assert appliance.service.state.slots().record("B") is not None


def test_two_disagreeing_write_targets_in_one_record_are_refused(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, payload = appliance.plan()
    tampered = dict(payload[os_update.AUTHORITY_FIELD]) | {"root_device": "/dev/mmcblk0p4"}
    appliance.service.operations.update_target(
        operation.operation_id, {os_update.AUTHORITY_FIELD: tampered}
    )

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code == "ab_authority_inconsistent"
    assert block_writes(appliance) == []


def test_a_plan_written_before_the_confirmed_authority_is_refused(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    appliance.service.operations.update_target(
        operation.operation_id, {os_update.CONFIRMED_AUTHORITY_FIELD: {}}
    )

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code == "ab_authority_missing"


def test_an_unchanged_deployment_still_updates_end_to_end(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()

    result = appliance.confirm_and_run(operation)

    assert result["stage"] == os_update.STAGE_TRYBOOT_REQUESTED
    assert result["pending_trial"]["deployment_fingerprint"] == (
        appliance.store.read().fingerprint
    )


def test_a_rollback_plan_is_bound_to_the_same_deployment_authority(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    appliance.service.state.record_known_good(
        ab_state.SlotRecord(
            slot="B",
            release_version="1.4.0",
            build_id="20260801-1",
            artifact_digest="sha256:" + "a" * 64,
            boot_digest="sha256:" + "d" * 64,
            rootfs_digest="sha256:" + "e" * 64,
        ),
        previous_slot="B",
    )
    operation = appliance.service.operations.create(os_update.TYPE_OS_ROLLBACK)

    payload = appliance.service.plan_rollback(operation)

    authority = payload[os_update.CONFIRMED_AUTHORITY_FIELD]
    assert authority["deployment_fingerprint"] == appliance.store.read().fingerprint


def test_a_rollback_confirmed_against_a_changed_deployment_is_refused(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    appliance.service.state.record_known_good(
        ab_state.SlotRecord(
            slot="B",
            release_version="1.4.0",
            build_id="20260801-1",
            artifact_digest="sha256:" + "a" * 64,
            boot_digest="sha256:" + "d" * 64,
            rootfs_digest="sha256:" + "e" * 64,
        ),
        previous_slot="B",
    )
    operation = appliance.service.operations.create(os_update.TYPE_OS_ROLLBACK)
    appliance.service.plan_rollback(operation)
    appliance.deployment.mutate_compose()

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code == "deployment_authority_drift"


# --- finding 2: a failed seed is not a lost deployment authority -------------


def test_a_failed_seed_keeps_the_confirmed_deployment_fingerprint(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    appliance.engine.save_fails = True

    result = appliance.confirm_and_run(operation)

    fingerprint = appliance.store.read().fingerprint
    assert fingerprint.startswith("sha256:")
    assert result["runtime_seed"]["state"] == ab_bootstrap.SEED_INCOMPLETE
    assert result["pending_trial"]["deployment_fingerprint"] == fingerprint
    assert appliance.service.state.pending().deployment_fingerprint == fingerprint


def test_a_slot_seeded_incompletely_still_rebuilds_from_the_registry(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    appliance.engine.save_fails = True
    appliance.confirm_and_run(operation)

    target = target_engine(
        appliance.deployment, registry=list(appliance.engine.images.values())
    )
    report = bootstrap_service(target, appliance.store, appliance.deployment).reconstruct()

    assert report.ok, report.problems
    assert set(target.pulled) == {ADMIN_REFERENCE, EMS_REFERENCE}


def test_a_deployment_that_cannot_be_captured_never_arms_a_trial(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    appliance.store.path.unlink()

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code in (
        "deployment_authority_missing",
        "deployment_authority_drift",
    )
    assert appliance.service.state.pending() is None


# --- finding 3: readiness is a backend precondition, not a disabled button ---


def blocker_codes(payload):
    return {entry["code"] for entry in payload["blockers"]}


def test_a_deployment_that_cannot_be_proven_blocks_planning_server_side(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    appliance.deployment.compose_file.chmod(0o666)

    _operation, payload = appliance.plan()

    assert "deployment_authority_drift" in blocker_codes(payload)


def test_an_appliance_with_no_resolvable_admin_blocks_planning(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    appliance.engine.remove_container(ADMIN_CONTAINER)

    _operation, payload = appliance.plan()

    assert "deployment_authority_missing" in blocker_codes(payload)


def test_a_fresh_appliance_without_ems_or_influx_can_still_plan(tmp_path):
    """Admin is the recovery path. EMS and InfluxDB are optional."""

    appliance = PlannedAppliance(tmp_path)
    appliance.engine.remove_container(EMS_CONTAINER)

    _operation, payload = appliance.plan()

    assert blocker_codes(payload) == set()


def test_a_broken_host_identity_blocks_planning(tmp_path, monkeypatch):
    appliance = PlannedAppliance(tmp_path)
    monkeypatch.setattr(appliance.service, "_host_identity_ready", lambda: False)

    _operation, payload = appliance.plan()

    assert "host_identity_unproven" in blocker_codes(payload)


# --- finding 7: a rename is not durable until its directory is --------------


def test_a_pending_trial_whose_directory_flush_fails_is_not_written(tmp_path):
    store = AbStateStore(tmp_path / "state")
    store.ensure()
    store._sync_directory = lambda: False

    with pytest.raises(AbStateError) as excinfo:
        store.set_pending(_trial())

    assert excinfo.value.code == "ab_state_write_failed"


def test_a_slot_history_whose_directory_flush_fails_is_not_written(tmp_path):
    store = AbStateStore(tmp_path / "state")
    store.ensure()
    store._sync_directory = lambda: False

    with pytest.raises(AbStateError) as excinfo:
        store.record_known_good(ab_state.SlotRecord(slot="B"))

    assert excinfo.value.code == "ab_state_write_failed"


def test_a_fallback_whose_directory_flush_fails_is_not_written(tmp_path):
    store = AbStateStore(tmp_path / "state")
    store.ensure()
    store._sync_directory = lambda: False

    with pytest.raises(AbStateError):
        store.record_fallback(
            ab_state.FallbackRecord(
                operation_id="op-1",
                source_slot="A",
                target_slot="B",
                target_release="r",
                target_build_id="b",
                observed_at=1.0,
            )
        )


def test_clearing_a_pending_trial_requires_a_durable_directory(tmp_path):
    store = AbStateStore(tmp_path / "state")
    store.ensure()
    store.set_pending(_trial())
    store._sync_directory = lambda: False

    with pytest.raises(AbStateError) as excinfo:
        store.clear_pending()

    assert excinfo.value.code == "ab_state_write_failed"


def test_a_directory_that_cannot_even_be_opened_fails_the_write(tmp_path):
    store = AbStateStore(tmp_path / "state")
    store.ensure()

    def refuse(path, flags, *args):
        if str(path) == str(store.directory) and flags == os.O_RDONLY:
            raise OSError(13, "permission denied")
        return real(path, flags, *args)

    real = os.open
    os.open = refuse
    try:
        with pytest.raises(AbStateError):
            store.set_pending(_trial())
    finally:
        os.open = real


def test_a_state_store_that_cannot_flush_stops_before_the_first_byte(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    selector = appliance.host.selector_path().read_text(encoding="utf-8")
    appliance.service.state._sync_directory = lambda: False

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code == "ab_state_write_failed"
    assert block_writes(appliance) == []
    assert appliance.host.selector_path().read_text(encoding="utf-8") == selector


def test_a_failed_pending_durability_never_reaches_the_reboot(tmp_path):
    """The interleaving: slot history flushes, the pending trial does not.

    Two directory flushes happen on the destructive path — invalidating the
    target slot, then recording the pending trial. Failing the second one is
    the state that must never be followed by a tryboot.
    """

    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    selector = appliance.host.selector_path().read_text(encoding="utf-8")
    state = appliance.service.state
    real = state._sync_directory
    flushes = []

    def flush():
        flushes.append(len(flushes) + 1)
        return real() if len(flushes) == 1 else False

    state._sync_directory = flush

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code == "ab_state_write_failed"
    assert flushes == [1, 2]
    assert appliance.host.selector_path().read_text(encoding="utf-8") == selector
    record = appliance.service.operations.get(operation.operation_id)
    assert record.result["reboot_requested"] is False
    assert record.state == "failed_recoverable"


def _trial():
    return PendingTrial(
        operation_id="op-1",
        source_slot="A",
        target_slot="B",
        target_release="r",
        target_build_id="b",
        artifact_digest="sha256:" + "c" * 64,
        expected_boot_partition=3,
        expected_root_partuuid="uuid",
        trial_requested_at=1.0,
    )


# --- finding 8: the running Admin is the authority --------------------------


class RecordingKnownGood:
    def __init__(self, reference):
        self.reference = reference

    def current(self):
        return {"admin_reference": self.reference}


def bootstrap_with_known_good(engine, store, deployment, reference):
    from appliance.ab_bootstrap import SlotBootstrapService
    from appliance.docker_backend import DockerBackend
    from tests.helpers.appliance_deployment import deployment_layout

    backend = DockerBackend(engine.runner(), compose_file=str(deployment.compose_file))
    return SlotBootstrapService(
        docker=backend,
        store=store,
        known_good=RecordingKnownGood(reference),
        deployment=deployment_layout(deployment),
    )


def test_a_known_good_admin_digest_that_is_not_running_blocks_planning(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = source_engine(deployment)
    store = ab_bootstrap.RuntimeRecordStore(tmp_path / "state")
    stale = f"{ADMIN_REPOSITORY}@sha256:{'a' * 64}"
    service = bootstrap_with_known_good(engine, store, deployment, stale)

    with pytest.raises(ab_bootstrap.BootstrapError) as excinfo:
        service.record_running_runtime()

    assert excinfo.value.code == "admin_runtime_authority_drift"


def test_a_known_good_admin_digest_that_matches_is_accepted(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = source_engine(deployment)
    store = ab_bootstrap.RuntimeRecordStore(tmp_path / "state")
    service = bootstrap_with_known_good(engine, store, deployment, ADMIN_REFERENCE)

    record = service.record_running_runtime()

    assert record.image(ab_bootstrap.ROLE_ADMIN).reference == ADMIN_REFERENCE


def test_without_a_known_good_record_the_running_digest_is_used(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = source_engine(deployment)
    store = ab_bootstrap.RuntimeRecordStore(tmp_path / "state")
    service = bootstrap_service(engine, store, deployment)

    record = service.record_running_runtime()

    assert record.image(ab_bootstrap.ROLE_ADMIN).digest == ADMIN_DIGEST


def test_a_mutable_admin_tag_is_recorded_as_repository_at_digest(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = source_engine(deployment)
    tagged = engine.add_image(Image(f"{ADMIN_REPOSITORY}:v1.5.0", ADMIN_DIGEST))
    engine.containers[ADMIN_CONTAINER]["image"] = tagged.reference
    store = ab_bootstrap.RuntimeRecordStore(tmp_path / "state")
    service = bootstrap_service(engine, store, deployment)

    record = service.record_running_runtime()

    assert record.image(ab_bootstrap.ROLE_ADMIN).reference == ADMIN_REFERENCE


# --- finding 9: a crashed container is not an intentionally stopped one -----


def container_state(engine, deployment, store, container, payload):
    engine.containers[container] = payload
    return bootstrap_service(engine, store, deployment)


def test_a_restarting_required_container_blocks_planning(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = source_engine(deployment)
    engine.containers[ADMIN_CONTAINER]["restarting"] = True
    store = ab_bootstrap.RuntimeRecordStore(tmp_path / "state")

    with pytest.raises(ab_bootstrap.BootstrapError) as excinfo:
        bootstrap_service(engine, store, deployment).record_running_runtime()

    assert excinfo.value.code == "runtime_state_not_settled"


def test_a_crashed_ems_is_recorded_as_failed_and_blocks_planning(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = source_engine(deployment)
    engine.containers[EMS_CONTAINER]["running"] = False
    engine.containers[EMS_CONTAINER]["exit_code"] = 137
    store = ab_bootstrap.RuntimeRecordStore(tmp_path / "state")

    with pytest.raises(ab_bootstrap.BootstrapError) as excinfo:
        bootstrap_service(engine, store, deployment).record_running_runtime()

    assert excinfo.value.code == "runtime_state_not_settled"


def test_a_cleanly_stopped_ems_is_recorded_as_stopped_clean(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = source_engine(deployment)
    engine.containers[EMS_CONTAINER]["running"] = False
    engine.containers[EMS_CONTAINER]["exit_code"] = 0
    store = ab_bootstrap.RuntimeRecordStore(tmp_path / "state")

    record = bootstrap_service(engine, store, deployment).record_running_runtime()

    assert record.image(ab_bootstrap.ROLE_EMS).state == ab_bootstrap.STATE_STOPPED_CLEAN


def test_a_created_but_never_started_container_is_not_settled(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = source_engine(deployment)
    engine.containers[EMS_CONTAINER]["running"] = False
    engine.containers[EMS_CONTAINER]["status"] = "created"
    store = ab_bootstrap.RuntimeRecordStore(tmp_path / "state")

    with pytest.raises(ab_bootstrap.BootstrapError) as excinfo:
        bootstrap_service(engine, store, deployment).record_running_runtime()

    assert excinfo.value.code == "runtime_state_not_settled"


# --- finding 10/11: health means the application answers --------------------


def running_ems(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = target_engine(deployment)
    image = engine.add_image(Image(EMS_REFERENCE, EMS_DIGEST))
    engine.add_container(EMS_CONTAINER, image, health="none")
    return deployment, engine


def test_a_running_ems_that_does_not_answer_its_diagnostic_is_not_healthy(tmp_path):
    """A PID is not an EMS. There is no container health check that says so."""

    deployment, engine = running_ems(tmp_path)
    engine.exec_results[EMS_CONTAINER] = lambda args, _engine: engine._result(
        args, 1, "", "python3: command not found"
    )

    result = trial_health(engine, deployment).ems_runtime(EMS_DIGEST, expected_running=True)

    assert not result.ok
    assert result.code == "ems_not_functional"


def test_a_running_ems_whose_own_diagnosis_is_error_is_not_healthy(tmp_path):
    deployment, engine = running_ems(tmp_path)
    engine.exec_results[EMS_CONTAINER] = lambda args, _engine: engine._result(
        args, 0, json.dumps({"diagnosis": {"status": "error"}})
    )

    result = trial_health(engine, deployment).ems_runtime(EMS_DIGEST, expected_running=True)

    assert not result.ok
    assert result.code == "ems_diagnosis_failed"


def test_a_running_ems_that_answers_its_diagnostic_is_healthy(tmp_path):
    deployment, engine = running_ems(tmp_path)

    assert trial_health(engine, deployment).ems_runtime(EMS_DIGEST, expected_running=True).ok
    assert engine.execs == [(EMS_CONTAINER, ab_docker_health.EMS_DIAGNOSE_ARGV)]


def test_a_stopped_ems_is_never_probed(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = target_engine(deployment)

    result = trial_health(engine, deployment).ems_runtime(EMS_DIGEST, expected_running=False)

    assert result.ok
    assert engine.execs == []


def test_a_running_influxdb_that_does_not_answer_a_ping_is_not_healthy(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = target_engine(deployment)
    image = engine.add_image(Image(INFLUX_REFERENCE, INFLUX_DIGEST))
    engine.add_container(INFLUX_CONTAINER, image, health="none")
    engine.exec_results[INFLUX_CONTAINER] = lambda args, _engine: engine._result(
        args, 1, "", "Error: failed to ping"
    )

    result = trial_health(engine, deployment).influxdb_runtime(
        INFLUX_DIGEST, expected_running=True
    )

    assert not result.ok
    assert result.code == "influxdb_not_functional"


def test_an_absent_influxdb_is_never_probed(tmp_path):
    deployment = EmsDeployment(tmp_path / "opt/ems-solarflow")
    engine = target_engine(deployment)

    result = trial_health(engine, deployment).influxdb_runtime(
        INFLUX_DIGEST, expected_running=False
    )

    assert result.ok
    assert engine.execs == []


def test_a_generic_json_responder_is_not_the_admin_console():
    assert not ab_docker_health._identifies_admin(json.dumps({"version": "not-admin"}))
    assert not ab_docker_health._identifies_admin(json.dumps({"service": "anything"}))
    assert not ab_docker_health._identifies_admin(json.dumps({"admin_version": "1"}))
    assert not ab_docker_health._identifies_admin("<html>nginx</html>")


def test_the_real_admin_auth_status_body_identifies_admin():
    body = json.dumps(
        {
            "admin_instance_id": "9f2c41d8a7b04e5c8d3f6a1b2c4e5f70",
            "auth_configured": True,
            "authenticated": False,
            "requires_initial_password": False,
            "recovery_required": False,
        }
    )

    assert ab_docker_health._identifies_admin(body)


def test_an_admin_body_with_the_wrong_types_is_refused():
    complete = {
        "admin_instance_id": "9f2c41d8a7b04e5c8d3f6a1b2c4e5f70",
        "auth_configured": True,
        "authenticated": False,
        "requires_initial_password": False,
        "recovery_required": False,
    }

    assert not ab_docker_health._identifies_admin(json.dumps(complete | {"auth_configured": "yes"}))
    assert not ab_docker_health._identifies_admin(json.dumps(complete | {"admin_instance_id": ""}))
    assert not ab_docker_health._identifies_admin(
        json.dumps({key: value for key, value in complete.items() if key != "recovery_required"})
    )


# --- finding 12: a half-placed host key is recoverable ----------------------


@requires_keygen
def test_a_missing_public_half_is_derived_and_never_regenerated(tmp_path):
    service = _identity_service(tmp_path)
    service.ensure()
    private = service.private_key("ed25519").read_bytes()
    public = service.public_key("ed25519")
    fingerprint = service.fingerprints()["ed25519"]
    public.unlink()

    report = service.ensure()

    assert report.ok, report.problems
    assert service.private_key("ed25519").read_bytes() == private
    assert public.is_file()
    assert service.fingerprints()["ed25519"] == fingerprint


@requires_keygen
def test_a_public_half_without_a_private_key_is_refused(tmp_path):
    service = _identity_service(tmp_path)
    service.ensure()
    material = service.public_key("ed25519").read_text(encoding="utf-8")
    service.private_key("ed25519").unlink()
    service.public_key("ed25519").write_text(material, encoding="utf-8")

    report = service.ensure()

    assert not report.ok
    assert any("host_key_private_half_missing" in problem for problem in report.problems)


@requires_keygen
def test_a_recovered_public_key_is_private_to_root_and_flushed(tmp_path):
    service = _identity_service(tmp_path)
    service.ensure()
    service.public_key("rsa").unlink()

    service.ensure()

    mode = service.public_key("rsa").stat().st_mode & 0o777
    assert mode == host_identity.PUBLIC_MODE


def _identity_service(tmp_path):
    from tests.helpers.appliance_ab import MACHINE_ID

    root = tmp_path / "host"
    for relative in (
        "var/lib/ems-appliance-manager/ssh",
        "etc/NetworkManager/system-connections",
        "persistent/common/etc",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "persistent/common/etc/machine-id").write_text(MACHINE_ID + "\n", encoding="utf-8")
    (root / "etc/machine-id").write_text(MACHINE_ID + "\n", encoding="utf-8")
    drop_in = root / str(host_identity.DROP_IN).lstrip("/")
    drop_in.parent.mkdir(parents=True, exist_ok=True)
    drop_in.write_text(
        "".join(
            f"HostKey {host_identity.KEY_DIRECTORY}/{private_key_name(key)}\n"
            for key in host_identity.HOST_KEY_TYPES
        ),
        encoding="utf-8",
    )
    return HostIdentityService(runner=_RealKeygen({}), root=root, require_root=False)


class _RealKeygen:
    def __init__(self, _responses):
        self.calls = []

    def available(self, tool):
        return shutil.which(tool) is not None

    def run(self, tool, args=(), **_kwargs):
        from appliance.commands import CommandResult

        args = tuple(args)
        self.calls.append((tool, args))
        completed = subprocess.run(
            [tool, *args], capture_output=True, text=True, timeout=120
        )
        return CommandResult(tool, args, completed.returncode, completed.stdout, completed.stderr)


# --- finding 5: exact parity means no undeclared member ---------------------


@requires_git
def test_an_archive_with_an_extra_file_is_not_the_tracked_tree(tmp_path):
    archive = _repo_archive(tmp_path)
    _append(archive, "evil-extra.txt", b"payload\n")

    report = source_bundle.verify(archive, root=ROOT)

    assert not report.ok
    assert "evil-extra.txt" in report.unexpected


@requires_git
def test_an_archive_with_a_traversing_member_is_refused(tmp_path):
    archive = _repo_archive(tmp_path)
    _append(archive, "../escape.txt", b"payload\n")

    report = source_bundle.verify(archive, root=ROOT)

    assert not report.ok
    assert report.unsafe


@requires_git
def test_an_archive_with_an_absolute_member_is_refused(tmp_path):
    archive = _repo_archive(tmp_path)
    _append(archive, "/etc/passwd", b"payload\n")

    report = source_bundle.verify(archive, root=ROOT)

    assert not report.ok
    assert report.unsafe


@requires_git
def test_an_archive_with_a_duplicate_member_is_refused(tmp_path):
    archive = _repo_archive(tmp_path)
    _append(archive, "README.md", b"a different README\n")

    report = source_bundle.verify(archive, root=ROOT)

    assert not report.ok
    assert "README.md" in report.duplicate


@requires_git
def test_an_archive_carrying_a_device_node_is_refused(tmp_path):
    archive = _repo_archive(tmp_path)
    with tarfile.open(archive, "a") as handle:
        member = tarfile.TarInfo("dev/zero")
        member.type = tarfile.CHRTYPE
        member.devmajor, member.devminor = 1, 5
        handle.addfile(member)

    report = source_bundle.verify(archive, root=ROOT)

    assert not report.ok
    assert report.unsafe


@requires_git
def test_a_faithful_archive_still_passes_exact_parity(tmp_path):
    archive = _repo_archive(tmp_path)

    report = source_bundle.verify(archive, root=ROOT)

    assert report.ok, report.to_dict()
    assert report.unexpected == ()
    assert report.unsafe == ()
    assert report.duplicate == ()


def _repo_archive(tmp_path, *, ref="HEAD"):
    target = tmp_path / "bundle.tar"
    subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", "-o", str(target), ref],
        check=True,
        timeout=600,
    )
    return target


def _append(archive, name, payload):
    with tarfile.open(archive, "a") as handle:
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        handle.addfile(member, __import__("io").BytesIO(payload))
    return archive


# --- finding 6: one canonical, self-verifying bundle creator ----------------


CREATOR = SCRIPTS / "appliance-create-source-bundle.sh"

WANTS = (
    "packaging/appliance/image/layer/ems-appliance.rootfs-overlay/"
    "etc/systemd/system/local-fs.target.wants"
)


@requires_git
def test_the_canonical_creator_produces_a_bundle_that_passes_parity(tmp_path):
    bundle = tmp_path / "review.tar.gz"

    completed = subprocess.run(
        ["sh", str(CREATOR), "--output", str(bundle)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
    )

    assert completed.returncode == 0, completed.stderr
    assert bundle.is_file()
    report = source_bundle.verify(bundle, root=ROOT, prefix=_prefix(bundle))
    assert report.ok, report.to_dict()


@requires_git
def shared_path_count():
    """One activation symlink per declared shared path, from the declaration."""

    from appliance import ab_persistence

    return len(ab_persistence.SHARED_PATHS)


def test_the_canonical_creator_preserves_every_persistence_symlink(tmp_path):
    bundle = tmp_path / "review.tar.gz"
    subprocess.run(
        ["sh", str(CREATOR), "--output", str(bundle)],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        timeout=900,
    )

    prefix = _prefix(bundle)
    with tarfile.open(bundle) as handle:
        links = [
            member.name
            for member in handle.getmembers()
            if member.issym() and f"{WANTS}/" in member.name
        ]

    assert len(links) == shared_path_count(), links
    assert prefix is not None


@requires_git
def test_the_canonical_creator_writes_a_manifest_beside_the_bundle(tmp_path):
    bundle = tmp_path / "review.tar.gz"
    subprocess.run(
        ["sh", str(CREATOR), "--output", str(bundle)],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        timeout=900,
    )

    manifest = json.loads(
        (bundle.parent / "review.manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["ok"] is True
    assert manifest["compared"] > 0
    assert manifest["missing"] == []
    assert manifest["unexpected"] == []
    assert manifest["unsafe"] == []
    assert manifest["duplicate"] == []
    assert manifest["symlinks"] == shared_path_count()
    assert len(manifest["ref"]) == 40


def _prefix(bundle):
    with tarfile.open(bundle) as handle:
        first = handle.next()
    return first.name.split("/", 1)[0] if first else ""


# --- finding 13/14/15: the project tree is part of build provenance ---------


@requires_git
def test_a_dirty_project_tree_has_no_production_provenance(tmp_path):
    from appliance import project_source

    project = _project_repo(tmp_path)
    (project / "appliance" / "os_update.py").write_text("# edited\n", encoding="utf-8")

    with pytest.raises(project_source.ProjectSourceError) as excinfo:
        project_source.assert_clean(project)

    assert excinfo.value.code == "project_source_dirty"


@requires_git
def test_a_staged_project_change_has_no_production_provenance(tmp_path):
    from appliance import project_source

    project = _project_repo(tmp_path)
    (project / "appliance" / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "appliance/new_module.py"], check=True)

    with pytest.raises(project_source.ProjectSourceError) as excinfo:
        project_source.assert_clean(project)

    assert excinfo.value.code == "project_source_dirty"


@requires_git
def test_an_untracked_build_input_has_no_production_provenance(tmp_path):
    from appliance import project_source

    project = _project_repo(tmp_path)
    (project / "packaging" / "extra.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(project_source.ProjectSourceError) as excinfo:
        project_source.assert_clean(project)

    assert excinfo.value.code == "project_source_untracked"


@requires_git
def test_a_clean_project_tree_yields_a_full_revision_and_tree_digest(tmp_path):
    from appliance import project_source

    project = _project_repo(tmp_path)

    identity = project_source.assert_clean(project)

    assert len(identity.revision) == 40
    assert identity.tree_sha256.startswith("sha256:")


@requires_git
def test_the_project_tree_digest_changes_with_the_tree(tmp_path):
    from appliance import project_source

    project = _project_repo(tmp_path)
    before = project_source.assert_clean(project).tree_sha256
    (project / "appliance" / "os_update.py").write_text("# edited\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "commit", "-am", "edit"], check=True,
                   capture_output=True)

    assert project_source.assert_clean(project).tree_sha256 != before


@requires_git
def test_a_project_tree_that_changed_during_the_build_is_not_signable(tmp_path):
    from appliance import project_source

    project = _project_repo(tmp_path)
    before = project_source.assert_clean(project)
    (project / "appliance" / "os_update.py").write_text("# edited\n", encoding="utf-8")

    with pytest.raises(project_source.ProjectSourceError) as excinfo:
        project_source.assert_unchanged(project, before)

    assert excinfo.value.code == "build_source_changed_during_build"


def test_the_build_authority_binds_both_source_trees(tmp_path):
    authority = _authority(tmp_path)

    assert authority.schema_version == build_authority.SCHEMA_VERSION
    assert authority.project.revision and len(authority.project.revision) == 40
    assert authority.project.tree_sha256.startswith("sha256:")
    assert authority.builder.source_tree_sha256.startswith("sha256:")


def test_an_update_signed_against_another_builds_authority_is_refused(tmp_path):
    first = _authority(tmp_path, build_id="build-a", payload=b"first")
    second = _authority(tmp_path, build_id="build-b", payload=b"second")

    problems = build_authority.verify_update(
        first, tmp_path / "build-b.update", build_id="build-b"
    )

    assert problems
    assert second.build_id == "build-b"


def test_an_authority_without_a_project_tree_digest_is_incomplete(tmp_path):
    from dataclasses import replace

    authority = _authority(tmp_path)
    stripped = replace(authority, project=build_authority.Project())

    problems = build_authority.verify_update(stripped, tmp_path / "build-a.update")

    assert any("project" in problem for problem in problems)


def _authority(tmp_path, *, build_id="build-a", payload=b"first"):
    update = tmp_path / f"{build_id}.update"
    update.write_bytes(payload)
    image = tmp_path / f"{build_id}.img"
    image.write_bytes(payload + b"-image")
    return build_authority.BuildAuthority(
        builder=build_authority.Builder(
            source_form="git",
            revision="f" * 40,
            source_tree_sha256="sha256:" + "1" * 64,
        ),
        project=build_authority.Project(
            revision="e" * 40, tree_sha256="sha256:" + "2" * 64
        ),
        profile="rpi5",
        build_id=build_id,
        image=build_authority.Artefact(
            path=str(image), sha256=build_authority.file_sha256(image)
        ),
        update=build_authority.Artefact(
            path=str(update), sha256=build_authority.file_sha256(update)
        ),
        completed=True,
    )


def _project_repo(tmp_path):
    """A minimal repository with the roots a project source authority guards."""

    project = tmp_path / "project"
    for relative, text in (
        ("appliance/os_update.py", "# appliance\n"),
        ("packaging/appliance/build-deb.sh", "#!/bin/sh\n"),
        ("scripts/appliance-build-rpi-ab-image.sh", "#!/bin/sh\n"),
        ("README.md", "project\n"),
    ):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(project), "-c", "user.email=t@e", "-c", "user.name=t",
         "commit", "-qm", "initial"],
        check=True,
        capture_output=True,
    )
    return project


# --- finding 4: a release gate that never ran is not a pass -----------------


GATES = SCRIPTS / "appliance-release-gates.sh"


def run_gates(tmp_path, *args, env=None):
    environment = dict(os.environ)
    environment.pop("EMS_RPI_IMAGE_GEN", None)
    environment.pop("EMS_APPLIANCE_OS_SIGN_KEY", None)
    environment.update(env or {})
    return subprocess.run(
        ["sh", str(GATES), "--output", str(tmp_path / "dist"), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
        env=environment,
    )


def test_a_release_with_every_gate_not_run_is_not_a_pass(tmp_path):
    completed = run_gates(tmp_path, "--profile", "rpi5", "--rpi-image-gen", str(tmp_path / "absent"))

    assert completed.returncode == 3, completed.stdout
    assert "RESULT: NOT RUN" in completed.stdout
    assert "RESULT: PASS" not in completed.stdout


def test_the_exploratory_mode_is_explicit_and_never_prints_pass(tmp_path):
    completed = run_gates(
        tmp_path,
        "--profile",
        "rpi5",
        "--rpi-image-gen",
        str(tmp_path / "absent"),
        "--allow-not-run",
    )

    assert completed.returncode == 0, completed.stdout
    assert "RESULT: INCOMPLETE" in completed.stdout
    assert "RESULT: PASS" not in completed.stdout


def test_the_gate_script_reports_which_required_gates_did_not_run(tmp_path):
    completed = run_gates(tmp_path, "--profile", "rpi5", "--rpi-image-gen", str(tmp_path / "absent"))

    assert "source-authority" in completed.stdout
    assert "required gates that did not run:" in completed.stdout


# --- the gate script's own exit contract -------------------------------------

STUB = "#!/bin/sh\nexit {status}\n"

BUILD_STUB = """\
#!/bin/sh
set -eu
OUTPUT=""
PROFILE=rpi5
while [ $# -gt 0 ]; do
    case "$1" in
        --output) OUTPUT=$2; shift 2 ;;
        --profile) PROFILE=$2; shift 2 ;;
        *) shift ;;
    esac
done
NAME=ems-solarflow-appliance-9.9.9-$PROFILE-arm64-ab
mkdir -p "$OUTPUT"
: > "$OUTPUT/$NAME.img"
: > "$OUTPUT/$NAME.update.tar.zst"
: > "$OUTPUT/$NAME.manifest.json"
: > "$OUTPUT/$NAME.build-authority.json"
exit {status}
"""


def gate_fixture(tmp_path, *, statuses=None):
    """A tree the gate script drives, with every sub-gate scripted."""

    statuses = dict(statuses or {})
    root = tmp_path / "fixture"
    (root / "scripts").mkdir(parents=True)
    (root / "appliance").mkdir(parents=True)
    (root / "appliance" / "version.py").write_text(
        'APPLIANCE_VERSION = "9.9.9"\n', encoding="utf-8"
    )
    shutil.copy(GATES, root / "scripts" / GATES.name)
    stubs = {
        "appliance-check-rpi-image-gen.sh": STUB,
        "appliance-verify-slot-mounts.sh": STUB,
        "appliance-build-rpi-ab-image.sh": BUILD_STUB,
        "appliance-inspect-rpi-ab-image.sh": STUB,
        "appliance-build-rpi-ab-update.sh": STUB,
        "appliance-inspect-rpi-ab-update.sh": STUB,
        "appliance-check-source-bundle.sh": STUB,
    }
    for name, template in stubs.items():
        target = root / "scripts" / name
        target.write_text(template.format(status=statuses.get(name, 0)), encoding="utf-8")
        target.chmod(0o755)
    bundle = root / "bundle.tar"
    bundle.write_bytes(b"")
    return root, bundle


def run_fixture_gates(root, bundle, tmp_path, *args):
    return subprocess.run(
        [
            "sh",
            str(root / "scripts" / GATES.name),
            "--output",
            str(tmp_path / "dist"),
            "--profile",
            "rpi5",
            "--rpi-image-gen",
            str(root),
            "--source-bundle",
            str(bundle),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_every_required_gate_passing_exits_zero(tmp_path):
    root, bundle = gate_fixture(tmp_path)

    completed = run_fixture_gates(root, bundle, tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RESULT: PASS" in completed.stdout
    assert "required gates that did not run" not in completed.stdout


def test_one_failing_gate_exits_one(tmp_path):
    root, bundle = gate_fixture(
        tmp_path, statuses={"appliance-inspect-rpi-ab-image.sh": 1}
    )

    completed = run_fixture_gates(root, bundle, tmp_path)

    assert completed.returncode == 1, completed.stdout
    assert "RESULT: FAIL" in completed.stdout


def test_a_failure_outranks_a_skip(tmp_path):
    root, bundle = gate_fixture(
        tmp_path,
        statuses={
            "appliance-inspect-rpi-ab-image.sh": 1,
            "appliance-verify-slot-mounts.sh": 3,
        },
    )

    completed = run_fixture_gates(root, bundle, tmp_path)

    assert completed.returncode == 1, completed.stdout
    assert "RESULT: FAIL" in completed.stdout


def test_one_required_gate_that_did_not_run_exits_three(tmp_path):
    root, bundle = gate_fixture(
        tmp_path, statuses={"appliance-verify-slot-mounts.sh": 3}
    )

    completed = run_fixture_gates(root, bundle, tmp_path)

    assert completed.returncode == 3, completed.stdout
    assert "RESULT: NOT RUN" in completed.stdout
    assert "slot-mounts" in completed.stdout


def test_an_unsigned_release_is_still_a_pass(tmp_path):
    """Rehearsing without a key is legitimate; the signature gate is optional."""

    root, bundle = gate_fixture(tmp_path)

    completed = run_fixture_gates(root, bundle, tmp_path)

    assert "sign-rpi5" in completed.stdout
    assert "RESULT: PASS" in completed.stdout


@requires_git
def test_a_bundle_prefix_is_detected_rather_than_guessed(tmp_path):
    bundle = tmp_path / "review.tar.gz"
    subprocess.run(
        ["sh", str(CREATOR), "--output", str(bundle), "--prefix", "delivered"],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        timeout=900,
    )

    assert source_bundle.detect_prefix(bundle) == "delivered"
    assert source_bundle.verify(bundle, root=ROOT, prefix="delivered").ok


@requires_git
def test_an_archive_with_no_single_root_has_no_detected_prefix(tmp_path):
    archive = _repo_archive(tmp_path)

    assert source_bundle.detect_prefix(archive) == ""


@requires_git
def test_the_checker_detects_the_prefix_of_a_canonical_bundle(tmp_path):
    bundle = tmp_path / "review.tar.gz"
    subprocess.run(
        ["sh", str(CREATOR), "--output", str(bundle)],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        timeout=900,
    )

    completed = subprocess.run(
        ["sh", str(SCRIPTS / "appliance-check-source-bundle.sh"), str(bundle)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "(detected)" in completed.stdout
    assert "RESULT: PASS" in completed.stdout
    assert f"symlinks: {shared_path_count()} preserved" in completed.stdout
