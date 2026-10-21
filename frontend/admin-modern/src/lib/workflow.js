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

export const ownerKey = (owner) => `workflow.owner.${WORKFLOW_OWNERS.includes(owner) ? owner : "unknown"}`;
export const stateKey = (state) => (WORKFLOW_STATES.includes(state) ? `workflow.state.${state}` : "workflow.state.unknown");

/** A guided workflow that holds the console, with the Classic page that continues it; null when none does. */
export function activeWorkflow(lifecycle) {
  if (!lifecycle || lifecycle.ok !== true) return null;
  const owner = String(lifecycle.owner || "none");
  if (owner === "none" || lifecycle.state === "idle") return null;
  const classic = owner === "guided_setup" ? "setup" : "maintenance-upgrade";
  return { owner, state: String(lifecycle.state || ""), classic, recoverable: Boolean(lifecycle.recoverable) };
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
