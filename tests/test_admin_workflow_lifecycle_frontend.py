# SPDX-License-Identifier: AGPL-3.0-or-later
"""The console renders the lifecycle verdict; it never decides it.

Switching between the guided workflows is one previewed, confirmed backend
operation. The browser shows what will be stopped and what stays untouched,
refuses to offer a force action when the backend refused for a reason it can
still prove, and clears only its own projections after the switch is reported.

See ``docs/technical/admin-workflow-state.md``.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.simulation

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _decl(js, header):
    body = js.split(header, 1)[1]
    return header + re.split(r"\n(?:async function |function |const |let )", body)[0]


def _run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the workflow lifecycle frontend tests")
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


SWITCH_HELPERS = (
    "const WORKFLOW_LIFECYCLE_BASE",
    "const WORKFLOW_OWNER_LABELS",
    "const WORKFLOW_BLOCKED_CODES",
    "const WORKFLOW_RECOVERABLE_CODES",
    "function workflowOwnerLabel",
    "function workflowSwitchConfirmText",
    "function workflowSwitchRefusal",
    "async function postWorkflowLifecycle",
    "async function requestWorkflowSwitch",
)


def _switch_driver(*, responses, preamble="", epilogue):
    js = _read("admin.js")
    helpers = "\n".join(_decl(js, header) for header in SWITCH_HELPERS)
    return _run_node(
        "const responses = "
        + json.dumps(responses)
        + ";\nconst calls = [];\nconst confirms = [];\n"
        + """
global.window = {
  confirm: (text) => {
    confirms.push(text);
    return global.__confirmAnswer !== false;
  },
};
global.fetch = async (url, options) => {
  calls.push({ url: String(url), body: JSON.parse((options && options.body) || "{}") });
  const queue = responses[String(url).indexOf("/preview") >= 0 ? "preview" : "execute"];
  const next = queue.length > 1 ? queue.shift() : queue[0];
  return { ok: next.ok, status: next.status || 200, json: async () => next.data };
};
"""
        + preamble
        + "\n"
        + helpers
        + "\n"
        + epilogue
    )


def _plan(**overrides):
    plan = {
        "ok": True,
        "target": "guided_upgrade",
        "target_owner": "guided_upgrade",
        "current_owner": "guided_setup",
        "action": "discard_guided_setup",
        "blocked": False,
        "blocking_reason": None,
        "confirmation_required": True,
        "will_reset": ["Guided Setup workflow", "Setup System Build transition"],
        "will_preserve": ["live EMS configuration", "backups"],
        "resume_available": False,
        "recoverable": False,
        "fingerprint": "sha256:one",
        "lifecycle": {"owner": "guided_setup", "state": "active"},
    }
    plan.update(overrides)
    return plan


# --- switch preview and confirmation ------------------------------------------


def test_a_switch_previews_before_it_executes():
    out = _switch_driver(
        responses={
            "preview": [{"ok": True, "data": _plan()}],
            "execute": [{"ok": True, "data": {"ok": True, "action": "discard"}}],
        },
        epilogue="""
(async () => {
  const result = await requestWorkflowSwitch("guided_upgrade");
  console.log(JSON.stringify({
    ok: result.ok,
    order: calls.map((call) => call.url),
    body: calls[1].body,
    confirms: confirms,
  }));
})();
""",
    )

    assert out["ok"] is True
    assert out["order"][0].endswith("/switch/preview")
    assert out["order"][1].endswith("/switch")
    # The execution is bound to the exact state the preview was computed for.
    assert out["body"] == {
        "target": "guided_upgrade",
        "confirm": True,
        "fingerprint": "sha256:one",
    }


def test_the_confirmation_lists_the_reset_and_the_preserved_state():
    out = _switch_driver(
        responses={
            "preview": [{"ok": True, "data": _plan()}],
            "execute": [{"ok": True, "data": {"ok": True}}],
        },
        epilogue="""
(async () => {
  await requestWorkflowSwitch("guided_upgrade");
  console.log(JSON.stringify({ confirms }));
})();
""",
    )

    text = out["confirms"][0]
    assert "Guided Setup workflow" in text
    assert "Setup System Build transition" in text
    assert "live EMS configuration" in text
    assert "backups" in text


def test_a_declined_confirmation_never_reaches_the_switch():
    out = _switch_driver(
        responses={
            "preview": [{"ok": True, "data": _plan()}],
            "execute": [{"ok": True, "data": {"ok": True}}],
        },
        preamble="global.__confirmAnswer = false;",
        epilogue="""
(async () => {
  const result = await requestWorkflowSwitch("guided_upgrade");
  console.log(JSON.stringify({
    ok: result.ok,
    cancelled: result.cancelled,
    urls: calls.map((call) => call.url),
  }));
})();
""",
    )

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert len(out["urls"]) == 1


def test_a_blocked_operation_offers_resume_and_recovery_but_never_a_force():
    out = _switch_driver(
        responses={
            "preview": [
                {
                    "ok": True,
                    "data": _plan(
                        blocked=True,
                        blocking_reason="workflow_operation_in_progress",
                        confirmation_required=False,
                        resume_available=True,
                        recoverable=False,
                        lifecycle={
                            "owner": "guided_upgrade",
                            "state": "operation_running",
                            "blocking_reason": "workflow_operation_in_progress",
                        },
                    ),
                }
            ],
            "execute": [{"ok": True, "data": {"ok": True}}],
        },
        epilogue="""
(async () => {
  const result = await requestWorkflowSwitch("guided_setup");
  console.log(JSON.stringify({
    ok: result.ok,
    blocked: result.blocked,
    resumeAvailable: result.resumeAvailable,
    message: result.message,
    urls: calls.map((call) => call.url),
  }));
})();
""",
    )

    assert out["ok"] is False
    assert out["blocked"] is True
    assert out["resumeAvailable"] is True
    assert "Resume" in out["message"]
    assert "force" not in out["message"].lower()
    # Nothing was executed: a blocked preview never becomes a mutation.
    assert len(out["urls"]) == 1


def test_a_stale_fingerprint_refreshes_the_preview_once():
    out = _switch_driver(
        responses={
            "preview": [
                {"ok": True, "data": _plan(fingerprint="sha256:one")},
                {"ok": True, "data": _plan(fingerprint="sha256:two")},
            ],
            "execute": [
                {
                    "ok": False,
                    "status": 409,
                    "data": {"ok": False, "error": "workflow_lifecycle_changed"},
                },
                {"ok": True, "data": {"ok": True, "action": "discard"}},
            ],
        },
        epilogue="""
(async () => {
  const result = await requestWorkflowSwitch("guided_upgrade");
  console.log(JSON.stringify({
    ok: result.ok,
    fingerprints: calls
      .filter((call) => call.body.fingerprint)
      .map((call) => call.body.fingerprint),
  }));
})();
""",
    )

    assert out["ok"] is True
    assert out["fingerprints"] == ["sha256:one", "sha256:two"]


def test_a_recoverable_refusal_names_the_maintenance_recovery():
    out = _switch_driver(
        responses={
            "preview": [{"ok": True, "data": _plan()}],
            "execute": [
                {
                    "ok": False,
                    "status": 409,
                    "data": {
                        "ok": False,
                        "error": "workflow_recovery_required",
                        "message": "Ownership review required.",
                    },
                }
            ],
        },
        epilogue="""
(async () => {
  const result = await requestWorkflowSwitch("guided_upgrade");
  console.log(JSON.stringify({ ok: result.ok, recoverable: result.recoverable }));
})();
""",
    )

    assert out["ok"] is False
    assert out["recoverable"] is True


# --- entry points --------------------------------------------------------------


def test_guided_setup_choice_switches_when_another_workflow_owns_the_console():
    js = _read("admin.js")
    start = _decl(js, "async function startPath")

    assert "fetchWorkflowLifecycle()" in start
    assert "LIFECYCLE_SWITCH_OWNERS.has(lifecycle.owner)" in start
    assert "startGuidedSetupThroughLifecycle()" in start
    owners = _decl(js, "const LIFECYCLE_SWITCH_OWNERS")
    assert '"guided_upgrade"' in owners
    assert '"unknown"' in owners


def test_guided_upgrade_choice_switches_when_setup_owns_the_console():
    js = _read("admin.js")
    prepare = _decl(js, "async function prepareUpgradeTarget")
    resolve = _decl(js, "async function resolveSetupConflictForUpgrade")

    assert 'data.error === "setup_abandon_required"' in prepare
    assert "resolveSetupConflictForUpgrade()" in prepare
    assert 'requestWorkflowSwitch("guided_upgrade")' in resolve


def test_a_successful_switch_clears_the_task_projection_and_reloads_state():
    js = _read("admin.js")
    resolve = _decl(js, "async function resolveSetupConflictForUpgrade")
    projection = _decl(js, "function clearWorkflowTaskProjection")

    assert resolve.index("switched.ok") < resolve.index(
        "clearWorkflowTaskProjection()"
    )
    assert "loadSystemAlignmentStatus()" in resolve
    for cleared in (
        "guidedSetupGeneration += 1",
        "clearGuidedSetupTimers()",
        "setupIntentId = null",
        "setSetupWorkflowId(null)",
        "showSetupCleanupIncomplete(null)",
    ):
        assert cleared in projection


# --- Maintenance recovery card --------------------------------------------------


def test_the_recovery_card_is_collapsed_and_quiet_on_a_healthy_console():
    html = _read("index.html")
    card = html.split('id="maintenance-workflow-recovery"', 1)[1]

    assert 'data-open="false"' in card.split(">", 1)[0]
    assert 'id="maintenance-workflow-recovery-body" hidden' in card
    for action in ("safe", "advanced"):
        button = card.split(f'id="maintenance-workflow-recovery-{action}"', 1)[1]
        assert "hidden" in button.split(">", 1)[0]


def _render_driver(plan, *, epilogue):
    js = _read("admin.js")
    helpers = "\n".join(
        _decl(js, header)
        for header in (
            "const WORKFLOW_OWNER_LABELS",
            "const WORKFLOW_STATE_LABELS",
            "function workflowOwnerLabel",
            "function workflowStateLabel",
            "function shortWorkflowReference",
            "function workflowRecoveryAge",
            "function workflowRecoveryReference",
            "function workflowRecoverySummaryText",
            "function renderWorkflowRecovery",
        )
    )
    preamble = """
const opened = [];
let workflowRecoveryPlan = null;
function setMaintenanceCardOpen(id, open) { opened.push([id, open]); }
function element() { return { textContent: "", hidden: true }; }
const workflowRecoveryEls = {
  summary: element(),
  owner: element(),
  state: element(),
  reference: element(),
  age: element(),
  safe: element(),
  advanced: element(),
  details: element(),
  files: element(),
  preserved: element(),
  fingerprint: element(),
};
"""
    return _run_node(
        preamble
        + helpers
        + "\nconst plan = "
        + json.dumps(plan)
        + ";\nrenderWorkflowRecovery(plan);\n"
        + epilogue
    )


def _recovery_plan(**overrides):
    plan = {
        "ok": True,
        "blocking": False,
        "operation_running": False,
        "safe": {"available": False, "actions": [], "confirmation_required": True},
        "advanced": {
            "available": False,
            "files": [],
            "confirmation_required": True,
            "reason_required": True,
        },
        "will_preserve": ["config/config.json", "docker-compose.yml"],
        "fingerprint": "sha256:" + "a" * 64,
        "lifecycle": {
            "owner": "none",
            "state": "idle",
            "setup": None,
            "transition": None,
        },
    }
    plan.update(overrides)
    return plan


def test_the_recovery_card_shows_the_safe_reset_only_when_the_backend_allows_it():
    hidden = _render_driver(
        _recovery_plan(),
        epilogue="""
console.log(JSON.stringify({
  safeHidden: workflowRecoveryEls.safe.hidden,
  advancedHidden: workflowRecoveryEls.advanced.hidden,
  opened: opened,
}));
""",
    )
    offered = _render_driver(
        _recovery_plan(
            blocking=True,
            safe={"available": True, "actions": ["setup_cleanup"]},
            lifecycle={
                "owner": "guided_setup",
                "state": "review_required",
                "setup": {"workflow_id": "wf-1234567890abcdef"},
                "transition": None,
            },
        ),
        epilogue="""
console.log(JSON.stringify({
  safeHidden: workflowRecoveryEls.safe.hidden,
  advancedHidden: workflowRecoveryEls.advanced.hidden,
  opened: opened,
  reference: workflowRecoveryEls.reference.textContent,
  state: workflowRecoveryEls.state.textContent,
}));
""",
    )

    assert hidden["safeHidden"] is True
    assert hidden["advancedHidden"] is True
    assert hidden["opened"] == []
    assert offered["safeHidden"] is False
    assert offered["advancedHidden"] is True
    # A blocking state opens the card by itself, so the operator finds it.
    assert offered["opened"] == [["maintenance-workflow-recovery", True]]
    assert offered["state"] == "Ownership review required"
    # Identifiers are shortened for display and never shown in full.
    assert offered["reference"] == "workflow wf-123456789…"


def test_the_recovery_card_lists_the_affected_admin_state_only():
    out = _render_driver(
        _recovery_plan(
            blocking=True,
            advanced={
                "available": True,
                "files": ["state/guided-setup-workflow.json"],
            },
            lifecycle={
                "owner": "unknown",
                "state": "malformed",
                "setup": None,
                "transition": None,
            },
        ),
        epilogue="""
console.log(JSON.stringify({
  files: workflowRecoveryEls.files.textContent,
  preserved: workflowRecoveryEls.preserved.textContent,
  fingerprint: workflowRecoveryEls.fingerprint.textContent,
  advancedHidden: workflowRecoveryEls.advanced.hidden,
}));
""",
    )

    assert out["files"] == "state/guided-setup-workflow.json"
    assert "config/config.json" in out["preserved"]
    assert "docker-compose.yml" in out["preserved"]
    assert out["fingerprint"].endswith("…")
    assert out["advancedHidden"] is False


def test_the_advanced_release_requires_two_confirmations_and_a_derived_reason():
    js = _read("admin.js")
    run = _decl(js, "async function runWorkflowRecovery")

    assert run.count("window.confirm(") >= 3
    assert "WORKFLOW_ADVANCED_CONFIRM" in run
    assert "WORKFLOW_ADVANCED_SECOND_CONFIRM" in run
    # The reason is derived from the state that was acted on, never typed.
    assert "workflowRecoveryReason(plan)" in run
    assert "window.prompt" not in run
    reason = _decl(js, "function workflowRecoveryReason")
    assert "lifecycle.blocking_reason" in reason


def test_recovery_actions_are_gated_on_the_backend_verdict():
    js = _read("admin.js")
    run = _decl(js, "async function runWorkflowRecovery")

    assert 'if (mode === "safe" && !plan.safe.available) return;' in run
    assert (
        'if (mode === "release_stale_state" && !plan.advanced.available) return;' in run
    )
    # Only a reported success clears the browser projection.
    assert run.index("executed.data.ok !== true") < run.index(
        "clearWorkflowTaskProjection()"
    )


def test_the_recovery_card_never_writes_dynamic_values_as_markup():
    js = _read("admin.js")
    render = _decl(js, "function renderWorkflowRecovery")

    assert "innerHTML" not in render
    assert render.count("textContent") >= 6
