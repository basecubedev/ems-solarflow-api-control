// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Admin workflow lifecycle (admin/workflow_lifecycle.py): who holds the
 * console and whether a stuck Guided Setup or Upgrade can be reset.
 * tests/classic-parity.test.js keeps these lists in step with Classic's
 * WORKFLOW_OWNER_LABELS, WORKFLOW_STATE_LABELS and WORKFLOW_STALE_REASON_LABELS.
 */

export const WORKFLOW_OWNERS = Object.freeze(["guided_setup", "guided_upgrade", "align_existing_install", "none", "conflict", "unknown"]);
export const WORKFLOW_STATES = Object.freeze(["idle", "active", "operation_running", "cleanup_pending", "review_required", "conflict", "malformed"]);
export const STALE_REASONS = Object.freeze([
  "unreadable_state",
  "unsupported_transition_mode",
  "unusable_upgrade_context",
  "workflow_owner_conflict",
  "setup_transition_context_mismatch",
  "setup_transition_owner_unproven",
]);

/** Refusals of a workflow switch that mean another workflow holds the console; Classic's WORKFLOW_BLOCKED_CODES. */
export const WORKFLOW_BLOCKED_CODES = Object.freeze(["workflow_operation_in_progress", "setup_operation_in_progress", "workflow_switch_blocked", "workflow_owner_unknown", "workflow_owner_conflict"]);

export const ownerKey = (owner) => `workflow.owner.${WORKFLOW_OWNERS.includes(owner) ? owner : "unknown"}`;
export const stateKey = (state) => (WORKFLOW_STATES.includes(state) ? `workflow.state.${state}` : "workflow.state.unknown");

const NATIVE = Object.freeze({
  guided_setup: { page: "setup", label: "workflow.showSetup" },
  guided_upgrade: { page: "updates", label: "workflow.showUpgrade" },
  align_existing_install: { page: "updates", label: "workflow.showAlignment" },
  conflict: { page: "system", label: "workflow.openRecovery" },
  unknown: { page: "system", label: "workflow.openRecovery" },
});

/**
 * A guided workflow that holds the console, with the page that continues it:
 * the Setup page for a Guided Setup, the Updates page for a Guided Upgrade or
 * a system alignment, and the System page's recovery for records no owner
 * can finish. Null when none holds it.
 */
export function activeWorkflow(lifecycle) {
  if (!lifecycle || lifecycle.ok !== true) return null;
  const owner = String(lifecycle.owner || "none");
  if (owner === "none" || lifecycle.state === "idle") return null;
  const known = WORKFLOW_OWNERS.includes(owner) ? owner : "unknown";
  const native = NATIVE[known] || NATIVE.unknown;
  return { owner, state: String(lifecycle.state || ""), native: native.page, label: native.label, recoverable: Boolean(lifecycle.recoverable) };
}

/** The stages of a system alignment this console names; another stage is "other". */
export const ALIGNMENT_STAGES = Object.freeze([
  "admin_update_pending",
  "admin_reconnect_pending",
  "admin_aligned",
  "resources_verified",
  "ems_operation_pending",
  "ems_operation_running",
  "healthcheck_pending",
  "failed_recoverable",
  "other",
]);

const CONTINUED = new Set(["admin_reconnect_pending", "admin_aligned", "ems_operation_running", "healthcheck_pending"]);
/** Where nothing on the server takes the alignment further: the console is released under System. */
const SETTLED = new Set(["resources_verified", "ems_operation_pending"]);

/**
 * A system alignment (``align_existing_install``) in progress, as going back
 * to the version that runs leaves one: where it stands, whether Continue (the
 * server's resume, as Classic's Resume sends it) moves it on, and whether only
 * a release of the console on the System page ends it. While an Admin
 * replacement still runs, the console answering is the one being replaced, so
 * nothing is offered.
 */
export function alignmentView(transition) {
  if (transition?.mode !== "align_existing_install" || !transition.operation_id) return null;
  if (!transition.stage || ["completed", "cancelled"].includes(transition.stage)) return null;
  const replacing = transition.replacement_active === true;
  const known = ALIGNMENT_STAGES.includes(transition.stage) ? transition.stage : "other";
  const stage = replacing && known === "admin_reconnect_pending" ? "admin_update_pending" : known;
  const failed = stage === "failed_recoverable";
  return {
    operationId: String(transition.operation_id),
    tag: transition.system_tag ?? null,
    stage,
    error: failed ? String(transition.error_message || "") : "",
    canContinue: !replacing && (failed ? transition.resume_available === true : CONTINUED.has(stage)),
    release: SETTLED.has(stage),
  };
}

/** Whether the recovery card has anything to say: a proven idle state with no reset on offer says nothing. */
export function recoveryWanted(plan) {
  if (!plan || plan.ok !== true) return true;
  if (String(plan.lifecycle?.state || "") !== "idle") return true;
  return Boolean(plan.safe?.available || plan.advanced?.available);
}

/**
 * The reason a recovery is recorded with: chosen by the state the operator
 * acted on, never typed, the way Classic records it.
 */
export function recoveryReason(plan) {
  const lifecycle = plan?.lifecycle || {};
  return `maintenance release: ${lifecycle.state || "unknown"}/${lifecycle.blocking_reason || "none"}`;
}

export function shortReference(value) {
  const text = typeof value === "string" ? value : "";
  if (!text) return "—";
  return text.length <= 12 ? text : `${text.slice(0, 12)}…`;
}

/** The reference that names a stuck workflow in a support bundle. */
export function workflowReference(lifecycle) {
  if (lifecycle?.transition?.operation_id) return { kind: "operation", id: shortReference(lifecycle.transition.operation_id) };
  if (lifecycle?.setup?.workflow_id) return { kind: "workflow", id: shortReference(lifecycle.setup.workflow_id) };
  return null;
}

export function workflowSince(lifecycle) {
  return lifecycle?.transition?.updated_at || lifecycle?.setup?.updated_at || null;
}

export function staleReasonKey(reason) {
  return STALE_REASONS.includes(reason) ? `workflow.stale.${reason}` : null;
}
