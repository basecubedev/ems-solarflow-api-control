# SPDX-License-Identifier: AGPL-3.0-or-later
"""A device plan is a decision of one Setup run, in one state of that run.

``workflow_id`` alone answers "which run", not "which run, still owning this".
A run that was cancelled, finished or replaced is no longer an owner, and a
plan issued inside it is a decision about a session that no longer exists — even
when the replacement happens to carry the same identity.

Every case below is refused with a project-consistent conflict and leaves the
live configuration byte-exact. Nothing is retried, and no plan is repaired: the
browser plans again, which is the safe direction.

See ``docs/developer/developer.md`` — "Device plan → config preview → apply".
"""

from pathlib import Path

import pytest

from admin.device_plan_registry import (
    REASON_CANDIDATES,
    REASON_CONFIRMATION,
    REASON_CONTRACT,
    REASON_DRAFT,
    REASON_UNKNOWN,
    REASON_WORKFLOW,
    DevicePlanRegistry,
    device_plan_conflict,
)
from admin.install_context import detect_install_context
from admin.guided_setup_workflow import (
    STATUS_ABANDONED,
    STATUS_COMPLETED,
    STATUS_SUPERSEDED,
    workflow_authority_revision,
)
from tests.test_admin_server import (
    _control_export_manager,
    _own_active_setup_transition,
    _request,
    _serve,
)
from tests.test_admin_setup_plan_binding import (
    _Devices,
    _apply,
    _device_plan,
    _draft,
    _inverter,
    _preview,
    _start_workflow,
    _write_live,
)

pytestmark = pytest.mark.simulation

LIVE = '{"live": "A"}\n'


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _planned_server(tmp_path):
    srv, base = _serve(
        mdns_provider=_Devices([_inverter()]),
        release_manager=_control_export_manager(tmp_path),
    )
    _write_live(LIVE)
    workflow_id = _start_workflow(base)
    # The harness pre-seeds an active Setup transition; production would have
    # linked it into the workflow pre-commit, and an unlinked owner may not
    # terminate itself.
    _own_active_setup_transition(srv, base, workflow_id)
    plan_id = _device_plan(base)["plan_id"]
    return srv, base, workflow_id, plan_id


def _abandon(base, workflow_id):
    status, _, payload = _request(
        f"{base}/api/setup/abandon",
        method="POST",
        body={"setup_workflow_id": workflow_id},
    )
    assert status == 200, payload


# --- a run that stopped owning its decisions ---------------------------------
def test_a_cancelled_workflow_cannot_review_its_own_plan(tmp_path):
    srv, base, workflow_id, plan_id = _planned_server(tmp_path)
    try:
        _abandon(base, workflow_id)

        status, _, payload = _preview(base, workflow_id, plan_id)
        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(
    "status_value", [STATUS_COMPLETED, STATUS_ABANDONED, STATUS_SUPERSEDED]
)
def test_a_terminal_workflow_cannot_review_its_own_plan(tmp_path, status_value):
    """Terminalized directly, so the plan is still in the registry when asked."""

    srv, base, workflow_id, plan_id = _planned_server(tmp_path)
    try:
        srv.setup_workflows.finish(workflow_id, status=status_value)
        assert srv.device_plans.get(plan_id) is not None

        status, _, payload = _preview(base, workflow_id, plan_id)
        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
    finally:
        srv.shutdown()
        srv.server_close()


def test_another_active_workflow_may_not_use_an_older_runs_plan(tmp_path):
    srv, base, old_workflow, old_plan = _planned_server(tmp_path)
    try:
        _abandon(base, old_workflow)
        new_workflow = _start_workflow(base)
        assert new_workflow != old_workflow

        status, _, payload = _preview(base, new_workflow, old_plan)
        assert status == 409, payload
        assert payload["error"] == "stale_device_plan"

        # The current run plans for itself and is unaffected.
        status, _, payload = _preview(base, new_workflow, _device_plan(base)["plan_id"])
        assert status == 200, payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_recreated_workflow_does_not_inherit_an_older_plan(tmp_path, monkeypatch):
    """Same identity, different run: the ownership revision is what separates them.

    The id factory is pinned so the replacement carries the identity the plan
    was issued under — the one case ``workflow_id`` alone cannot tell apart.
    """

    import admin.guided_setup_workflow as guided

    stamps = iter(f"2026-08-03T00:00:{second:02d}Z" for second in range(1, 60))
    monkeypatch.setattr(guided, "utc_now_iso", lambda: next(stamps))

    srv, base, workflow_id, plan_id = _planned_server(tmp_path)
    try:
        workflows = srv.setup_workflows
        before = workflow_authority_revision(workflows.active())
        monkeypatch.setattr(workflows, "_id_factory", lambda: workflow_id)
        workflows.finish(workflow_id, status=STATUS_ABANDONED)
        recreated = workflows.start_replacement()

        assert recreated["workflow_id"] == workflow_id
        assert workflow_authority_revision(recreated) != before
        assert srv.device_plans.get(plan_id) is not None

        status, _, payload = _preview(base, workflow_id, plan_id)
        assert status == 409, payload
        assert payload["error"] == "stale_device_plan"
    finally:
        srv.shutdown()
        srv.server_close()


# --- direct mutation with a stale run ----------------------------------------
@pytest.mark.parametrize("path", ["apply", "write"])
def test_a_mutation_with_a_stale_workflow_changes_nothing(tmp_path, path):
    srv, base, workflow_id, plan_id = _planned_server(tmp_path)
    try:
        status, _, preview = _preview(base, workflow_id, plan_id)
        assert status == 200, preview
        preview_id = preview["config_preview_id"]

        _abandon(base, workflow_id)
        _start_workflow(base)

        request = dict(_draft())
        request["setup_workflow_id"] = workflow_id
        request["config_preview_id"] = preview_id
        request["device_plan_id"] = plan_id
        status, _, payload = _request(
            f"{base}/api/setup/config/{path}", method="POST", body=request
        )

        assert status == 409, payload
        assert payload["error"] == "setup_workflow_not_active"
        live = Path(detect_install_context().config_path)
        assert live.read_text(encoding="utf-8") == LIVE
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_applied_run_leaves_no_reusable_plan(tmp_path):
    """The full chain still applies once, and its plan is spent with the run."""

    srv, base, workflow_id, plan_id = _planned_server(tmp_path)
    try:
        status, _, preview = _preview(base, workflow_id, plan_id)
        assert status == 200, preview

        status, _, payload = _apply(
            base, workflow_id, preview["config_preview_id"], plan_id
        )
        assert status == 200, payload

        status, _, payload = _apply(
            base, workflow_id, preview["config_preview_id"], plan_id
        )
        assert status == 409, payload
    finally:
        srv.shutdown()
        srv.server_close()


# --- what the ownership revision is made of ----------------------------------
def _record(**overrides):
    record = {
        "format_version": 2,
        "workflow_id": "w" * 32,
        "type": "guided_setup",
        "status": "active",
        "created_at": "2026-08-03T00:00:01Z",
    }
    record.update(overrides)
    return record


def test_the_ownership_revision_moves_with_the_run_and_its_state():
    base = workflow_authority_revision(_record())
    assert workflow_authority_revision(None) is None
    assert workflow_authority_revision("nonsense") is None
    for moved in (
        {"workflow_id": "x" * 32},
        {"status": STATUS_COMPLETED},
        {"status": STATUS_ABANDONED},
        {"status": STATUS_SUPERSEDED},
        {"created_at": "2026-08-03T00:00:02Z"},
        {"format_version": 3},
        {"type": "guided_upgrade"},
    ):
        assert workflow_authority_revision(_record(**moved)) != base, moved


def test_normal_progress_inside_a_run_keeps_its_plans():
    """Reviewing, linking a transition or binding artifacts is not a new owner.

    A revision that moved on ordinary progress would revoke a plan every time
    the browser previewed, and the wizard could never converge.
    """

    base = workflow_authority_revision(_record())
    for unchanged in (
        {"preview": {"preview_id": "p" * 32}},
        {"operation_id": "op-1"},
        {"updated_at": "2026-08-03T00:05:00Z"},
        {"artifacts": {"generated_config": "workflows/guided-setup/w/config.json"}},
        {"selected_system_tag": "v1.2.3"},
    ):
        assert workflow_authority_revision(_record(**unchanged)) == base, unchanged


# --- which fact refused the plan ---------------------------------------------
def _contract(**overrides):
    contract = {
        "workflow_id": "wf-1",
        "workflow_revision": "sha256:" + "1" * 64,
        "draft_revision": "plan:v1:state",
        "candidate_authority_fingerprint": "plan:v1:generation",
        "confirmation_fingerprint": "plan:v1:settled",
        "decision_fingerprint": "plan:v1:decisions",
        "executable_operations_fingerprint": "plan:v1:operations",
        "expected_draft_fingerprint": "plan:v1:draft",
    }
    contract.update(overrides)
    return contract


def _current(**overrides):
    current = {
        "workflow_id": "wf-1",
        "workflow_revision": "sha256:" + "1" * 64,
        "candidate_authority_fingerprint": "plan:v1:generation",
        "settled_confirmation_fingerprint": "plan:v1:settled",
        "submitted_draft_fingerprint": "plan:v1:draft",
    }
    current.update(overrides)
    return current


def _entry(**overrides):
    return DevicePlanRegistry().record("plan:v1:a", **_contract(**overrides))


def test_a_current_contract_authorizes_the_mutation():
    assert device_plan_conflict(_entry(), **_current()) is None


@pytest.mark.parametrize(
    "moved, reason",
    [
        ({"workflow_id": "wf-2"}, REASON_WORKFLOW),
        ({"workflow_id": None}, REASON_WORKFLOW),
        ({"workflow_revision": "sha256:" + "9" * 64}, REASON_WORKFLOW),
        ({"workflow_revision": None}, REASON_WORKFLOW),
        ({"candidate_authority_fingerprint": "plan:v1:moved"}, REASON_CANDIDATES),
        ({"settled_confirmation_fingerprint": "plan:v1:pending"}, REASON_CONFIRMATION),
        ({"submitted_draft_fingerprint": "plan:v1:other"}, REASON_DRAFT),
    ],
)
def test_each_authority_fact_names_its_own_refusal(moved, reason):
    assert device_plan_conflict(_entry(), **_current(**moved)) == reason


def test_a_missing_or_tampered_contract_is_refused():
    assert device_plan_conflict(None, **_current()) == REASON_UNKNOWN
    for field in ("plan_fingerprint", "mutation_authority_fingerprint"):
        tampered = dict(_entry(), **{field: "sha256:" + "0" * 64})
        assert device_plan_conflict(tampered, **_current()) == REASON_CONTRACT
    # A part rewritten without its digests is the same refusal: the contract is
    # only ever accepted whole.
    for field in ("expected_draft_fingerprint", "draft_revision", "workflow_revision"):
        tampered = dict(_entry(), **{field: "moved"})
        assert device_plan_conflict(tampered, **_current()) == REASON_CONTRACT


def test_a_plan_that_authorized_nothing_never_matches_a_draft():
    entry = _entry(expected_draft_fingerprint=None)
    assert device_plan_conflict(entry, **_current()) == REASON_DRAFT
