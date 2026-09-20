# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Guided Upgrade context has an owned lifecycle, not just a clear() method.

``guided-upgrade-context.json`` is the durable, secret-free execution context an
automatic resume rebuilds a run from. It used to have no terminal owner: Cancel
upgrade ended the transition but left the context behind, and a completed
upgrade kept it as a stale "current upgrade". These tests pin the lifecycle:
cancellation and completion clear exactly their own operation's context,
recovery keeps it, and the known-good System Build record — durable
installed-system state — survives every cancellation.

See ``docs/technical/admin-workflow-state.md``.
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from admin.guided_upgrade import (
    ALL_OPTIONS,
    _normalized_options,
    guided_upgrade_request_fingerprint,
)
from admin.guided_upgrade_context import GuidedUpgradeContextStore
from admin.server import continue_guided_upgrade, resume_pending_guided_upgrade
from admin.system_alignment import SystemAlignmentError
from tests.test_admin_server import _control_export_manager, _request, _serve
from tests.test_admin_setup_cancellation_ownership import (
    _CancelRecordingAlignment,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


TAG = "v0.8.0"


def _options(backup=False):
    options = {key: False for key in ALL_OPTIONS}
    options["backup"] = backup
    return _normalized_options(options)


def _seed_context(state_dir, operation_id):
    store = GuidedUpgradeContextStore(state_dir)
    options = _options()
    store.save(
        operation_id=operation_id,
        target_system_tag=TAG,
        options=options,
        request_fingerprint=guided_upgrade_request_fingerprint(TAG, options),
    )
    assert store.path.is_file()
    return store


def _seed_known_good(state_dir):
    path = state_dir / "known-good-system-build.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"system_tag": "v0.7.0"}, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    return path, payload.encode("utf-8")


class _UpgradeAlignment(_CancelRecordingAlignment):
    def __init__(self, stage="failed_recoverable"):
        super().__init__(stage=stage, mode="guided_upgrade")


def _cancel(base, operation_id="op-1"):
    return _request(
        f"{base}/api/admin/system-alignment/cancel",
        method="POST",
        body={"operation_id": operation_id, "confirm": True},
    )


# --- Cancel upgrade owns its context ------------------------------------------


def test_cancel_upgrade_clears_its_own_context_and_keeps_known_good(tmp_path):
    state_dir = tmp_path / "state"
    store = _seed_context(state_dir, "op-1")
    known_good, known_good_bytes = _seed_known_good(state_dir)
    alignment = _UpgradeAlignment()
    srv, base = _serve(
        release_manager=_release_manager(tmp_path), system_alignment=alignment
    )
    try:
        status, _, payload = _cancel(base, "op-1")

        assert status == 200, payload
        assert payload["stage"] == "cancelled"
        assert alignment.cancelled == ["op-1"]
        assert not store.path.exists(), (
            "a cancelled upgrade must not leave an active context behind"
        )
        assert known_good.read_bytes() == known_good_bytes, (
            "known-good is durable installed-system state, not a workflow artifact"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_cancel_upgrade_cannot_clear_a_newer_operations_context(tmp_path):
    state_dir = tmp_path / "state"
    store = _seed_context(state_dir, "a-newer-operation")
    alignment = _UpgradeAlignment()
    srv, base = _serve(
        release_manager=_release_manager(tmp_path), system_alignment=alignment
    )
    try:
        status, _, payload = _cancel(base, "op-1")

        assert status == 200, payload
        assert store.path.exists(), (
            "an old cancellation must not clear a newer upgrade's context"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_refused_cancel_keeps_the_context(tmp_path):
    """A worker-active (or otherwise refused) cancel changes nothing."""

    from admin.system_alignment import SystemAlignmentError

    class _RefusingAlignment(_UpgradeAlignment):
        def cancel(self, *, operation_id, coordinator=None):
            raise SystemAlignmentError(
                "transition_worker_active",
                "The System Build operation is still running.",
            )

    state_dir = tmp_path / "state"
    store = _seed_context(state_dir, "op-1")
    srv, base = _serve(
        release_manager=_release_manager(tmp_path),
        system_alignment=_RefusingAlignment(),
    )
    try:
        status, _, payload = _cancel(base, "op-1")

        assert status == 409, payload
        assert payload["error"] == "transition_worker_active"
        assert store.path.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def _release_manager(tmp_path):
    return _control_export_manager(tmp_path)


# --- completion clears the context after the durable success -----------------


class _StubUpgradeExecutor:
    """Route-shaped Guided Upgrade executor with a scripted run() outcome."""

    def __init__(self, result):
        self._result = result
        self.run_calls = 0

    def preflight(self, target_release, options, *, confirm, system_build):
        del confirm, system_build
        return None, SimpleNamespace(
            migration={"required": False},
            options=_normalized_options(options),
        )

    @staticmethod
    def request_fingerprint(target_release, options):
        return guided_upgrade_request_fingerprint(
            target_release, _normalized_options(options)
        )

    def prepare_alignment(self, run_context):
        del run_context
        return None, SimpleNamespace(steps=[])

    def resume_alignment(self, run_context, **kwargs):
        del run_context, kwargs
        return SimpleNamespace(steps=[])

    def run(self, run_context, *, pre_alignment=None, progress=None):
        del run_context, pre_alignment
        if callable(progress):
            progress("upgrade", "running")
        self.run_calls += 1
        return dict(self._result)


class _ExecutableUpgradeAlignment(_CancelRecordingAlignment):
    """Alignment fake that walks the execute flow to completion."""

    def __init__(self):
        super().__init__(stage="resources_verified", mode="guided_upgrade")
        self.active = False
        self._claimed = False

    def resolve(self, requested_tag):
        return self._system_build(requested_tag)

    def selection_fingerprint(self, build):
        return "fp:" + str(build.get("canonical_tag"))

    def start_resolved(
        self,
        *,
        system_build,
        mode,
        request_fingerprint=None,
        development_risk_acknowledged=False,
        pre_launch=None,
    ):
        del development_risk_acknowledged
        self.mode = mode
        self.request_fingerprint = request_fingerprint
        self.active = True
        if pre_launch is not None:
            pre_launch(SimpleNamespace(operation_id="op-1"))
        return {
            "ok": True,
            "status": "ready_for_ems",
            "stage": "resources_verified",
            "operation_id": "op-1",
            "system_build": system_build,
        }

    def begin_ems_operation(self, *, operation_id):
        self.stage = "ems_operation_pending"
        return {"operation_id": operation_id, "stage": self.stage}

    def claim_ems_operation(self, *, operation_id):
        del operation_id
        if self._claimed or self.stage != "ems_operation_pending":
            return False
        self._claimed = True
        self.stage = "ems_operation_running"
        return True

    def finish_ems_operation(self, *, operation_id, succeeded, **_kwargs):
        self.stage = "healthcheck_pending" if succeeded else "failed_recoverable"
        return {"operation_id": operation_id, "stage": self.stage}

    def finish_healthcheck(self, *, operation_id, passed, **_kwargs):
        self.stage = "completed" if passed else "failed_recoverable"
        self.active = self.stage != "completed"
        return {"operation_id": operation_id, "stage": self.stage}


def _execute(base, tag=TAG):
    return _request(
        f"{base}/api/admin/maintenance/upgrade/execute",
        method="POST",
        body={
            "confirm": True,
            "target_release": tag,
            "options": dict.fromkeys(ALL_OPTIONS, False),
            "selection_fingerprint": "fp:" + tag,
        },
    )


def _wait_for_stage(alignment, stages, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if alignment.stage in stages:
            return True
        time.sleep(0.02)
    return alignment.stage in stages


def test_a_completed_upgrade_clears_its_context(tmp_path):
    state_dir = tmp_path / "state"
    store = GuidedUpgradeContextStore(state_dir)
    executor = _StubUpgradeExecutor(
        {
            "ok": True,
            "status": "success",
            "diagnostics": {"available": True, "summary": {"status": "ok"}},
        }
    )
    alignment = _ExecutableUpgradeAlignment()
    srv, base = _serve(
        release_manager=_release_manager(tmp_path),
        system_alignment=alignment,
        guided_upgrade=executor,
        guided_upgrade_context=store,
    )
    try:
        status, _, payload = _execute(base)
        assert status in (200, 202), payload
        assert _wait_for_stage(alignment, {"completed"}), alignment.stage
        deadline = time.time() + 5.0
        while store.path.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert not store.path.exists(), (
            "a completed upgrade must clear its context after the durable "
            "success state"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_recoverable_failure_keeps_the_context(tmp_path):
    state_dir = tmp_path / "state"
    store = GuidedUpgradeContextStore(state_dir)
    executor = _StubUpgradeExecutor(
        {"ok": False, "reason": "ems_upgrade_failed", "message": "boom"}
    )
    alignment = _ExecutableUpgradeAlignment()
    srv, base = _serve(
        release_manager=_release_manager(tmp_path),
        system_alignment=alignment,
        guided_upgrade=executor,
        guided_upgrade_context=store,
    )
    try:
        status, _, payload = _execute(base)
        assert status in (200, 202, 409, 500), payload
        assert _wait_for_stage(alignment, {"failed_recoverable"}), alignment.stage
        assert executor.run_calls == 1
        time.sleep(0.1)
        assert store.path.exists(), (
            "failed_recoverable keeps the context so recovery can resume"
        )
    finally:
        srv.shutdown()
        srv.server_close()


# --- the replaced Admin continues its own upgrade --------------------------
#
# After the Admin container is replaced, the only thing that ever continued the
# upgrade was a browser: an authenticated page load posted the resume. With the
# console closed -- or simply not reloaded after the reconnect poller gave up --
# the durable transition sat at admin_reconnect_pending until its deadline, and
# the operator came back to an upgrade that could only be abandoned, with the
# new Admin installed and the old EMS still running.


class _ReconnectedUpgradeAlignment(_ExecutableUpgradeAlignment):
    """Waiting at admin_reconnect_pending, as a replaced Admin finds it."""

    def __init__(self, *, mode="guided_upgrade"):
        super().__init__()
        self.stage = "admin_reconnect_pending"
        self.mode = mode
        self.active = True

    def resume(self, *, operation_id, **kwargs):
        del kwargs
        self.stage = "admin_aligned"
        return {"operation_id": operation_id, "stage": self.stage}

    def verify_resources(self, *, operation_id):
        self.stage = "resources_verified"
        return {"operation_id": operation_id, "stage": self.stage}

    def transition_build(self, *, operation_id):
        del operation_id
        return self._system_build(TAG)


def _reconnected(tmp_path, alignment, executor):
    state_dir = tmp_path / "state"
    _seed_context(state_dir, "op-1")
    return _serve(
        release_manager=_release_manager(tmp_path),
        system_alignment=alignment,
        guided_upgrade=executor,
        guided_upgrade_context=GuidedUpgradeContextStore(state_dir),
    )


def test_a_replaced_admin_continues_its_upgrade_without_a_browser(tmp_path):
    alignment = _ReconnectedUpgradeAlignment()
    executor = _StubUpgradeExecutor(
        {
            "ok": True,
            "status": "success",
            "diagnostics": {"available": True, "summary": {"status": "ok"}},
        }
    )
    srv, _base = _reconnected(tmp_path, alignment, executor)
    try:
        outcome = resume_pending_guided_upgrade(srv)
        assert outcome is not None
        assert _wait_for_stage(alignment, {"completed"}), alignment.stage
    finally:
        srv.shutdown()

    assert executor.run_calls == 1


def test_a_setup_transition_is_never_continued_as_an_upgrade(tmp_path):
    alignment = _ReconnectedUpgradeAlignment(mode="fresh_install")
    executor = _StubUpgradeExecutor({"ok": True, "status": "success"})
    srv, _base = _reconnected(tmp_path, alignment, executor)
    try:
        assert resume_pending_guided_upgrade(srv) is None
    finally:
        srv.shutdown()

    assert alignment.stage == "admin_reconnect_pending"
    assert executor.run_calls == 0


class _ObservedLock:
    """A lock that reports when a caller had to wait behind another."""

    def __init__(self):
        self._lock = threading.Lock()
        self.waited = threading.Event()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self.waited.set()
            self._lock.acquire()
        return self

    def __exit__(self, *exc_info):
        self._lock.release()
        return False


class _ImportingUpgradeAlignment(_ReconnectedUpgradeAlignment):
    """Held inside the resource import until the test lets it finish.

    A second caller that reaches the import while it runs gets exactly what
    the production service answers: the claim is taken, so it is refused.
    """

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.importing = False

    def verify_resources(self, *, operation_id):
        if self.importing:
            raise SystemAlignmentError(
                "resource_verification_in_progress",
                "embedded resources are already being verified",
            )
        self.importing = True
        self.entered.set()
        assert self.release.wait(10), "the import was never released"
        self.importing = False
        return super().verify_resources(operation_id=operation_id)


def test_a_second_continuation_waits_for_the_first_and_attaches_to_its_job(tmp_path):
    """The replaced Admin and the browser both continue; only one may drive.

    The startup thread and the browser's reconnect resume arrive in the same
    moment, and the steps before the job -- reconnect, resource import, the
    EMS-operation edge -- are durable and not idempotent against a caller in
    the middle of them. The loser used to get the import's refusal, which the
    console rendered as a failed upgrade while the upgrade ran on without it.
    Interleaving pinned: the second caller arrives while the first is inside
    the import, waits for it, and attaches to the job it started.
    """

    alignment = _ImportingUpgradeAlignment()
    executor = _StubUpgradeExecutor(
        {
            "ok": True,
            "status": "success",
            "diagnostics": {"available": True, "summary": {"status": "ok"}},
        }
    )
    srv, _base = _reconnected(tmp_path, alignment, executor)
    lock = _ObservedLock()
    srv.guided_upgrade_continuation_lock = lock
    outcomes = {}

    def continue_as(name):
        try:
            outcomes[name] = continue_guided_upgrade(srv, "op-1")
        except Exception as exc:  # the interleaving under test may raise
            outcomes[name] = exc

    try:
        first = threading.Thread(target=continue_as, args=("first",))
        first.start()
        assert alignment.entered.wait(5), "the first continuation never imported"
        second = threading.Thread(target=continue_as, args=("second",))
        second.start()
        assert lock.waited.wait(5), "the second continuation did not wait for the first"
        alignment.release.set()
        first.join(10)
        second.join(10)
        assert _wait_for_stage(alignment, {"completed"}), alignment.stage
    finally:
        alignment.release.set()
        srv.shutdown()

    assert outcomes["first"]["outcome"] == "started"
    assert outcomes["second"]["outcome"] == "reattached"
    assert outcomes["second"]["job"] is outcomes["first"]["job"]
    assert executor.run_calls == 1


def test_the_startup_thread_and_a_request_take_the_same_continuation_lock(tmp_path):
    """One driver at a time, across the two objects production continues from.

    ``__main__`` hands the startup continuation the ``AdminRuntime``; a request
    hands it the ``AdminServer``, and with HTTPS enabled there are two of those
    over one runtime. A lock that lives on only one of them serializes nothing,
    and the test that pinned the rule injected it onto a single object, so the
    split never showed. Interleaving pinned: the runtime caller is inside the
    resource import when the server caller arrives, which must wait rather than
    reach the import and take its refusal.
    """

    alignment = _ImportingUpgradeAlignment()
    executor = _StubUpgradeExecutor(
        {
            "ok": True,
            "status": "success",
            "diagnostics": {"available": True, "summary": {"status": "ok"}},
        }
    )
    srv, _base = _reconnected(tmp_path, alignment, executor)
    outcomes = {}

    def continue_as(name, entry):
        try:
            outcomes[name] = continue_guided_upgrade(entry, "op-1")
        except Exception as exc:  # the interleaving under test may raise
            outcomes[name] = exc

    try:
        startup = threading.Thread(target=continue_as, args=("startup", srv.runtime))
        startup.start()
        assert alignment.entered.wait(5), "the startup continuation never imported"
        request = threading.Thread(target=continue_as, args=("request", srv))
        request.start()
        # Parked on the lock, rather than through to the import and its refusal.
        request.join(0.5)
        assert request.is_alive(), (
            "the request continuation did not wait for the startup one: "
            f"{outcomes.get('request')!r}"
        )
        alignment.release.set()
        startup.join(10)
        request.join(10)
        assert _wait_for_stage(alignment, {"completed"}), alignment.stage
    finally:
        alignment.release.set()
        srv.shutdown()

    assert outcomes["startup"]["outcome"] == "started"
    assert outcomes["request"]["outcome"] == "reattached"
    assert outcomes["request"]["job"] is outcomes["startup"]["job"]
    assert executor.run_calls == 1


class _ExpiredUpgradeAlignment(_ReconnectedUpgradeAlignment):
    """Past its deadline: every forward path refuses, only abandon is left."""

    def status(self, *, operation_active=None):
        result = super().status(operation_active=operation_active)
        result["transition"]["expired"] = True
        return result

    def resume(self, *, operation_id, **kwargs):
        raise AssertionError("a record past its deadline must not be resumed")


def test_an_expired_transition_is_left_for_the_operator_on_startup(tmp_path):
    """Nothing to continue: every forward path refuses an expired record, so
    the startup thread would only spend Docker calls to learn that. It says
    so and leaves the escape to the operator."""

    alignment = _ExpiredUpgradeAlignment()
    executor = _StubUpgradeExecutor({"ok": True, "status": "success"})
    srv, _base = _reconnected(tmp_path, alignment, executor)
    try:
        outcome = resume_pending_guided_upgrade(srv)
    finally:
        srv.shutdown()

    assert outcome is not None and "expired" in outcome, outcome
    assert "could not be continued" not in outcome, outcome
    assert alignment.stage == "admin_reconnect_pending"
    assert executor.run_calls == 0


def test_nothing_is_continued_when_no_transition_is_pending(tmp_path):
    alignment = _ExecutableUpgradeAlignment()
    alignment.active = False
    executor = _StubUpgradeExecutor({"ok": True, "status": "success"})
    srv, _base = _reconnected(tmp_path, alignment, executor)
    try:
        assert resume_pending_guided_upgrade(srv) is None
    finally:
        srv.shutdown()

    assert executor.run_calls == 0
