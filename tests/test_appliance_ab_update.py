# SPDX-License-Identifier: AGPL-3.0-or-later
"""Planning an OS update and writing it into the inactive slot.

Two properties are load-bearing and are asserted from several directions:

- the running slot's devices are never opened for writing, whatever the plan,
  the record or the disk say afterwards;
- until a booted target slot has proven itself, the default boot slot is exactly
  what it was, so an interrupted update costs a reboot and nothing else.

The block layer is the fake backend, so the destructive path runs in full
without a real partition. What the fake cannot prove — that a real controller
behaves this way — is the physical gate in docs/appliance/ab-hardware-validation.md.
"""

import pytest

from appliance import ab_blocks, os_update
from appliance.operations import STATE_FAILED_RECOVERABLE
from appliance.os_update import AUTHORITY_FIELD, OsUpdateError, TYPE_OS_UPDATE
from tests.helpers.appliance_ab import (
    DEVICE,
    PARTITIONS,
    ApplianceAbHost,
    build_ab_service,
)
from tests.helpers.appliance_ab_artifacts import ReleaseDirectory

pytestmark = [pytest.mark.unit, pytest.mark.simulation]


@pytest.fixture
def host(tmp_path):
    return ApplianceAbHost(tmp_path)


@pytest.fixture
def releases(tmp_path):
    directory = ReleaseDirectory(tmp_path)
    directory.publish()
    return directory


@pytest.fixture
def service(tmp_path, host, releases):
    return build_ab_service(tmp_path, host, releases)


RELEASE_ID = "ems-solarflow-appliance-1.5.0-arm64-ab"


def plan(service, **kwargs):
    active = service.operations.active()
    if active is not None:
        service.operations.cancel(active.operation_id)
    operation = service.operations.create(TYPE_OS_UPDATE)
    return operation, service.plan_update(operation, RELEASE_ID, **kwargs)


def confirm_and_run(service, operation):
    service.operations.await_confirmation(operation.operation_id, {"plan": True})
    record = service.operations.get(operation.operation_id, include_token=True)
    service.operations.confirm(operation.operation_id, record.confirmation_token)
    return service.execute(operation)


# --- the plan ----------------------------------------------------------------


def test_a_plan_names_both_slots_the_artifact_and_the_fallback(service):
    _operation, payload = plan(service)

    assert payload["blockers"] == []
    assert payload["current_slot"] == "A"
    assert payload["target_slot"] == "B"
    assert payload["current_release"] == "1.4.0"
    assert payload["target_release"] == "1.5.0"
    assert payload["artifact_digest"].startswith("sha256:")
    assert payload["expects_reboot"] is True
    assert "one-shot" in payload["automatic_fallback"]


def test_the_plan_binds_the_exact_physical_target(service):
    _operation, payload = plan(service)

    authority = payload[AUTHORITY_FIELD]
    assert authority["device"] == DEVICE
    assert authority["active_slot"] == "A"
    assert authority["target_slot"] == "B"
    assert authority["boot_device"] == f"{DEVICE}p3"
    assert authority["root_device"] == f"{DEVICE}p5"
    assert authority["boot_partuuid"] and authority["root_partuuid"]
    assert authority["boot_digest"].startswith("sha256:")
    assert authority["rootfs_digest"].startswith("sha256:")
    assert authority["layout_id"] == "ems-appliance-rota-v1"


def test_the_authority_fingerprint_changes_with_the_target(service):
    _operation, payload = plan(service)
    authority = os_update.authority_from_dict(payload[AUTHORITY_FIELD])
    other = os_update.authority_from_dict(
        dict(payload[AUTHORITY_FIELD]) | {"root_device": f"{DEVICE}p4"}
    )

    assert authority.fingerprint() != other.fingerprint()


# --- preconditions -----------------------------------------------------------


def blocker_codes(payload):
    return {entry["code"] for entry in payload["blockers"]}


def test_a_single_slot_appliance_is_told_to_re_image(tmp_path, releases):
    host = ApplianceAbHost(tmp_path)
    host.remove_layout_manifest()
    service = build_ab_service(tmp_path, host, releases)

    _operation, payload = plan(service)

    assert "ab_layout_not_present" in blocker_codes(payload)
    assert any("A/B-capable appliance image" in e["message"] for e in payload["blockers"])


def test_layout_drift_blocks_the_confirmation(tmp_path, host, releases):
    host.mount("/", "/dev/sda9")
    service = build_ab_service(tmp_path, host, releases)

    _operation, payload = plan(service)

    assert "layout_drift" in blocker_codes(payload)


def test_a_missing_persistent_partition_blocks_the_confirmation(tmp_path, host, releases):
    host.unmount("/persistent")
    service = build_ab_service(tmp_path, host, releases)

    _operation, payload = plan(service)

    assert "persistence_unavailable" in blocker_codes(payload)


def test_an_unsigned_artifact_is_never_planned(tmp_path, host, releases):
    releases.unsign(RELEASE_ID)
    service = build_ab_service(tmp_path, host, releases)
    operation = service.operations.create(TYPE_OS_UPDATE)

    with pytest.raises(Exception) as caught:
        service.plan_update(operation, RELEASE_ID)

    assert getattr(caught.value, "code", "") == "release_signature_missing"


def test_a_development_override_artifact_is_blocked_from_installing(tmp_path, host, releases):
    releases.unsign(RELEASE_ID)
    releases.allow_unsigned = True
    service = build_ab_service(tmp_path, host, releases)

    _operation, payload = plan(service)

    assert "artifact_not_signed" in blocker_codes(payload)


def test_an_incompatible_artifact_is_blocked(tmp_path, host, tmp_path_factory):
    directory = ReleaseDirectory(tmp_path_factory.mktemp("releases"))
    directory.publish(RELEASE_ID, manifest_overrides={"layout_id": "another-layout"})
    service = build_ab_service(tmp_path, host, directory)

    _operation, payload = plan(service)

    assert "artifact_layout_unknown" in blocker_codes(payload)


def test_an_inactive_partition_too_small_is_blocked(tmp_path, host, tmp_path_factory):
    directory = ReleaseDirectory(tmp_path_factory.mktemp("releases"))
    directory.publish(RELEASE_ID)
    from tests.helpers.appliance_ab import lsblk_payload

    host.set_block_devices(lsblk_payload(sizes={"system_b": 16}))
    service = build_ab_service(tmp_path, host, directory)
    _operation, payload = plan(service)

    assert "inactive_partition_too_small" in blocker_codes(payload)


def test_a_pending_trial_blocks_another_update(tmp_path, host, releases):
    service = build_ab_service(tmp_path, host, releases)
    operation, _payload = plan(service)
    confirm_and_run(service, operation)
    service.operations.finish(operation.operation_id, "succeeded")

    _second, payload = plan(service)

    assert "ab_trial_pending" in blocker_codes(payload)


def test_insufficient_staging_space_blocks_the_confirmation(tmp_path, host, releases):
    service = build_ab_service(
        tmp_path, host, releases, minimum_staging_bytes=1 << 62
    )

    _operation, payload = plan(service)

    assert "insufficient_staging_space" in blocker_codes(payload)


def test_the_running_build_is_only_written_again_as_an_explicit_repair(
    tmp_path, host, tmp_path_factory
):
    directory = ReleaseDirectory(tmp_path_factory.mktemp("releases"))
    directory.publish(RELEASE_ID, manifest_overrides={"build_id": "20260801-1"})
    service = build_ab_service(tmp_path, host, directory)

    _operation, payload = plan(service)
    assert "artifact_already_active" in blocker_codes(payload)

    _operation, repaired = plan(service, repair=True)
    assert "artifact_already_active" not in blocker_codes(repaired)


# --- the write ---------------------------------------------------------------


def test_a_confirmed_plan_writes_both_images_into_the_inactive_slot(service):
    operation, _payload = plan(service)

    result = confirm_and_run(service, operation)

    assert result["target_slot"] == "B"
    assert result["boot_device"] == f"{DEVICE}p3"
    assert result["root_device"] == f"{DEVICE}p5"
    assert result["default_slot_unchanged"] is True
    assert sorted(service.backend.contents) == [f"{DEVICE}p3", f"{DEVICE}p5"]


def test_the_active_slot_is_never_opened_for_writing(service):
    operation, _payload = plan(service)

    confirm_and_run(service, operation)

    written = {path for kind, path in service.backend.calls if kind == "write"}
    assert f"{DEVICE}p2" not in written
    assert f"{DEVICE}p4" not in written


def test_every_written_partition_is_read_back(service):
    operation, _payload = plan(service)

    confirm_and_run(service, operation)

    read = [path for kind, path in service.backend.calls if kind == "read"]
    assert read == [f"{DEVICE}p3", f"{DEVICE}p5"]


def test_arming_the_trial_never_moves_the_default_slot(host, service):
    """[tryboot] points at the target; [all] stays exactly where it was."""

    from appliance.ab_boot import parse_selector

    operation, _payload = plan(service)

    result = confirm_and_run(service, operation)

    selector = parse_selector(host.selector_path().read_text(encoding="utf-8"))
    assert selector.default_partition == 2
    assert selector.tryboot_partition == 3
    assert result["default_slot_unchanged"] is True
    assert result["stage"] == "tryboot_requested"


def test_the_pending_trial_is_durable_before_the_reboot(host, service):
    operation, _payload = plan(service)

    confirm_and_run(service, operation)

    trial = service.state.pending()
    assert trial.operation_id == operation.operation_id
    assert trial.source_slot == "A"
    assert trial.target_slot == "B"
    assert trial.expected_boot_partition == 3
    assert trial.committed is False
    assert trial.artifact_digest.startswith("sha256:")
    assert (host.ab_state_dir / "pending-trial.json").is_file()


def test_the_trial_reboot_uses_one_fixed_argument(service):
    operation, _payload = plan(service)

    confirm_and_run(service, operation)

    reboots = [
        args for tool, args, _ in service.runner.calls if tool == "systemctl" and args[:1] == ("reboot",)
    ]
    assert reboots == [("reboot", "0 tryboot")]


def test_the_target_slot_stops_being_a_rollback_candidate_before_the_first_byte(
    tmp_path, host, releases
):
    """An interrupted write must never leave a slot a rollback would trust."""

    from appliance.ab_state import SlotRecord

    service = build_ab_service(tmp_path, host, releases)
    service.state.record_known_good(SlotRecord(slot="B", build_id="old-build"))
    service.backend.fail_write_after = 4
    operation, _payload = plan(service)

    with pytest.raises(OsUpdateError):
        confirm_and_run(service, operation)

    assert service.state.slots().record("B") is None


@pytest.mark.parametrize(
    "failure, code",
    [
        ({"short_write_after": 8}, "block_write_short"),
        ({"fail_write_after": 8}, "block_write_failed"),
        ({"fail_flush": True}, "block_flush_failed"),
        ({"corrupt_readback": True}, "block_readback_mismatch"),
        ({"disappear_before_readback": True}, "block_device_unavailable"),
    ],
    ids=["short_write", "eio", "flush_failed", "corrupt_readback", "device_gone"],
)
def test_a_failed_write_leaves_the_default_slot_unchanged(
    tmp_path, host, releases, failure, code
):
    service = build_ab_service(tmp_path, host, releases)
    for key, value in failure.items():
        setattr(service.backend, key, value)
    before = host.selector_path().read_text(encoding="utf-8")
    operation, _payload = plan(service)

    with pytest.raises(OsUpdateError) as caught:
        confirm_and_run(service, operation)

    assert caught.value.code == code
    assert host.selector_path().read_text(encoding="utf-8") == before
    record = service.operations.get(operation.operation_id)
    assert record.state == STATE_FAILED_RECOVERABLE
    assert record.result["default_slot_unchanged"] is True


def test_a_busy_inactive_partition_is_never_written(tmp_path, host, releases):
    service = build_ab_service(tmp_path, host, releases)
    service.backend.mark_busy(f"{DEVICE}p3")
    operation, _payload = plan(service)

    with pytest.raises(OsUpdateError) as caught:
        confirm_and_run(service, operation)

    assert caught.value.code == "block_device_busy"


def test_the_staging_directory_is_removed_after_a_failed_write(tmp_path, host, releases):
    service = build_ab_service(tmp_path, host, releases)
    service.backend.fail_write_after = 8
    operation, _payload = plan(service)

    with pytest.raises(OsUpdateError):
        confirm_and_run(service, operation)

    assert not service.state.staging_dir.exists()


def test_the_staging_directory_is_removed_after_a_successful_write(service):
    operation, _payload = plan(service)

    confirm_and_run(service, operation)

    assert not service.state.staging_dir.exists()


# --- revalidation ------------------------------------------------------------


def test_a_plan_confirmed_against_a_changed_disk_is_refused(tmp_path, host, releases):
    service = build_ab_service(tmp_path, host, releases)
    operation, _payload = plan(service)
    service.operations.await_confirmation(operation.operation_id, {"plan": True})
    record = service.operations.get(operation.operation_id, include_token=True)
    service.operations.confirm(operation.operation_id, record.confirmation_token)
    # The appliance rebooted into the other slot between plan and confirmation.
    host.boot_slot("B")
    host.write_selector(default=3, trial=2)
    host.mount_defaults()
    service.probe = host.probe()

    with pytest.raises(OsUpdateError) as caught:
        service.execute(operation)

    assert caught.value.code in ("active_slot_changed", "inactive_slot_changed")
    assert service.backend.contents == {}


def test_an_authority_naming_the_active_slot_is_refused(tmp_path, host, releases):
    """Even a record that says so may not write the running slot."""

    service = build_ab_service(tmp_path, host, releases)
    operation, payload = plan(service)
    tampered = dict(payload[AUTHORITY_FIELD])
    tampered["root_device"] = f"{DEVICE}p4"
    service.operations.update_target(operation.operation_id, {AUTHORITY_FIELD: tampered})
    service.operations.await_confirmation(operation.operation_id, {"plan": True})
    record = service.operations.get(operation.operation_id, include_token=True)
    service.operations.confirm(operation.operation_id, record.confirmation_token)

    with pytest.raises(OsUpdateError) as caught:
        service.execute(operation)

    assert caught.value.code == "inactive_slot_changed"
    assert service.backend.contents == {}


def test_a_plan_without_a_write_authority_is_refused(tmp_path, host, releases):
    service = build_ab_service(tmp_path, host, releases)
    operation = service.operations.create(TYPE_OS_UPDATE)
    service.operations.await_confirmation(operation.operation_id, {"plan": True})
    record = service.operations.get(operation.operation_id, include_token=True)
    service.operations.confirm(operation.operation_id, record.confirmation_token)

    with pytest.raises(OsUpdateError) as caught:
        service.execute(operation)

    assert caught.value.code == "ab_authority_missing"


# --- status ------------------------------------------------------------------


def test_the_status_reports_the_slot_the_persistence_and_the_releases(service):
    payload = service.status()

    assert payload["mode"] == "ab"
    assert payload["active_slot"] == "A"
    assert payload["persistence"]["ok"] is True
    assert [item["release_id"] for item in payload["releases"]] == [RELEASE_ID]
    assert payload["ab_state"]["pending_trial"] is None


def test_a_real_block_backend_is_the_production_default(tmp_path, host, releases):
    from appliance.ab_state import AbStateStore
    from appliance.operations import OperationStore
    from appliance.os_update import OsUpdateService
    from types import SimpleNamespace

    service = OsUpdateService(
        paths=SimpleNamespace(),
        config=SimpleNamespace(),
        operations=OperationStore(tmp_path / "ops2"),
        catalogue=releases.catalogue(),
        state=AbStateStore(host.ab_state_dir),
        probe=host.probe(),
    )

    assert isinstance(service.backend, ab_blocks.RealBlockBackend)


def test_the_layout_partitions_and_the_fake_sizes_agree():
    """The harness must not quietly model a disk the layout does not describe."""

    assert {entry[2] for entry in PARTITIONS} == {f"{DEVICE}p{n}" for n in range(1, 7)}


# --- the written slot is proven before the firmware is pointed at it ----------


class RecordingInspector:
    """An inspector that records when it ran, relative to the selector."""

    def __init__(self, selector_path, *, report=None):
        self.selector_path = selector_path
        self.calls = []
        self.selector_at_inspection = None
        self._report = report

    def assert_no_leak(self):
        return True

    def inspect(self, authority, release, **kwargs):
        from appliance.ab_inspect import InspectionReport

        self.calls.append(authority.target_slot)
        self.selector_at_inspection = self.selector_path.read_text(encoding="utf-8")
        if self._report is not None:
            return self._report
        return InspectionReport(target_slot=authority.target_slot, findings=(), cleaned=True)


def refusing_report(problem="the written slot carries another build"):
    from appliance.ab_inspect import FAIL, Finding, InspectionReport

    return InspectionReport(
        target_slot="B",
        findings=(Finding("os_build_marker", FAIL, problem),),
        cleaned=True,
    )


def test_the_written_slot_is_inspected_before_the_trial_is_armed(tmp_path, host, releases):
    inspector = RecordingInspector(host.selector_path())
    service = build_ab_service(tmp_path, host, releases, inspector=inspector)
    operation, _payload = plan(service)

    confirm_and_run(service, operation)

    assert inspector.calls == ["B"]
    # The selector still pointed at the untouched pairing when the inspection ran.
    assert "boot_partition=3" not in inspector.selector_at_inspection.split("[tryboot]")[0]


def test_a_slot_that_fails_inspection_never_arms_the_trial(tmp_path, host, releases):
    inspector = RecordingInspector(host.selector_path(), report=refusing_report())
    service = build_ab_service(tmp_path, host, releases, inspector=inspector)
    operation, _payload = plan(service)
    before = host.selector_path().read_text(encoding="utf-8")

    with pytest.raises(OsUpdateError) as caught:
        confirm_and_run(service, operation)

    assert caught.value.code == "inactive_slot_inspection_failed"
    assert host.selector_path().read_text(encoding="utf-8") == before
    assert service.state.pending() is None


def test_a_failed_inspection_leaves_the_operation_recoverable(tmp_path, host, releases):
    inspector = RecordingInspector(host.selector_path(), report=refusing_report())
    service = build_ab_service(tmp_path, host, releases, inspector=inspector)
    operation, _payload = plan(service)

    with pytest.raises(OsUpdateError):
        confirm_and_run(service, operation)

    record = service.operations.get(operation.operation_id)
    assert record.result["default_slot_unchanged"] is True
    assert record.stage == "verifying_inactive"


def test_a_leaked_inspection_mount_blocks_the_next_plan(tmp_path, host, releases):
    from tests.helpers.appliance_ab import AcceptingInspector

    service = build_ab_service(
        tmp_path, host, releases, inspector=AcceptingInspector(leaked=True)
    )

    _operation, payload = plan(service)

    assert "inspection_mount_leaked" in blocker_codes(payload)


def test_the_runtime_is_seeded_before_the_trial_reboot(tmp_path, host, releases):
    """A slot with no registry access must still be able to start Admin."""

    class Bootstrap:
        def __init__(self):
            self.order = []

        def record_running_runtime(self):
            self.order.append("record")
            return object()

        def seed(self, record):
            self.order.append("seed")
            return ("admin",)

    bootstrap = Bootstrap()
    service = build_ab_service(tmp_path, host, releases)
    service.bootstrap = bootstrap
    operation, _payload = plan(service)

    result = confirm_and_run(service, operation)

    assert bootstrap.order == ["record", "seed"]
    assert result["runtime_seed"]["seeded"] == ["admin"]


def test_a_runtime_that_cannot_be_seeded_does_not_stop_the_trial(tmp_path, host, releases):
    """The trial slot can still pull; its health gates decide, not the seed."""

    class Failing:
        def record_running_runtime(self):
            raise RuntimeError("no space")

        def seed(self, record):
            raise AssertionError("never reached")

    service = build_ab_service(tmp_path, host, releases)
    service.bootstrap = Failing()
    operation, _payload = plan(service)

    result = confirm_and_run(service, operation)

    assert result["runtime_seed"]["seeded"] == []
    assert result["stage"] == "tryboot_requested"


# --- production prerequisites -------------------------------------------------


def test_a_missing_artifact_decoder_blocks_the_plan(service, monkeypatch):
    """Before the artifact is fetched, not after it has filled the partition."""

    from appliance import install_check

    monkeypatch.setattr(
        install_check, "_which", lambda tool: "" if tool == "zstd" else "/usr/bin/x"
    )
    _operation, payload = plan(service)

    codes = {blocker["code"] for blocker in payload["blockers"]}
    assert "artifact_decoder_missing" in codes
    assert payload["risk"] == "blocked"


def test_the_status_reports_decoder_readiness_apart_from_ab_support(service):
    status = service.status()

    assert status["artifacts"]["sparse_decoder_ready"] is True
    assert set(status["readiness"]) == {
        "hardware_supported",
        "artifact_decoder_ready",
        "sparse_decoder_ready",
        "persistence_ready",
        "host_identity_ready",
        "docker_reconstruction_ready",
        "layout_ready",
    }
