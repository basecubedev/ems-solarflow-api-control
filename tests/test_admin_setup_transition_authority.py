# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Setup transition belongs to exactly one Guided Setup workflow.

Archive 100 made every Setup *mutation* hold a lifecycle claim on its workflow,
but the routes that **create** the Setup System Build transition stayed outside
that model, and the link from the workflow to its transition was written after
the transition already existed:

* an unlinked workflow could cancel whatever Setup-owned transition happened to
  be active, because a Setup-owned *mode* was read as ownership;
* Update Admin / Confirm / release preparation held no claim, so an Abandon could
  terminalize the workflow while a transition for it was still being created —
  and both sides returned success;
* a setup intent proved "this session confirmed Fresh Setup", not "for this
  workflow", so an intent issued for a superseded workflow authorized its
  replacement;
* the workflow→transition link suppressed ``OSError`` *after* System Alignment
  had already committed and launched, producing exactly the unlinked state the
  first point exploits.

These tests pin the closing contract: a transition may be created, resumed or
cancelled only by the exact active workflow that can prove it owns it, and the
proof is persisted before the transition is committed.

Synchronization is explicit (``threading.Event``) — never a sleep.

See ``docs/technical/admin-workflow-state.md``.
"""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.system_alignment import SystemAlignmentError
from tests.admin_auth_helpers import auth_headers
from tests.test_admin_server import (
    _FakeSystemAlignment,
    _attach_system_alignment,
    _control_export_manager,
    _request,
    _serve,
)
from tests.test_admin_setup_lifecycle_exclusion import _Mutation
from tests.test_admin_setup_preview_authority import _start_workflow

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.setup,
    pytest.mark.workflow,
    pytest.mark.integration,
    pytest.mark.simulation,
]

CONFIRM = "/api/setup/system-build/confirm"
UPDATE_ADMIN = "/api/setup/system-build/update-admin"
PREPARE = "/api/setup/releases/prepare"
AUTOMATED_PREPARE = "/api/setup/automated/releases/prepare"

CREATION_ROUTES = (
    (UPDATE_ADMIN, "fresh_install"),
    (CONFIRM, "fresh_install"),
    (PREPARE, "fresh_install"),
    (AUTOMATED_PREPARE, "automated_setup"),
)


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


class _Gate:
    """A commit boundary a test can hold open, with no sleeps involved."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self):
        self.entered.set()
        assert self.release.wait(20), "the held System Build commit was never released"


class _CommitTrackingAlignment(_FakeSystemAlignment):
    """System Alignment double with an explicit, observable commit boundary.

    Production mints the operation id, runs ``pre_launch``, and only then commits
    the transition and launches an Admin replacement. Keeping that order visible
    lets a test prove that nothing was committed and nothing was launched.
    """

    def __init__(self, *, admin_update_required=False, operation_id="op-1"):
        super().__init__(
            stage="admin_update_pending" if admin_update_required else "admin_aligned",
            active=False,
        )
        self.operation_id = operation_id
        self.admin_update_required = admin_update_required
        self.committed = []
        self.launched = []
        self.cancelled = []
        self.gate = None

    def _commit(self, *, requested_tag, mode, pre_launch):
        if self.gate is not None:
            self.gate.wait()
        if pre_launch is not None:
            pre_launch(SimpleNamespace(operation_id=self.operation_id))
        self.active = True
        self.mode = mode
        self.committed.append(self.operation_id)
        result = {
            "ok": True,
            "operation_id": self.operation_id,
            "system_build": self._system_build(requested_tag),
        }
        if self.admin_update_required:
            self.stage = "admin_reconnect_pending"
            self.launched.append(self.operation_id)
            return {
                **result,
                "status": "admin_alignment_started",
                "stage": self.stage,
                "reconnect": True,
            }
        self.stage = "resources_verified"
        return {
            **result,
            "status": "ready_for_ems",
            "stage": self.stage,
            "resources_verified": True,
            "next_allowed": True,
        }

    def start(self, *, requested_tag, mode, development_risk_acknowledged=False):
        self._reject_floating(requested_tag)
        self.development_acknowledgements.append(development_risk_acknowledged)
        self.start_calls.append({"requested_tag": requested_tag, "mode": mode})
        return self._commit(requested_tag=requested_tag, mode=mode, pre_launch=None)

    def start_resolved(
        self,
        *,
        system_build,
        mode,
        request_fingerprint=None,
        development_risk_acknowledged=False,
        pre_launch=None,
    ):
        del request_fingerprint
        requested_tag = system_build["canonical_tag"]
        self._reject_floating(requested_tag)
        self.development_acknowledgements.append(development_risk_acknowledged)
        self.start_calls.append({"requested_tag": requested_tag, "mode": mode})
        return self._commit(
            requested_tag=requested_tag, mode=mode, pre_launch=pre_launch
        )

    def confirm_setup_build(
        self,
        *,
        requested_tag,
        mode,
        development_risk_acknowledged=False,
        pre_launch=None,
    ):
        self._reject_floating(requested_tag)
        self.development_acknowledgements.append(development_risk_acknowledged)
        self.confirm_calls.append({"requested_tag": requested_tag, "mode": mode})
        return self._commit(
            requested_tag=requested_tag, mode=mode, pre_launch=pre_launch
        )

    def prepare_setup_resources(
        self,
        *,
        requested_tag,
        mode,
        development_risk_acknowledged=False,
        pre_launch=None,
    ):
        self._reject_floating(requested_tag)
        self.development_acknowledgements.append(development_risk_acknowledged)
        self.prepare_calls.append({"requested_tag": requested_tag, "mode": mode})
        return self._commit(
            requested_tag=requested_tag, mode=mode, pre_launch=pre_launch
        )

    def cancel(self, *, operation_id, coordinator=None):
        del coordinator
        self.cancelled.append(operation_id)
        self.stage = "cancelled"
        self.active = False
        return {"ok": True, "operation_id": operation_id, "stage": "cancelled"}


class _ActiveTransitionAlignment(_CommitTrackingAlignment):
    """A Setup transition is already active and non-terminal."""

    def __init__(self, *, mode="fresh_install", operation_id="op-1"):
        super().__init__(operation_id=operation_id)
        self.active = True
        self.mode = mode
        self.stage = "resources_verified"


def _authority(base, *, confirm=True):
    """The exact workflow id and the one-shot intent issued for it."""

    status, _, payload = _request(
        f"{base}/api/admin/start-path",
        method="POST",
        body={"choice": "setup_new", "confirm": confirm},
    )
    assert status == 200, payload
    assert payload["ok"] is True, payload
    return payload["setup_workflow_id"], payload["setup_intent_id"]


def _direct_intent(srv, base, workflow_id):
    """Mint a setup intent without going through the entry route.

    Once a transition cannot be proven to belong to the active workflow, the
    lifecycle arbiter refuses entry — including ``/api/admin/start-path``, the
    only route that issues an intent. The creation routes keep their own
    ownership check as defence in depth, and reaching it now takes an intent
    minted directly.
    """

    cookie = auth_headers(f"{base}/", "POST").get("Cookie", "")
    session_id = cookie.split("=", 1)[1] if "=" in cookie else ""
    return srv.setup_intents.issue(
        session_id=session_id, workflow_id=workflow_id
    ).intent_id


def _post_creation(base, route, workflow_id, intent_id, *, tag="v0.8.0"):
    body = {"tag": tag}
    if workflow_id is not None:
        body["setup_workflow_id"] = workflow_id
    headers = {"X-Setup-Intent-ID": intent_id} if intent_id else None
    return _request(f"{base}{route}", method="POST", body=body, extra_headers=headers)


def _abandon(base, workflow_id):
    return _request(
        f"{base}/api/setup/abandon",
        method="POST",
        body={"setup_workflow_id": workflow_id},
    )


def _workflow_view(base):
    status, _, payload = _request(f"{base}/api/setup/workflow")
    assert status == 200, payload
    return payload.get("workflow")


def _link(srv, workflow_id, *, operation_id, mode="fresh_install", tag="v0.8.0"):
    """The ownership production now persists inside the pre-commit boundary."""

    return srv.setup_workflows.record_transition(
        workflow_id,
        operation_id=operation_id,
        transition_mode=mode,
        selected_system_tag=tag,
    )


def _own_generated_artifact(srv, workflow_id):
    """Give the workflow one artifact it can prove it owns."""

    from admin.setup_workflow import SetupWorkflowArtifacts

    artifacts = SetupWorkflowArtifacts(
        srv.setup_workflows.admin_data_dir, workflow_id=workflow_id
    )
    generated = artifacts.generated_config_path
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text('{"devices": []}\n', encoding="utf-8")
    artifacts.record_generated(
        workflow_id=workflow_id,
        preview_id="pv-" + "0" * 16,
        draft_fingerprint="sha256:" + "0" * 64,
        base_config_revision={"expected_revision": None, "expect_absent": True},
        prepared_config_sha256="0" * 64,
    )
    return generated


# --- strict cancellation proof ------------------------------------------------


def test_unlinked_workflow_cannot_cancel_active_setup_transition(tmp_path):
    """The exact reproduction: a Setup-owned mode is not ownership.

    Workflow W never linked a transition, so it cannot prove it owns the active
    ``fresh_install`` operation — the abandon must change nothing at all.
    """

    alignment = _ActiveTransitionAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id = _start_workflow(base)
        assert _workflow_view(base)["operation_id"] is None
        generated = _own_generated_artifact(srv, workflow_id)

        status, _, payload = _abandon(base, workflow_id)

        assert status == 409, payload
        assert payload["error"] == "setup_transition_owner_unproven"
        assert alignment.cancelled == [], "an unproven owner must cancel nothing"
        assert alignment.stage == "resources_verified"
        assert generated.exists(), "an unproven owner must clean up nothing"
        view = _workflow_view(base)
        assert view["workflow_id"] == workflow_id
        assert view["status"] == "active"
        assert view["operation_id"] is None
        assert view["cleanup"]["state"] == "not_required"
    finally:
        srv.shutdown()
        srv.server_close()


def test_mismatched_workflow_cannot_cancel_foreign_setup_transition(tmp_path):
    alignment = _ActiveTransitionAlignment(operation_id="op-1")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id = _start_workflow(base)
        _link(srv, workflow_id, operation_id="op-from-another-workflow")
        generated = _own_generated_artifact(srv, workflow_id)

        status, _, payload = _abandon(base, workflow_id)

        assert status == 409, payload
        assert payload["error"] == "setup_transition_context_mismatch"
        assert alignment.cancelled == []
        assert generated.exists()
        view = _workflow_view(base)
        assert view["status"] == "active"
        assert view["operation_id"] == "op-from-another-workflow"
    finally:
        srv.shutdown()
        srv.server_close()


def test_workflow_cancels_only_its_own_linked_operation(tmp_path):
    alignment = _ActiveTransitionAlignment(operation_id="op-1")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id = _start_workflow(base)
        _link(srv, workflow_id, operation_id="op-1")
        generated = _own_generated_artifact(srv, workflow_id)

        status, _, payload = _abandon(base, workflow_id)

        assert status == 200, payload
        assert payload["ok"] is True
        assert alignment.cancelled == ["op-1"]
        assert not generated.exists()
        assert _workflow_view(base)["status"] == "abandoned"
    finally:
        srv.shutdown()
        srv.server_close()


def test_unlinked_workflow_can_cleanup_when_no_transition_is_active(tmp_path):
    """No transition to cancel means no operation id has to be proven."""

    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id = _start_workflow(base)
        generated = _own_generated_artifact(srv, workflow_id)

        status, _, payload = _abandon(base, workflow_id)

        assert status == 200, payload
        assert payload["ok"] is True
        assert alignment.cancelled == []
        assert not generated.exists(), "provably owned artifacts must still be cleaned"
        assert _workflow_view(base)["status"] == "abandoned"
    finally:
        srv.shutdown()
        srv.server_close()


def test_unlinked_workflow_can_cleanup_after_a_terminal_transition(tmp_path):
    alignment = _ActiveTransitionAlignment()
    alignment.stage = "completed"
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id = _start_workflow(base)
        generated = _own_generated_artifact(srv, workflow_id)

        status, _, payload = _abandon(base, workflow_id)

        assert status == 200, payload
        assert alignment.cancelled == []
        assert not generated.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_setup_owner_cannot_cancel_a_foreign_transition_mode(tmp_path):
    """A Guided Upgrade transition is never adopted, and never cleaned around."""

    alignment = _ActiveTransitionAlignment(mode="guided_upgrade")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        # The Setup authority is earned on a free console; the foreign upgrade
        # transition arrives afterwards, which is what abandonment must refuse.
        workflow_id = _start_workflow(base)
        generated = _own_generated_artifact(srv, workflow_id)
        _attach_system_alignment(srv, alignment)

        status, _, payload = _abandon(base, workflow_id)

        assert status == 409, payload
        assert payload["error"] == "setup_transition_owner_unproven"
        assert alignment.cancelled == []
        assert generated.exists()
        assert _workflow_view(base)["status"] == "active"
    finally:
        srv.shutdown()
        srv.server_close()


# --- creation routes name the exact workflow ----------------------------------


@pytest.mark.parametrize("route,mode", CREATION_ROUTES)
def test_creation_routes_refuse_a_missing_workflow_id(tmp_path, route, mode):
    del mode
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        _workflow_id, intent_id = _authority(base)

        status, _, payload = _post_creation(base, route, None, intent_id)

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_required"
        assert alignment.committed == []
        assert alignment.launched == []
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("route,mode", CREATION_ROUTES)
def test_creation_routes_refuse_a_foreign_workflow_id(tmp_path, route, mode):
    del mode
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        current, intent_id = _authority(base)

        status, _, payload = _post_creation(base, route, "an-older-tab", intent_id)

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
        assert alignment.committed == []
        assert alignment.launched == []
        assert _workflow_view(base)["workflow_id"] == current
    finally:
        srv.shutdown()
        srv.server_close()


# --- creation and abandon are mutually exclusive ------------------------------


def _held_creation(base, srv, route, alignment):
    """Start one creation route and hold it at its commit boundary."""

    del srv
    workflow_id, intent_id = _authority(base)
    alignment.gate = _Gate()
    creation = _Mutation(
        base,
        route,
        {"tag": "v0.8.0", "setup_workflow_id": workflow_id},
        extra_headers={"X-Setup-Intent-ID": intent_id},
    )
    creation.start()
    assert alignment.gate.entered.wait(20), "the creation never reached its commit"
    return workflow_id, creation


@pytest.mark.parametrize("route,mode", CREATION_ROUTES)
def test_creation_and_abandon_are_mutually_exclusive(tmp_path, route, mode):
    """The Update Admin / Abandon race, for every creation route.

    Creation owns the workflow across the whole window in which its transition
    can still commit, so the concurrent Abandon is refused instead of
    terminalizing underneath it.
    """

    del mode
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, creation = _held_creation(base, srv, route, alignment)

        status, _, payload = _abandon(base, workflow_id)

        assert status == 409, payload
        assert payload["error"] == "setup_operation_in_progress"
        assert alignment.cancelled == []

        alignment.gate.release.set()
        creation.join(20)
        assert creation.status in {200, 202}, creation.payload
        assert alignment.committed == ["op-1"]
        view = _workflow_view(base)
        assert view["workflow_id"] == workflow_id
        assert view["status"] == "active"
        assert view["operation_id"] == "op-1", (
            "the committed transition must be durably linked to its workflow"
        )
    finally:
        if alignment.gate is not None:
            alignment.gate.release.set()
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("route,mode", CREATION_ROUTES)
def test_creation_cannot_commit_after_abandon_owns_the_workflow(tmp_path, route, mode):
    """The other side of the race: a terminal workflow creates nothing."""

    del mode
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _authority(base)

        with srv.setup_lifecycle.claim_termination(
            workflow_id=workflow_id, operation="abandon"
        ):
            status, _, payload = _post_creation(base, route, workflow_id, intent_id)

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
        assert alignment.committed == [], "no transition may be committed"
        assert alignment.launched == [], "no Admin replacement may be launched"
        assert _workflow_view(base)["operation_id"] is None
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("route,mode", CREATION_ROUTES)
def test_creation_cannot_create_transition_after_abandon(tmp_path, route, mode):
    """A completed abandon is a hard barrier for the creation routes."""

    del mode
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _authority(base)
        status, _, abandoned = _abandon(base, workflow_id)
        assert status == 200 and abandoned["ok"] is True, abandoned

        status, _, payload = _post_creation(base, route, workflow_id, intent_id)

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
        assert alignment.committed == []
        assert alignment.launched == []
    finally:
        srv.shutdown()
        srv.server_close()


# --- atomic transition ownership ---------------------------------------------


class _UnwritableWorkflows:
    """Make the workflow→transition link fail exactly like a full disk."""

    def __init__(self, store):
        self._store = store
        self._real = store.link_transition
        self.calls = []

    def install(self):
        self._store.link_transition = self
        return self

    def restore(self):
        self._store.link_transition = self._real

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise OSError(28, "No space left on device")


@pytest.mark.parametrize("route,mode", CREATION_ROUTES)
def test_transition_link_failure_prevents_transition_commit(tmp_path, route, mode):
    del mode
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _authority(base)
        failing = _UnwritableWorkflows(srv.setup_workflows).install()

        status, _, payload = _post_creation(base, route, workflow_id, intent_id)

        assert status >= 500, payload
        assert payload["error"] == "setup_transition_link_failed"
        assert failing.calls, "the link must be attempted inside the boundary"
        assert alignment.committed == [], "a failed link must commit no transition"
        assert alignment.active is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_transition_link_failure_prevents_admin_launcher(tmp_path):
    alignment = _CommitTrackingAlignment(admin_update_required=True)
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _authority(base)
        _UnwritableWorkflows(srv.setup_workflows).install()

        status, _, payload = _post_creation(base, UPDATE_ADMIN, workflow_id, intent_id)

        assert status >= 500, payload
        assert payload["error"] == "setup_transition_link_failed"
        assert alignment.launched == [], "no Admin replacement may be launched"
        assert alignment.committed == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_transition_link_failure_leaves_the_workflow_active_and_unlinked(tmp_path):
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _authority(base)
        failing = _UnwritableWorkflows(srv.setup_workflows).install()

        status, _, _payload = _post_creation(base, CONFIRM, workflow_id, intent_id)
        assert status >= 500

        view = _workflow_view(base)
        assert view["workflow_id"] == workflow_id
        assert view["status"] == "active"
        assert view["operation_id"] is None

        # Persistence restored: the retry starts cleanly and links the new
        # transition before it is committed.
        failing.restore()
        _workflow_id, retry_intent = _authority(base)
        status, _, payload = _post_creation(base, CONFIRM, workflow_id, retry_intent)
        assert status == 200, payload
        assert alignment.committed == ["op-1"]
        assert _workflow_view(base)["operation_id"] == "op-1"
    finally:
        srv.shutdown()
        srv.server_close()


# --- an existing transition is only ever resumed by its owner ----------------


def test_existing_transition_requires_exact_workflow_operation_id(tmp_path):
    alignment = _ActiveTransitionAlignment(operation_id="op-1")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _authority(base)

        unproven_status, _, unproven = _post_creation(
            base, CONFIRM, workflow_id, intent_id
        )
        assert unproven_status == 409, unproven
        assert unproven["error"] == "setup_transition_owner_unproven"
        assert alignment.confirm_calls == [], (
            "an unowned transition must never be advanced"
        )

        _link(srv, workflow_id, operation_id="op-somewhere-else")
        entry_status, _, entry = _request(
            f"{base}/api/admin/start-path",
            method="POST",
            body={"choice": "setup_new", "confirm": True},
        )
        assert entry_status == 409, entry
        assert entry["detail"] == "setup_transition_context_mismatch"

        mismatch_intent = _direct_intent(srv, base, workflow_id)
        mismatch_status, _, mismatch = _post_creation(
            base, CONFIRM, workflow_id, mismatch_intent
        )
        assert mismatch_status == 409, mismatch
        assert mismatch["error"] == "setup_transition_context_mismatch"
        assert alignment.confirm_calls == []

        _link(srv, workflow_id, operation_id="op-1")
        _workflow_id, owner_intent = _authority(base)
        owned_status, _, owned = _post_creation(
            base, CONFIRM, workflow_id, owner_intent
        )
        assert owned_status == 200, owned
        assert alignment.confirm_calls == [
            {"requested_tag": "v0.8.0", "mode": "fresh_install"}
        ]
    finally:
        srv.shutdown()
        srv.server_close()


def test_unproven_transition_ownership_keeps_the_setup_intent(tmp_path):
    """A refused authority check must not spend the one-shot confirmation."""

    alignment = _ActiveTransitionAlignment(operation_id="op-1")
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _authority(base)

        status, _, refused = _post_creation(base, CONFIRM, workflow_id, intent_id)
        assert status == 409, refused
        assert refused["error"] == "setup_transition_owner_unproven"

        _link(srv, workflow_id, operation_id="op-1")
        status, _, accepted = _post_creation(base, CONFIRM, workflow_id, intent_id)
        assert status == 200, accepted
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_creation_route_still_needs_its_setup_intent(tmp_path):
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, _intent_id = _authority(base)

        status, _, payload = _post_creation(base, CONFIRM, workflow_id, None)

        assert status == 409, payload
        assert payload["error"] == "setup_intent_required"
        assert alignment.committed == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_refused_lifecycle_claim_does_not_spend_the_setup_intent(tmp_path):
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _authority(base)

        with srv.setup_lifecycle.claim_mutation(
            workflow_id=workflow_id, operation="config_write"
        ):
            status, _, refused = _post_creation(base, CONFIRM, workflow_id, intent_id)
        assert status == 409, refused
        assert refused["error"] == "setup_operation_in_progress"

        status, _, accepted = _post_creation(base, CONFIRM, workflow_id, intent_id)
        assert status == 200, accepted
        assert alignment.committed == ["op-1"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_setup_transition_survives_and_is_resumed_by_its_owner(tmp_path):
    """The link is durable: a fresh Admin process resumes the same transition."""

    manager = _control_export_manager(tmp_path)
    alignment = _CommitTrackingAlignment()
    srv, base = _serve(release_manager=manager)
    _attach_system_alignment(srv, alignment)
    try:
        workflow_id, intent_id = _authority(base)
        status, _, created = _post_creation(base, CONFIRM, workflow_id, intent_id)
        assert status == 200, created
        record = json.loads(
            (
                Path(srv.setup_workflows.admin_data_dir)
                / "state"
                / "guided-setup-workflow.json"
            ).read_text(encoding="utf-8")
        )
        assert record["operation_id"] == "op-1"
        assert record["transition_mode"] == "fresh_install"
        assert record["selected_system_tag"] == "v0.8.0"
    finally:
        srv.shutdown()
        srv.server_close()

    resumed_alignment = _ActiveTransitionAlignment(operation_id="op-1")
    srv, base = _serve(release_manager=manager)
    _attach_system_alignment(srv, resumed_alignment)
    try:
        # The same durable workflow owns the transition after the restart, and a
        # foreign tab still cannot resume or cancel it.
        view = _workflow_view(base)
        assert view["workflow_id"] == workflow_id
        assert view["operation_id"] == "op-1"

        status, _, foreign = _request(
            f"{base}{CONFIRM}",
            method="POST",
            body={"tag": "v0.8.0", "setup_workflow_id": "an-older-tab"},
        )
        assert status == 409, foreign
        assert foreign["error"] == "setup_workflow_not_active"
        assert resumed_alignment.confirm_calls == []

        status, _, refused = _abandon(base, "an-older-tab")
        assert status == 409, refused
        assert refused["error"] == "setup_workflow_not_active"
        assert resumed_alignment.cancelled == []

        status, _, discarded = _abandon(base, workflow_id)
        assert status == 200, discarded
        assert resumed_alignment.cancelled == ["op-1"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_status_read_failure_refuses_the_creation_routes(tmp_path):
    """An unreadable transition can never be assumed absent."""

    class _UnreadableStatus(_CommitTrackingAlignment):
        def status(self, *, operation_active=None):
            del operation_active
            raise SystemAlignmentError("transition_state_unreadable", "cannot read")

    alignment = _UnreadableStatus()
    srv, base = _serve(release_manager=_control_export_manager(tmp_path))
    try:
        # The authority is earned while the transition is still readable; the
        # unreadable status is what the creation route then has to refuse.
        workflow_id, intent_id = _authority(base)
        _attach_system_alignment(srv, alignment)

        status, _, payload = _post_creation(base, CONFIRM, workflow_id, intent_id)

        assert status == 409, payload
        assert payload["error"] == "transition_status_unavailable"
        assert alignment.committed == []
    finally:
        srv.shutdown()
        srv.server_close()
