// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The appliance's operations as the owner follows them: plan, confirm, run,
 * settle. The agent owns every one of these states; this file only reads them
 * and decides which words and which buttons go with them.
 */

/**
 * Every operation type the agent runs. tests/test_appliance_modern.py walks
 * the TYPE_ constants of appliance/*.py against it, plus the two the agent
 * builds as f"system.{action}".
 */
export const OPERATION_TYPES = Object.freeze([
  "admin.install",
  "admin.rollback",
  "admin.repair",
  "admin.lifecycle",
  "updates.install",
  "updates.repair",
  "manager.update",
  "manager.revert",
  "network.wifi",
  "network.hostname",
  "system.timezone",
  "system.reboot",
  "system.shutdown",
  "ssh.service",
  "ssh.key_add",
  "ssh.key_remove",
  "ssh.revoke_all",
  "ssh.shell_access",
  "support.archive",
]);

/**
 * Every state an operation record can be in (appliance/operations.py STATE_*),
 * and its tone. A recoverable failure has stopped acting on the host and is
 * an outcome the owner may dismiss, retry or cancel.
 */
export const OPERATION_STATES = Object.freeze({
  planned: "c-idle",
  awaiting_confirmation: "c-acc",
  running: "c-acc",
  verifying: "c-acc",
  rolling_back: "c-warn",
  succeeded: "c-ok",
  rolled_back: "c-warn",
  manual_action_required: "c-warn",
  failed_recoverable: "c-fault",
  failed_terminal: "c-fault",
  cancelled: "c-idle",
});

/**
 * Cancel is a legal transition only out of a plan that has not started; a
 * running image write cannot be interrupted by a state flip.
 */
export const CANCELLABLE_STATES = Object.freeze(["planned", "awaiting_confirmation", "failed_recoverable"]);

/** Why a verified Admin was not one (admin_lifecycle verification failures). */
export const VERIFICATION_FAILURES = Object.freeze([
  "container_missing",
  "container_not_running",
  "container_still_running",
  "container_unhealthy",
  "image_mismatch",
  "api_unreachable",
  "start_timed_out",
  "version_unreadable",
  "version_mismatch",
]);

/** The two operations after which the appliance stops answering for a while. */
export const POWER_OPERATIONS = Object.freeze(["system.reboot", "system.shutdown"]);

const spelled = (value) => String(value || "").replace(/_/g, " ");

/** The operation the banner shows: the one running, else the newest the owner has not acknowledged. */
export function currentOperation(payload) {
  if (!payload) return null;
  return payload.active || (payload.unacknowledged || [])[0] || null;
}

export function operationTitleKey(operation) {
  const type = String((operation || {}).type || "");
  return OPERATION_TYPES.includes(type) ? `op.${type}` : null;
}

/** The executor's own stage, spelled out; empty when it only repeats the state the pill already says. */
export function operationStage(operation) {
  const stage = spelled((operation || {}).stage);
  return stage === spelled((operation || {}).state) ? "" : stage;
}

/** The state pill: a known state in the reader's language, an unknown one spelled out. */
export function stateOf(operation) {
  const state = String((operation || {}).state || "");
  if (OPERATION_STATES[state]) return { key: `opstate.${state}`, tone: OPERATION_STATES[state] };
  return { text: spelled(state), lang: "en", tone: operation && operation.settled ? "c-fault" : "c-warn" };
}

export function cancellable(operation) {
  return Boolean(operation) && CANCELLABLE_STATES.includes(operation.state);
}

export function acknowledgeable(operation) {
  return Boolean(operation) && Boolean(operation.settled) && !operation.acknowledged;
}

/** The archive lives in root-owned agent state; without this link it could be created and never retrieved. */
export function archiveHref(operation) {
  if (!operation || operation.type !== "support.archive" || operation.state !== "succeeded") return null;
  const id = String(operation.operation_id || "");
  return /^[0-9a-f]{32}$/.test(id) ? `/api/support/archive/${id}` : null;
}

/** The last steps the executor reported, oldest first. */
export function recentProgress(operation, limit = 6) {
  return ((operation && operation.progress) || []).slice(-limit).map((entry) => ({
    stage: spelled(entry.stage),
    detail: entry.detail ? String(entry.detail) : "",
    at: entry.at,
  }));
}

/** What the result says beyond "done": every part is optional, and an empty result says nothing. */
export function operationDetails(operation) {
  const result = (operation && operation.result) || {};
  const verification = result.verification && !result.verification.verified ? result.verification : null;
  const details = {
    untouched: result.admin_untouched === true,
    bootstrapFailed: result.bootstrap_failed === true,
    deploymentCreated: result.deployment_created === true,
    replan: result.replan_required === true,
    verification,
    unverified: result.unverified_actions || [],
    manual: (result.manual_actions || []).map(String),
    remaining: result.remaining_findings || [],
  };
  details.any = Boolean(
    details.untouched || details.bootstrapFailed || details.replan || verification
      || details.unverified.length || details.manual.length || details.remaining.length,
  );
  return details;
}

/**
 * One more settled record, or one that finished later than the last seen, is
 * the signal that the appliance changed. The list does not only grow: a
 * recoverable failure leaves it while it is retried.
 */
export function settledSince(payload, seen) {
  const records = (payload && payload.unacknowledged) || [];
  const finishedAt = records.reduce((latest, record) => Math.max(latest, Number(record.finished_at) || 0), 0);
  return {
    settled: records.length > seen.count || finishedAt > seen.finishedAt,
    count: records.length,
    finishedAt: Math.max(seen.finishedAt, finishedAt),
  };
}

/** A request id the agent hands a plan back against when its answer never reached the page. */
export function planRequestId(random = globalThis.crypto) {
  const bytes = new Uint8Array(16);
  random.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}
