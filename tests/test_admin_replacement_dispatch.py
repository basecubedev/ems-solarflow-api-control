# SPDX-License-Identifier: AGPL-3.0-or-later
"""One operation dispatches at most one Admin replacement.

Archive 105 made both durable commit boundaries decide against B1 instead of
against the exception, and it did so for one caller at a time. Two overlapping
retries of the same durable ``admin_update_pending`` transition still read the
same stage, and both entered the replacement start:

``A`` advances ``admin_update_pending`` -> ``admin_reconnect_pending``, ``B``'s
advance sees the new stage and returns idempotently — and then *both* invoke the
launcher. The second dispatch is the real Docker sidecar name collision, so it
raises, and the losing request marks the transition ``failed_recoverable`` with
``admin_update_launch_failed`` while the first replacement sidecar is already
running.

Guided Setup routes hold a ``SetupLifecycleCoordinator`` mutation claim, so two
mutations of one Setup workflow are already serialized. Guided Upgrade runs on
``ThreadingHTTPServer`` with no equivalent claim around the dispatch, so two
browser tabs, a replayed request or a network retry reach it.

The closing contract, per operation:

* one durable transition owner;
* one reconnect-stage reconciliation;
* at most one Admin replacement dispatch.

The dispatch owner commits the reconnect stage and launches. Every concurrent
caller waits for it, re-reads B1 and reports the operation it finds — it never
launches, and it never fails a transition another caller dispatched.

The claim alone does not close it. It is transient and disappears with its last
live caller, so a request that read ``admin_update_pending`` and was descheduled
before entering finds an empty claim and dispatched a second replacement anyway.
Its owner therefore re-reads B1 *under* the claim, and only an exact
``admin_update_pending`` authorizes a launcher call; the same two ownerships
guard ``retry()``, the other way into the launcher.

That left one gap, and it is what this module's retry section pins down. A claim
covers one *dispatch attempt*, and an explicit retry owes the operator a new
launcher call — so it may not be answered by the attempt whose failure it is
recovering from. While that failed attempt still has a waiter its entry is
alive, and a retry that reopened ``admin_update_pending`` durably *before*
entering the claim walked straight into it: it was handed the old
``admin_update_launch_failed``, launched nothing, and left the operation
stranded at a reopened ``admin_update_pending``. The recovery edge therefore
happens inside a *new* attempt, which detaches the settled one instead of
answering from it — detaches, so the old waiters still receive the old outcome,
and once, under the coordinator's guard, so two simultaneous retries share one
new attempt and one launch.

Concurrency is expressed with barriers and events only, never a sleep. Each test
names the exact interleaving it protects, and the orderings that no durable or
service-level signal can express — "a caller is now waiting inside the claim" —
are taken at the coordinator's one registration point.

See ``docs/technical/admin-workflow-state.md``.
"""

import dataclasses
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from admin.admin_update import make_transition_record
from admin.system_alignment import SystemAlignmentError
from tests.test_admin_reconnect_stage_commit import (
    STAGE_RECONNECT_PENDING,
    STAGE_UPDATE_PENDING,
    _aligning_service,
    _outdated_admin,
    _UnreadableAfterCommitStore,
    _uncommitted_stage_service,
    unlock_fault_at,  # noqa: F401 - a fixture this module reuses
)
from tests.test_admin_server import _attach_system_alignment, _request, _serve
from tests.test_admin_setup_continuation_authority import (
    T0,
    _identity_for,
    _WorkflowLink,
    _workflows,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.integration,
    pytest.mark.simulation,
    pytest.mark.system_build,
]

TIMEOUT = 20


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _seed_update_pending(
    transitions, build, *, mode="fresh_install", request_fingerprint=None
):
    """Persist exactly what an Admin-alignment start leaves durable at B1."""

    record = make_transition_record(
        mode=mode,
        system_tag=build.canonical_tag,
        build_id=build.build_id,
        revision=build.revision,
        admin_image=build.admin_image,
        admin_digest=build.admin_digest,
        ems_image=build.ems_image,
        ems_digest=build.ems_digest,
        stage=STAGE_UPDATE_PENDING,
        admin_alignment_required=True,
        request_fingerprint=request_fingerprint,
        now=T0,
    )
    return transitions.begin(record, now=T0).operation_id


def _park_at_dispatch(service, parties=2):
    """Release every caller together, at the point it would dispatch.

    The barrier sits *outside* the replacement start, so each party has already
    read the durable ``admin_update_pending`` stage and resolved its build. What
    happens from there is exactly what the service does under overlap.

    The park is single-shot: it is lifted by the first party through, so a test
    may still drive the unparked service afterwards.
    """

    barrier = threading.Barrier(parties, timeout=TIMEOUT)
    start_replacement = service._start_admin_replacement

    def parked(record, build, decision=None):
        if barrier.wait() == 0:
            service._start_admin_replacement = start_replacement
        return start_replacement(record, build, decision=decision)

    service._start_admin_replacement = parked
    return barrier


def _park_until_the_first_dispatch_returned(service):
    """Let the first caller leave its dispatch entirely before the second enters.

    The barrier proves both callers read the same durable ``admin_update_pending``
    stage; its per-thread index then picks the one that runs first. The delayed
    caller only enters the replacement start once the first has returned — so by
    the time it gets there the first caller has published its outcome, released
    the claim and dropped the last live interest in it.

    The park is single-shot: it is lifted before the delayed caller dispatches,
    so a test may still drive the unparked service afterwards.
    """

    read_b1 = threading.Barrier(2, timeout=TIMEOUT)
    returned = threading.Event()
    start_replacement = service._start_admin_replacement

    def parked(record, build, decision=None):
        if read_b1.wait():
            assert returned.wait(TIMEOUT), "the first dispatch never returned"
            service._start_admin_replacement = start_replacement
            return start_replacement(record, build, decision=decision)
        try:
            return start_replacement(record, build, decision=decision)
        finally:
            returned.set()

    service._start_admin_replacement = parked
    return read_b1


class _Callers:
    """``count`` threads running one call, released together, joined on demand.

    ``call`` receives the caller's 0-based index, so a test can hand each thread
    its own per-request state. Joining separately is what lets a test keep one
    caller parked inside the service while it drives the next one.
    """

    def __init__(self, call, count=2):
        self.results, self.errors = [], []
        self._call = call
        self._guard = threading.Lock()
        self._start = threading.Barrier(count, timeout=TIMEOUT)
        self._threads = [
            threading.Thread(target=self._run, args=(caller,))
            for caller in range(count)
        ]
        for thread in self._threads:
            thread.start()

    def _run(self, caller):
        self._start.wait()
        try:
            value = self._call(caller)
        except Exception as exc:
            with self._guard:
                self.errors.append(exc)
        else:
            with self._guard:
                self.results.append(value)

    def join(self):
        for thread in self._threads:
            thread.join(TIMEOUT)
        assert not any(
            thread.is_alive() for thread in self._threads
        ), "a caller never returned"
        return self.results, self.errors


def _concurrently(call, count=2):
    """Run ``call`` on ``count`` threads, released together, and collect outcomes."""

    return _Callers(call, count).join()


def _seed_failed_recoverable(transitions, build, *, mode="fresh_install"):
    """Persist exactly what a failed Admin replacement launch leaves durable."""

    operation_id = _seed_update_pending(transitions, build, mode=mode)
    transitions.mark_failed(
        operation_id,
        error_code="admin_update_launch_failed",
        error_message="Admin update could not be launched: docker is unavailable",
        resume_stage=STAGE_UPDATE_PENDING,
        now=T0,
    )
    return operation_id


def _claims_registered(service, count):
    """An event set once ``count`` further callers have registered an attempt.

    Registration is the one point every caller passes before it can block on a
    live claim, and it is shared by the start and the retry entry. No durable or
    service-level signal can express "a caller is now waiting inside the claim",
    so this is what orders an owner against a caller that is about to wait.

    Only callers that register after this call are counted, so a test may install
    it once the callers it is not ordering against have already claimed.
    """

    reached = threading.Event()
    guard = threading.Lock()
    seen = []
    coordinator = service._dispatch
    register = coordinator._enter

    def registered(*args, **kwargs):
        entry = register(*args, **kwargs)
        with guard:
            seen.append(entry)
            if len(seen) >= count:
                reached.set()
        return entry

    coordinator._enter = registered
    return reached


def _fault_between_the_retry_reads(service, fault):
    """Run ``fault`` after the retry's first read, before its authority read.

    ``retry()`` resolves the build from a first, unowned read; only the read
    taken under the new attempt's claim may authorize anything. Faulting exactly
    between them is what proves the second read is the one that decides.
    """

    resolve = service._resolved_transition_build

    def resolve_then_fault(record):
        build = resolve(record)
        fault()
        return build

    service._resolved_transition_build = resolve_then_fault


def _park_the_answered_caller(service):
    """Hold a caller that a settled attempt answers inside that attempt.

    Its live interest is what keeps the settled attempt's entry alive — exactly
    the state a launcher failure leaves behind while an overlapping caller is
    still being answered.
    """

    answering = threading.Event()
    release = threading.Event()
    report = service._report_completed_dispatch

    def parked(record, build, dispatch):
        answering.set()
        assert release.wait(TIMEOUT), "the answered caller was never released"
        return report(record, build, dispatch)

    service._report_completed_dispatch = parked
    return answering, release


# --- the service-level overlapping retry --------------------------------------


def test_overlapping_retries_dispatch_one_admin_replacement(tmp_path):
    """The exact reproduction (01-08): two retries, one dispatch."""

    service, transitions, build, launched = _aligning_service(tmp_path)
    operation_id = _seed_update_pending(transitions, build)
    _park_at_dispatch(service)

    results, errors = _concurrently(
        lambda _caller: service.start_resolved(system_build=build, mode="fresh_install")
    )

    assert errors == [], "an overlapping retry may not fail"
    assert len(results) == 2
    assert {result["operation_id"] for result in results} == {operation_id}
    assert len(launched) == 1, "only one caller may dispatch the replacement"
    assert launched[0].operation_id == operation_id
    durable = transitions.read()
    assert durable.stage == STAGE_RECONNECT_PENDING
    assert durable.error_code is None
    assert all(result["stage"] == STAGE_RECONNECT_PENDING for result in results)


def test_a_second_dispatch_failure_cannot_poison_the_first(tmp_path):
    """The second launcher is the Docker name collision: it must never run."""

    dispatched = []

    def launcher(record):
        dispatched.append(record)
        if len(dispatched) > 1:
            raise RuntimeError(
                'Conflict. The container name "/ems-admin-update" is already in use'
            )

    service, transitions, build, _launched = _aligning_service(
        tmp_path, launcher=launcher
    )
    operation_id = _seed_update_pending(transitions, build)
    _park_at_dispatch(service)

    results, errors = _concurrently(
        lambda _caller: service.start_resolved(system_build=build, mode="fresh_install")
    )

    assert errors == [], "a duplicate retry may not answer admin_update_launch_failed"
    assert len(dispatched) == 1
    assert {result["operation_id"] for result in results} == {operation_id}
    durable = transitions.read()
    assert durable.stage == STAGE_RECONNECT_PENDING
    assert durable.error_code is None
    assert durable.resume_stage is None


def test_a_concurrent_caller_reports_the_durable_transition(tmp_path):
    """The waiting caller answers B1's operation, never a fresh alignment start."""

    service, transitions, build, launched = _aligning_service(tmp_path)
    operation_id = _seed_update_pending(transitions, build)
    _park_at_dispatch(service)

    results, errors = _concurrently(
        lambda _caller: service.start_resolved(system_build=build, mode="fresh_install")
    )

    assert errors == []
    assert len(launched) == 1
    for result in results:
        assert result["ok"] is True
        assert result["operation_id"] == operation_id
        assert result["reconnect"] is True
        assert result["config_written"] is False
        assert result["ems_started"] is False


def test_a_failed_dispatch_is_reported_without_a_second_attempt(tmp_path):
    """A launcher that really failed is answered, never retried underneath."""

    attempts = []

    def launcher(record):
        attempts.append(record)
        raise RuntimeError("docker is unavailable")

    service, transitions, build, _launched = _aligning_service(
        tmp_path, launcher=launcher
    )
    _seed_update_pending(transitions, build)
    _park_at_dispatch(service)

    results, errors = _concurrently(
        lambda _caller: service.start_resolved(system_build=build, mode="fresh_install")
    )

    assert results == []
    assert len(attempts) == 1, "only the dispatch owner may attempt the launch"
    assert [exc.code for exc in errors] == ["admin_update_launch_failed"] * 2
    durable = transitions.read()
    assert durable.stage == "failed_recoverable"
    assert durable.resume_stage == STAGE_UPDATE_PENDING


# --- the delayed caller, after the first claim was already released ------------


def test_delayed_overlapping_retry_cannot_dispatch_after_first_claim_is_released(
    tmp_path,
):
    """Two retries read one ``admin_update_pending``; the second acts far later.

    A transient claim only covers callers that were already waiting when its
    owner released it. A request that read the same stale stage and was then
    descheduled arrives at an empty claim, so nothing tells it that the single
    dispatch already happened — and its ``advance`` finds the reconnect stage
    committed and returns idempotently rather than refusing. Only the durable
    stage, re-read under the claim, can answer this caller.
    """

    service, transitions, build, launched = _aligning_service(tmp_path)
    operation_id = _seed_update_pending(transitions, build, mode="guided_upgrade")
    _park_until_the_first_dispatch_returned(service)

    results, errors = _concurrently(
        lambda _caller: service.start_resolved(
            system_build=build, mode="guided_upgrade"
        )
    )

    assert errors == [], "a delayed retry may not fail"
    assert len(launched) == 1, "a delayed retry may not start a second replacement"
    assert launched[0].operation_id == operation_id
    assert {result["operation_id"] for result in results} == {operation_id}
    assert all(result["stage"] == STAGE_RECONNECT_PENDING for result in results)
    assert all(result["reconnect"] is True for result in results)
    durable = transitions.read()
    assert durable.stage == STAGE_RECONNECT_PENDING
    assert durable.error_code is None


def test_a_delayed_retry_cannot_poison_a_launched_replacement(tmp_path):
    """The delayed second launcher is the Docker name collision: it must not run."""

    dispatched = []

    def launcher(record):
        dispatched.append(record)
        if len(dispatched) > 1:
            raise RuntimeError(
                'Conflict. The container name "/ems-admin-update" is already in use'
            )

    service, transitions, build, _launched = _aligning_service(
        tmp_path, launcher=launcher
    )
    operation_id = _seed_update_pending(transitions, build, mode="guided_upgrade")
    _park_until_the_first_dispatch_returned(service)

    results, errors = _concurrently(
        lambda _caller: service.start_resolved(
            system_build=build, mode="guided_upgrade"
        )
    )

    assert errors == [], "a delayed retry may not answer admin_update_launch_failed"
    assert len(dispatched) == 1
    assert {result["operation_id"] for result in results} == {operation_id}
    durable = transitions.read()
    assert durable.stage == STAGE_RECONNECT_PENDING, "the running replacement stands"
    assert durable.error_code is None
    assert durable.resume_stage is None


def test_a_delayed_retry_reports_a_really_failed_dispatch_and_stays_retryable(
    tmp_path,
):
    """A launch that really failed is answered from B1, never attempted again.

    The delayed caller reports the recoverable failure the durable record
    carries — the same answer a caller that had waited inside the claim gets —
    and the operator's explicit retry still performs the missing launch.
    """

    attempts = []
    unavailable = threading.Event()
    unavailable.set()

    def launcher(record):
        attempts.append(record)
        if unavailable.is_set():
            raise RuntimeError("docker is unavailable")

    service, transitions, build, _launched = _aligning_service(
        tmp_path, launcher=launcher
    )
    operation_id = _seed_update_pending(transitions, build, mode="guided_upgrade")
    _park_until_the_first_dispatch_returned(service)

    results, errors = _concurrently(
        lambda _caller: service.start_resolved(
            system_build=build, mode="guided_upgrade"
        )
    )

    assert results == []
    assert len(attempts) == 1, "only the dispatch owner may attempt the launch"
    assert [exc.code for exc in errors] == ["admin_update_launch_failed"] * 2
    assert transitions.read().stage == "failed_recoverable"

    unavailable.clear()
    retried = service.retry(operation_id=operation_id)

    assert len(attempts) == 2, "an explicit retry may still perform the launch"
    assert retried["operation_id"] == operation_id
    assert transitions.read().stage == STAGE_RECONNECT_PENDING


# --- the explicit retry as its own dispatch attempt ---------------------------


def test_explicit_retry_starts_a_new_attempt_while_old_failed_claim_has_waiters(
    tmp_path,
):
    """Archive 107's remaining gap: the retry inherited the failed attempt.

    ``A`` and ``B`` share one attempt, ``A``'s launcher fails, and ``B`` is still
    being answered from it — so its entry is alive. The operator's retry then
    reopened ``admin_update_pending`` durably and walked into that same entry,
    which already carried ``admin_update_launch_failed``: it returned the old
    failure, launched nothing, and left the operation stranded at
    ``admin_update_pending``.

    The retry is a *new* attempt. It launches, and ``B`` still receives the
    failure of the attempt it actually waited for.
    """

    attempts = []
    both_registered = None

    def launcher(record):
        attempts.append(record)
        if len(attempts) == 1:
            assert both_registered.wait(TIMEOUT), "the second caller never claimed"
            raise RuntimeError("docker is unavailable")

    service, transitions, build, _launched = _aligning_service(
        tmp_path, launcher=launcher
    )
    operation_id = _seed_update_pending(transitions, build, mode="guided_upgrade")
    answering, release_answered = _park_the_answered_caller(service)
    both_registered = _claims_registered(service, 2)
    _park_at_dispatch(service)

    starts = _Callers(
        lambda _caller: service.start_resolved(
            system_build=build, mode="guided_upgrade"
        )
    )
    assert answering.wait(TIMEOUT), "no caller was left waiting in the failed attempt"
    failed = transitions.read()
    assert failed.stage == "failed_recoverable"
    assert failed.error_code == "admin_update_launch_failed"

    retry_registered = _claims_registered(service, 1)
    retries = _Callers(lambda _caller: service.retry(operation_id=operation_id), 1)
    assert retry_registered.wait(TIMEOUT), "the retry never claimed an attempt"
    release_answered.set()

    retry_results, retry_errors = retries.join()
    start_results, start_errors = starts.join()

    assert retry_errors == [], "an accepted retry may not answer the old failure"
    assert len(attempts) == 2, "an accepted retry performs exactly one new launch"
    assert attempts[1].operation_id == operation_id
    assert [result["stage"] for result in retry_results] == [STAGE_RECONNECT_PENDING]
    durable = transitions.read()
    assert durable.stage == STAGE_RECONNECT_PENDING
    assert durable.error_code is None
    assert start_results == [], "both start callers waited on the failed attempt"
    assert [exc.code for exc in start_errors] == ["admin_update_launch_failed"] * 2


def test_simultaneous_explicit_retries_dispatch_one_replacement(tmp_path):
    """Two retry requests for one failure: one new attempt, one launch."""

    launched = []
    both_registered = None

    def launcher(record):
        launched.append(record)
        assert both_registered.wait(TIMEOUT), "the second retry never claimed"

    service, transitions, build, _launched = _aligning_service(
        tmp_path, launcher=launcher
    )
    operation_id = _seed_failed_recoverable(transitions, build, mode="guided_upgrade")
    both_registered = _claims_registered(service, 2)

    results, errors = _concurrently(
        lambda _caller: service.retry(operation_id=operation_id)
    )

    assert errors == [], "a second retry may not fail while the first dispatches"
    assert len(launched) == 1, "two retries may not both launch"
    assert launched[0].operation_id == operation_id
    assert {result["operation_id"] for result in results} == {operation_id}
    assert all(result["stage"] == STAGE_RECONNECT_PENDING for result in results)
    durable = transitions.read()
    assert durable.stage == STAGE_RECONNECT_PENDING
    assert durable.error_code is None


def test_an_explicit_recovery_and_a_delayed_start_dispatch_one_replacement(tmp_path):
    """The other way into the launcher: ``retry`` reopens ``admin_update_pending``.

    The recovery edge is taken inside the retry's own attempt, but a start or
    resume caller only has to read the reopened stage to reach the dispatch as
    well. It joins the retry's attempt and is answered by it.
    """

    service, transitions, build, launched = _aligning_service(tmp_path)
    operation_id = _seed_failed_recoverable(transitions, build, mode="guided_upgrade")
    reopened = threading.Event()
    start_registered = _claims_registered(service, 2)
    store_retry = transitions.retry

    def parked_retry(operation, *, now=None):
        record = store_retry(operation, now=now)
        reopened.set()
        assert start_registered.wait(TIMEOUT), "the overlapping start never claimed"
        return record

    transitions.retry = parked_retry

    def call(caller):
        if not caller:
            return service.retry(operation_id=operation_id)
        assert reopened.wait(TIMEOUT), "the recovery never reopened the stage"
        return service.start_resolved(system_build=build, mode="guided_upgrade")

    results, errors = _concurrently(call)

    assert errors == [], "neither path may fail while the other dispatches"
    assert len(launched) == 1, "recovery and a start may not both dispatch"
    assert launched[0].operation_id == operation_id
    assert {result["operation_id"] for result in results} == {operation_id}
    assert all(result["stage"] == STAGE_RECONNECT_PENDING for result in results)
    durable = transitions.read()
    assert durable.stage == STAGE_RECONNECT_PENDING
    assert durable.error_code is None


def test_a_failed_retry_launch_stays_explicitly_retryable(tmp_path):
    """A retry whose launcher fails is recorded as itself and retried again."""

    attempts = []
    unavailable = threading.Event()
    unavailable.set()

    def launcher(record):
        attempts.append(record)
        if unavailable.is_set():
            raise RuntimeError("docker is unavailable")

    service, transitions, build, _launched = _aligning_service(
        tmp_path, launcher=launcher
    )
    operation_id = _seed_failed_recoverable(transitions, build, mode="guided_upgrade")

    with pytest.raises(SystemAlignmentError) as failed:
        service.retry(operation_id=operation_id)

    assert failed.value.code == "admin_update_launch_failed"
    assert len(attempts) == 1
    durable = transitions.read()
    assert durable.stage == "failed_recoverable"
    assert durable.resume_stage == STAGE_UPDATE_PENDING

    unavailable.clear()
    recovered = service.retry(operation_id=operation_id)

    assert len(attempts) == 2, "a failed retry stays explicitly retryable"
    assert recovered["stage"] == STAGE_RECONNECT_PENDING
    assert transitions.read().stage == STAGE_RECONNECT_PENDING


def test_a_retry_whose_durable_record_cannot_be_read_launches_nothing(
    tmp_path, read_fault
):
    """An unreadable B1 under the retry attempt proves nothing, so nothing runs."""

    service, transitions, build, launched = _aligning_service(tmp_path)
    operation_id = _seed_failed_recoverable(transitions, build)
    _fault_between_the_retry_reads(
        service, lambda: read_fault(transitions.path, OSError(13, "denied"))
    )

    with pytest.raises(SystemAlignmentError) as exc:
        service.retry(operation_id=operation_id)

    assert exc.value.code == "transition_stage_commit_unprovable"
    assert launched == [], "an unprovable retry may launch nothing"
    read_fault(transitions.path)
    assert transitions.read().stage == "failed_recoverable"


def test_a_retry_whose_durable_identity_changed_launches_nothing(tmp_path):
    """A durable record that is no longer the caller's exact operation refuses.

    The operation id still matches; only the immutable request fingerprint
    differs, so nothing but the full identity comparison catches it.
    """

    service, transitions, build, launched = _aligning_service(tmp_path)
    operation_id = _seed_failed_recoverable(transitions, build)

    def replace_identity():
        record = transitions.read()
        transitions._write_raw(
            dataclasses.replace(
                record, request_fingerprint=f"sha256:{'f' * 64}"
            ).as_dict()
        )

    _fault_between_the_retry_reads(service, replace_identity)

    with pytest.raises(SystemAlignmentError) as exc:
        service.retry(operation_id=operation_id)

    assert exc.value.code == "transition_stage_commit_unprovable"
    assert launched == [], "a foreign durable identity may launch nothing"
    assert transitions.read().stage == "failed_recoverable", "no durable edge taken"


# --- Archive 105's post-commit fault, under overlap ---------------------------


def test_post_commit_stage_fault_with_an_overlapping_retry_launches_once(
    tmp_path, unlock_fault_at  # noqa: F811
):
    """The real second ``LOCK_UN`` fault, then two overlapping retries.

    The first start commits ``admin_reconnect_pending`` and launches out of the
    post-commit classification. Neither retry may dispatch a second replacement.
    """

    store, workflow_id = _workflows(tmp_path)
    service, transitions, build, launched = _aligning_service(tmp_path)
    unlock_fault_at(transitions, 2)
    first = service.start_resolved(
        system_build=build,
        mode="fresh_install",
        pre_launch=_WorkflowLink(store, workflow_id),
    )
    assert transitions.read().stage == STAGE_RECONNECT_PENDING
    assert len(launched) == 1

    retry_links = [_WorkflowLink(store, workflow_id) for _ in range(2)]
    _park_at_dispatch(service)
    results, errors = _concurrently(
        lambda caller: service.start_resolved(
            system_build=build,
            mode="fresh_install",
            pre_launch=retry_links[caller],
        ),
    )

    assert errors == []
    assert len(launched) == 1, "a durable reconnect stage is already dispatched"
    assert {result["operation_id"] for result in results} == {first["operation_id"]}
    assert transitions.read().stage == STAGE_RECONNECT_PENDING
    assert store.load()["operation_id"] == first["operation_id"], "B0 keeps its owner"
    assert all(link.linked == [] for link in retry_links)


def test_overlapping_retries_after_a_proven_non_commit_dispatch_once(
    tmp_path, rename_fault
):
    """A proven non-commit stays retryable — and exactly one retry dispatches."""

    service, transitions, build, launched = _uncommitted_stage_service(
        tmp_path, rename_fault
    )
    with pytest.raises(SystemAlignmentError) as exc:
        service.start_resolved(system_build=build, mode="fresh_install")
    assert exc.value.code == "transition_state_write_failed"
    operation_id = transitions.read().operation_id
    assert launched == []

    rename_fault(transitions.path)
    _park_at_dispatch(service)
    results, errors = _concurrently(
        lambda _caller: service.start_resolved(system_build=build, mode="fresh_install")
    )

    assert errors == []
    assert len(launched) == 1, "exactly one retry may perform the missing dispatch"
    assert launched[0].operation_id == operation_id
    assert {result["operation_id"] for result in results} == {operation_id}
    assert transitions.read().stage == STAGE_RECONNECT_PENDING


def test_an_unprovable_stage_outcome_still_fails_closed_under_overlap(
    tmp_path, read_fault
):
    """An unreadable B1 proves nothing, so no caller may dispatch."""

    transitions = _UnreadableAfterCommitStore(tmp_path / "state", arm=read_fault)
    service, _transitions, build, launched = _aligning_service(
        tmp_path, transitions=transitions
    )
    _park_at_dispatch(service, parties=1)

    results, errors = _concurrently(
        lambda _caller: service.start_resolved(system_build=build, mode="fresh_install"),
        count=1,
    )

    assert results == []
    assert [exc.code for exc in errors] == ["transition_stage_commit_unprovable"]
    assert launched == [], "an unprovable stage may launch nothing"


# --- the Guided Upgrade HTTP regression ---------------------------------------


UPGRADE_TAG = "v0.8.0"
UPGRADE_EXECUTE = "/api/admin/maintenance/upgrade/execute"
UPGRADE_OPTIONS = {"backup": False}


class _UpgradeExecutor:
    """The Guided Upgrade executor surface the execute route needs pre-launch."""

    def __init__(self):
        self.prepared = 0
        self.resumed = 0

    @staticmethod
    def request_fingerprint(target_release, options):
        payload = json.dumps([target_release, options], sort_keys=True).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def preflight(self, target_release, options, *, confirm, system_build):
        return None, SimpleNamespace(options=dict(options), migration={"required": False})

    def prepare_alignment(self, run_context):
        self.prepared += 1
        return None, {"backup": {"status": "skipped"}}

    def resume_alignment(self, run_context):
        self.resumed += 1
        return {"backup": {"status": "skipped"}}


def test_concurrent_guided_upgrade_execute_requests_dispatch_once(tmp_path):
    """Two overlapping execute/resume requests for one operation and fingerprint.

    Guided Upgrade has no lifecycle claim, so this is the production exposure:
    both requests reach the alignment service through ``ThreadingHTTPServer``.
    """

    service, transitions, build, launched = _aligning_service(tmp_path)
    fingerprint = _UpgradeExecutor.request_fingerprint(UPGRADE_TAG, UPGRADE_OPTIONS)
    operation_id = _seed_update_pending(
        transitions, build, mode="guided_upgrade", request_fingerprint=fingerprint
    )
    executor = _UpgradeExecutor()
    srv, base = _serve(guided_upgrade=executor)
    _attach_system_alignment(srv, service)
    _park_at_dispatch(service)
    body = {
        "confirm": True,
        "target_release": UPGRADE_TAG,
        "options": UPGRADE_OPTIONS,
        "selection_fingerprint": service.selection_fingerprint(build),
    }
    try:
        responses, errors = _concurrently(
            lambda _caller: _request(f"{base}{UPGRADE_EXECUTE}", method="POST", body=body)
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert errors == []
    statuses = [status for status, _headers, _payload in responses]
    payloads = [payload for _status, _headers, payload in responses]
    assert statuses == [202, 202], payloads
    assert len(launched) == 1, "one operation may dispatch one Admin replacement"
    assert {payload["operation_id"] for payload in payloads} == {operation_id}
    assert all(payload["stage"] == STAGE_RECONNECT_PENDING for payload in payloads)
    assert not any(
        payload.get("error") == "admin_update_launch_failed" for payload in payloads
    )
    durable = transitions.read()
    assert durable.stage == STAGE_RECONNECT_PENDING
    assert durable.error_code is None
    assert executor.resumed == 2, "both requests resume the same prepared upgrade"
    assert executor.prepared == 0, "no request may prepare a second alignment"


def test_a_delayed_guided_upgrade_execute_request_dispatches_nothing(tmp_path):
    """The production exposure end to end: the retried request arrives too late.

    A replayed browser request or a network retry reaches the execute route
    after the first one has already answered and its claim is gone. It must
    report the durable operation and start no second replacement.
    """

    service, transitions, build, launched = _aligning_service(tmp_path)
    fingerprint = _UpgradeExecutor.request_fingerprint(UPGRADE_TAG, UPGRADE_OPTIONS)
    operation_id = _seed_update_pending(
        transitions, build, mode="guided_upgrade", request_fingerprint=fingerprint
    )
    executor = _UpgradeExecutor()
    srv, base = _serve(guided_upgrade=executor)
    _attach_system_alignment(srv, service)
    _park_until_the_first_dispatch_returned(service)
    body = {
        "confirm": True,
        "target_release": UPGRADE_TAG,
        "options": UPGRADE_OPTIONS,
        "selection_fingerprint": service.selection_fingerprint(build),
    }
    try:
        responses, errors = _concurrently(
            lambda _caller: _request(f"{base}{UPGRADE_EXECUTE}", method="POST", body=body)
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert errors == []
    statuses = [status for status, _headers, _payload in responses]
    payloads = [payload for _status, _headers, payload in responses]
    assert statuses == [202, 202], payloads
    assert len(launched) == 1, "one operation may dispatch one Admin replacement"
    assert {payload["operation_id"] for payload in payloads} == {operation_id}
    assert all(payload["stage"] == STAGE_RECONNECT_PENDING for payload in payloads)
    assert not any(
        payload.get("error") == "admin_update_launch_failed" for payload in payloads
    )
    durable = transitions.read()
    assert durable.stage == STAGE_RECONNECT_PENDING
    assert durable.error_code is None


# --- the Guided Upgrade HTTP recovery route -----------------------------------


RECOVER = "/api/admin/system-alignment/resume"


def test_concurrent_guided_upgrade_recovery_requests_launch_once(tmp_path):
    """Two overlapping recovery requests for one recoverable launcher failure.

    The recovery route is the production explicit retry: two browser tabs, a
    replayed request or a network retry reach it exactly the way they reach the
    execute route. Both must answer one new attempt.

    The race is held open at the reopening write: until it happens both requests
    read the same ``failed_recoverable`` status and both ask the service to
    retry, which is the interleaving the durable edge alone cannot decide. (A
    request that arrives *after* the reopen sees the pending transition and
    reports it — that path never reaches the launcher at all.)
    """

    attempts = []
    both_registered = None

    def launcher(record):
        attempts.append(record)
        raise RuntimeError("docker is unavailable")

    service, transitions, build, _launched = _aligning_service(
        tmp_path, launcher=launcher
    )
    operation_id = _seed_failed_recoverable(transitions, build, mode="guided_upgrade")
    both_registered = _claims_registered(service, 2)
    reopen = service._reopen_recoverable_failure

    def parked_reopen(record):
        assert both_registered.wait(TIMEOUT), "the second request never claimed"
        return reopen(record)

    service._reopen_recoverable_failure = parked_reopen
    srv, base = _serve()
    _attach_system_alignment(srv, service)
    body = {"operation_id": operation_id, "tag": build.canonical_tag}
    try:
        responses, errors = _concurrently(
            lambda _caller: _request(f"{base}{RECOVER}", method="POST", body=body)
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert errors == []
    payloads = [payload for _status, _headers, payload in responses]
    assert len(attempts) == 1, "two recovery requests may not both launch"
    assert attempts[0].operation_id == operation_id
    assert all(
        payload.get("error") == "admin_update_launch_failed" for payload in payloads
    ), payloads
    durable = transitions.read()
    assert durable.stage == "failed_recoverable"
    assert durable.resume_stage == STAGE_UPDATE_PENDING


def test_a_guided_upgrade_recovery_request_launches_once_and_aligns(tmp_path):
    """The recovery route end to end: one launch, then the reconnected Admin."""

    attempts = []
    identity = {"running": _outdated_admin()}

    def launcher(record):
        attempts.append(record)
        identity["running"] = _identity_for(build)

    service, transitions, build, _launched = _aligning_service(
        tmp_path, launcher=launcher
    )
    service._current_identity = lambda: identity["running"]
    operation_id = _seed_failed_recoverable(transitions, build, mode="guided_upgrade")
    srv, base = _serve()
    _attach_system_alignment(srv, service)
    try:
        status, _headers, payload = _request(
            f"{base}{RECOVER}",
            method="POST",
            body={"operation_id": operation_id, "tag": build.canonical_tag},
        )
    finally:
        srv.shutdown()
        srv.server_close()

    assert status == 200, payload
    assert len(attempts) == 1, "one recovery request performs one launch"
    assert payload["operation_id"] == operation_id
    assert payload["stage"] == "resources_verified"
    assert transitions.read().stage == "resources_verified"
