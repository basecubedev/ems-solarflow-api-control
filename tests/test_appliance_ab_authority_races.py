# SPDX-License-Identifier: AGPL-3.0-or-later
"""The windows between two steps, and what has to happen inside each one.

Every gate in the A/B stack proves something at one moment. This module is about
the interval that follows it: the plan is confirmed and then the compose file
changes, the generator tree is proven and then the build runs for an hour, the
selector is committed and then the power goes.

Each case names its interleaving explicitly and drives it deterministically —
no sleeps, no threads, no polling. The failure mode being tested is not a timing
bug but an authority that was cached across a step, so the interleaving is the
test rather than a way of provoking one.

The production adapters are used unchanged: the real ``DockerBackend``,
``SlotBootstrapService``, ``DockerTrialHealth``, ``TrialHealthService`` and
``OsUpdateService``. Only the block layer, the command runner and the firmware
are fixtures.
"""

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from appliance import ab_bootstrap, ab_health, os_update, source_bundle
from appliance.ab_state import AbStateError, SlotRecord
from appliance.os_update import OsUpdateError
from appliance.ab_persistence import PERSISTENT_SCHEMA_VERSION
from tests.helpers.appliance_ab import SLOT_BOOT_PARTITION
from tests.helpers.appliance_deployment import (
    ADMIN_SERVICE,
    EMS_CONTAINER,
    EMS_REFERENCE,
    EMS_SERVICE,
    PlannedAppliance,
    TrialAppliance,
    bootstrap_service,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation]

ROOT = Path(__file__).resolve().parents[1]

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required to enumerate the tracked tree"
)


# --- phase 23: what may change between two steps -----------------------------


def test_a_deployment_that_changes_between_plan_and_execute_fails_closed(tmp_path):
    """plan → confirm → compose edit → execute. The edit wins nothing."""

    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    appliance.service.operations.await_confirmation(operation.operation_id, {"plan": True})
    record = appliance.service.operations.get(operation.operation_id, include_token=True)
    appliance.service.operations.confirm(operation.operation_id, record.confirmation_token)
    appliance.deployment.mutate_compose()

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.service.execute(operation)

    assert excinfo.value.code == "deployment_authority_drift"
    assert appliance.service.backend.contents == {}


def test_an_environment_that_changes_between_plan_and_execute_fails_closed(tmp_path):
    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    appliance.deployment.mutate_environment()

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code == "deployment_authority_drift"
    assert appliance.service.backend.contents == {}


def test_a_deployment_restored_to_the_confirmed_state_may_be_retried(tmp_path):
    """The refusal is recoverable, and the retry runs the same revalidation."""

    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    original = appliance.deployment.compose_file.read_text(encoding="utf-8")
    appliance.deployment.mutate_compose()
    with pytest.raises(OsUpdateError):
        appliance.confirm_and_run(operation)
    appliance.deployment.compose_file.write_text(original, encoding="utf-8")

    record = appliance.service.operations.get(operation.operation_id, include_token=True)
    appliance.service.operations.retry(operation.operation_id, record.confirmation_token)
    result = appliance.service.execute(operation)

    assert result["stage"] == os_update.STAGE_TRYBOOT_REQUESTED


@requires_git
def test_a_source_bundle_altered_after_its_manifest_no_longer_matches(tmp_path):
    """The manifest describes the archive as it was, not as it is now."""

    bundle = tmp_path / "review.tar.gz"
    subprocess.run(
        ["sh", str(ROOT / "scripts" / "appliance-create-source-bundle.sh"),
         "--output", str(bundle)],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        timeout=900,
    )
    manifest = json.loads((tmp_path / "review.manifest.json").read_text(encoding="utf-8"))
    assert manifest["ok"] is True

    altered = tmp_path / "altered.tar"
    with tarfile.open(bundle) as source, tarfile.open(altered, "w") as target:
        for member in source.getmembers():
            stream = source.extractfile(member) if member.isreg() else None
            target.addfile(member, stream)
        payload = b"added after the manifest was written\n"
        extra = tarfile.TarInfo(f"{manifest['prefix']}/packaging/injected.sh")
        extra.size = len(payload)
        target.addfile(extra, __import__("io").BytesIO(payload))

    report = source_bundle.verify(altered, root=ROOT, prefix=manifest["prefix"])

    assert not report.ok
    assert "packaging/injected.sh" in report.unexpected


def test_a_blocked_plan_never_replaces_the_authority_a_trial_is_bound_to(tmp_path):
    """A second plan while a trial is in flight must not re-record anything."""

    from appliance.operations import STATE_SUCCEEDED

    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    appliance.confirm_and_run(operation)
    appliance.service.operations.finish(
        operation.operation_id, STATE_SUCCEEDED, stage=os_update.STAGE_TRYBOOT_REQUESTED
    )
    armed = appliance.store.read()
    seeds = dict(armed.seeds)

    _second, payload = appliance.plan()

    assert "ab_trial_pending" in {entry["code"] for entry in payload["blockers"]}
    assert payload[os_update.CONFIRMED_AUTHORITY_FIELD] == {} or (
        payload[os_update.CONFIRMED_AUTHORITY_FIELD]["deployment_fingerprint"] == ""
    )
    assert appliance.store.read().fingerprint == armed.fingerprint
    assert appliance.store.read().seeds == seeds
    assert appliance.service.state.pending().deployment_fingerprint == armed.fingerprint


def test_a_deployment_that_changes_between_revalidation_and_arming_is_refused(tmp_path):
    """The last window: execute revalidated, then the compose file moved."""

    appliance = PlannedAppliance(tmp_path)
    appliance.service.state.record_known_good(
        SlotRecord(
            slot="B",
            release_version="1.4.0",
            build_id="20260801-1",
            artifact_digest="sha256:" + "a" * 64,
        ),
        previous_slot="B",
    )
    operation = appliance.service.operations.create(os_update.TYPE_OS_ROLLBACK)
    appliance.service.plan_rollback(operation)
    service = appliance.service
    real = service._revalidate_deployment
    calls = []

    def drift_after_the_first_check(confirmed):
        calls.append(len(calls) + 1)
        record = real(confirmed)
        if len(calls) == 1:
            appliance.deployment.mutate_compose()
        return record

    service._revalidate_deployment = drift_after_the_first_check

    with pytest.raises(OsUpdateError) as excinfo:
        appliance.confirm_and_run(operation)

    assert excinfo.value.code == "deployment_authority_drift"
    assert calls == [1, 2]
    assert appliance.service.state.pending() is None
    record = appliance.service.operations.get(operation.operation_id)
    assert record.result["inactive_slot_untouched"] is True
    assert record.result["replan_required"] is True


# --- phase 24: the crash matrix ----------------------------------------------


def test_a_selector_that_cannot_be_armed_leaves_no_pending_trial(tmp_path):
    """pending durable → selector write fails. Nothing may look armed."""

    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    appliance.host.selector_path().write_text("nonsense\n", encoding="utf-8")

    with pytest.raises(OsUpdateError):
        appliance.confirm_and_run(operation)

    assert appliance.service.state.pending() is None
    record = appliance.service.operations.get(operation.operation_id)
    assert record.result["default_slot_unchanged"] is True


def test_a_reboot_request_that_fails_is_reported_and_not_assumed(tmp_path):
    """selector durable → reboot command fails. The trial is still recorded."""

    appliance = PlannedAppliance(tmp_path)
    operation, _payload = appliance.plan()
    runner = appliance.service.runner
    real = runner.run

    def refuse(tool, args=(), **kwargs):
        if tool == "systemctl" and tuple(args)[:1] == ("reboot",):
            from appliance.commands import CommandResult

            return CommandResult(tool, tuple(args), 1, "", "firmware refused the trial boot")
        return real(tool, args, **kwargs)

    runner.run = refuse

    result = appliance.confirm_and_run(operation)

    assert result["reboot_requested"] is False
    assert appliance.service.state.pending() is not None


def test_admin_starting_and_ems_failing_is_an_incomplete_reconstruction(tmp_path):
    """slot bootstrap: Admin comes up, EMS does not. The report says so."""

    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    engine = appliance.target
    real = engine._compose

    def refuse_ems(args):
        if args[-1] == EMS_SERVICE and "up" in args:
            engine.compose_calls.append(args)
            return engine._result(args, 1, "", "error while creating mount source path")
        return real(args)

    engine._compose = refuse_ems

    report = appliance.reconstruct()

    assert not report.ok
    assert ADMIN_SERVICE in report.started
    assert EMS_SERVICE not in report.started
    assert any(EMS_SERVICE in problem for problem in report.problems)
    assert report.code == ab_bootstrap.RECONSTRUCTION_INCOMPLETE


def test_a_healthy_admin_with_a_failing_ems_diagnostic_does_not_commit(tmp_path):
    """health: Admin answers, EMS is up but its own diagnosis fails."""

    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    appliance.target.exec_results[EMS_CONTAINER] = lambda args, engine: engine._result(
        args, 0, json.dumps({"diagnosis": {"status": "error"}})
    )

    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert any("ems_runtime" in reason for reason in health.reasons)


def test_a_commit_whose_state_write_fails_still_leaves_the_selector_authoritative(
    tmp_path,
):
    """The interleaving: selector commit succeeds, slot history does not."""

    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    service = _health_service(appliance)
    report = service.evaluate()
    assert report.result == ab_health.RESULT_HEALTHY, report.reasons

    def refuse(*_args, **_kwargs):
        raise AbStateError("ab_state_write_failed", "the directory could not be flushed")

    appliance.state.record_known_good = refuse

    with pytest.raises(AbStateError):
        service.commit(report)

    from appliance.ab_boot import parse_selector

    selector = parse_selector(appliance.host.selector_path().read_text(encoding="utf-8"))
    assert selector.default_partition == SLOT_BOOT_PARTITION["B"]


# --- phase 25: the selector is the authority the next boot reconciles from ---


def test_an_ordinary_boot_of_a_committed_slot_reconciles_the_state(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.host.write_selector(
        default=SLOT_BOOT_PARTITION["B"], trial=SLOT_BOOT_PARTITION["A"]
    )
    appliance.host.boot_slot("B", tryboot=False)
    appliance.host.mount_defaults()

    reconciled = ab_health.reconcile_boot(
        appliance.host.probe(), appliance.state, appliance.host.selector_path()
    )

    assert reconciled is not None
    assert reconciled.slot == "B"
    assert appliance.state.slots().known_good_slot == "B"
    assert appliance.state.slots().previous_slot == "A"
    assert appliance.state.pending().committed is True


def test_an_ordinary_boot_of_the_source_slot_is_a_fallback_and_never_a_commit(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.host.boot_slot("A", tryboot=False)
    appliance.host.mount_defaults()

    reconciled = ab_health.reconcile_boot(
        appliance.host.probe(), appliance.state, appliance.host.selector_path()
    )
    fallback = ab_health.classify_fallback(appliance.host.probe(), appliance.state)

    assert reconciled is None
    assert fallback is not None
    assert appliance.state.slots().known_good_slot == ""


def test_a_trial_boot_is_never_reconciled_into_a_commit(tmp_path):
    """Reconciliation is for an ordinary boot only; a trial commits itself."""

    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.host.boot_slot("B", tryboot=True)
    appliance.host.mount_defaults()

    reconciled = ab_health.reconcile_boot(
        appliance.host.probe(), appliance.state, appliance.host.selector_path()
    )

    assert reconciled is None
    assert appliance.state.slots().known_good_slot == ""


def test_a_committed_trial_is_not_reconciled_twice(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.state.record_known_good(SlotRecord(slot="B"), previous_slot="A")
    appliance.state.mark_committed("op-1")
    appliance.host.write_selector(
        default=SLOT_BOOT_PARTITION["B"], trial=SLOT_BOOT_PARTITION["A"]
    )
    appliance.host.boot_slot("B", tryboot=False)
    appliance.host.mount_defaults()

    assert (
        ab_health.reconcile_boot(
            appliance.host.probe(), appliance.state, appliance.host.selector_path()
        )
        is None
    )


# --- phase 18: platform is part of the identity, at every source -------------


def test_a_seed_that_loads_the_wrong_platform_is_refused(tmp_path):
    from tests.helpers.appliance_deployment import EMS_DIGEST, Image

    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    appliance.target.registry[EMS_REFERENCE] = Image(
        EMS_REFERENCE, EMS_DIGEST, architecture="amd64"
    )

    def wrong_platform(args, engine):
        engine.images[EMS_REFERENCE] = engine.registry[EMS_REFERENCE]
        return engine._result(args, 0, f"Loaded image: {EMS_REFERENCE}\n")

    appliance.target.load_result = wrong_platform

    report = appliance.reconstruct()

    outcome = next(item for item in report.outcomes if item.role == ab_bootstrap.ROLE_EMS)
    assert not outcome.available
    assert "platform" in outcome.detail


def test_an_image_whose_platform_is_unknown_is_refused(tmp_path):
    from tests.helpers.appliance_deployment import EMS_DIGEST, Image

    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    appliance.target.registry[EMS_REFERENCE] = Image(
        EMS_REFERENCE, EMS_DIGEST, architecture="", os_name=""
    )
    (appliance.store.seed_directory / f"{ab_bootstrap.ROLE_EMS}.tar").unlink()

    report = appliance.reconstruct()

    outcome = next(item for item in report.outcomes if item.role == ab_bootstrap.ROLE_EMS)
    assert not outcome.available


def test_the_required_platform_is_enforced_even_when_the_record_has_none(tmp_path):
    from appliance.ab_bootstrap import SlotBootstrapService
    from appliance.docker_backend import DockerBackend
    from tests.helpers.appliance_deployment import deployment_layout

    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    backend = DockerBackend(
        appliance.target.runner(), compose_file=str(appliance.deployment.compose_file)
    )
    service = SlotBootstrapService(
        docker=backend,
        store=appliance.store,
        deployment=deployment_layout(appliance.deployment),
        required_platform={"os": "linux", "architecture": "amd64"},
    )

    report = service.reconstruct()

    assert not report.ok
    assert any("platform" in problem for problem in report.problems)


# --- phase 27: the whole path over production adapters -----------------------


def test_the_whole_plan_bootstrap_health_path_runs_on_production_adapters(tmp_path):
    """One appliance, planned and executed, then rebuilt and judged.

    Nothing between the update service and Docker is a double: the block layer
    is fake because a partition is, and the firmware is modelled because a
    bootloader is. Everything above them is the code that ships.
    """

    appliance = PlannedAppliance(tmp_path, influx=True)
    operation, payload = appliance.plan()
    assert payload["blockers"] == []

    result = appliance.confirm_and_run(operation)
    assert result["stage"] == os_update.STAGE_TRYBOOT_REQUESTED
    assert result["reboot_requested"] is True
    fingerprint = appliance.store.read().fingerprint
    assert result["pending_trial"]["deployment_fingerprint"] == fingerprint
    assert sorted(result["runtime_seed"]["seeded"]) == ["admin", "ems", "influxdb"]

    argv = [args for tool, args in appliance.engine.calls if tool == "docker"]
    assert ("save", "-o", str(appliance.store.seed_directory / "admin.tar"),
            "ghcr.io/basecubedev/ems-solarflow-admin@sha256:" + "a1" * 32) in argv

    # The reboot. The shared partition and the EMS install root are the same
    # ones; what the target slot does not have is an image store.
    from tests.helpers.appliance_deployment import target_engine

    appliance.host.write_os_build(
        {
            "release_version": "1.5.0",
            "build_id": payload["target_build_id"],
            "layout_id": "ems-appliance-rota-v1",
            "persistent_schema_version": PERSISTENT_SCHEMA_VERSION,
            "slot_schema_version": 2,
        }
    )
    appliance.host.boot_slot("B", tryboot=True)
    appliance.host.mount_defaults()
    target = target_engine(
        appliance.deployment, registry=list(appliance.engine.images.values())
    )

    report = bootstrap_service(target, appliance.store, appliance.deployment).reconstruct()
    assert report.ok, report.problems
    assert target.pulled == []
    assert set(target.started_services()) == {"influxdb", "ems-solarflow-admin", "ems"}

    health = _trial_health_service(appliance, target).evaluate()
    assert health.result == ab_health.RESULT_HEALTHY, health.reasons


def _health_service(appliance):
    return _trial_health_service(appliance, appliance.target, state=appliance.state)


def _trial_health_service(appliance, engine, *, state=None):
    from tests.helpers.appliance_ab import build_health_service
    from tests.helpers.appliance_deployment import trial_health

    return build_health_service(
        appliance.host,
        state if state is not None else appliance.service.state,
        docker=trial_health(engine, appliance.deployment),
        runtime=appliance.store,
        bootstrap=bootstrap_service(engine, appliance.store, appliance.deployment),
        runner=appliance.host.runner(),
        time_fn=lambda: 1100.0,
    )
