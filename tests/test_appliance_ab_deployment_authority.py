# SPDX-License-Identifier: AGPL-3.0-or-later
"""The application runtime a trial slot has to rebuild, end to end.

An OS update is only safe if the appliance an operator had before the reboot is
the appliance they have after it. That is three separate claims, and each one
was assumed rather than proven:

- the source slot records what it is made of, including which services were
  meant to be running. Reconstruction started the Admin console and stopped
  there, so an appliance running Admin *and* EMS came back with EMS down, the
  EMS health gate correctly refused the slot, and every ordinary update fell
  back. This module drives bootstrap into ``TrialHealthService`` with nothing
  injected in between, so a reconstruction that misses a service cannot be
  papered over by a test that starts the container itself.

- the record carries a compose digest and an environment digest. Nothing
  compared them against the files on disk before running ``docker compose up``,
  so an edit made after the plan was confirmed was simply executed.

- an image is a digest, not a name. ``docker load`` printing success says
  nothing about which digest entered the store, and an arm64 appliance that
  seeded or pulled an amd64 image would commit a slot whose containers cannot
  start.

The production ``DockerBackend``, ``SlotBootstrapService``, ``DockerTrialHealth``
and ``TrialHealthService`` are used exactly as ``appliance/services.py`` builds
them. Only the command runner is a double, and it answers real docker argv.
"""

import hashlib
import json

import pytest

from appliance import ab_bootstrap, ab_health
from appliance.ab_bootstrap import SOURCE_REGISTRY, SOURCE_SEED
from tests.helpers.appliance_deployment import (
    ADMIN_CONTAINER,
    ADMIN_DIGEST,
    ADMIN_REFERENCE,
    ADMIN_SERVICE,
    EMS_CONTAINER,
    EMS_DIGEST,
    EMS_REFERENCE,
    EMS_SERVICE,
    INFLUX_CONTAINER,
    INFLUX_DIGEST,
    INFLUX_SERVICE,
    DockerEngine,
    Image,
    TrialAppliance,
    answering_admin,
    bootstrap_service,
)

pytestmark = [pytest.mark.integration, pytest.mark.simulation]


# --- finding 1: the recorded runtime is rebuilt, not just Admin --------------


def test_a_source_slot_running_admin_and_ems_reconstructs_both(tmp_path):
    appliance = TrialAppliance(tmp_path)

    report, health = appliance.trial()

    assert report.ok, report.problems
    assert appliance.target.started_services() == [ADMIN_SERVICE, EMS_SERVICE]
    assert set(appliance.target.containers) == {ADMIN_CONTAINER, EMS_CONTAINER}
    assert health.result == ab_health.RESULT_HEALTHY, health.reasons


def test_the_ems_gate_cannot_pass_without_the_bootstrap_starting_ems(tmp_path):
    """The old happy path, stated as a property: Admin alone is not enough."""

    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    # What the previous bootstrap left behind: images restored, Admin up, and
    # an EMS the source slot was running that nothing ever started.
    appliance.target.remove_container(EMS_CONTAINER)

    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert any("ems_runtime" in reason for reason in health.reasons)


def test_a_source_slot_running_influxdb_reconstructs_all_three(tmp_path):
    appliance = TrialAppliance(tmp_path, influx=True)

    report, health = appliance.trial()

    assert report.ok, report.problems
    assert appliance.target.started_services() == [INFLUX_SERVICE, ADMIN_SERVICE, EMS_SERVICE]
    assert set(appliance.target.containers) == {
        ADMIN_CONTAINER,
        EMS_CONTAINER,
        INFLUX_CONTAINER,
    }
    assert health.result == ab_health.RESULT_HEALTHY, health.reasons


def test_an_intentionally_stopped_ems_stays_stopped_and_still_commits(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.source.containers[EMS_CONTAINER]["running"] = False

    report, health = appliance.trial()

    assert report.ok, report.problems
    assert appliance.target.started_services() == [ADMIN_SERVICE]
    assert EMS_CONTAINER not in appliance.target.containers
    assert health.result == ab_health.RESULT_HEALTHY, health.reasons


def test_a_stopped_ems_still_has_its_image_authority_proven(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.source.containers[EMS_CONTAINER]["running"] = False

    record = appliance.capture()
    report = appliance.reconstruct()

    entry = record.image(ab_bootstrap.ROLE_EMS)
    assert entry.state == ab_bootstrap.STATE_STOPPED_CLEAN
    outcome = next(item for item in report.outcomes if item.role == ab_bootstrap.ROLE_EMS)
    assert outcome.available
    assert outcome.digest == EMS_DIGEST


def test_a_missing_ems_seed_with_no_registry_fails_the_trial(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    (appliance.store.seed_directory / f"{ab_bootstrap.ROLE_EMS}.tar").unlink()
    appliance.target.registry.pop(EMS_REFERENCE)

    report = appliance.reconstruct()
    health = appliance.health()

    outcome = next(item for item in report.outcomes if item.role == ab_bootstrap.ROLE_EMS)
    assert not outcome.available
    assert health.result == ab_health.RESULT_UNHEALTHY
    assert any("ems_runtime" in reason for reason in health.reasons)


def test_an_appliance_without_ems_reconstructs_admin_only(tmp_path):
    """A fresh Appliance with no EMS deployment yet is a supported state."""

    appliance = TrialAppliance(tmp_path, ems=False)

    report, health = appliance.trial()

    assert report.ok, report.problems
    assert appliance.target.started_services() == [ADMIN_SERVICE]
    assert appliance.store.read().image(ab_bootstrap.ROLE_EMS) is None
    assert health.result == ab_health.RESULT_HEALTHY, health.reasons


# --- finding 2: the recorded deployment is the one that is executed ----------


def test_a_compose_file_edited_after_the_capture_stops_reconstruction(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.deployment.mutate_compose()

    report = appliance.reconstruct()

    assert not report.ok
    assert report.code == ab_bootstrap.DEPLOYMENT_AUTHORITY_DRIFT
    assert appliance.target.mutations == []
    assert appliance.target.containers == {}


def test_an_environment_file_edited_after_the_capture_stops_reconstruction(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.deployment.mutate_environment()

    report = appliance.reconstruct()

    assert not report.ok
    assert report.code == ab_bootstrap.DEPLOYMENT_AUTHORITY_DRIFT
    assert appliance.target.mutations == []


def test_a_removed_environment_file_is_drift_and_not_an_empty_digest(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.deployment.environment_file.unlink()

    report = appliance.reconstruct()

    assert report.code == ab_bootstrap.DEPLOYMENT_AUTHORITY_DRIFT
    assert appliance.target.mutations == []


def test_a_compose_file_whose_owner_changed_is_drift(tmp_path):
    """Recorded, not required: what matters is that it is the same file."""

    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    monkeypatched = appliance.deployment.compose_file
    real = type(monkeypatched).lstat

    def lstat(self, *args, **kwargs):
        info = real(self, *args, **kwargs)
        if self != monkeypatched:
            return info
        import os

        fields = list(info[:10])
        fields[4] = info.st_uid + 1
        return os.stat_result(fields)

    type(monkeypatched).lstat = lstat
    try:
        report = appliance.reconstruct()
    finally:
        type(monkeypatched).lstat = real

    assert report.code == ab_bootstrap.DEPLOYMENT_AUTHORITY_DRIFT
    assert appliance.target.mutations == []


def test_a_world_writable_compose_file_is_never_executed(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.deployment.compose_file.chmod(0o666)

    report = appliance.reconstruct()

    assert report.code == ab_bootstrap.DEPLOYMENT_AUTHORITY_DRIFT
    assert appliance.target.mutations == []


def test_a_compose_file_replaced_by_a_symlink_is_drift(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    elsewhere = tmp_path / "elsewhere.yml"
    elsewhere.write_text(appliance.deployment.compose_file.read_text(), encoding="utf-8")
    appliance.deployment.compose_file.unlink()
    appliance.deployment.compose_file.symlink_to(elsewhere)

    report = appliance.reconstruct()

    assert report.code == ab_bootstrap.DEPLOYMENT_AUTHORITY_DRIFT
    assert appliance.target.mutations == []


def test_drift_is_never_repaired_by_recapturing_the_fingerprint(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.deployment.mutate_compose()

    appliance.reconstruct()

    assert appliance.store.read().fingerprint == record.fingerprint


def test_the_drift_check_runs_before_the_docker_daemon_is_even_asked(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.deployment.mutate_compose()
    appliance.target.version = None

    report = appliance.reconstruct()

    assert report.code == ab_bootstrap.DEPLOYMENT_AUTHORITY_DRIFT


# --- phase 17: one fingerprint, verified at every phase ----------------------


def test_the_pending_trial_carries_the_captured_deployment_fingerprint(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)

    assert record.fingerprint.startswith("sha256:")
    assert appliance.state.pending().deployment_fingerprint == record.fingerprint


def test_a_trial_bound_to_another_deployment_fingerprint_is_not_healthy(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    pending = appliance.state.pending()
    pending.deployment_fingerprint = "sha256:" + "0" * 64
    appliance.state.set_pending(pending)

    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert any("deployment_authority" in reason for reason in health.reasons)


def test_the_fingerprint_changes_when_the_deployment_changes(tmp_path):
    appliance = TrialAppliance(tmp_path)
    first = appliance.capture()
    appliance.deployment.mutate_compose()
    second = bootstrap_service(
        appliance.source, appliance.store, appliance.deployment
    ).record_running_runtime()

    assert first.fingerprint != second.fingerprint


def test_the_slot_that_replaces_a_slot_can_still_read_what_it_wrote(tmp_path):
    """The reader is one schema ahead across exactly the update that ships one.

    The record is written by the slot being replaced and read by the slot
    replacing it, and the trial is gated on the fingerprint the writer computed.
    A reader that could not reproduce it would fail that gate, fall back, and
    fail again on the next attempt with the same record — so the update that
    introduces a schema would be the one update that can never be installed.
    """

    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    payload = json.loads(appliance.store.path.read_text(encoding="utf-8"))
    current = appliance.store.read().fingerprint

    # What the previous schema wrote: no image id anywhere, version 3.
    payload["version"] = 3
    for entry in payload["images"]:
        entry.pop("image_id", None)
    appliance.store.path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    older = appliance.store.read()

    assert older.images, "a record one schema older is not readable at all"
    assert older.version == 3
    assert "image_id" not in json.dumps(older.authority())

    # The fingerprint the older writer computed, reproduced the way it computed
    # it: sha256 over its own canonical authority object.
    expected = hashlib.sha256(
        json.dumps(older.authority(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert older.fingerprint == f"sha256:{expected}"
    # And it is genuinely a different object from the current shape, which is
    # what the trial gate would have tripped over.
    assert older.fingerprint != current


def test_an_unchanged_deployment_is_not_called_changed_by_an_older_record(tmp_path):
    """The observation is taken at the current schema, the record is not.

    Comparing them raw would make every appliance holding an older record look
    like one whose deployment had been replaced, and block the update that would
    have replaced the record.
    """

    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    payload = json.loads(appliance.store.path.read_text(encoding="utf-8"))
    payload["version"] = 3
    for entry in payload["images"]:
        entry.pop("image_id", None)
    appliance.store.path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    service = bootstrap_service(appliance.source, appliance.store, appliance.deployment)

    assert service.deployment_changed() == ()


def test_a_record_from_an_unknown_schema_is_not_guessed_at(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    payload = json.loads(appliance.store.path.read_text(encoding="utf-8"))
    payload["version"] = 99
    appliance.store.path.write_text(json.dumps(payload), encoding="utf-8")

    assert appliance.store.read().images == ()


# --- phase 1/19: the authority object itself ---------------------------------


def test_the_record_is_a_versioned_deployment_authority(tmp_path):
    appliance = TrialAppliance(tmp_path, influx=True)

    record = appliance.capture()
    payload = json.loads(appliance.store.path.read_text(encoding="utf-8"))

    assert payload["version"] == ab_bootstrap.RECORD_VERSION
    assert payload["compose"]["path"] == str(appliance.deployment.compose_file)
    assert payload["compose"]["sha256"].startswith("sha256:")
    assert payload["environment"]["path"] == str(appliance.deployment.environment_file)
    assert record.image(ab_bootstrap.ROLE_INFLUXDB).state == (
        ab_bootstrap.STATE_RUNNING
    )


def test_an_unknown_service_state_blocks_planning(tmp_path):
    appliance = TrialAppliance(tmp_path)

    class Unreachable(DockerEngine):
        def _inspect_container(self, args):
            if args[-1] == EMS_CONTAINER:
                return self._result(args, 125, "", "cannot connect to the Docker daemon")
            return super()._inspect_container(args)

    engine = Unreachable(compose_file=str(appliance.deployment.compose_file))
    engine.images = dict(appliance.source.images)
    engine.containers = dict(appliance.source.containers)
    service = bootstrap_service(engine, appliance.store, appliance.deployment)

    with pytest.raises(ab_bootstrap.BootstrapError) as excinfo:
        service.record_running_runtime()

    assert excinfo.value.code == "runtime_state_unknown"


# --- phase 5/23/24: what a loaded or pulled image has to prove ---------------


def test_a_seeded_image_is_verified_by_digest_and_not_by_load_output(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.capture()

    def wrong_digest(args, engine):
        other = Image(EMS_REFERENCE, "sha256:" + "9" * 64)
        engine.images[EMS_REFERENCE] = other
        return engine._result(args, 0, f"Loaded image: {EMS_REFERENCE}\n")

    appliance.target.load_result = wrong_digest
    appliance.target.registry.pop(EMS_REFERENCE)

    report = appliance.reconstruct()

    outcome = next(item for item in report.outcomes if item.role == ab_bootstrap.ROLE_EMS)
    assert not outcome.available
    assert "digest" in outcome.detail


def test_an_image_for_another_platform_is_refused(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    appliance.target.registry[EMS_REFERENCE] = Image(
        EMS_REFERENCE, EMS_DIGEST, architecture="amd64"
    )
    (appliance.store.seed_directory / f"{ab_bootstrap.ROLE_EMS}.tar").unlink()

    report = appliance.reconstruct()

    outcome = next(item for item in report.outcomes if item.role == ab_bootstrap.ROLE_EMS)
    assert not outcome.available
    assert "platform" in outcome.detail


def test_a_truncated_seed_archive_falls_back_to_the_exact_digest(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    (appliance.store.seed_directory / f"{ab_bootstrap.ROLE_EMS}.tar").write_bytes(b"")

    report = appliance.reconstruct()

    outcome = next(item for item in report.outcomes if item.role == ab_bootstrap.ROLE_EMS)
    assert outcome.source == SOURCE_REGISTRY
    assert appliance.target.pulled == [EMS_REFERENCE]


def test_a_seed_archive_that_matches_is_loaded_and_never_pulled(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.capture()

    report = appliance.reconstruct()

    assert all(outcome.source == SOURCE_SEED for outcome in report.outcomes)
    assert appliance.target.pulled == []


def test_the_seed_metadata_names_the_archive_hash_and_size(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()

    seed = record.seed(ab_bootstrap.ROLE_ADMIN)
    archive = appliance.store.seed_directory / seed["file"]
    assert seed["sha256"].startswith("sha256:")
    assert seed["size_bytes"] == archive.stat().st_size
    assert seed["reference"] == ADMIN_REFERENCE


# --- phase 30: the exact argv the production adapters build ------------------


def docker_argv(engine):
    return [args for tool, args in engine.calls if tool == "docker"]


def test_reconstruction_builds_exactly_the_expected_docker_argv(tmp_path):
    appliance = TrialAppliance(tmp_path, influx=True)
    appliance.capture()
    appliance.target.calls.clear()

    appliance.reconstruct()

    compose = str(appliance.deployment.compose_file)
    # The recorded compose file is authority and is never rewritten, so the
    # verified image is handed to compose as an overlay after it.
    pins = str(appliance.store.directory / ab_bootstrap.RUNTIME_IMAGE_PINS)
    argv = docker_argv(appliance.target)
    assert ("version", "--format", "{{.Server.Version}}") in argv
    assert ("load", "-i", str(appliance.store.seed_directory / "admin.tar")) in argv
    assert ("image", "inspect", "--format", "{{json .}}", ADMIN_REFERENCE) in argv
    for service in (INFLUX_SERVICE, ADMIN_SERVICE, EMS_SERVICE):
        assert (
            "compose", "-f", compose, "-f", pins, "up", "-d", "--no-deps", service
        ) in argv


def test_the_registry_fallback_names_a_digest_and_never_a_tag(tmp_path):
    appliance = TrialAppliance(tmp_path)
    appliance.capture()
    for role in (ab_bootstrap.ROLE_ADMIN, ab_bootstrap.ROLE_EMS):
        (appliance.store.seed_directory / f"{role}.tar").unlink()

    appliance.reconstruct()

    for reference in appliance.target.pulled:
        assert "@sha256:" in reference


def test_health_probes_the_admin_console_over_loopback_only(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()

    urls = []

    def probe(url, timeout):
        urls.append((url, timeout))
        return answering_admin(url, timeout)

    appliance.health(http_probe=probe)

    assert urls
    for url, timeout in urls:
        assert url.startswith("http://127.0.0.1:")
        assert timeout > 0


def test_an_admin_console_that_answers_something_else_is_not_admin(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()

    health = appliance.health(http_probe=lambda url, timeout: (200, "<html>nginx</html>"))

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert any("admin_runtime" in reason for reason in health.reasons)


def test_an_influxdb_recorded_as_running_is_a_required_runtime_gate(tmp_path):
    appliance = TrialAppliance(tmp_path, influx=True)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()
    appliance.target.remove_container(INFLUX_CONTAINER)

    health = appliance.health()

    assert health.result == ab_health.RESULT_UNHEALTHY
    assert any("influxdb_runtime" in reason for reason in health.reasons)


def test_an_appliance_without_influxdb_has_no_influx_gate(tmp_path):
    appliance = TrialAppliance(tmp_path)
    record = appliance.capture()
    appliance.arm(record)
    appliance.reconstruct()

    health = appliance.health()

    assert [gate.name for gate in health.gates if gate.name == "influxdb_runtime"] == []
    assert health.result == ab_health.RESULT_HEALTHY, health.reasons


def test_the_recorded_platform_is_part_of_the_authority(tmp_path):
    appliance = TrialAppliance(tmp_path)

    record = appliance.capture()

    entry = record.image(ab_bootstrap.ROLE_ADMIN)
    assert entry.platform == {"os": "linux", "architecture": "arm64"}
    assert entry.digest == ADMIN_DIGEST
    assert record.image(ab_bootstrap.ROLE_EMS).digest == EMS_DIGEST
    assert INFLUX_DIGEST not in json.dumps(record.to_dict())
