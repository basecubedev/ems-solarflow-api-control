// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The appliance's operations as the owner follows them: plan, confirm, run,
 * settle. The agent owns every one of these states; this file only reads them
 * and decides which words and which buttons go with them.
 */
import { REFRESH_INDEX_ACTION } from "./plans.js";

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
  "ssh.backup_access",
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
export const CANCELLABLE_STATES = Object.freeze(["planned", "awaiting_confirmation"]);

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

/**
 * Installing or reverting the Manager package: the record ends once the
 * install unit is started, and the agent and web service restart a little
 * later, while dpkg unpacks it. Whether the install stands is the deadline's
 * verdict, minutes after that.
 */
export const MANAGER_OPERATIONS = Object.freeze(["manager.update", "manager.revert"]);

/**
 * Whether the page waiting on a Manager install may stop: "back" once the web
 * service has restarted, "unchanged" when nothing was installed, else
 * "waiting". The old service keeps answering until the new package restarts
 * it, so an answer alone proves nothing -- a new version does, a session it no
 * longer knows does, and so does a gap between two answers. Confirming
 * returns before the operation runs: one that then fails installs nothing,
 * and until it has reached the installer the installer's outcome is the last
 * install's, not this one's.
 */
export function managerReconnectState({ before = "", outage = false, session = null, manager = null, operation = null } = {}) {
  if (outage) return "back";
  if (!session) return "waiting";
  if (session.authenticated === false) return "back";
  if (before && session.appliance_version && session.appliance_version !== before) return "back";
  if (!operation) return "waiting";
  if (operation.settled && operation.state !== "succeeded") return "unchanged";
  if (operation.state !== "succeeded") return "waiting";
  // Sessions live in the web service's memory: an outcome answered by a service
  // that still knows this session, at the same version, was reached without
  // a restart -- an install that did not start, or a dpkg run that failed
  // before its postinst.
  const outcome = manager && manager.outcome && manager.outcome.outcome;
  return outcome && outcome !== "pending" ? "unchanged" : "waiting";
}

/** The record of one operation in an /api/operations answer, wherever it is listed. */
export function findOperation(payload, operationId) {
  if (!payload || !operationId) return null;
  const records = [payload.active, ...(payload.unacknowledged || []), ...(payload.recent || [])];
  return records.find((record) => record && record.operation_id === operationId) || null;
}

const spelled = (value) => String(value || "").replace(/_/g, " ");

/** The operation the banner shows: the one running, else the newest the owner has not acknowledged. */
export function currentOperation(payload) {
  if (!payload) return null;
  return payload.active || (payload.unacknowledged || [])[0] || null;
}

/** A refresh runs as one of the package manager's repairs, but nothing about it is a repair. */
const ACTION_TITLES = Object.freeze({
  "updates.repair": Object.freeze({ [REFRESH_INDEX_ACTION]: "op.updates.refresh_index" }),
  "admin.lifecycle": Object.freeze({ start: "op.admin.lifecycle.start", stop: "op.admin.lifecycle.stop", restart: "op.admin.lifecycle.restart" }),
});

/** A switch says which way it goes; its target names it by "enabled" (appliance/ssh_service.py). */
const SWITCH_TITLES = Object.freeze({
  "ssh.service": Object.freeze({ on: "op.ssh.service.on", off: "op.ssh.service.off" }),
  "ssh.shell_access": Object.freeze({ on: "op.ssh.shell_access.on", off: "op.ssh.shell_access.off" }),
  "ssh.backup_access": Object.freeze({ on: "op.ssh.backup_access.on", off: "op.ssh.backup_access.off" }),
});

export function operationTitleKey(operation, action = ((operation || {}).requested_target || {}).action) {
  const type = String((operation || {}).type || "");
  if (!OPERATION_TYPES.includes(type)) return null;
  const enabled = ((operation || {}).requested_target || {}).enabled;
  if (SWITCH_TITLES[type] && typeof enabled === "boolean") return SWITCH_TITLES[type][enabled ? "on" : "off"];
  return (ACTION_TITLES[type] || {})[action] || `op.${type}`;
}

/** The executor's own stage, spelled out; empty when it only repeats the state the pill already says. */
export function operationStage(operation) {
  const stage = spelled((operation || {}).stage);
  return stage === spelled((operation || {}).state) ? "" : stage;
}

/** The state pill: a known state in the reader's language, an unknown one spelled out. */
export function stateOf(operation) {
  const state = String((operation || {}).state || "");
  if (state === "succeeded" && MANAGER_OPERATIONS.includes(String((operation || {}).type || ""))) {
    return { key: "op.managerStarted", tone: "c-idle" };
  }
  if (OPERATION_STATES[state]) return { key: `opstate.${state}`, tone: OPERATION_STATES[state] };
  return { text: spelled(state), lang: "en", tone: operation && operation.settled ? "c-fault" : "c-warn" };
}

export function cancellable(operation) {
  return Boolean(operation) && CANCELLABLE_STATES.includes(operation.state);
}

export function acknowledgeable(operation) {
  return Boolean(operation) && Boolean(operation.settled) && !operation.acknowledged;
}

/** Where an operation that did not finish is taken up again, by its family. */
const NEXT_STEPS = Object.freeze({
  "updates.install": Object.freeze({ key: "op.next.updates", page: "updates" }),
  "updates.repair": Object.freeze({ key: "op.next.updates", page: "updates" }),
  "manager.update": Object.freeze({ key: "op.next.manager", page: "updates" }),
  "manager.revert": Object.freeze({ key: "op.next.manager", page: "updates" }),
  "admin.install": Object.freeze({ key: "op.next.admin", page: "admin" }),
  "admin.rollback": Object.freeze({ key: "op.next.admin", page: "admin" }),
  "admin.repair": Object.freeze({ key: "op.next.admin", page: "admin" }),
  "admin.lifecycle": Object.freeze({ key: "op.next.admin", page: "admin" }),
  "network.wifi": Object.freeze({ key: "op.next.network", page: "network" }),
  "network.hostname": Object.freeze({ key: "op.next.network", page: "network" }),
});
const FALLBACK_STEP = Object.freeze({ key: "op.next.diagnostics", page: "diagnostics" });
const UNFINISHED = Object.freeze(["failed_recoverable", "failed_terminal", "manual_action_required"]);

/** What to do about an operation that did not finish, and on which page; null for any other. */
export function nextStep(operation) {
  if (!operation || !UNFINISHED.includes(operation.state)) return null;
  return NEXT_STEPS[String(operation.type || "")] || FALLBACK_STEP;
}

/** How many support archives the agent keeps, counted by record (appliance/support_archive.py). */
export const KEPT_ARCHIVES = 3;

/**
 * The support archives still kept, newest first, from an operations listing:
 * the agent keeps the files of its newest archive records, failed ones
 * included, so only those of them that succeeded can still be fetched.
 */
export function keptArchives(payload) {
  const seen = new Set();
  return [...((payload && payload.recent) || []), ...((payload && payload.unacknowledged) || [])]
    .filter((item) => item && item.type === "support.archive" && !seen.has(item.operation_id) && seen.add(item.operation_id))
    .sort((left, right) => (Number(right.started_at) || 0) - (Number(left.started_at) || 0))
    .slice(0, KEPT_ARCHIVES)
    .map((item) => ({ id: item.operation_id, href: archiveHref(item), at: Number(item.finished_at) || 0 }))
    .filter((item) => item.href);
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
