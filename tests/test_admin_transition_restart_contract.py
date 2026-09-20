# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable paired-build recovery across an Admin process restart.

These tests deliberately construct a second ``SystemAlignmentService`` with a
new ``PendingTransitionStore`` instance over the same state directory.  That is
the process boundary the route-level fakes cannot model: transition state is
durable, while deployment/upgrade job registries are not.
"""

from datetime import datetime, timedelta, timezone

import pytest

from admin.admin_update import (
    PendingTransitionStore,
    TransitionStateError,
    make_transition_record,
)
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
    replacement_activity=None,
    current_identity=None,
):
    return SystemAlignmentService(
        resolver=_Resolver(build),
        transition_store=PendingTransitionStore(state_dir),
        embedded_resources=embedded or _EmbeddedResources(),
        known_good_store=KnownGoodStore(state_dir),
        current_identity=current_identity
        or (lambda: _identity_for(build, role="admin")),
        current_ems_identity=lambda: running_ems["identity"],
        persistent_ref=lambda: build.admin_image,
        launcher=lambda _record: pytest.fail("aligned Admin must not launch updater"),
        now=now or (lambda: NOW),
        replacement_activity=replacement_activity,
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


# --- the deadline bounds inaction, not duration ----------------------------
#
# The whole chain between confirming an upgrade and the EMS job taking its
# claim -- Admin pull, recreate, reconnect, embedded-resource import -- had to
# fit inside one window measured from the moment the operator pressed the
# button. An installation that needed longer (a small board on a slow link)
# found every forward path refused with ``expired`` and could only be
# abandoned, with the new Admin already in place and the old EMS still running.


def _transition_at(state_dir, stage, *, ttl_seconds=60):
    build = _build()
    return PendingTransitionStore(state_dir).begin(
        make_transition_record(
            mode="guided_upgrade",
            system_tag=build.canonical_tag,
            build_id=build.build_id,
            revision=build.revision,
            admin_image=build.admin_image,
            admin_digest=build.admin_digest,
            ems_image=build.ems_image,
            ems_digest=build.ems_digest,
            stage=stage,
            ttl_seconds=ttl_seconds,
            now=NOW,
        ),
        now=NOW,
    )


def test_progress_renews_the_transition_window(tmp_path):
    """A step taken inside the window buys the next one a window of its own."""

    state_dir = tmp_path / "state"
    record = _transition_at(state_dir, STAGE_RESOURCES_VERIFIED)
    store = PendingTransitionStore(state_dir)

    store.advance(
        record.operation_id,
        expected_stage=STAGE_RESOURCES_VERIFIED,
        new_stage=STAGE_EMS_OPERATION_PENDING,
        now=NOW + timedelta(seconds=50),
    )

    # Past the deadline the record was created with, and still claimable.
    claimed = store.claim(
        record.operation_id,
        expected_stage=STAGE_EMS_OPERATION_PENDING,
        new_stage=STAGE_EMS_OPERATION_RUNNING,
        now=NOW + timedelta(seconds=100),
    )
    assert claimed is True
    assert store.read().stage == STAGE_EMS_OPERATION_RUNNING


def test_an_untouched_transition_still_expires(tmp_path):
    """Renewal must not turn the deadline off: nothing happened here."""

    state_dir = tmp_path / "state"
    record = _transition_at(state_dir, STAGE_EMS_OPERATION_PENDING)
    store = PendingTransitionStore(state_dir)

    with pytest.raises(TransitionStateError) as exc_info:
        store.claim(
            record.operation_id,
            expected_stage=STAGE_EMS_OPERATION_PENDING,
            new_stage=STAGE_EMS_OPERATION_RUNNING,
            now=NOW + timedelta(seconds=100),
        )
    assert exc_info.value.reason == "expired"


def test_a_forced_expiry_is_not_undone_by_the_next_step(tmp_path):
    """A record whose stored deadline precedes its creation stays expired.

    Test support forces expiry by writing a deadline in the past. Renewal reads
    the record's own window, so a negative one renews nothing.
    """

    state_dir = tmp_path / "state"
    record = _transition_at(state_dir, STAGE_RESOURCES_VERIFIED)
    store = PendingTransitionStore(state_dir)
    raw = store._read_raw()
    raw["expires_at"] = "2000-01-01T00:00:00Z"
    store._write_raw(raw)

    store.advance(
        record.operation_id,
        expected_stage=STAGE_RESOURCES_VERIFIED,
        new_stage=STAGE_EMS_OPERATION_PENDING,
        now=NOW,
    )
    assert store.read().is_expired(NOW) is True


# --- a replacement that is gone is not a replacement that is running -------
#
# admin_reconnect_pending is the one stage the Admin cannot leave by itself:
# the sidecar pulls, rewrites compose and recreates the container, and only the
# replacement Admin can report back. A sidecar killed outright -- power cut,
# OOM, a reboot mid-pull -- writes no failure, so the record stayed at that
# stage, fresh, with cancel refused as "mutation in progress", resume not
# offered and the advanced recovery refusing too. The console was unusable for
# every further build operation until the deadline ran out.


def _reconnect_pending(state_dir, *, build, ttl_seconds=3600):
    return PendingTransitionStore(state_dir).begin(
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
            ttl_seconds=ttl_seconds,
            now=NOW,
        ),
        now=NOW,
    )


def _stranger_admin(build):
    """A running Admin that is not the build the transition was to install."""

    return ImageIdentity(
        image_ref=build.admin_image,
        digest="sha256:" + "9" * 64,
        revision="a" * 40,
        channel=build.channel,
        build_id="v0.0.1-aaaaaaa",
        release_tag="v0.0.1",
    )


def test_a_dead_replacement_makes_the_reconnect_stage_escapable(tmp_path):
    """Gone, and the Admin it should have installed is not the one running.

    Both halves are the proof. The sidecar exits after a *successful* swap too,
    so its absence alone says nothing about whether the replacement happened.
    """

    state_dir = tmp_path / "state"
    build = _build()
    record = _reconnect_pending(state_dir, build=build)
    service = _service(
        state_dir,
        build=build,
        running_ems={"identity": _identity_for(build, role="ems")},
        current_identity=lambda: _stranger_admin(build),
        replacement_activity=lambda _operation_id: "inactive",
    )

    transition = service.status(operation_active=lambda _op: False)["transition"]
    assert transition["expired"] is False
    assert transition["replacement_active"] is False
    assert transition["cancel_available"] is True

    cancelled = service.cancel(operation_id=record.operation_id)
    assert cancelled["stage"] == "cancelled"


def test_a_succeeded_replacement_is_not_offered_as_an_abandon(tmp_path):
    """The sidecar is gone because it finished; the target Admin is answering.

    That operation is not stranded, it is waiting to be continued, and offering
    to abandon it invites discarding an upgrade one step from done.
    """

    state_dir = tmp_path / "state"
    build = _build()
    _reconnect_pending(state_dir, build=build)
    service = _service(
        state_dir,
        build=build,
        running_ems={"identity": _identity_for(build, role="ems")},
        replacement_activity=lambda _operation_id: "inactive",
    )

    transition = service.status(operation_active=lambda _op: False)["transition"]
    assert transition["replacement_active"] is None
    assert transition["cancel_available"] is False


def test_an_unreadable_running_admin_is_not_proof_the_replacement_is_gone(tmp_path):
    """Half the proof is missing, which is not the same as false.

    The running-Admin identity degrades to an all-``None`` identity when Docker
    will not answer, and the identity check reports that as unverifiable rather
    than as a mismatch. Only a verified different build proves the swap did not
    happen; reading "could not read it" as that proof offers to abandon an
    upgrade that may already have landed. The deadline stays the escape.
    """

    state_dir = tmp_path / "state"
    build = _build()

    def _docker_will_not_answer():
        raise RuntimeError("docker is not answering")

    for current_identity in (ImageIdentity, _docker_will_not_answer):
        record = _reconnect_pending(state_dir, build=build)
        service = _service(
            state_dir,
            build=build,
            running_ems={"identity": _identity_for(build, role="ems")},
            current_identity=current_identity,
            replacement_activity=lambda _operation_id: "inactive",
        )

        transition = service.status(operation_active=lambda _op: False)["transition"]
        assert transition["expired"] is False
        assert transition["replacement_active"] is None
        assert transition["cancel_available"] is False

        with pytest.raises(SystemAlignmentError):
            service.cancel(operation_id=record.operation_id)
        PendingTransitionStore(state_dir).clear()


def test_a_running_replacement_keeps_the_reconnect_stage_closed(tmp_path):
    """The sidecar is mid-pull; cancelling would leave compose half-rewritten."""

    state_dir = tmp_path / "state"
    build = _build()
    record = _reconnect_pending(state_dir, build=build)
    service = _service(
        state_dir,
        build=build,
        running_ems={"identity": _identity_for(build, role="ems")},
        replacement_activity=lambda _operation_id: "active",
    )

    transition = service.status(operation_active=lambda _op: False)["transition"]
    assert transition["replacement_active"] is True
    assert transition["cancel_available"] is False

    with pytest.raises(SystemAlignmentError) as exc_info:
        service.cancel(operation_id=record.operation_id)
    assert exc_info.value.code == "mutation_in_progress"


def test_an_unprovable_replacement_keeps_the_reconnect_stage_closed(tmp_path):
    """A daemon that cannot answer is not an answer; no probe at all is none either."""

    state_dir = tmp_path / "state"
    build = _build()
    running_ems = {"identity": _identity_for(build, role="ems")}
    for probe in (lambda _operation_id: "unknown", None):
        record = _reconnect_pending(state_dir, build=build)
        service = _service(
            state_dir,
            build=build,
            running_ems=running_ems,
            replacement_activity=probe,
        )

        transition = service.status(operation_active=lambda _op: False)["transition"]
        assert transition["replacement_active"] is None
        assert transition["cancel_available"] is False

        with pytest.raises(SystemAlignmentError):
            service.cancel(operation_id=record.operation_id)
        PendingTransitionStore(state_dir).clear()


def test_every_step_buys_the_same_window(tmp_path):
    """A step renews the window the record started with, never a longer one.

    The window is read off the record, and a record whose deadline has already
    been moved must not read the moved deadline as a longer window: a step that
    bought an hour leaves the next step an hour, not an hour plus everything
    that has elapsed since the operator confirmed. Six steps at ten-minute
    intervals used to end with a deadline three and a half hours out.
    """

    build = _build()
    running_admin = {
        "digest": build.admin_digest,
        "build_id": build.build_id,
        "revision": build.revision,
    }
    state_dir = tmp_path / "state"
    record = _transition_at(
        state_dir, STAGE_ADMIN_RECONNECT_PENDING, ttl_seconds=3600
    )
    store = PendingTransitionStore(state_dir)
    operation_id = record.operation_id
    steps = (
        lambda now: store.claim_admin_update(operation_id, now=now),
        lambda now: store.resume_after_admin_reconnect(
            operation_id, running_admin=running_admin, now=now
        ),
        lambda now: store.claim_resource_verification(operation_id, now=now),
        lambda now: store.advance(
            operation_id,
            expected_stage=STAGE_ADMIN_ALIGNED,
            new_stage=STAGE_RESOURCES_VERIFIED,
            now=now,
        ),
        lambda now: store.advance(
            operation_id,
            expected_stage=STAGE_RESOURCES_VERIFIED,
            new_stage=STAGE_EMS_OPERATION_PENDING,
            now=now,
        ),
        lambda now: store.claim(
            operation_id,
            expected_stage=STAGE_EMS_OPERATION_PENDING,
            new_stage=STAGE_EMS_OPERATION_RUNNING,
            now=now,
        ),
    )

    for index, step in enumerate(steps, start=1):
        now = NOW + timedelta(minutes=10 * index)
        step(now)
        renewed = store.read()
        assert not renewed.is_expired(now + timedelta(seconds=3599)), (
            f"step {index} left less than the window it started with"
        )
        assert renewed.is_expired(now + timedelta(seconds=3600)), (
            f"step {index} bought more than the window it started with"
        )


def test_a_step_that_lands_after_the_deadline_does_not_reopen_the_window(tmp_path):
    """Expiry is a one-way door, and a late step must not pull it shut again.

    The deadline bounds inaction, so progress renews it -- but once it has
    passed, the record is the operator's to abandon and the console has already
    offered that. A worker whose step finally lands must not take the offer
    back: an EMS operation that outran its window would otherwise re-arm a full
    window on entering healthcheck_pending, a stage that refuses cancellation
    on its own, and the way out would close for another hour.
    """

    state_dir = tmp_path / "state"
    record = _transition_at(
        state_dir, STAGE_EMS_OPERATION_RUNNING, ttl_seconds=3600
    )
    store = PendingTransitionStore(state_dir)
    late = NOW + timedelta(minutes=90)
    assert store.read().is_expired(late)

    advanced = store.advance(
        record.operation_id,
        expected_stage=STAGE_EMS_OPERATION_RUNNING,
        new_stage=STAGE_HEALTHCHECK_PENDING,
        now=late,
    )

    assert advanced.stage == STAGE_HEALTHCHECK_PENDING
    assert advanced.is_expired(late), "the late step moved a deadline that had passed"
    cancelled = store.cancel(operation_id=record.operation_id, now=late)
    assert cancelled.stage == "cancelled"


def test_a_proof_of_absence_names_the_claim_it_was_read_against(tmp_path):
    """A sidecar that claims after the probe is the mutation the stage refuses.

    The proof that the replacement is gone is read outside the store's lock. A
    sidecar that starts, claims the transition and begins its Compose rewrite
    in between is exactly what ``admin_reconnect_pending`` exists to protect,
    so the proof carries the claim it was read against and the store refuses
    one whose claim has moved.
    """

    state_dir = tmp_path / "state"
    record = _transition_at(
        state_dir, STAGE_ADMIN_RECONNECT_PENDING, ttl_seconds=3600
    )
    store = PendingTransitionStore(state_dir)
    observed = store.read().admin_update_claimed_at
    assert observed is None
    assert store.claim_admin_update(record.operation_id, now=NOW) is True

    with pytest.raises(TransitionStateError) as excinfo:
        store.cancel(
            operation_id=record.operation_id,
            now=NOW,
            replacement_inactive=True,
            replacement_claimed_at=observed,
        )
    assert excinfo.value.reason == "mutation_in_progress"
    assert store.read().stage == STAGE_ADMIN_RECONNECT_PENDING

    cancelled = store.cancel(
        operation_id=record.operation_id,
        now=NOW,
        replacement_inactive=True,
        replacement_claimed_at=store.read().admin_update_claimed_at,
    )
    assert cancelled.stage == "cancelled"


def test_a_repeated_step_does_not_renew_the_window(tmp_path):
    """Renewal follows progress, and repeating a step is not progress.

    This is what keeps the deadline reachable at all. An Admin that crashes and
    restarts re-enters the same continuation every time; if its idempotent
    no-ops wrote, each restart would push the deadline out by a whole window and
    a stage that refuses cancellation would never become escapable. Every one of
    these returns the record it found, and writing nothing is what makes that
    true.
    """

    build = _build()
    running_admin = {
        "digest": build.admin_digest,
        "build_id": build.build_id,
        "revision": build.revision,
    }
    later = NOW + timedelta(minutes=50)
    past_the_window = NOW + timedelta(minutes=61)

    # A claimed resource import, re-claimed by every restart.
    aligned_dir = tmp_path / "aligned"
    record = _transition_at(aligned_dir, STAGE_ADMIN_ALIGNED, ttl_seconds=3600)
    aligned = PendingTransitionStore(aligned_dir)
    assert aligned.claim_resource_verification(record.operation_id, now=NOW) is True
    deadline = aligned.read().expires_at
    assert aligned.claim_resource_verification(record.operation_id, now=later) is False
    assert aligned.read().expires_at == deadline
    assert aligned.read().is_expired(past_the_window) is True

    # A reconnect that already landed, polled again by every restart.
    reconnect_dir = tmp_path / "reconnect"
    record = _transition_at(
        reconnect_dir, STAGE_ADMIN_RECONNECT_PENDING, ttl_seconds=3600
    )
    reconnect = PendingTransitionStore(reconnect_dir)
    reconnect.resume_after_admin_reconnect(
        record.operation_id, running_admin=running_admin, now=NOW
    )
    deadline = reconnect.read().expires_at
    reconnect.resume_after_admin_reconnect(
        record.operation_id, running_admin=running_admin, now=later
    )
    assert reconnect.read().expires_at == deadline
    assert reconnect.read().is_expired(past_the_window) is True

    # An EMS claim that is already held, re-attempted by every restart.
    ems_dir = tmp_path / "ems"
    record = _transition_at(ems_dir, STAGE_EMS_OPERATION_PENDING, ttl_seconds=3600)
    ems = PendingTransitionStore(ems_dir)
    assert (
        ems.claim(
            record.operation_id,
            expected_stage=STAGE_EMS_OPERATION_PENDING,
            new_stage=STAGE_EMS_OPERATION_RUNNING,
            now=NOW,
        )
        is True
    )
    deadline = ems.read().expires_at
    assert (
        ems.claim(
            record.operation_id,
            expected_stage=STAGE_EMS_OPERATION_PENDING,
            new_stage=STAGE_EMS_OPERATION_RUNNING,
            now=later,
        )
        is False
    )
    assert ems.read().expires_at == deadline
    assert ems.read().is_expired(past_the_window) is True
