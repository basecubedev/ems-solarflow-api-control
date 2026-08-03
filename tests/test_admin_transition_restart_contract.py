# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable paired-build recovery across an Admin process restart.

These tests deliberately construct a second ``SystemAlignmentService`` with a
new ``PendingTransitionStore`` instance over the same state directory.  That is
the process boundary the route-level fakes cannot model: transition state is
durable, while deployment/upgrade job registries are not.
"""

from datetime import datetime, timezone

import pytest

from admin.admin_update import PendingTransitionStore, make_transition_record
from admin.image_identity import ImageIdentity
from admin.operation_coordinator import OperationCoordinator
from admin.known_good import KnownGoodStore
from admin.system_alignment import SystemAlignmentError, SystemAlignmentService
from admin.system_build import SystemBuild


pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.workflow,
    pytest.mark.contract,
    pytest.mark.simulation,
]

REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

STAGE_ADMIN_RECONNECT_PENDING = "admin_reconnect_pending"
STAGE_ADMIN_ALIGNED = "admin_aligned"
STAGE_RESOURCES_VERIFIED = "resources_verified"
STAGE_EMS_OPERATION_PENDING = "ems_operation_pending"
STAGE_EMS_OPERATION_RUNNING = "ems_operation_running"
STAGE_HEALTHCHECK_PENDING = "healthcheck_pending"
STAGE_FAILED_RECOVERABLE = "failed_recoverable"
STAGE_COMPLETED = "completed"


def _build(
    *,
    tag="v0.8.0",
    revision=REVISION,
    build_id="v0.8.0-f7265fc",
    admin_digest="sha256:target-admin",
    ems_digest="sha256:target-ems",
):
    return SystemBuild(
        requested_tag=tag,
        canonical_tag=tag,
        channel="stable",
        revision=revision,
        build_id=build_id,
        admin_image=f"ghcr.io/basecubedev/ems-solarflow-admin:{tag}",
        admin_digest=admin_digest,
        ems_image=f"ghcr.io/basecubedev/ems-solarflow-api-control:{tag}",
        ems_digest=ems_digest,
        release_tag=tag,
    )


def _identity_for(build, *, role):
    image = build.admin_image if role == "admin" else build.ems_image
    digest = build.admin_digest if role == "admin" else build.ems_digest
    return ImageIdentity(
        image_ref=image,
        digest=digest,
        revision=build.revision,
        channel=build.channel,
        build_id=build.build_id,
        release_tag=build.release_tag,
    )


class _Resolver:
    def __init__(self, build):
        self.build = build

    def resolve(self, requested_tag):
        assert requested_tag == self.build.canonical_tag
        return self.build


class _EmbeddedResources:
    def __init__(self):
        self.imports = []

    def import_into_cache(self, *, running_build):
        self.imports.append(dict(running_build))
        return "verified"


def _service(
    state_dir,
    *,
    build,
    running_ems,
    embedded=None,
    now=None,
):
    return SystemAlignmentService(
        resolver=_Resolver(build),
        transition_store=PendingTransitionStore(state_dir),
        embedded_resources=embedded or _EmbeddedResources(),
        known_good_store=KnownGoodStore(state_dir),
        current_identity=lambda: _identity_for(build, role="admin"),
        current_ems_identity=lambda: running_ems["identity"],
        persistent_ref=lambda: build.admin_image,
        launcher=lambda _record: pytest.fail("aligned Admin must not launch updater"),
        now=now or (lambda: NOW),
    )


def _resources_verified(state_dir, *, build, running_ems):
    service = _service(state_dir, build=build, running_ems=running_ems)
    started = service.start(
        requested_tag=build.canonical_tag,
        mode="fresh_install",
    )
    operation_id = started["operation_id"]
    verified = service.verify_resources(operation_id=operation_id)
    assert verified["stage"] == STAGE_RESOURCES_VERIFIED
    return service, operation_id


def _healthcheck_pending(state_dir, *, build, running_ems):
    service, operation_id = _resources_verified(
        state_dir,
        build=build,
        running_ems=running_ems,
    )
    service.begin_ems_operation(operation_id=operation_id)
    assert service.claim_ems_operation(operation_id=operation_id) is True
    finished = service.finish_ems_operation(
        operation_id=operation_id,
        succeeded=True,
    )
    assert finished["stage"] == STAGE_HEALTHCHECK_PENDING
    return service, operation_id


def test_new_store_claims_durable_pending_ems_operation_once(tmp_path):
    """A crash after durable intent but before the claim may safely continue."""

    state_dir = tmp_path / "state"
    build = _build()
    running_ems = {"identity": _identity_for(build, role="ems")}
    before_restart, operation_id = _resources_verified(
        state_dir,
        build=build,
        running_ems=running_ems,
    )
    pending = before_restart.begin_ems_operation(operation_id=operation_id)
    assert pending["stage"] == STAGE_EMS_OPERATION_PENDING

    # New service and store objects model a newly-started Admin process.  The
    # repeated begin is an idempotent acknowledgement of the committed edge.
    after_restart = _service(
        state_dir,
        build=build,
        running_ems=running_ems,
    )
    repeated = after_restart.begin_ems_operation(operation_id=operation_id)
    assert repeated["stage"] == STAGE_EMS_OPERATION_PENDING
    assert after_restart.claim_ems_operation(operation_id=operation_id) is True
    assert after_restart.claim_ems_operation(operation_id=operation_id) is False
    assert PendingTransitionStore(state_dir).read().stage == STAGE_EMS_OPERATION_RUNNING


def test_restart_reconciles_running_target_to_healthcheck_without_reexecution(tmp_path):
    """An already-running exact target needs health checks, not another EMS write."""

    state_dir = tmp_path / "state"
    build = _build()
    running_ems = {"identity": _identity_for(build, role="ems")}
    before_restart, operation_id = _resources_verified(
        state_dir,
        build=build,
        running_ems=running_ems,
    )
    before_restart.begin_ems_operation(operation_id=operation_id)
    assert before_restart.claim_ems_operation(operation_id=operation_id) is True

    after_restart = _service(
        state_dir,
        build=build,
        running_ems=running_ems,
    )
    recovered = after_restart.recover_ems_operation(operation_id=operation_id)

    assert recovered["stage"] == STAGE_HEALTHCHECK_PENDING
    assert PendingTransitionStore(state_dir).read().stage == STAGE_HEALTHCHECK_PENDING
    assert KnownGoodStore(state_dir).current() is None


def test_restart_with_old_ems_marks_running_claim_recoverable_at_pending(tmp_path):
    """An abandoned claim must not pretend the old EMS completed the target build."""

    state_dir = tmp_path / "state"
    target = _build()
    previous = _build(
        tag="v0.7.0",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
        admin_digest="sha256:previous-admin",
        ems_digest="sha256:previous-ems",
    )
    running_ems = {"identity": _identity_for(previous, role="ems")}
    known_good = KnownGoodStore(state_dir)
    known_good.record(previous)
    before_restart, operation_id = _resources_verified(
        state_dir,
        build=target,
        running_ems=running_ems,
    )
    before_restart.begin_ems_operation(operation_id=operation_id)
    assert before_restart.claim_ems_operation(operation_id=operation_id) is True

    after_restart = _service(
        state_dir,
        build=target,
        running_ems=running_ems,
    )
    recovered = after_restart.recover_ems_operation(operation_id=operation_id)

    assert recovered["stage"] == STAGE_FAILED_RECOVERABLE
    record = PendingTransitionStore(state_dir).read()
    assert record.stage == STAGE_FAILED_RECOVERABLE
    assert record.failed_stage == STAGE_EMS_OPERATION_RUNNING
    assert record.resume_stage == STAGE_EMS_OPERATION_PENDING
    assert record.error_code == "ems_operation_interrupted"
    assert KnownGoodStore(state_dir).current()["build_id"] == previous.build_id


def test_restart_at_healthcheck_pending_completes_matching_healthy_build(tmp_path):
    """Recovery reruns only health verification before committing known-good."""

    state_dir = tmp_path / "state"
    build = _build()
    running_ems = {"identity": _identity_for(build, role="ems")}
    _before_restart, operation_id = _healthcheck_pending(
        state_dir,
        build=build,
        running_ems=running_ems,
    )

    after_restart = _service(
        state_dir,
        build=build,
        running_ems=running_ems,
    )
    recovered = after_restart.recover_ems_operation(
        operation_id=operation_id,
        healthcheck_passed=True,
    )

    assert recovered["stage"] == STAGE_COMPLETED
    assert PendingTransitionStore(state_dir).read().stage == STAGE_COMPLETED
    assert KnownGoodStore(state_dir).current()["build_id"] == build.build_id


def test_failed_healthcheck_recovers_after_restart_without_reclaiming_ems(tmp_path):
    """A health failure retries health, not the already-successful EMS mutation."""

    state_dir = tmp_path / "state"
    build = _build()
    running_ems = {"identity": _identity_for(build, role="ems")}
    before_restart, operation_id = _healthcheck_pending(
        state_dir,
        build=build,
        running_ems=running_ems,
    )
    failed = before_restart.finish_healthcheck(
        operation_id=operation_id,
        passed=False,
        error_code="healthcheck_failed",
        error_message="dashboard was not ready",
    )
    assert failed["stage"] == STAGE_FAILED_RECOVERABLE
    assert KnownGoodStore(state_dir).current() is None

    after_restart = _service(
        state_dir,
        build=build,
        running_ems=running_ems,
    )
    recovered = after_restart.recover_ems_operation(
        operation_id=operation_id,
        healthcheck_passed=True,
    )

    assert recovered["stage"] == STAGE_COMPLETED
    assert PendingTransitionStore(state_dir).read().stage == STAGE_COMPLETED
    assert KnownGoodStore(state_dir).current()["build_id"] == build.build_id
    with pytest.raises(SystemAlignmentError) as exc_info:
        after_restart.claim_ems_operation(operation_id=operation_id)
    assert getattr(exc_info.value, "code", None) == "not_resumable"


def test_expired_reconnect_pending_transition_is_escapable_after_restart(tmp_path):
    """The wedged live case: Admin replaced, but the reconnect resume never landed.

    The durable record sits at admin_reconnect_pending until its TTL runs out;
    every later resume (including after an Admin restart) fails with
    ``expired``. The console must still offer an escape: status reports the
    expired transition as cancellable, cancel succeeds, and a new operation
    for the same build may then begin.
    """

    state_dir = tmp_path / "state"
    build = _build()
    running_ems = {"identity": _identity_for(build, role="ems")}
    record = PendingTransitionStore(state_dir).begin(
        make_transition_record(
            mode="guided_upgrade",
            system_tag=build.canonical_tag,
            build_id=build.build_id,
            revision=build.revision,
            admin_image=build.admin_image,
            admin_digest=build.admin_digest,
            ems_image=build.ems_image,
            ems_digest=build.ems_digest,
            stage=STAGE_ADMIN_RECONNECT_PENDING,
            ttl_seconds=60,
            now=NOW,
        ),
        now=NOW,
    )

    later = datetime(2026, 7, 14, 14, 0, 0, tzinfo=timezone.utc)
    after_restart = _service(
        state_dir,
        build=build,
        running_ems=running_ems,
        now=lambda: later,
    )

    with pytest.raises(SystemAlignmentError) as resume_exc:
        after_restart.resume(operation_id=record.operation_id)
    assert resume_exc.value.code == "expired"

    # The restarted Admin owns a fresh, empty coordinator: the orphan holds no
    # claim, so the liveness lookup succeeds and proves the worker inactive.
    status = after_restart.status(
        operation_active=OperationCoordinator().is_active
    )
    assert status["active"] is True
    transition = status["transition"]
    assert transition["expired"] is True
    assert transition["worker_active"] is False
    assert transition["worker_status_available"] is True
    assert transition["cancel_available"] is True
    assert transition["resume_available"] is False

    cancelled = after_restart.cancel(operation_id=record.operation_id)
    assert cancelled["stage"] == "cancelled"
    assert after_restart.status()["active"] is False

    restarted = after_restart.start(
        requested_tag=build.canonical_tag,
        mode="guided_upgrade",
    )
    assert restarted["operation_id"] != record.operation_id
    assert restarted["stage"] == STAGE_ADMIN_ALIGNED
