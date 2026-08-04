# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Guided Setup workflow owns every operation that can still commit for it.

Archive 101 bound Setup intents, transition *creation* and cancellation to the
exact workflow. Three gaps remained at the boundary between the durable Guided
Setup record (B0) and the shared System Alignment transition (B1):

* the workflow→transition link is persisted inside the pre-commit boundary, but a
  failure of the transition commit that follows it left B0 naming an operation
  that never existed in B1;
* ``claim_resource_verification`` marks an externally mutating stage while the
  visible stage still reads ``admin_aligned``, so cancellation — and therefore
  ``/api/setup/abandon`` — could succeed while the resource importer was still
  writing the shared cache;
* ``return-to-running-build`` cancels the failed Setup transition and *then*
  starts a new ``align_existing`` one, with no claim on the Setup workflow in
  between, so an abandon could terminalize the workflow and both sides still
  reported success.

The closing contract:

* a failed transition commit compare-and-restores the exact previous B0 link and
  never erases a newer one, while a launcher failure *after* a durable commit
  keeps the committed link;
* a claimed resource verification is an externally mutating stage for
  cancellation purposes;
* the shared return primitive is refused for Setup-mode transitions, so exactly
  one terminal action can win.

Three narrower gaps stayed open behind that contract, and the last sections here
close them:

* the compensation was reached only for a normalized ``TransitionStateError``, so
  a raw filesystem ``OSError`` out of ``PendingTransitionStore.begin`` left B0
  linked to an operation that never became durable;
* the compensating read used the fail-closed ``load()``, so an unreadable or
  corrupt B0 record read as "nothing to restore" and the route reported only the
  commit error while the stale link stayed on disk;
* expiry bypasses the durable resource claim on purpose (an orphaned record must
  stay escapable), but nothing proved whether a *live* importer still owned the
  operation, so an expired transition could be abandoned mid-mutation.

The closing contract for those three: every operational failure before the
durable commit runs the exact undo once; a compensation that cannot read the
durable record answers ``setup_transition_link_unreconciled`` instead of
pretending it was stale; and every resource import holds an
``OperationCoordinator`` claim, so expiry alone can no longer outrank a running
cache mutation while a restarted Admin — which holds no claim — keeps the escape.

Synchronization is explicit (``threading.Event``) — never a sleep.

See ``docs/technical/admin-workflow-state.md``.
"""

import errno
import fcntl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from admin.admin_update import (
    ADMIN_IMAGE_REPO,
    EMS_IMAGE_REPO,
    PendingTransitionStore,
    TransitionStateError,
)
from admin.guided_setup_workflow import (
    GuidedSetupWorkflowReadError,
    GuidedSetupWorkflowStore,
)
from admin.image_identity import ImageIdentity
from admin.known_good import KnownGoodStore
from admin.operation_coordinator import OperationCoordinator
from admin.setup_workflow import (
    SetupWorkflowAbandonError,
    abandon_setup_workflow,
)
from admin.system_alignment import SystemAlignmentError, SystemAlignmentService
from admin.system_build import SystemBuild
from tests.test_admin_server import (
    _attach_system_alignment,
    _control_export_manager,
    _request,
    _serve,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
    pytest.mark.system_build,
]

REVISION = "f7265fc747c2223f126f0ee7801e030c6226edf4"
T0 = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
TIMEOUT = 20


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _build(tag="v0.8.0", admin_digest="sha256:v080admin"):
    return SystemBuild(
        requested_tag=tag,
        canonical_tag=tag,
        channel="stable",
        revision=REVISION,
        build_id="v0.8.0-f7265fc",
        admin_image=f"{ADMIN_IMAGE_REPO}:{tag}",
        admin_digest=admin_digest,
        ems_image=f"{EMS_IMAGE_REPO}:{tag}",
        ems_digest=f"sha256:{tag}-ems",
        release_tag=tag,
    )


def _identity_for(build):
    return ImageIdentity(
        image_ref=build.admin_image,
        digest=build.admin_digest,
        revision=build.revision,
        build_id=build.build_id,
    )


def _ems_identity_for(build):
    return ImageIdentity(
        image_ref=build.ems_image,
        digest=build.ems_digest,
        revision=build.revision,
        channel=build.channel,
        build_id=build.build_id,
        release_tag=build.release_tag,
    )


class _Resolver:
    def __init__(self, *builds):
        self.builds = {build.canonical_tag: build for build in builds}
        self.resolved = []

    def resolve(self, tag):
        self.resolved.append(tag)
        return self.builds[tag]


class _Resources:
    """Resource provider whose cache mutation is an observable boundary."""

    def __init__(self):
        self.imported = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def hold(self):
        self.release.clear()
        return self

    def verify(self, *, running_build):
        return running_build

    def import_into_cache(self, *, running_build):
        self.entered.set()
        assert self.release.wait(TIMEOUT), "the held resource import was never released"
        self.imported.append(running_build)
        return running_build.get("canonical_tag")


def _no_worker(_operation_id):
    return False


def _transition_status(service):
    """The worker-aware status read production always performs."""

    return service.status(operation_active=_no_worker)["transition"]


class _Clock:
    """A service clock the test advances explicitly — never a sleep."""

    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)


def _service(
    tmp_path,
    *,
    build=None,
    resolver=None,
    resources=None,
    running=None,
    running_ems=None,
    transitions=None,
    known_good=None,
    launched=None,
    coordinator=None,
    now=None,
):
    build = build or _build()
    resources = resources or _Resources()
    launched = [] if launched is None else launched
    transitions = transitions or PendingTransitionStore(tmp_path / "state")
    known_good = known_good or KnownGoodStore(tmp_path / "state")
    service = SystemAlignmentService(
        resolver=resolver or _Resolver(build),
        transition_store=transitions,
        embedded_resources=resources,
        release_archive_resources=resources,
        known_good_store=known_good,
        current_identity=lambda: running or _identity_for(build),
        current_ems_identity=lambda: running_ems or _ems_identity_for(build),
        persistent_ref=lambda: build.admin_image,
        launcher=launched.append,
        now=now or (lambda: T0),
        # Only the worker-ownership cases bind the coordinator, so every other
        # case keeps exercising the unobserved-worker construction.
        **({} if coordinator is None else {"operation_coordinator": coordinator}),
    )
    return service, transitions, known_good, launched, resources


# --- finding 1: a failed transition commit must not leave B0 linked -----------


class _FailingBeginStore(PendingTransitionStore):
    """A transition store whose commit fails exactly ``failures`` times."""

    def __init__(self, path, *, failures=1):
        super().__init__(path)
        self.failures = failures
        self.begin_attempts = 0

    def begin(self, record, *, now=None):
        self.begin_attempts += 1
        if self.begin_attempts <= self.failures:
            raise TransitionStateError(
                "transition_state_write_failed",
                "the transition state could not be written",
            )
        return super().begin(record, now=now)


class _WorkflowLink:
    """The production pre-commit link: persist ownership, hand back its undo."""

    def __init__(self, workflows, workflow_id, *, mode="fresh_install", tag="v0.8.0"):
        self.workflows = workflows
        self.workflow_id = workflow_id
        self.mode = mode
        self.tag = tag
        self.linked = []
        self.undone = []

    def __call__(self, transition):
        operation_id = transition.operation_id
        record, previous = self.workflows.link_transition(
            self.workflow_id,
            operation_id=operation_id,
            transition_mode=self.mode,
            selected_system_tag=self.tag,
        )
        assert record is not None
        self.linked.append(operation_id)

        def undo():
            self.undone.append(operation_id)
            self.workflows.restore_transition_link(
                self.workflow_id,
                expected_operation_id=str(operation_id),
                previous_operation_id=previous,
            )

        return undo


def _workflows(tmp_path):
    store = GuidedSetupWorkflowStore(tmp_path)
    record = store.ensure_active(transition_mode="fresh_install")
    return store, record["workflow_id"]


def test_transition_commit_failure_restores_previous_workflow_link(tmp_path):
    """The exact reproduction: link persisted, commit failed, B0 stayed linked."""

    build = _build()
    store, workflow_id = _workflows(tmp_path)
    service, transitions, _, launched, _ = _service(
        tmp_path,
        build=build,
        transitions=_FailingBeginStore(tmp_path / "state"),
    )
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build, mode="fresh_install", pre_launch=link
        )

    assert exc.value.code == "transition_state_write_failed"
    assert link.linked, "the link must still be written inside the pre-commit boundary"
    assert link.undone == link.linked, "the exact link must be compensated"
    assert transitions.read() is None, "no transition may survive a failed commit"
    assert launched == [], "no Admin replacement may be launched"
    assert store.load()["operation_id"] is None
    assert store.load()["status"] == "active", "the workflow stays retryable"


def test_transition_commit_failure_launches_no_admin_replacement(tmp_path):
    """An unaligned Admin would normally be replaced; a failed commit must not."""

    build = _build(admin_digest="sha256:target-admin")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        digest="sha256:running-admin",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
    )
    store, workflow_id = _workflows(tmp_path)
    service, transitions, _, launched, _ = _service(
        tmp_path,
        build=build,
        running=running,
        transitions=_FailingBeginStore(tmp_path / "state"),
    )

    with pytest.raises(SystemAlignmentError):
        service.start_resolved(
            system_build=build,
            mode="fresh_install",
            pre_launch=_WorkflowLink(store, workflow_id),
        )

    assert launched == []
    assert transitions.read() is None
    assert store.load()["operation_id"] is None


def test_transition_link_rollback_does_not_clear_a_newer_operation(tmp_path):
    """A stale compensation must never erase a newer successful link."""

    store, workflow_id = _workflows(tmp_path)
    store.link_transition(
        workflow_id,
        operation_id="op-old",
        transition_mode="fresh_install",
        selected_system_tag="v0.8.0",
    )
    store.link_transition(
        workflow_id,
        operation_id="op-new",
        transition_mode="fresh_install",
        selected_system_tag="v0.8.0",
    )

    restored = store.restore_transition_link(
        workflow_id, expected_operation_id="op-old", previous_operation_id=None
    )

    assert restored is None, "a compare-and-restore that loses the race changes nothing"
    assert store.load()["operation_id"] == "op-new"


def test_transition_link_rollback_ignores_a_foreign_workflow(tmp_path):
    store, workflow_id = _workflows(tmp_path)
    store.link_transition(
        workflow_id,
        operation_id="op-1",
        transition_mode="fresh_install",
        selected_system_tag="v0.8.0",
    )

    assert (
        store.restore_transition_link(
            workflow_id + "x", expected_operation_id="op-1", previous_operation_id=None
        )
        is None
    )
    assert store.load()["operation_id"] == "op-1"


def test_launcher_failure_keeps_the_committed_operation_link(tmp_path):
    """Once B1 is durable the link is correct — a launcher failure never undoes it."""

    build = _build(admin_digest="sha256:target-admin")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        digest="sha256:running-admin",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
    )
    store, workflow_id = _workflows(tmp_path)

    def _explode(_record):
        raise RuntimeError("docker is unavailable")

    transitions = PendingTransitionStore(tmp_path / "state")
    service = SystemAlignmentService(
        resolver=_Resolver(build),
        transition_store=transitions,
        embedded_resources=_Resources(),
        known_good_store=KnownGoodStore(tmp_path / "state"),
        current_identity=lambda: running,
        current_ems_identity=lambda: _ems_identity_for(build),
        persistent_ref=lambda: build.admin_image,
        launcher=_explode,
        now=lambda: T0,
    )
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build, mode="fresh_install", pre_launch=link
        )

    assert exc.value.code == "admin_update_launch_failed"
    assert link.undone == [], "a durable transition must keep its owner"
    record = transitions.read()
    assert record is not None
    assert store.load()["operation_id"] == record.operation_id


def test_retry_after_transition_commit_failure_links_cleanly(tmp_path):
    build = _build()
    store, workflow_id = _workflows(tmp_path)
    service, transitions, _, _, _ = _service(
        tmp_path,
        build=build,
        transitions=_FailingBeginStore(tmp_path / "state", failures=1),
    )

    with pytest.raises(SystemAlignmentError):
        service.start_resolved(
            system_build=build,
            mode="fresh_install",
            pre_launch=_WorkflowLink(store, workflow_id),
        )
    assert store.load()["operation_id"] is None

    result = service.start_resolved(
        system_build=build,
        mode="fresh_install",
        pre_launch=_WorkflowLink(store, workflow_id),
    )

    assert result["ok"] is True
    assert store.load()["operation_id"] == result["operation_id"]
    assert transitions.read().operation_id == result["operation_id"]


def _authority(base):
    """The exact workflow id and the one-shot intent issued for it."""

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


def _confirm(base, workflow_id, intent_id, *, tag="v0.8.0"):
    return _request(
        f"{base}{CONFIRM}",
        method="POST",
        body={"tag": tag, "setup_workflow_id": workflow_id},
        extra_headers={"X-Setup-Intent-ID": intent_id},
    )


CONFIRM = "/api/setup/system-build/confirm"


def test_confirm_route_leaves_no_workflow_link_when_the_commit_fails(tmp_path):
    """The exact route reproduction (01-08 of the finding).

    Confirm links the minted operation id into the workflow, the transition
    commit then fails, and the route answers ``transition_state_write_failed``.
    B1 holds no transition, so B0 must hold no reference to one either — while
    the workflow itself stays active and retryable.
    """

    build = _build()
    transitions = _FailingBeginStore(tmp_path / "state")
    service, *_ = _service(tmp_path, build=build, transitions=transitions)
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        workflow_id, intent_id = _authority(base)

        status, _, payload = _confirm(base, workflow_id, intent_id)

        assert status == 400, payload
        assert payload["error"] == "transition_state_write_failed"
        assert transitions.read() is None, "no System Alignment transition may exist"
        view = _workflow_view(base)
        assert view["workflow_id"] == workflow_id
        assert view["status"] == "active", "the workflow stays retryable"
        assert view["operation_id"] is None, (
            "B0 may not name an operation that never reached B1"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_confirm_route_retry_after_a_failed_commit_links_the_new_operation(tmp_path):
    build = _build()
    transitions = _FailingBeginStore(tmp_path / "state", failures=1)
    service, *_ = _service(tmp_path, build=build, transitions=transitions)
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        workflow_id, intent_id = _authority(base)
        status, _, _payload = _confirm(base, workflow_id, intent_id)
        assert status == 400
        assert _workflow_view(base)["operation_id"] is None

        # A refused authority check never spends the intent, but a consumed one
        # has to be reissued: request a fresh confirmation for the same workflow.
        _workflow_id, retry_intent = _authority(base)
        assert _workflow_id == workflow_id
        status, _, payload = _confirm(base, workflow_id, retry_intent)

        assert status == 200, payload
        record = transitions.read()
        assert record is not None
        assert _workflow_view(base)["operation_id"] == record.operation_id
    finally:
        srv.shutdown()
        srv.server_close()


def test_confirm_route_reports_an_unreconciled_link_when_the_undo_fails(tmp_path):
    """A failed compensation is its own error, never a silent "consistent"."""

    build = _build()
    transitions = _FailingBeginStore(tmp_path / "state")
    service, *_ = _service(tmp_path, build=build, transitions=transitions)
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        workflow_id, intent_id = _authority(base)

        def _unwritable(*_args, **_kwargs):
            raise OSError("read-only file system")

        srv.setup_workflows.restore_transition_link = _unwritable

        status, _, payload = _confirm(base, workflow_id, intent_id)

        assert status == 500, payload
        assert payload["error"] == "setup_transition_link_unreconciled"
        assert transitions.read() is None, "nothing may be started"
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_return_route_is_refused_with_an_actionable_conflict(tmp_path):
    service, transitions, launched, operation_id = _failed_setup_service(tmp_path)
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/system-alignment/return-to-running-build",
            method="POST",
            body={"operation_id": operation_id, "confirm": True},
        )

        assert status == 409, payload
        assert payload["error"] == "setup_return_unsupported"
        assert "Discard setup" in payload["message"]
        assert transitions.read().stage == "failed_recoverable"
        assert launched == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_guided_upgrade_return_route_is_unchanged(tmp_path):
    service, transitions, launched, operation_id = _failed_setup_service(
        tmp_path, mode="guided_upgrade"
    )
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/system-alignment/return-to-running-build",
            method="POST",
            body={"operation_id": operation_id, "confirm": True},
        )

        assert status == 200, payload
        assert payload["status"] == "admin_return_started"
        assert transitions.read().mode == "align_existing_install"
        assert len(launched) == 1
    finally:
        srv.shutdown()
        srv.server_close()


# --- finding 2: a claimed resource verification is externally mutating --------


def _admin_aligned(tmp_path, **kwargs):
    """A committed Setup transition parked at ``admin_aligned``."""

    service, transitions, known_good, launched, resources = _service(
        tmp_path, **kwargs
    )
    started = service.start_resolved(
        system_build=service.resolve("v0.8.0"), mode="fresh_install"
    )
    assert started["stage"] == "admin_aligned"
    return service, transitions, known_good, launched, resources, started["operation_id"]


def test_cancel_rejects_claimed_resource_verification(tmp_path):
    """The exact reproduction: cancel succeeded while the importer still ran."""

    resources = _Resources()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, resources=resources
    )
    resources.hold()
    failures = []

    def _verify():
        try:
            service.verify_resources(operation_id=operation_id)
        except Exception as exc:  # recorded, asserted after the join
            failures.append(exc)

    worker = threading.Thread(target=_verify, daemon=True)
    worker.start()
    assert resources.entered.wait(TIMEOUT), "the resource import never started"

    with pytest.raises(SystemAlignmentError) as exc:
        service.cancel(operation_id=operation_id)

    assert exc.value.code == "mutation_in_progress"
    assert transitions.read().stage == "admin_aligned"

    resources.release.set()
    worker.join(TIMEOUT)
    assert not worker.is_alive()
    assert failures == [], "the claimed verification must finish under its own claim"
    assert resources.imported, "the import completes rather than being orphaned"
    assert transitions.read().stage == "resources_verified"


def test_cancel_available_is_false_while_resources_are_claimed(tmp_path):
    resources = _Resources()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, resources=resources
    )
    assert _transition_status(service)["cancel_available"] is True

    resources.hold()
    worker = threading.Thread(
        target=lambda: service.verify_resources(operation_id=operation_id),
        daemon=True,
    )
    worker.start()
    assert resources.entered.wait(TIMEOUT)

    transition = _transition_status(service)
    assert transition["stage"] == "admin_aligned", (
        "the visible stage stays admin_aligned while the importer mutates"
    )
    assert transition["worker_status_available"] is True
    assert transition["cancel_available"] is False

    resources.release.set()
    worker.join(TIMEOUT)
    assert not worker.is_alive()


def test_cancel_is_allowed_after_resource_verification_completes(tmp_path):
    service, transitions, _, _, _, operation_id = _admin_aligned(tmp_path)
    service.verify_resources(operation_id=operation_id)

    assert _transition_status(service)["cancel_available"] is True
    cancelled = service.cancel(operation_id=operation_id)

    assert cancelled["stage"] == "cancelled"


class _FailingResources(_Resources):
    def import_into_cache(self, *, running_build):
        raise RuntimeError("the bundle is unreadable")


def test_cancel_is_allowed_after_failed_verification_is_retried_or_reset(tmp_path):
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, resources=_FailingResources()
    )

    with pytest.raises(SystemAlignmentError):
        service.verify_resources(operation_id=operation_id)

    record = transitions.read()
    assert record.stage == "failed_recoverable"
    assert record.resources_claimed_at, "the claim outlives the failed attempt"
    assert _transition_status(service)["cancel_available"] is True
    assert service.cancel(operation_id=operation_id)["stage"] == "cancelled"


def test_retry_clears_the_claim_and_restores_cancellability(tmp_path):
    resources = _FailingResources()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, resources=resources
    )
    with pytest.raises(SystemAlignmentError):
        service.verify_resources(operation_id=operation_id)

    retried = service.retry(operation_id=operation_id)

    assert retried["stage"] == "admin_aligned"
    assert transitions.read().resources_claimed_at is None
    assert _transition_status(service)["cancel_available"] is True


def _legacy_build(tag="v0.7.0"):
    return SystemBuild(
        requested_tag=tag,
        canonical_tag=tag,
        channel="stable",
        revision="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        build_id="123456789-1",
        admin_image=f"{ADMIN_IMAGE_REPO}:{tag}",
        admin_digest="sha256:legacy-admin",
        ems_image=f"{EMS_IMAGE_REPO}:{tag}",
        ems_digest="sha256:legacy-ems",
        release_tag=tag,
    )


def test_release_archive_strategy_shares_the_resource_claim(tmp_path):
    """Both resource strategies claim the same durable slot, so both block cancel."""

    build = _legacy_build()
    archive = _Resources()
    transitions = PendingTransitionStore(tmp_path / "state")
    service = SystemAlignmentService(
        resolver=_Resolver(build),
        transition_store=transitions,
        embedded_resources=_Resources(),
        release_archive_resources=archive,
        known_good_store=KnownGoodStore(tmp_path / "state"),
        current_identity=lambda: ImageIdentity(
            image_ref=f"{ADMIN_IMAGE_REPO}:v0.9.0",
            digest="sha256:modern-admin",
            revision="b" * 40,
            build_id="v0.9.0-bbbbbbb",
        ),
        current_ems_identity=lambda: _ems_identity_for(build),
        persistent_ref=lambda: f"{ADMIN_IMAGE_REPO}:v0.9.0",
        launcher=lambda record: None,
        now=lambda: T0,
    )
    started = service.start_resolved(system_build=build, mode="fresh_install")
    operation_id = started["operation_id"]
    assert started["stage"] == "admin_aligned"

    archive.hold()
    worker = threading.Thread(
        target=lambda: service.verify_resources(operation_id=operation_id),
        daemon=True,
    )
    worker.start()
    assert archive.entered.wait(TIMEOUT), "the release archive import never started"

    assert _transition_status(service)["cancel_available"] is False
    with pytest.raises(SystemAlignmentError) as exc:
        service.cancel(operation_id=operation_id)
    assert exc.value.code == "mutation_in_progress"

    archive.release.set()
    worker.join(TIMEOUT)
    assert not worker.is_alive()
    assert archive.imported, "the legacy strategy prepares the release archive"
    assert transitions.read().stage == "resources_verified"


def _setup_workflow_for(store, operation_id, *, tag="v0.8.0"):
    record = store.ensure_active(transition_mode="fresh_install")
    store.record_transition(
        record["workflow_id"],
        operation_id=operation_id,
        transition_mode="fresh_install",
        selected_system_tag=tag,
    )
    return record["workflow_id"]


def test_setup_abandon_waits_for_resource_verification(tmp_path):
    """A successful abandon must never precede a resource-cache mutation."""

    resources = _Resources()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, resources=resources
    )
    workflows = GuidedSetupWorkflowStore(tmp_path / "admin-data")
    workflow_id = _setup_workflow_for(workflows, operation_id)
    generated = workflows.generated_config_path(workflow_id)
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text('{"devices": []}\n', encoding="utf-8")

    resources.hold()
    worker = threading.Thread(
        target=lambda: service.verify_resources(operation_id=operation_id),
        daemon=True,
    )
    worker.start()
    assert resources.entered.wait(TIMEOUT)

    with pytest.raises(SystemAlignmentError) as exc:
        abandon_setup_workflow(
            alignment=service,
            workflows=workflows,
            workflow_id=workflow_id,
            status=service.status(),
        )

    assert exc.value.code == "mutation_in_progress"
    assert workflows.load()["status"] == "active", "nothing may be terminalized"
    assert generated.exists(), "nothing may be cleaned"

    resources.release.set()
    worker.join(TIMEOUT)
    assert not worker.is_alive()

    result = abandon_setup_workflow(
        alignment=service,
        workflows=workflows,
        workflow_id=workflow_id,
        status=service.status(),
    )

    assert result["ok"] is True
    assert workflows.load()["status"] == "abandoned"
    assert not generated.exists()


# --- finding 3: return-to-running may not outlive its Setup owner -------------


def _failed_setup_service(
    tmp_path, *, mode="fresh_install", transitions=None, resources=None
):
    """A ``failed_recoverable`` transition whose known-good rollback is available."""

    old = SystemBuild(
        requested_tag="v0.7.0",
        canonical_tag="v0.7.0",
        channel="stable",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
        admin_image=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        admin_digest="sha256:old-admin",
        ems_image=f"{EMS_IMAGE_REPO}:v0.7.0",
        ems_digest="sha256:old-ems",
        release_tag="v0.7.0",
    )
    target = _build(admin_digest="sha256:new-admin")
    transitions = transitions or PendingTransitionStore(tmp_path / "state")
    known_good = KnownGoodStore(tmp_path / "state")
    known_good.record(old)
    launched = []
    service = SystemAlignmentService(
        resolver=_Resolver(old, target),
        transition_store=transitions,
        embedded_resources=resources or _Resources(),
        known_good_store=known_good,
        current_identity=lambda: _identity_for(target),
        current_ems_identity=lambda: _ems_identity_for(old),
        persistent_ref=lambda: target.admin_image,
        launcher=launched.append,
        now=lambda: T0,
    )
    started = service.start(requested_tag="v0.8.0", mode=mode)
    operation_id = started["operation_id"]
    service.verify_resources(operation_id=operation_id)
    service.begin_ems_operation(operation_id=operation_id)
    assert service.claim_ems_operation(operation_id=operation_id)
    service.finish_ems_operation(operation_id=operation_id, succeeded=True)
    service.finish_healthcheck(operation_id=operation_id, passed=False)
    assert transitions.read().stage == "failed_recoverable"
    return service, transitions, launched, operation_id


def test_setup_return_to_running_is_rejected_without_a_durable_owner(tmp_path):
    """The shared return primitive is not offered to a Setup-owned transition."""

    service, transitions, launched, operation_id = _failed_setup_service(tmp_path)

    with pytest.raises(SystemAlignmentError) as exc:
        service.return_to_running_build(operation_id=operation_id, confirm=True)

    assert exc.value.code == "setup_return_unsupported"
    assert transitions.read().stage == "failed_recoverable"
    assert transitions.read().mode == "fresh_install"
    assert launched == [], "nothing may be launched by a refused return"


@pytest.mark.parametrize("mode", ["fresh_install", "automated_setup"])
def test_setup_return_is_not_offered_in_status(tmp_path, mode):
    service, _, _, _ = _failed_setup_service(tmp_path, mode=mode)

    transition = _transition_status(service)

    assert transition["mode"] == mode
    assert transition["return_available"] is False
    assert transition["cancel_available"] is True, "Discard setup stays available"


def test_foreign_workflow_cannot_return_setup_transition(tmp_path):
    """No workflow identity can authorize the Setup return path — not even its own."""

    service, transitions, launched, operation_id = _failed_setup_service(tmp_path)
    workflows = GuidedSetupWorkflowStore(tmp_path / "admin-data")
    _setup_workflow_for(workflows, operation_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.return_to_running_build(operation_id=operation_id, confirm=True)

    assert exc.value.code == "setup_return_unsupported"
    assert workflows.load()["operation_id"] == operation_id
    assert transitions.read().stage == "failed_recoverable"
    assert launched == []


class _PausingCancelStore(PendingTransitionStore):
    """Makes the gap between the return's cancel and its new commit observable."""

    def __init__(self, path):
        super().__init__(path)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.settled = threading.Event()
        self.arm_for = None
        self.arm_thread = None

    def arm(self, operation_id, thread_name):
        """Park only the return's own cancel — never another caller's."""

        self.arm_for = operation_id
        self.arm_thread = thread_name
        self.release.clear()

    def cancel(self, *, operation_id=None, now=None):
        record = super().cancel(operation_id=operation_id, now=now)
        if (
            self.arm_for is not None
            and operation_id == self.arm_for
            and threading.current_thread().name == self.arm_thread
        ):
            self.arm_for = None
            self.entered.set()
            self.settled.set()
            assert self.release.wait(TIMEOUT), "the parked return was never released"
        return record


def test_return_to_running_and_abandon_are_mutually_exclusive(tmp_path):
    """The exact reproduction: both terminal actions used to report success.

    The park point sits in the durable gap the shared return primitive opens
    between cancelling the old transition and committing the new one. Either the
    return never reaches it (refused up front) or it is parked there while the
    abandon runs — in both worlds exactly one side may end up having won.
    """

    transitions = _PausingCancelStore(tmp_path / "state")
    service, transitions, launched, operation_id = _failed_setup_service(
        tmp_path, transitions=transitions
    )
    workflows = GuidedSetupWorkflowStore(tmp_path / "admin-data")
    workflow_id = _setup_workflow_for(workflows, operation_id)

    returned = []
    transitions.arm(operation_id, "setup-return")

    def _return():
        try:
            returned.append(
                _call(
                    service.return_to_running_build,
                    operation_id=operation_id,
                    confirm=True,
                )
            )
        finally:
            # The same event the park point sets: whichever happens first, the
            # main thread continues without polling.
            transitions.settled.set()

    worker = threading.Thread(target=_return, name="setup-return", daemon=True)
    worker.start()
    assert transitions.settled.wait(TIMEOUT), "the return neither parked nor returned"
    parked = transitions.entered.is_set()

    abandoned = _call(
        abandon_setup_workflow,
        alignment=service,
        workflows=workflows,
        workflow_id=workflow_id,
        status=service.status(),
    )

    transitions.release.set()
    worker.join(TIMEOUT)
    assert not worker.is_alive()

    winners = [
        name
        for name, outcome in (("return", returned[0]), ("abandon", abandoned))
        if not isinstance(outcome, Exception)
    ]
    assert winners == ["abandon"], (
        f"exactly the abandon may win (return parked={parked}, "
        f"return={returned[0]!r}, abandon={abandoned!r})"
    )
    assert returned[0].code == "setup_return_unsupported"
    assert workflows.load()["status"] == "abandoned"
    assert transitions.read().stage == "cancelled"
    assert transitions.read().mode == "fresh_install"
    assert launched == [], "no align_existing transition may be launched"


def _call(func, **kwargs):
    """Run ``func`` and return its result or the exception it raised."""

    try:
        return func(**kwargs)
    except (SystemAlignmentError, SetupWorkflowAbandonError, TransitionStateError) as exc:
        return exc


def test_abandon_winner_prevents_align_existing_launch(tmp_path):
    service, transitions, launched, operation_id = _failed_setup_service(tmp_path)
    workflows = GuidedSetupWorkflowStore(tmp_path / "admin-data")
    workflow_id = _setup_workflow_for(workflows, operation_id)

    abandon_setup_workflow(
        alignment=service,
        workflows=workflows,
        workflow_id=workflow_id,
        status=service.status(),
    )

    with pytest.raises(SystemAlignmentError) as exc:
        service.return_to_running_build(operation_id=operation_id, confirm=True)

    assert exc.value.code in {"setup_return_unsupported", "invalid_transition"}
    assert transitions.read().stage == "cancelled"
    assert transitions.read().mode == "fresh_install"
    assert launched == [], "an abandoned workflow may not launch align_existing"


def test_return_winner_has_defined_durable_owner_or_is_rejected_for_setup(tmp_path):
    """Setup takes the reject branch; the workflow and its transition are intact."""

    service, transitions, launched, operation_id = _failed_setup_service(tmp_path)
    workflows = GuidedSetupWorkflowStore(tmp_path / "admin-data")
    workflow_id = _setup_workflow_for(workflows, operation_id)

    with pytest.raises(SystemAlignmentError):
        service.return_to_running_build(operation_id=operation_id, confirm=True)

    record = workflows.load()
    assert record["status"] == "active"
    assert record["operation_id"] == operation_id
    assert transitions.read().operation_id == operation_id
    assert launched == []
    # The refusal leaves the documented Setup recovery actions usable.
    assert _transition_status(service)["resume_available"] is True
    assert (
        abandon_setup_workflow(
            alignment=service,
            workflows=workflows,
            workflow_id=workflow_id,
            status=service.status(),
        )["ok"]
        is True
    )


def test_guided_upgrade_return_contract_is_unchanged(tmp_path):
    service, transitions, launched, operation_id = _failed_setup_service(
        tmp_path, mode="guided_upgrade"
    )

    assert _transition_status(service)["return_available"] is True
    result = service.return_to_running_build(operation_id=operation_id, confirm=True)

    assert result["status"] == "admin_return_started"
    assert result["target_system_tag"] == "v0.7.0"
    record = transitions.read()
    assert record.mode == "align_existing_install"
    assert record.system_tag == "v0.7.0"
    assert len(launched) == 1


def test_align_existing_return_contract_is_unchanged(tmp_path):
    service, transitions, launched, operation_id = _failed_setup_service(
        tmp_path, mode="align_existing_install"
    )

    result = service.return_to_running_build(operation_id=operation_id, confirm=True)

    assert result["status"] == "admin_return_started"
    assert transitions.read().mode == "align_existing_install"
    assert len(launched) == 1


# --- phase 2: the remaining shared continuation routes ------------------------


def test_resume_cannot_continue_after_a_successful_setup_abandon(tmp_path):
    """Resume needs no extra workflow claim: the cancelled record refuses it."""

    service, transitions, launched, operation_id = _failed_setup_service(tmp_path)
    workflows = GuidedSetupWorkflowStore(tmp_path / "admin-data")
    workflow_id = _setup_workflow_for(workflows, operation_id)
    abandon_setup_workflow(
        alignment=service,
        workflows=workflows,
        workflow_id=workflow_id,
        status=service.status(),
    )

    with pytest.raises(SystemAlignmentError) as retry_exc:
        service.retry(operation_id=operation_id)
    assert retry_exc.value.code == "not_resumable"

    with pytest.raises(SystemAlignmentError) as recover_exc:
        service.recover_ems_operation(operation_id=operation_id)
    assert recover_exc.value.code == "invalid_transition"

    with pytest.raises(SystemAlignmentError) as resume_exc:
        service.resume(operation_id=operation_id)
    assert resume_exc.value.code == "not_resumable"

    assert _transition_status(service)["resume_available"] is False
    assert launched == []


def test_verify_resources_cannot_continue_after_a_successful_setup_abandon(tmp_path):
    resources = _Resources()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, resources=resources
    )
    workflows = GuidedSetupWorkflowStore(tmp_path / "admin-data")
    workflow_id = _setup_workflow_for(workflows, operation_id)
    abandon_setup_workflow(
        alignment=service,
        workflows=workflows,
        workflow_id=workflow_id,
        status=service.status(),
    )

    with pytest.raises(SystemAlignmentError) as exc:
        service.verify_resources(operation_id=operation_id)

    assert exc.value.code == "invalid_transition"
    assert resources.imported == [], "no resource cache mutation may follow abandon"


def test_abandon_refuses_an_unowned_transition_before_touching_anything(tmp_path):
    """Archive 101's exact-ownership proof still gates every abandon."""

    service, transitions, _, operation_id = _failed_setup_service(tmp_path)
    workflows = GuidedSetupWorkflowStore(tmp_path / "admin-data")
    record = workflows.ensure_active(transition_mode="fresh_install")

    with pytest.raises(SetupWorkflowAbandonError) as exc:
        abandon_setup_workflow(
            alignment=service,
            workflows=workflows,
            workflow_id=record["workflow_id"],
            status=service.status(),
        )

    assert exc.value.code == "setup_transition_owner_unproven"
    assert transitions.read().stage == "failed_recoverable"


def _abandon_route(base, workflow_id):
    return _request(
        f"{base}/api/setup/abandon",
        method="POST",
        body={"setup_workflow_id": workflow_id},
    )


CONTINUATION_ROUTES = (
    ("/api/admin/system-alignment/resume", {}),
    ("/api/admin/system-alignment/verify-resources", {}),
    ("/api/admin/system-alignment/return-to-running-build", {"confirm": True}),
)


@pytest.mark.parametrize("route,extra", CONTINUATION_ROUTES)
def test_no_shared_continuation_route_survives_a_successful_setup_abandon(
    tmp_path, route, extra
):
    """The audited surface: none of the three may mutate after abandon won.

    A cancelled record is terminal, so every forward edge (retry, resume,
    resource claim, EMS recovery) and the return primitive fail closed on the
    durable state alone — no extra workflow claim is needed on these routes.
    """

    resources = _Resources()
    service, transitions, launched, operation_id = _failed_setup_service(
        tmp_path, resources=resources
    )
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        workflow_id, _intent = _authority(base)
        srv.setup_workflows.record_transition(
            workflow_id,
            operation_id=operation_id,
            transition_mode="fresh_install",
            selected_system_tag="v0.8.0",
        )
        status, _, payload = _abandon_route(base, workflow_id)
        assert status == 200, payload
        assert transitions.read().stage == "cancelled"
        imports_before = len(resources.imported)

        status, _, payload = _request(
            f"{base}{route}",
            method="POST",
            body={"operation_id": operation_id, **extra},
        )

        assert status == 409, (route, payload)
        assert payload["ok"] is False
        assert transitions.read().stage == "cancelled"
        assert len(resources.imported) == imports_before, (
            "no resource cache mutation may follow a successful abandon"
        )
        assert launched == [], "no Admin replacement may be launched"
        assert srv.setup_workflows.load()["status"] == "abandoned"
    finally:
        srv.shutdown()
        srv.server_close()


def test_resume_route_still_drives_a_live_setup_transition(tmp_path):
    """The audit must not close the route Guided Setup actually needs."""

    resources = _Resources()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, resources=resources
    )
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        status, _, payload = _request(
            f"{base}/api/admin/system-alignment/resume",
            method="POST",
            body={"operation_id": operation_id},
        )

        assert status == 200, payload
        assert payload["stage"] == "resources_verified"
        assert resources.imported, "the live transition still prepares its resources"
    finally:
        srv.shutdown()
        srv.server_close()


# --- finding 4: a raw store OSError must compensate like any commit failure ----


def _disk_full():
    return OSError(errno.ENOSPC, "No space left on device")


def _unwritable_commit(tmp_path, rename_fault, **kwargs):
    """A service whose real transition commit fails at its rename syscall."""

    transitions = PendingTransitionStore(tmp_path / "state")
    service, _transitions, _known_good, launched, _resources = _service(
        tmp_path, transitions=transitions, **kwargs
    )
    rename_fault(transitions.path, _disk_full())
    return service, transitions, launched


def test_oserror_transition_commit_restores_previous_workflow_link(
    tmp_path, rename_fault
):
    """The exact reproduction: the raw store error bypassed the undo entirely."""

    build = _build()
    store, workflow_id = _workflows(tmp_path)
    service, transitions, _launched = _unwritable_commit(
        tmp_path, rename_fault, build=build
    )
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError):
        service.start_resolved(
            system_build=build, mode="fresh_install", pre_launch=link
        )

    assert link.linked, "the link is still written inside the pre-commit boundary"
    assert link.undone == link.linked, "the exact link must be compensated once"
    assert transitions.read() is None, "no transition may survive a failed commit"
    assert store.load()["operation_id"] is None
    assert store.load()["status"] == "active", "the workflow stays retryable"


def test_oserror_transition_commit_launches_nothing(tmp_path, rename_fault):
    """An unaligned Admin would normally be replaced; a failed commit must not."""

    build = _build(admin_digest="sha256:target-admin")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        digest="sha256:running-admin",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
    )
    store, workflow_id = _workflows(tmp_path)
    service, transitions, launched = _unwritable_commit(
        tmp_path, rename_fault, build=build, running=running
    )

    with pytest.raises(SystemAlignmentError):
        service.start_resolved(
            system_build=build,
            mode="fresh_install",
            pre_launch=_WorkflowLink(store, workflow_id),
        )

    assert launched == [], "no Admin replacement may be launched"
    assert transitions.read() is None
    assert store.load()["operation_id"] is None


def test_oserror_transition_commit_is_normalized_to_stable_store_error(
    tmp_path, rename_fault
):
    """The raw filesystem error never reaches the caller or the route."""

    build = _build()
    store, workflow_id = _workflows(tmp_path)
    service, _transitions, _launched = _unwritable_commit(
        tmp_path, rename_fault, build=build
    )

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build,
            mode="fresh_install",
            pre_launch=_WorkflowLink(store, workflow_id),
        )

    assert exc.value.code == "transition_state_write_failed"


def test_retry_after_oserror_transition_commit_links_cleanly(tmp_path, rename_fault):
    build = _build()
    store, workflow_id = _workflows(tmp_path)
    service, transitions, _launched = _unwritable_commit(
        tmp_path, rename_fault, build=build
    )

    with pytest.raises(SystemAlignmentError):
        service.start_resolved(
            system_build=build,
            mode="fresh_install",
            pre_launch=_WorkflowLink(store, workflow_id),
        )
    assert store.load()["operation_id"] is None

    rename_fault(transitions.path)
    result = service.start_resolved(
        system_build=build,
        mode="fresh_install",
        pre_launch=_WorkflowLink(store, workflow_id),
    )

    assert result["ok"] is True
    assert store.load()["operation_id"] == result["operation_id"]
    assert transitions.read().operation_id == result["operation_id"]


def test_confirm_route_normalizes_a_raw_commit_oserror(tmp_path, rename_fault):
    """The route contract is the same for a raw disk failure as for a store error."""

    build = _build()
    transitions = PendingTransitionStore(tmp_path / "state")
    service, *_ = _service(tmp_path, build=build, transitions=transitions)
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        workflow_id, intent_id = _authority(base)
        rename_fault(transitions.path, _disk_full())

        status, _, payload = _confirm(base, workflow_id, intent_id)

        assert status == 400, payload
        assert payload["error"] == "transition_state_write_failed"
        assert transitions.read() is None, "no System Alignment transition may exist"
        view = _workflow_view(base)
        assert view["status"] == "active"
        assert view["operation_id"] is None, (
            "B0 may not name an operation that never reached B1"
        )
    finally:
        srv.shutdown()
        srv.server_close()


# --- finding 5: an unreadable compensation record is not a stale no-op --------


class _CommitFaultStore(PendingTransitionStore):
    """Fails the commit and applies ``on_commit`` at that exact moment.

    A compensation fault has to land between the link write and the compensating
    read, so it is triggered from inside the failing commit rather than before
    the request — where it would only break the route's authority read.
    """

    def __init__(self, path, *, on_commit=None):
        super().__init__(path)
        self.on_commit = on_commit
        self.begin_attempts = 0

    def begin(self, record, *, now=None):
        self.begin_attempts += 1
        if self.on_commit is not None:
            self.on_commit()
        raise TransitionStateError(
            "transition_state_write_failed",
            "the transition state could not be written",
        )


def _unreconciled_route(tmp_path, on_commit, *, build=None, running=None):
    """Serve a confirm/update-admin pair whose compensation read is faulted."""

    build = build or _build()
    record_path = {}
    transitions = _CommitFaultStore(
        tmp_path / "state", on_commit=lambda: on_commit(record_path["path"])
    )
    service, _transitions, _known_good, launched, _resources = _service(
        tmp_path, build=build, running=running, transitions=transitions
    )
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    record_path["path"] = srv.setup_workflows.path
    return srv, base, transitions, launched, record_path["path"]


def test_transition_link_restore_read_error_is_unreconciled(tmp_path, read_fault):
    """The exact reproduction: an unreadable record read as "nothing to restore"."""

    srv, base, transitions, launched, record_path = _unreconciled_route(
        tmp_path, lambda path: read_fault(path, OSError(errno.EACCES, "denied"))
    )
    try:
        workflow_id, intent_id = _authority(base)

        status, _, payload = _confirm(base, workflow_id, intent_id)

        assert status == 500, payload
        assert payload["error"] == "setup_transition_link_unreconciled"
        assert transitions.read() is None, "nothing may be started"
        assert launched == []

        read_fault(record_path)
        assert srv.setup_workflows.load()["operation_id"] is not None, (
            "an unprovable compensation may not report a restored link"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_transition_link_restore_malformed_record_is_unreconciled(tmp_path):
    corrupt = b'{"format_version": 2, "type": "guided_setup"'
    srv, base, transitions, launched, record_path = _unreconciled_route(
        tmp_path, lambda path: Path(path).write_bytes(corrupt)
    )
    try:
        workflow_id, intent_id = _authority(base)

        status, _, payload = _confirm(base, workflow_id, intent_id)

        assert status == 500, payload
        assert payload["error"] == "setup_transition_link_unreconciled"
        assert transitions.read() is None
        assert launched == []
        assert Path(record_path).read_bytes() == corrupt, (
            "a corrupt record is never cleared, replaced or reconstructed"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_transition_link_restore_operation_mismatch_remains_a_noop(tmp_path):
    """A stale compensation is harmless, so it must not raise either."""

    store, workflow_id = _workflows(tmp_path)
    store.link_transition(
        workflow_id,
        operation_id="op-new",
        transition_mode="fresh_install",
        selected_system_tag="v0.8.0",
    )

    assert (
        store.restore_transition_link(
            workflow_id,
            expected_operation_id="op-old",
            previous_operation_id=None,
        )
        is None
    )
    assert store.load()["operation_id"] == "op-new"


def test_reconciliation_read_rejects_every_unusable_durable_state(tmp_path):
    """Unreadable, oversized, malformed and foreign-shaped all fail closed."""

    store, workflow_id = _workflows(tmp_path)
    store.link_transition(
        workflow_id,
        operation_id="op-new",
        transition_mode="fresh_install",
        selected_system_tag="v0.8.0",
    )
    valid = store.path.read_bytes()
    oversized = b'{"padding": "' + b"x" * (64 * 1024) + b'"}'
    foreign = b'{"format_version": 1, "type": "guided_setup"}'

    for payload in (b"{not json", oversized, foreign):
        store.path.write_bytes(payload)
        with pytest.raises(GuidedSetupWorkflowReadError):
            store.restore_transition_link(
                workflow_id,
                expected_operation_id="op-new",
                previous_operation_id=None,
            )
        assert store.path.read_bytes() == payload, (
            "an unusable record is never cleared, replaced or reconstructed"
        )
        # The same state stays a fail-closed None for ordinary authority reads.
        assert store.load() is None
        assert store.active() is None

    store.path.write_bytes(valid)
    assert store.load()["operation_id"] == "op-new"


def test_reconciliation_read_treats_a_missing_record_as_nothing_to_restore(tmp_path):
    """No record can name a stale operation, so there is nothing to reconcile."""

    store, workflow_id = _workflows(tmp_path)
    store.path.unlink()

    assert (
        store.restore_transition_link(
            workflow_id,
            expected_operation_id="op-new",
            previous_operation_id=None,
        )
        is None
    )
    assert not store.path.exists(), "a missing record is not recreated"


def test_unreconciled_restore_launches_nothing(tmp_path, read_fault):
    """The launcher path: an unprovable compensation still replaces no Admin."""

    build = _build(admin_digest="sha256:target-admin")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        digest="sha256:running-admin",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
    )
    srv, base, transitions, launched, record_path = _unreconciled_route(
        tmp_path,
        lambda path: read_fault(path, OSError(errno.EACCES, "denied")),
        build=build,
        running=running,
    )
    try:
        workflow_id, intent_id = _authority(base)

        status, _, payload = _request(
            f"{base}/api/setup/system-build/update-admin",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": workflow_id},
            extra_headers={"X-Setup-Intent-ID": intent_id},
        )

        assert status == 500, payload
        assert payload["error"] == "setup_transition_link_unreconciled"
        assert launched == [], "no Admin replacement may be launched"
        assert transitions.read() is None
    finally:
        read_fault(record_path)
        srv.shutdown()
        srv.server_close()


# --- finding 6: expiry never overrides a live resource worker ------------------


def _held_import(service, operation_id, resources):
    """Start the resource importer and park it inside its cache mutation."""

    resources.hold()
    failures = []

    def _verify():
        try:
            service.verify_resources(operation_id=operation_id)
        except Exception as exc:  # recorded, asserted after the join
            failures.append(exc)

    worker = threading.Thread(target=_verify, name="resource-import", daemon=True)
    worker.start()
    assert resources.entered.wait(TIMEOUT), "the resource import never started"
    return worker, failures


def _settle(worker, resources):
    resources.release.set()
    worker.join(TIMEOUT)
    assert not worker.is_alive()


def _expired_live_import(tmp_path, *, resources=None, **kwargs):
    """An expired Setup transition whose resource importer is still running."""

    clock = _Clock()
    coordinator = OperationCoordinator()
    resources = resources or _Resources()
    service, transitions, _known_good, _launched, _resources, operation_id = (
        _admin_aligned(
            tmp_path,
            resources=resources,
            coordinator=coordinator,
            now=clock,
            **kwargs,
        )
    )
    worker, failures = _held_import(service, operation_id, resources)
    clock.advance(hours=2)
    assert transitions.read().is_expired(clock()), "the transition must be expired"
    return service, transitions, coordinator, resources, operation_id, worker, failures


def test_expired_transition_cannot_cancel_a_live_resource_worker(tmp_path):
    """The exact reproduction: expiry cancelled a running cache mutation."""

    (
        service,
        transitions,
        coordinator,
        resources,
        operation_id,
        worker,
        failures,
    ) = _expired_live_import(tmp_path)

    assert coordinator.is_active(operation_id) is True
    with pytest.raises(SystemAlignmentError) as exc:
        service.cancel(operation_id=operation_id, coordinator=coordinator)

    assert exc.value.code == "transition_worker_active"
    assert transitions.read().stage == "admin_aligned"
    assert resources.imported == [], "no mutation may have completed yet"

    _settle(worker, resources)
    assert failures == [], "the held import finishes under its own claim"
    assert resources.imported, "the import completes rather than being orphaned"
    assert transitions.read().stage == "resources_verified"
    assert coordinator.is_active(operation_id) is False
    assert service.cancel(operation_id=operation_id, coordinator=coordinator)[
        "stage"
    ] == "cancelled"


def test_status_keeps_cancel_unavailable_for_expired_live_resource_worker(tmp_path):
    (
        service,
        _transitions,
        coordinator,
        resources,
        _operation_id,
        worker,
        _failures,
    ) = _expired_live_import(tmp_path)

    transition = service.status(operation_active=coordinator.is_active)["transition"]

    assert transition["expired"] is True
    assert transition["stage"] == "admin_aligned"
    assert transition["worker_status_available"] is True
    assert transition["worker_active"] is True
    assert transition["cancel_available"] is False

    _settle(worker, resources)
    settled = service.status(operation_active=coordinator.is_active)["transition"]
    assert settled["cancel_available"] is True


def test_expired_setup_abandon_waits_for_live_resource_worker(tmp_path):
    """A successful abandon may never precede a resource-cache mutation."""

    (
        service,
        _transitions,
        coordinator,
        resources,
        operation_id,
        worker,
        _failures,
    ) = _expired_live_import(tmp_path)
    workflows = GuidedSetupWorkflowStore(tmp_path / "admin-data")
    workflow_id = _setup_workflow_for(workflows, operation_id)
    generated = workflows.generated_config_path(workflow_id)
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text('{"devices": []}\n', encoding="utf-8")

    def _abandon():
        return abandon_setup_workflow(
            alignment=service,
            coordinator=coordinator,
            workflows=workflows,
            workflow_id=workflow_id,
            status=service.status(operation_active=coordinator.is_active),
        )

    with pytest.raises(SystemAlignmentError) as exc:
        _abandon()

    assert exc.value.code == "transition_worker_active"
    assert workflows.load()["status"] == "active", "nothing may be terminalized"
    assert generated.exists(), "nothing may be cleaned"
    assert resources.imported == []

    _settle(worker, resources)
    result = _abandon()

    assert result["ok"] is True
    assert workflows.load()["status"] == "abandoned"
    assert not generated.exists()
    # The order that matters: the mutation is complete before abandon wins.
    assert resources.imported


@pytest.mark.parametrize("strategy_tag", ["v0.8.0", "v0.7.0"])
def test_expired_live_resource_worker_blocks_cancel_for_both_strategies(
    tmp_path, strategy_tag
):
    """Embedded bundle and release archive share the one worker claim."""

    build = _build() if strategy_tag == "v0.8.0" else _legacy_build()
    running = (
        _identity_for(build)
        if strategy_tag == "v0.8.0"
        else ImageIdentity(
            image_ref=f"{ADMIN_IMAGE_REPO}:v0.9.0",
            digest="sha256:modern-admin",
            revision="b" * 40,
            build_id="v0.9.0-bbbbbbb",
        )
    )
    clock = _Clock()
    coordinator = OperationCoordinator()
    resources = _Resources()
    service, transitions, _known_good, _launched, _resources = _service(
        tmp_path,
        build=build,
        resolver=_Resolver(build),
        resources=resources,
        running=running,
        coordinator=coordinator,
        now=clock,
    )
    started = service.start_resolved(system_build=build, mode="fresh_install")
    assert started["stage"] == "admin_aligned"
    operation_id = started["operation_id"]
    worker, failures = _held_import(service, operation_id, resources)
    clock.advance(hours=2)

    with pytest.raises(SystemAlignmentError) as exc:
        service.cancel(operation_id=operation_id, coordinator=coordinator)

    assert exc.value.code == "transition_worker_active"

    _settle(worker, resources)
    assert failures == []
    assert resources.imported, "the strategy's own importer still runs to completion"
    assert transitions.read().stage == "resources_verified"


def test_abandon_winner_prevents_resource_import_from_starting(tmp_path):
    """The abandonment marker alone closes the importer, before any durable read."""

    coordinator = OperationCoordinator()
    resources = _Resources()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, resources=resources, coordinator=coordinator
    )
    # Abandonment wins the coordinator without committing the durable cancel, so
    # only the claim can refuse the importer here.
    coordinator.abandon(operation_id, lambda: None)

    with pytest.raises(SystemAlignmentError):
        service.verify_resources(operation_id=operation_id)

    assert resources.imported == [], "no cache mutation may follow a won abandon"
    assert transitions.read().resources_claimed_at is None


def test_resource_worker_claim_releases_on_success(tmp_path):
    coordinator = OperationCoordinator()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, coordinator=coordinator
    )

    assert service.verify_resources(operation_id=operation_id)[
        "stage"
    ] == "resources_verified"

    assert coordinator.is_active(operation_id) is False
    assert service.cancel(operation_id=operation_id, coordinator=coordinator)[
        "stage"
    ] == "cancelled"


def test_resource_worker_claim_releases_on_failure(tmp_path):
    coordinator = OperationCoordinator()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, resources=_FailingResources(), coordinator=coordinator
    )

    with pytest.raises(SystemAlignmentError):
        service.verify_resources(operation_id=operation_id)

    assert coordinator.is_active(operation_id) is False
    assert transitions.read().stage == "failed_recoverable"
    assert service.cancel(operation_id=operation_id, coordinator=coordinator)[
        "stage"
    ] == "cancelled"


def test_expired_stale_resource_claim_is_abandonable_after_admin_restart(tmp_path):
    """The recovery escape survives: a restart holds no claim, so expiry wins."""

    clock = _Clock()
    resources = _Resources()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path,
        resources=resources,
        coordinator=OperationCoordinator(),
        now=clock,
    )
    worker, _failures = _held_import(service, operation_id, resources)
    clock.advance(hours=2)

    # A restarted Admin: the durable claim outlives the process, the in-memory
    # worker registry does not.
    restarted = OperationCoordinator()
    assert transitions.read().resources_claimed_at, "the durable claim is stale"
    transition = service.status(operation_active=restarted.is_active)["transition"]

    assert transition["expired"] is True
    assert transition["worker_status_available"] is True
    assert transition["worker_active"] is False
    assert transition["cancel_available"] is True
    assert service.cancel(operation_id=operation_id, coordinator=restarted)[
        "stage"
    ] == "cancelled"

    _settle(worker, resources)


def test_expired_live_resource_worker_blocks_the_setup_abandon_route(tmp_path):
    """The productive route wiring: the server's own coordinator is consulted."""

    clock = _Clock()
    coordinator = OperationCoordinator()
    resources = _Resources()
    service, transitions, _, _, _, operation_id = _admin_aligned(
        tmp_path, resources=resources, coordinator=coordinator, now=clock
    )
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    srv.operation_coordinator = coordinator
    srv.runtime.operation_coordinator = coordinator
    try:
        workflow_id, _intent = _authority(base)
        srv.setup_workflows.record_transition(
            workflow_id,
            operation_id=operation_id,
            transition_mode="fresh_install",
            selected_system_tag="v0.8.0",
        )
        worker, failures = _held_import(service, operation_id, resources)
        clock.advance(hours=2)

        status, _, payload = _abandon_route(base, workflow_id)

        assert status == 409, payload
        assert payload["error"] == "transition_worker_active"
        assert srv.setup_workflows.load()["status"] == "active"
        assert transitions.read().stage == "admin_aligned"

        _settle(worker, resources)
        assert failures == []

        status, _, payload = _abandon_route(base, workflow_id)
        assert status == 200, payload
        assert payload["ok"] is True
        assert transitions.read().stage == "cancelled"
    finally:
        srv.shutdown()
        srv.server_close()


# --- finding 7: only a proven non-commit may compensate its Setup owner --------


class _PostCommitFaultStore(PendingTransitionStore):
    """Commits the transition durably, then fails the way a post-commit fails.

    The atomic replace has already happened when the exception leaves ``begin``,
    so the exact operation *is* durable even though the caller only sees a
    failure. A wrapper is the deterministic form of the same window the real
    store has between ``os.replace`` and leaving its lock.
    """

    def __init__(self, path, *, failures=1, error=None):
        super().__init__(path)
        self.failures = failures
        self.error = error
        self.begin_attempts = 0

    def begin(self, record, *, now=None):
        self.begin_attempts += 1
        committed = super().begin(record, now=now)
        if self.begin_attempts <= self.failures:
            raise self.error or OSError(errno.EIO, "the store lock could not be released")
        return committed


@pytest.fixture
def unlock_fault(monkeypatch):
    """Fault the real store lock release, after its atomic replace committed.

    ``PendingTransitionStore`` releases its file lock in a ``finally`` that runs
    after the state file was replaced, so a failure there raises out of ``begin``
    with the transition already durable. Only the transition store locks, so a
    single armed fault is deterministic.
    """

    armed = {"error": None}
    real_flock = fcntl.flock

    def flock(fd, operation):
        error = armed["error"]
        if operation == fcntl.LOCK_UN and error is not None:
            armed["error"] = None
            real_flock(fd, operation)
            raise error
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", flock)

    def arm(error=None):
        armed["error"] = error or OSError(errno.EIO, "lock release failed")

    return arm


def test_post_commit_exception_does_not_remove_workflow_owner(
    tmp_path, unlock_fault
):
    """The exact reproduction (01-10), on the real store's own lock boundary."""

    build = _build()
    store, workflow_id = _workflows(tmp_path)
    service, transitions, _, launched, _ = _service(tmp_path, build=build)
    link = _WorkflowLink(store, workflow_id)
    unlock_fault()

    result = service.start_resolved(
        system_build=build, mode="fresh_install", pre_launch=link
    )

    durable = transitions.read()
    assert durable is not None, "the atomic replace really did commit"
    assert durable.operation_id == result["operation_id"]
    assert store.load()["operation_id"] == durable.operation_id, (
        "a durable transition must keep the workflow that owns it"
    )
    assert link.undone == [], "compensation may run only for a proven non-commit"
    assert launched == []


def test_post_commit_exception_does_not_leave_an_unowned_admin_update_transition(
    tmp_path,
):
    """Alignment requires an Admin replacement: it runs once, still owned."""

    build = _build(admin_digest="sha256:target-admin")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        digest="sha256:running-admin",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
    )
    store, workflow_id = _workflows(tmp_path)
    transitions = _PostCommitFaultStore(tmp_path / "state")
    service, _transitions, _known_good, launched, _resources = _service(
        tmp_path, build=build, running=running, transitions=transitions
    )
    link = _WorkflowLink(store, workflow_id)

    result = service.start_resolved(
        system_build=build, mode="fresh_install", pre_launch=link
    )

    durable = transitions.read()
    assert durable is not None
    assert durable.stage == "admin_reconnect_pending"
    assert result["operation_id"] == durable.operation_id
    assert store.load()["operation_id"] == durable.operation_id
    assert link.undone == []
    assert [record.operation_id for record in launched] == [durable.operation_id], (
        "the Admin replacement runs exactly once for the durable transition"
    )


def test_post_commit_exception_does_not_start_a_second_operation_on_retry(tmp_path):
    """A durable transition is resumed by the retry, never replaced by a new one."""

    build = _build()
    store, workflow_id = _workflows(tmp_path)
    transitions = _PostCommitFaultStore(tmp_path / "state")
    service, *_ = _service(tmp_path, build=build, transitions=transitions)

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
    assert transitions.begin_attempts == 1, "no second operation may be minted"
    assert retry_link.linked == [], "the resumed transition is already owned"
    assert store.load()["operation_id"] == first["operation_id"]
    assert transitions.read().operation_id == first["operation_id"]


def test_post_commit_launcher_failure_keeps_the_committed_owner(tmp_path):
    """The post-commit path may still fail — but never without its owner."""

    build = _build(admin_digest="sha256:target-admin")
    running = ImageIdentity(
        image_ref=f"{ADMIN_IMAGE_REPO}:v0.7.0",
        digest="sha256:running-admin",
        revision="a" * 40,
        build_id="v0.7.0-aaaaaaa",
    )
    store, workflow_id = _workflows(tmp_path)
    transitions = _PostCommitFaultStore(tmp_path / "state")

    def _explode(_record):
        raise RuntimeError("docker is unavailable")

    service = SystemAlignmentService(
        resolver=_Resolver(build),
        transition_store=transitions,
        embedded_resources=_Resources(),
        known_good_store=KnownGoodStore(tmp_path / "state"),
        current_identity=lambda: running,
        current_ems_identity=lambda: _ems_identity_for(build),
        persistent_ref=lambda: build.admin_image,
        launcher=_explode,
        now=lambda: T0,
    )
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build, mode="fresh_install", pre_launch=link
        )

    assert exc.value.code == "admin_update_launch_failed"
    assert link.undone == [], "a durable transition keeps its owner on any later failure"
    durable = transitions.read()
    assert durable.stage == "failed_recoverable"
    assert store.load()["operation_id"] == durable.operation_id


class _UnprovableCommitStore(PendingTransitionStore):
    """A commit whose durable outcome cannot be established afterwards."""

    def __init__(self, path, *, read_fault, commit=True):
        super().__init__(path)
        self._read_fault = read_fault
        self._commit = commit
        self.begin_attempts = 0

    def begin(self, record, *, now=None):
        self.begin_attempts += 1
        try:
            if self._commit:
                super().begin(record, now=now)
        finally:
            self._read_fault(self.path, OSError(errno.EACCES, "denied"))
        raise OSError(errno.EIO, "the transition state could not be written")


def test_unprovable_commit_outcome_keeps_the_workflow_owner(tmp_path, read_fault):
    """A durable transition whose state cannot be read back keeps its owner."""

    build = _build()
    store, workflow_id = _workflows(tmp_path)
    transitions = _UnprovableCommitStore(tmp_path / "state", read_fault=read_fault)
    service, _transitions, _known_good, launched, _resources = _service(
        tmp_path, build=build, transitions=transitions
    )
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build, mode="fresh_install", pre_launch=link
        )

    assert exc.value.code == "transition_commit_unprovable"
    assert link.undone == [], "an unprovable outcome may not compensate"
    assert launched == []
    read_fault(transitions.path)
    durable = transitions.read()
    assert durable is not None
    assert store.load()["operation_id"] == durable.operation_id


def test_unprovable_commit_outcome_never_compensates_blindly(tmp_path, read_fault):
    """Even with nothing committed, an unreadable B1 may not remove the owner."""

    build = _build()
    store, workflow_id = _workflows(tmp_path)
    transitions = _UnprovableCommitStore(
        tmp_path / "state", read_fault=read_fault, commit=False
    )
    service, *_ = _service(tmp_path, build=build, transitions=transitions)
    link = _WorkflowLink(store, workflow_id)

    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(
            system_build=build, mode="fresh_install", pre_launch=link
        )

    assert exc.value.code == "transition_commit_unprovable"
    assert link.undone == []
    read_fault(transitions.path)
    assert transitions.read() is None
    assert store.load()["operation_id"] == link.linked[0], (
        "ownership is retained until the outcome is proven"
    )
    assert store.load()["status"] == "active"


SETUP_TRANSITION_ROUTES = (
    "/api/setup/system-build/update-admin",
    "/api/setup/system-build/confirm",
    "/api/setup/releases/prepare",
    "/api/setup/automated/releases/prepare",
)


def _setup_transition_route(base, path, workflow_id, intent_id, *, tag="v0.8.0"):
    return _request(
        f"{base}{path}",
        method="POST",
        body={"tag": tag, "setup_workflow_id": workflow_id},
        extra_headers={"X-Setup-Intent-ID": intent_id},
    )


@pytest.mark.parametrize("path", SETUP_TRANSITION_ROUTES)
def test_setup_route_post_commit_failure_keeps_one_owned_operation(tmp_path, path):
    """Every Setup route that creates a transition keeps B0 and B1 in agreement."""

    build = _build()
    transitions = _PostCommitFaultStore(tmp_path / "state")
    service, *_ = _service(tmp_path, build=build, transitions=transitions)
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        workflow_id, intent_id = _authority(base)

        status, _, payload = _setup_transition_route(
            base, path, workflow_id, intent_id
        )

        assert status in {200, 202}, payload
        durable = transitions.read()
        assert durable is not None, "no orphaned failure may replace the commit"
        view = _workflow_view(base)
        assert view["operation_id"] == durable.operation_id, (
            "workflow and transition must still name the same operation"
        )
        assert view["status"] == "active"

        _same_workflow, retry_intent = _authority(base)
        status, _, payload = _setup_transition_route(
            base, path, workflow_id, retry_intent
        )

        assert status in {200, 202}, payload
        assert transitions.begin_attempts == 1, "a retry may not mint a second operation"
        assert transitions.read().operation_id == durable.operation_id
        assert _workflow_view(base)["operation_id"] == durable.operation_id
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_route_unprovable_commit_fails_closed(tmp_path, read_fault):
    """An unprovable outcome is a stable 500 that changes nothing on disk."""

    build = _build()
    transitions = _UnprovableCommitStore(tmp_path / "state", read_fault=read_fault)
    service, _transitions, _known_good, launched, _resources = _service(
        tmp_path, build=build, transitions=transitions
    )
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, service)
    try:
        workflow_id, intent_id = _authority(base)

        status, _, payload = _setup_transition_route(
            base, "/api/setup/system-build/confirm", workflow_id, intent_id
        )

        assert status == 500, payload
        assert payload["error"] == "transition_commit_unprovable"
        assert "Traceback" not in payload.get("message", "")
        assert launched == []
        read_fault(transitions.path)
        durable = transitions.read()
        assert durable is not None
        assert srv.setup_workflows.load()["operation_id"] == durable.operation_id, (
            "no destructive cleanup and no owner removal"
        )
        assert srv.setup_workflows.load()["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()
