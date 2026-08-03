# SPDX-License-Identifier: AGPL-3.0-or-later
"""The browser's half of the Setup device-plan authority chain.

The backend decides whether a plan still authorizes a mutation; these tests do
not restate any of that. What they pin is the only thing the browser is
responsible for: a refused plan must leave *no* mutation authority behind in the
tab, and must not be turned back into one without a fresh plan and a fresh
review earned against current state.

All four refusals — no plan, a stale one, an unanswered confirmation and a draft
the plan did not authorize — are repaired the same way, so they are handled the
same way here.

See ``docs/developer/developer.md`` — "Device plan → config preview → apply".
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.simulation

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)

CONFLICT_CODES = (
    "device_plan_required",
    "stale_device_plan",
    "device_plan_draft_mismatch",
    "device_plan_confirmation_required",
)


def _admin_js():
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        return handle.read()


def _fragment(source, start, end):
    assert start in source, start
    body = source[source.index(start) :]
    assert end in body, end
    return body[: body.index(end)]


def _harness():
    """The plan-conflict handling from admin.js, with everything else stubbed."""

    source = _admin_js()
    return "\n".join(
        [
            _fragment(
                source,
                "const SETUP_DEVICE_PLAN_CONFLICT_ERRORS",
                "\n// Workflow-identity conflicts",
            ),
            _fragment(
                source, "function setSetupPreviewId(", "\nfunction setConfigBaseline("
            ),
            _fragment(
                source, "function handleSetupDevicePlanConflict(", "\n// The exact preview"
            ),
            _fragment(
                source, "function configExportAllowed(", "\nfunction setConfigExportReady("
            ),
        ]
    )


def _run(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the setup plan authority contract")
    script = f"""
const calls = {{planRefreshes: 0, previews: 0, mutations: 0, saved: 0}};
let setupConfigPreviewId = "preview-1";
let latestConfigPreview = {{ready: true}};
let configPreviewPlanId = "plan:v1:a";
const setupPlan = {{plan_id: "plan:v1:a"}};
const configEls = {{previewReady: {{textContent: "Ready"}}}};
function saveSetupWorkflowState() {{ calls.saved += 1; }}
function setConfigExportReady(ready) {{ calls.exportReady = ready; }}
function renderConfigValidation() {{ calls.validationRenders = (calls.validationRenders || 0) + 1; }}
function refreshSetupPlan() {{ calls.planRefreshes += 1; }}
function requestConfigPreview() {{ calls.previews += 1; }}
function submitSetupMutation() {{ calls.mutations += 1; }}
{_harness()}
{scenario}
"""
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _report():
    return """
console.log(JSON.stringify({
  previewId: setupConfigPreviewId,
  exportAllowed: configExportAllowed(),
  ready: configEls.previewReady.textContent,
  calls,
}));
"""


# --- every refusal revokes this tab's mutation authority ----------------------
@pytest.mark.parametrize("code", CONFLICT_CODES)
def test_a_refused_plan_leaves_no_preview_authority(code):
    outcome = _run(
        f'handleSetupDevicePlanConflict({{error: "{code}", message: "moved"}});'
        + _report()
    )

    assert outcome["previewId"] is None
    assert outcome["exportAllowed"] is False
    assert outcome["calls"]["exportReady"] is False
    assert outcome["ready"] == "Needs attention"


def test_a_stale_confirmation_invalidates_the_preview():
    """An unanswered switch is not a preview that can be finished later."""

    outcome = _run(
        'handleSetupDevicePlanConflict({error: "device_plan_confirmation_required"});'
        + _report()
    )

    assert outcome["previewId"] is None
    assert outcome["exportAllowed"] is False


def test_a_stale_workflow_revision_invalidates_the_preview():
    """A run that stopped owning the plan is refused as a stale plan.

    The browser never learns *which* fact moved — a plan from a replaced run and
    a plan from a moved discovery state are the same repair.
    """

    outcome = _run(
        'handleSetupDevicePlanConflict({error: "stale_device_plan"});' + _report()
    )

    assert outcome["previewId"] is None
    assert outcome["exportAllowed"] is False
    assert outcome["calls"]["planRefreshes"] == 1


# --- nothing is retried automatically ----------------------------------------
def test_a_refused_plan_asks_for_a_new_plan_and_nothing_else():
    """The repair is a fresh plan. Not a re-preview, and never the mutation."""

    outcome = _run(
        'handleSetupDevicePlanConflict({error: "stale_device_plan"});' + _report()
    )

    assert outcome["calls"]["planRefreshes"] == 1
    assert outcome["calls"]["previews"] == 0
    assert outcome["calls"]["mutations"] == 0


def test_a_refusal_for_an_older_plan_does_not_re_plan():
    """A newer plan is already in flight; asking again would only race it."""

    outcome = _run(
        'configPreviewPlanId = "plan:v1:old";'
        'handleSetupDevicePlanConflict({error: "stale_device_plan"});' + _report()
    )

    assert outcome["previewId"] is None
    assert outcome["exportAllowed"] is False
    assert outcome["calls"]["planRefreshes"] == 0


def test_the_refusal_message_never_reaches_the_page_unescaped():
    """The server message is shown as validation text, not as markup."""

    outcome = _run(
        'handleSetupDevicePlanConflict({error: "stale_device_plan", '
        'message: "<img src=x onerror=alert(1)>"});'
        "console.log(JSON.stringify({"
        "  message: latestConfigPreview.validation.errors[0].message,"
        "  ready: latestConfigPreview.ready,"
        "}));"
    )

    assert outcome["message"] == "<img src=x onerror=alert(1)>"
    assert outcome["ready"] is False


# --- the conflict set the browser recognizes ---------------------------------
def test_only_the_four_plan_conflicts_revoke_authority():
    outcome = _run(
        "console.log(JSON.stringify({"
        "  known: [...SETUP_DEVICE_PLAN_CONFLICT_ERRORS].sort(),"
        '  plan: isSetupDevicePlanConflict({error: "stale_device_plan"}),'
        '  other: isSetupDevicePlanConflict({error: "stale_setup_config"}),'
        "  empty: isSetupDevicePlanConflict(null),"
        "}));"
    )

    assert outcome["known"] == sorted(CONFLICT_CODES)
    assert outcome["plan"] is True
    assert outcome["other"] is False
    assert outcome["empty"] is False


# --- a newer plan drops the preview it was not issued for --------------------
def test_a_new_plan_drops_the_preview_authority_issued_for_the_old_one():
    """Pins the guard in ``refreshSetupPlan``, read from the shipped source."""

    source = _admin_js()
    body = _fragment(source, "async function refreshSetupPlan(", "\n// Auto-select")
    assert "if (configPreviewPlanId !== setupPlan.plan_id) renderConfigPreview();" in body

    # And ``renderConfigPreview`` is what revokes the exact preview authority.
    render = _fragment(source, "function renderConfigPreview(", "\nasync function requestConfigPreview(")
    assert "setSetupPreviewId(null);" in render
    assert "setConfigExportReady(false);" in render


def test_the_preview_request_routes_plan_conflicts_before_success():
    """A conflict response must never be read as a preview."""

    source = _admin_js()
    body = _fragment(
        source, "async function requestConfigPreview(", "\nfunction configExportBody("
    )
    conflict = body.index("isSetupDevicePlanConflict(data)")
    assert conflict < body.index("latestConfigPreview = data;")
    assert conflict < body.index("setSetupPreviewId(data.config_preview_id")
