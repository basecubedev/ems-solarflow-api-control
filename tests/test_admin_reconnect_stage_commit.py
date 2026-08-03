# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reconnect-stage commit decides whether the Admin replacement may start.

Archive 104 closed the first durable boundary: a failure raised out of
``PendingTransitionStore.begin`` is classified against the durable record, so a
transition that really committed keeps its Guided Setup owner and one that
provably did not is compensated. The stage write that immediately follows had
the same ambiguity and none of the classification:

``admin_update_pending`` -> ``admin_reconnect_pending`` -> launch the replacement

``advance`` commits the reconnect stage atomically and then leaves its file
lock. A failure raised after that atomic replace — a lock release error, a
filesystem error on the way out — skipped the launcher, so the persisted
transition claimed a replacement Admin was expected while none had been
started. A retry read the durable ``admin_reconnect_pending`` record and
reported it, launching nothing: the workflow stayed parked forever.

The closing contract:

* the durable stage, never the exception, decides whether launching is safe;
* a proven committed stage launches the replacement exactly once;
* a proven uncommitted stage launches nothing and stays retryable, and the
  retry performs the stage write and the one launch;
* an outcome that cannot be proven fails closed with a stable error, keeps the
  workflow owner and starts nothing.

The second finding is the identity proof itself. Archive 104 compared only the
selected build fields, so a durable record that shared the operation id but
differed in ``request_fingerprint``, ``resource_strategy``, the Admin-alignment
decision or the orchestrator Admin was classified as the caller's own commit.
Commit classification now compares the canonical immutable projection of a
transition record — every field ``make_transition_record`` fixes, with only the
documented lifecycle fields excluded.

The store faults are armed deterministically at the real lock, rename and read
syscalls — never a sleep.

See ``docs/technical/admin-workflow-state.md``.
"""

import dataclasses
import errno
import fcntl
import os
from dataclasses import replace

import pytest

from admin.admin_update import (
    ADMIN_IMAGE_REPO,
    TRANSITION_IDENTITY_FIELDS,
    TRANSITION_MUTABLE_FIELDS,
    PendingTransitionStore,
    TransitionRecord,
    TransitionStateError,
    transition_identity,
)
from admin.image_identity import ImageIdentity
from admin.known_good import KnownGoodStore
from admin.system_alignment import SystemAlignmentError, SystemAlignmentService
from tests.test_admin_server import (
    _attach_system_alignment,
    _control_export_manager,
    _request,
    _serve,
)
from tests.test_admin_setup_continuation_authority import (
    T0,
    _Resolver,
    _Resources,
    _WorkflowLink,
    _build,
    _ems_identity_for,
    _workflows,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
    pytest.mark.system_build,
]

STAGE_UPDATE_PENDING = "admin_update_pending"
STAGE_RECONNECT_PENDING = "admin_reconnect_pending"
STAGE_COMMIT_UNPROVABLE = "transition_stage_commit_unprovable"


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _outdated_admin():
    """A running Admin that is not the selected build, so alignment is required."""

    return ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        digest="sha256:running-admin",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
    )


def _aligning_service(tmp_path, *, transitions=None, build=None, launcher=None):
    """A service whose next start must replace the Admin before it can continue."""

    build = build or _build(admin_digest="sha256:target-admin")
    transitions = transitions or PendingTransitionStore(tmp_path / "state")
    launched = []
    service = SystemAlignmentService(
        resolver=_Resolver(build),
        transition_store=transitions,
        embedded_resources=_Resources(),
        release_archive_resources=_Resources(),
        known_good_store=KnownGoodStore(tmp_path / "state"),
        current_identity=_outdated_admin,
        current_ems_identity=lambda: _ems_identity_for(build),
        persistent_ref=lambda: build.admin_image,
        launcher=launcher or launched.append,
        now=lambda: T0,
    )
    return service, transitions, build, launched


@pytest.fixture
def unlock_fault_at(monkeypatch):
    """Fault one exact lock release of one exact transition store.

    ``PendingTransitionStore`` releases its file lock in a ``finally`` that runs
    after the atomic replace, so a failure there raises out of the store call
    with the write already durable. Releases are counted per lock inode, so a
    workflow record written in the same request can never shift the ordinal:
    release 1 is ``begin``, release 2 is the reconnect-stage ``advance``.
    """

    armed = {"inode": None, "ordinal": None, "seen": 0, "error": None}
    real_flock = fcntl.flock

    def flock(fd, operation):
        if operation != fcntl.LOCK_UN or armed["inode"] is None:
            return real_flock(fd, operation)
        mine = os.fstat(fd).st_ino == armed["inode"]
        real_flock(fd, operation)
        if not mine:
            return None
        armed["seen"] += 1
        if armed["seen"] == armed["ordinal"]:
            raise armed["error"]
        return None

    monkeypatch.setattr(fcntl, "flock", flock)

    def arm(store, ordinal, error=None):
        store.state_dir.mkdir(parents=True, exist_ok=True)
        store.lock_path.touch()
        armed.update(
            inode=store.lock_path.stat().st_ino,
            ordinal=ordinal,
            seen=0,
            error=error or OSError(errno.EIO, "lock release failed"),
        )

    return arm


# --- a post-commit stage failure may not strand the reconnect -----------------


def test_reconnect_stage_post_commit_failure_launches_the_replacement_once(
    tmp_path, unlock_fault_at
):
    """The exact reproduction (01-05), on the real store's own lock boundary.

    Release 1 lets ``begin`` commit ``admin_update_pending``. Release 2 unlocks
    for real and then raises, so the reconnect stage *is* durable when the
    exception leaves ``advance``.
    """

    store, workflow_id = _workflows(tmp_path)
    service, transitions, build, launched = _aligning_service(tmp_path)
    link = _WorkflowLink(store, workflow_id)
    unlock_fault_at(transitions, 2)

    result = service.start_resolved(
        system_build=build, mode="fresh_install", pre_launch=link
    )

    durable = transitions.read()
    assert durable is not None, "the atomic replace really did commit"
    assert durable.stage == STAGE_RECONNECT_PENDING
    assert result["operation_id"] == durable.operation_id
    assert result["stage"] == STAGE_RECONNECT_PENDING
    assert [record.operation_id for record in launched] == [durable.operation_id], (
        "a durable reconnect stage must launch exactly one Admin replacement"
    )
    assert store.load()["operation_id"] == durable.operation_id, (
        "B0 keeps the operation it owns"
    )
    assert link.undone == [], "a durable transition is never compensated"


def test_reconnect_stage_post_commit_retry_starts_nothing_new(
    tmp_path, unlock_fault_at
):
    """One operation, one durable owner, at most one Admin replacement."""

    store, workflow_id = _workflows(tmp_path)
    service, transitions, build, launched = _aligning_service(tmp_path)
    unlock_fault_at(transitions, 2)
    first = service.start_resolved(
        system_build=build,
        mode="fresh_install",
        pre_launch=_WorkflowLink(store, workflow_id),
    )

    retry_link = _WorkflowLink(store, workflow_id)
    second = service.start_resolved(
        system_build=build, mode="fresh_install", pre_launch=retry_link
    )

    assert second["operation_id"] == first["operation_id"]
    assert second["stage"] == STAGE_RECONNECT_PENDING
    assert len(launched) == 1, "exactly one Admin replacement may ever run"
    assert retry_link.linked == [], "the resumed transition is already owned"
    assert transitions.read().operation_id == first["operation_id"]
    assert store.load()["operation_id"] == first["operation_id"]


class _ArmAfterCommitStore(PendingTransitionStore):
    """Commit the transition for real, then arm a fault on the next state write.

    Everything the production writer does still runs, so the reconnect-stage
    ``advance`` fails exactly the way a full or unreadable disk fails it — with
    the transition durable at ``admin_update_pending``.
    """

    def __init__(self, path, *, arm, error):
        super().__init__(path)
        self._arm = arm
        self._error = error

    def begin(self, record, *, now=None):
        committed = super().begin(record, now=now)
        self._arm(self.path, self._error)
        return committed


def _uncommitted_stage_service(tmp_path, rename_fault):
    """A service whose reconnect-stage write fails at its rename syscall."""

    transitions = _ArmAfterCommitStore(
        tmp_path / "state",
        arm=rename_fault,
        error=OSError(errno.ENOSPC, "No space left on device"),
    )
    return _aligning_service(tmp_path, transitions=transitions)


def test_uncommitted_reconnect_stage_launches_nothing(tmp_path, rename_fault):
    """A proven non-commit starts no replacement and keeps its workflow owner."""

    store, workflow_id = _workflows(tmp_path)
    service, transitions, build, launched = _uncommitted_stage_service(
        tmp_path, rename_fault
    )
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build, mode="fresh_install", pre_launch=link
        )

    assert exc.value.code == "transition_state_write_failed"
    assert launched == [], "an uncommitted reconnect stage may launch nothing"
    durable = transitions.read()
    assert durable.stage == STAGE_UPDATE_PENDING, "the stage write really did fail"
    assert store.load()["operation_id"] == durable.operation_id
    assert link.undone == [], "a durable transition keeps its owner"


def test_retry_after_uncommitted_reconnect_stage_commits_and_launches_once(
    tmp_path, rename_fault
):
    """The retry performs the missing stage write and the single launch."""

    store, workflow_id = _workflows(tmp_path)
    service, transitions, build, launched = _uncommitted_stage_service(
        tmp_path, rename_fault
    )
    with pytest.raises(SystemAlignmentError):
        service.start_resolved(
            system_build=build,
            mode="fresh_install",
            pre_launch=_WorkflowLink(store, workflow_id),
        )
    first = transitions.read().operation_id
    assert launched == []

    rename_fault(transitions.path)
    retry_link = _WorkflowLink(store, workflow_id)
    result = service.start_resolved(
        system_build=build, mode="fresh_install", pre_launch=retry_link
    )

    assert result["operation_id"] == first, "no second operation may be minted"
    assert result["stage"] == STAGE_RECONNECT_PENDING
    assert [record.operation_id for record in launched] == [first]
    assert retry_link.linked == [], "the resumed transition is already owned"
    assert transitions.read().stage == STAGE_RECONNECT_PENDING
    assert store.load()["operation_id"] == first


def test_second_retry_after_a_launched_reconnect_stage_launches_nothing(
    tmp_path, rename_fault
):
    """Once the replacement is launched, no further retry may launch again."""

    store, workflow_id = _workflows(tmp_path)
    service, transitions, build, launched = _uncommitted_stage_service(
        tmp_path, rename_fault
    )
    with pytest.raises(SystemAlignmentError):
        service.start_resolved(
            system_build=build,
            mode="fresh_install",
            pre_launch=_WorkflowLink(store, workflow_id),
        )
    rename_fault(transitions.path)
    service.start_resolved(system_build=build, mode="fresh_install")

    service.start_resolved(system_build=build, mode="fresh_install")

    assert len(launched) == 1
    assert transitions.read().operation_id == launched[0].operation_id


def test_launcher_failure_after_a_committed_reconnect_stage_is_unchanged(tmp_path):
    """The existing recoverable-launcher contract survives the classification."""

    def _explode(_record):
        raise RuntimeError("docker is unavailable")

    store, workflow_id = _workflows(tmp_path)
    service, transitions, build, _launched = _aligning_service(
        tmp_path, launcher=_explode
    )
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build, mode="fresh_install", pre_launch=link
        )

    assert exc.value.code == "admin_update_launch_failed"
    durable = transitions.read()
    assert durable.stage == "failed_recoverable"
    assert durable.resume_stage == STAGE_UPDATE_PENDING
    assert durable.error_code == "admin_update_launch_failed"
    assert link.undone == [], "a durable transition keeps its owner"
    assert store.load()["operation_id"] == durable.operation_id


# --- an unprovable stage outcome fails closed ---------------------------------


class _UnreadableAfterCommitStore(_ArmAfterCommitStore):
    """The durable stage cannot be read back after the transition is committed."""

    def __init__(self, path, *, arm):
        super().__init__(path, arm=arm, error=OSError(errno.EACCES, "denied"))


class _ForeignRecordStore(PendingTransitionStore):
    """Another operation is durable when the reconnect-stage write fails."""

    def advance(self, operation_id, *, expected_stage, new_stage, now=None):
        record = self._record_locked(operation_id)
        self._write_raw(
            replace(record, operation_id="f" * 32, stage=new_stage).as_dict()
        )
        raise OSError(errno.EIO, "the store lock could not be released")


def test_unreadable_reconnect_stage_outcome_fails_closed(tmp_path, read_fault):
    """An unprovable stage launches nothing and removes no workflow owner."""

    store, workflow_id = _workflows(tmp_path)
    transitions = _UnreadableAfterCommitStore(tmp_path / "state", arm=read_fault)
    service, _transitions, build, launched = _aligning_service(
        tmp_path, transitions=transitions
    )
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build, mode="fresh_install", pre_launch=link
        )

    assert exc.value.code == STAGE_COMMIT_UNPROVABLE
    assert "Traceback" not in exc.value.message
    assert launched == [], "an unprovable stage may launch nothing"
    assert link.undone == [], "an unprovable stage may not compensate"
    read_fault(transitions.path)
    durable = transitions.read()
    assert durable is not None, "no destructive cleanup"
    assert store.load()["operation_id"] == durable.operation_id
    assert store.load()["status"] == "active"


def test_foreign_durable_operation_is_unprovable(tmp_path):
    """A different operation in B1 is never treated as this caller's commit."""

    store, workflow_id = _workflows(tmp_path)
    transitions = _ForeignRecordStore(tmp_path / "state")
    service, _transitions, build, launched = _aligning_service(
        tmp_path, transitions=transitions
    )
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build, mode="fresh_install", pre_launch=link
        )

    assert exc.value.code == STAGE_COMMIT_UNPROVABLE
    assert launched == []
    assert link.undone == []
    assert transitions.read().operation_id == "f" * 32
    assert store.load()["operation_id"] == link.linked[0]


def test_store_error_on_the_reconnect_stage_keeps_its_stable_code(tmp_path):
    """A normalized store failure is still reported as itself, never as unprovable."""

    class _RefusingStore(PendingTransitionStore):
        def advance(self, operation_id, *, expected_stage, new_stage, now=None):
            raise TransitionStateError(
                "transition_state_write_failed",
                "the transition state could not be written",
            )

    store, workflow_id = _workflows(tmp_path)
    service, transitions, build, launched = _aligning_service(
        tmp_path, transitions=_RefusingStore(tmp_path / "state")
    )

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build,
            mode="fresh_install",
            pre_launch=_WorkflowLink(store, workflow_id),
        )

    assert exc.value.code == "transition_state_write_failed"
    assert launched == []
    assert transitions.read().stage == STAGE_UPDATE_PENDING


# --- commit identity is the whole immutable projection ------------------------


def test_transition_identity_covers_every_field_that_is_not_lifecycle_state():
    """A field added to the record is immutable identity unless declared mutable."""

    names = {field.name for field in dataclasses.fields(TransitionRecord)}

    assert TRANSITION_MUTABLE_FIELDS < names
    assert set(TRANSITION_IDENTITY_FIELDS) == names - TRANSITION_MUTABLE_FIELDS
    assert {
        "operation_id",
        "created_at",
        "expires_at",
        "next_step",
        "resume_path",
        "request_fingerprint",
        "admin_alignment_required",
        "compatibility_mode",
        "resource_strategy",
        "development_risk_acknowledged",
        "development_risk_acknowledged_for_tag",
        "orchestrator_admin_digest",
    } <= set(TRANSITION_IDENTITY_FIELDS)


def test_transition_identity_ignores_every_mutable_lifecycle_field(tmp_path):
    """Stage, timestamps, claims and failure metadata are not identity."""

    transitions = PendingTransitionStore(tmp_path / "state")
    service, _transitions, build, _launched = _aligning_service(
        tmp_path, transitions=transitions
    )
    service.start_resolved(system_build=build, mode="fresh_install")
    record = transitions.read()

    drifted = replace(
        record,
        stage="admin_aligned",
        updated_at="2026-07-14T13:00:00Z",
        admin_update_claimed_at="2026-07-14T13:00:00Z",
        resources_claimed_at="2026-07-14T13:00:00Z",
    )

    assert transition_identity(drifted) == transition_identity(record)


class _IdentityDriftStore(PendingTransitionStore):
    """Commit a record that shares the operation id but not its identity.

    The exception is raised after the atomic replace, so only a complete
    identity projection can tell the caller's operation from the one that
    actually became durable.
    """

    def __init__(self, path, *, drift):
        super().__init__(path)
        self.drift = drift

    def begin(self, record, *, now=None):
        super().begin(replace(record, **self.drift), now=now)
        raise OSError(errno.EIO, "the store lock could not be released")


IDENTITY_DRIFTS = {
    "request_fingerprint": {"request_fingerprint": "sha256:" + "a" * 64},
    "resource_strategy": {"resource_strategy": "release_archive"},
    "compatibility_mode": {"compatibility_mode": "legacy_release"},
    "admin_alignment": {"admin_alignment_required": False},
    "development_acknowledgement": {
        "development_risk_acknowledged": True,
        "development_risk_acknowledged_for_tag": "v0.8.0",
    },
    "orchestrator_admin": {
        "orchestrator_build_id": "v0.9.0-bbbbbbb",
        "orchestrator_revision": "b" * 40,
        "orchestrator_admin_image": f"{ADMIN_IMAGE_REPO}:v0.9.0",
        "orchestrator_admin_digest": "sha256:orchestrator-admin",
    },
    "expires_at": {"expires_at": "2026-07-14T18:00:00Z"},
    "next_step": {"next_step": "resume_admin"},
}


@pytest.mark.parametrize("name", sorted(IDENTITY_DRIFTS))
def test_commit_identity_drift_is_unprovable(tmp_path, name):
    """The same operation id is not proof; every immutable field must agree."""

    store, workflow_id = _workflows(tmp_path)
    transitions = _IdentityDriftStore(tmp_path / "state", drift=IDENTITY_DRIFTS[name])
    service, _transitions, build, launched = _aligning_service(
        tmp_path, transitions=transitions
    )
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build,
            mode="fresh_install",
            request_fingerprint="sha256:" + "b" * 64,
            pre_launch=link,
        )

    assert exc.value.code == "transition_commit_unprovable"
    assert launched == [], "an unprovable commit may launch nothing"
    assert link.undone == [], "an unprovable commit may not compensate blindly"
    assert store.load()["operation_id"] == link.linked[0]
    assert store.load()["status"] == "active"


def test_only_a_mutable_stage_difference_is_classified_by_the_stage_contract(tmp_path):
    """A durable record differing only in stage is this caller's own commit.

    The commit is therefore *not* unprovable and nothing is compensated: the
    caller keeps its workflow owner and continues on the durable transition.
    It still launches nothing, because the reconnect stage it finds was never
    written under its own dispatch — a stage that is already past
    ``admin_update_pending`` withdraws the launch rather than demanding one.
    """

    store, workflow_id = _workflows(tmp_path)
    transitions = _IdentityDriftStore(
        tmp_path / "state", drift={"stage": STAGE_RECONNECT_PENDING}
    )
    service, _transitions, build, launched = _aligning_service(
        tmp_path, transitions=transitions
    )
    link = _WorkflowLink(store, workflow_id)

    result = service.start_resolved(
        system_build=build, mode="fresh_install", pre_launch=link
    )

    assert result["stage"] == STAGE_RECONNECT_PENDING
    assert result["reconnect"] is True
    assert launched == [], "a reconnect stage this caller never wrote proves nothing"
    assert link.undone == []
    assert store.load()["operation_id"] == result["operation_id"]


# --- the Setup routes that create a transition --------------------------------


SETUP_TRANSITION_ROUTES = (
    "/api/setup/system-build/update-admin",
    "/api/setup/system-build/confirm",
    "/api/setup/releases/prepare",
    "/api/setup/automated/releases/prepare",
)
UPDATE_ADMIN = SETUP_TRANSITION_ROUTES[0]


def _authority(base):
    status, _, payload = _request(
        f"{base}/api/admin/start-path",
        method="POST",
        body={"choice": "setup_new", "confirm": True},
    )
    assert status == 200, payload
    return payload["setup_workflow_id"], payload["setup_intent_id"]


def _workflow_view(base):
    status, _, payload = _request(f"{base}/api/setup/workflow")
    assert status == 200, payload
    return payload.get("workflow")


def _route(base, path, workflow_id, intent_id, *, tag="v0.8.0"):
    return _request(
        f"{base}{path}",
        method="POST",
        body={"tag": tag, "setup_workflow_id": workflow_id},
        extra_headers={"X-Setup-Intent-ID": intent_id},
    )


@pytest.mark.parametrize("path", SETUP_TRANSITION_ROUTES)
def test_setup_route_reconnect_stage_post_commit_keeps_one_launch(
    tmp_path, unlock_fault_at, path
):
    """Every Setup transition-creating route, with Admin alignment required.

    ``update-admin`` is the only route that opens an Admin alignment; the three
    resource routes refuse an unaligned Admin before any transition exists. The
    invariant is the same either way: one owned operation, at most one Admin
    replacement, and a retry that starts nothing new.
    """

    service, transitions, _build, launched = _aligning_service(tmp_path)
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        workflow_id, intent_id = _authority(base)
        unlock_fault_at(transitions, 2)

        status, _, payload = _route(base, path, workflow_id, intent_id)

        if path != UPDATE_ADMIN:
            assert status == 409, payload
            assert payload["error"] == "system_build_alignment_required"
            assert transitions.read() is None, "no transition may be opened"
            assert launched == []
            assert _workflow_view(base)["operation_id"] is None
            return

        assert status == 202, payload
        durable = transitions.read()
        assert durable is not None
        assert durable.stage == STAGE_RECONNECT_PENDING
        assert [record.operation_id for record in launched] == [durable.operation_id]
        assert _workflow_view(base)["operation_id"] == durable.operation_id
        assert _workflow_view(base)["status"] == "active"

        _same_workflow, retry_intent = _authority(base)
        status, _, payload = _route(base, path, workflow_id, retry_intent)

        assert status == 202, payload
        assert len(launched) == 1, "a retry may not launch a second replacement"
        assert transitions.read().operation_id == durable.operation_id
        assert _workflow_view(base)["operation_id"] == durable.operation_id
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_route_uncommitted_reconnect_stage_is_retryable(tmp_path, rename_fault):
    """A proven non-commit answers the stable store failure and retries cleanly."""

    service, transitions, _build, launched = _uncommitted_stage_service(
        tmp_path, rename_fault
    )
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        workflow_id, intent_id = _authority(base)

        status, _, payload = _route(base, UPDATE_ADMIN, workflow_id, intent_id)

        assert status == 400, payload
        assert payload["error"] == "transition_state_write_failed"
        durable = transitions.read()
        assert durable.stage == STAGE_UPDATE_PENDING
        assert launched == []
        assert _workflow_view(base)["operation_id"] == durable.operation_id

        rename_fault(transitions.path)
        _same_workflow, retry_intent = _authority(base)
        status, _, payload = _route(base, UPDATE_ADMIN, workflow_id, retry_intent)

        assert status == 202, payload
        assert transitions.read().stage == STAGE_RECONNECT_PENDING
        assert [record.operation_id for record in launched] == [durable.operation_id]
        assert _workflow_view(base)["operation_id"] == durable.operation_id
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_route_unprovable_reconnect_stage_fails_closed(tmp_path, read_fault):
    """An unprovable stage outcome is a stable 500 that changes nothing."""

    transitions = _UnreadableAfterCommitStore(tmp_path / "state", arm=read_fault)
    service, _transitions, _build, launched = _aligning_service(
        tmp_path, transitions=transitions
    )
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        workflow_id, intent_id = _authority(base)

        status, _, payload = _route(base, UPDATE_ADMIN, workflow_id, intent_id)

        assert status == 500, payload
        assert payload["error"] == STAGE_COMMIT_UNPROVABLE
        assert "Traceback" not in payload.get("message", "")
        assert launched == []
        read_fault(transitions.path)
        durable = transitions.read()
        assert durable is not None, "no destructive cleanup"
        assert srv.setup_workflows.load()["operation_id"] == durable.operation_id
        assert srv.setup_workflows.load()["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()
