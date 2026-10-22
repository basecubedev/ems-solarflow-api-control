// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Guided Setup as the Classic console runs it (admin/static/admin.js, the
 * setup wizard and the System Build step): its five steps, which of them are
 * open, and the System Build's state from selection to confirmation. The
 * server decides every step; these helpers only read its answers. Pure.
 */

import { STEP_ICONS, STEP_STATES } from "./backups.js";

export const SETUP_STEPS = Object.freeze(["release", "devices", "config", "deployment", "start"]);
export const SETUP_MODES = Object.freeze(["fresh_install", "automated_setup"]);

/** The states of the System Build step. */
export const BUILD_STATES = Object.freeze(["idle", "selected", "validating", "valid", "invalid", "confirming", "updating", "reconnecting", "failed"]);

/** A one-shot setup intent the server refused as spent or stale: the start gate has to confirm Fresh Setup again. */
export const STALE_INTENT = Object.freeze(["setup_intent_consumed", "setup_intent_expired", "setup_state_changed", "setup_intent_required"]);

/** The release channels the selector shows, in its order; another channel is never offered. */
export const CHANNEL_GROUPS = Object.freeze([
  { id: "latest", channels: ["latest"] },
  { id: "stable", channels: ["stable"] },
  { id: "unstable", channels: ["rc", "release_candidate"] },
  { id: "experimental", channels: ["development"] },
]);

export function channelGroup(channel) {
  return CHANNEL_GROUPS.find((group) => group.channels.includes(channel))?.id || null;
}

/** The catalogue grouped for the selector; a build in an unknown channel is dropped, never shown as stable. */
export function groupReleases(releases) {
  const list = Array.isArray(releases) ? releases : [];
  return CHANNEL_GROUPS.map((group) => ({ id: group.id, releases: list.filter((release) => group.channels.includes(release?.channel)) })).filter((group) => group.releases.length);
}

/**
 * The build the selector starts on: one a running setup already targets, the
 * catalogue's default, the prepared one, or the first that can be chosen.
 */
export function initialRelease(grouped, { restored, defaultTag, preparedTag } = {}) {
  const supported = grouped.flatMap((group) => group.releases);
  return (
    (restored && supported.find((release) => release.tag === restored)) ||
    supported.find((release) => release.tag === defaultTag) ||
    supported.find((release) => release.tag === preparedTag && release.selectable !== false) ||
    supported.find((release) => release.selectable !== false) ||
    null
  );
}

/** A transition a Fresh or Automated Setup still runs, which the wizard resumes on its build. */
export function setupTransitionActive(transition) {
  return Boolean(transition?.system_tag && SETUP_MODES.includes(transition.mode) && !["completed", "cancelled"].includes(transition.stage));
}

const CONFIRMED_STAGES = new Set(["resources_verified", "ems_operation_pending", "ems_operation_running", "healthcheck_pending", "completed"]);

/** The confirmed operation a running setup transition stands for, once its resources are verified. */
export function confirmedOperation(transition) {
  if (!setupTransitionActive(transition) || !transition.operation_id || !CONFIRMED_STAGES.has(transition.stage)) return null;
  return { operationId: String(transition.operation_id), systemTag: String(transition.system_tag) };
}

/** The server's answer of what the selected build allows now. */
export function actionState(result) {
  return result?.action_state && typeof result.action_state === "object" ? result.action_state : null;
}

/** Whether Verify may run: a freshly chosen build that can be chosen, with Fresh Setup confirmed. */
export function verifyAllowed(build, releases, intentNeeded) {
  if (build.state !== "selected" || !build.tag || intentNeeded) return false;
  const release = (releases || []).find((item) => item.tag === build.tag);
  return !release || release.selectable !== false;
}

export function updateAllowed(build, intentNeeded) {
  const action = actionState(build.result);
  return !intentNeeded && build.state === "valid" && Boolean(action) && action.admin_update_allowed === true && action.continue_allowed !== true;
}

export function continueAllowed(build, intentNeeded) {
  const action = actionState(build.result);
  return !intentNeeded && build.state === "valid" && Boolean(action) && action.continue_allowed === true && action.admin_update_allowed !== true;
}

export function buildBusy(build) {
  return build.locked || ["confirming", "updating", "reconnecting"].includes(build.state);
}

/**
 * The key of the sentence the System Build step says, from its state and the
 * server's answer, in Classic's order of precedence.
 */
export function buildMessage(build) {
  const result = build.result;
  const action = actionState(result);
  switch (build.state) {
    case "updating":
      return "updating";
    case "reconnecting":
      return "reconnecting";
    case "selected":
      return "selected";
    case "confirming":
      return "confirming";
    case "validating":
      return "validating";
    case "failed":
      return build.failedAction === "validate" ? "failedValidate" : build.failedAction === "confirm" ? "failedConfirm" : "failedUpdate";
    default:
      break;
  }
  if (action?.busy === true) return "busy";
  if (action?.terminal_error) return "terminal";
  if (result?.alignment) {
    if (result.recovery_required) return "recovery";
    if (result.transition_in_progress && !result.next_allowed) return "inProgress";
    if (result.alignment === "aligned") return result.resource_strategy === "release_archive" ? "legacyReady" : "aligned";
    return "updateRequired";
  }
  return build.tag ? "" : "choose";
}

/**
 * The two actions of the System Build step, as Classic offers them: the left
 * one updates the Admin console or retries what failed before it; the right
 * one verifies a chosen build, continues once it is ready, or retries a
 * failed confirmation. Exactly one is the way on.
 */
export function buildActions(build, releases, intentNeeded) {
  const busy = buildBusy(build);
  const failed = build.state === "failed";
  const aligning = ["updating", "reconnecting"].includes(build.state);
  let left = { label: "update", enabled: !busy && !failed && updateAllowed(build, intentNeeded) };
  if (aligning) left = { label: build.state === "reconnecting" ? "reconnecting" : "updatingAdmin", enabled: false };
  else if (failed) {
    if (build.failedAction === "validate") left = { label: "checkAgain", enabled: true };
    else if (build.failedAction === "confirm") left = { label: "update", enabled: false };
    else left = { label: "tryAgain", enabled: true };
  }
  let right = { label: "continue", enabled: !busy && !failed && continueAllowed(build, intentNeeded) };
  if (!busy && !failed && build.state === "selected") right = { label: "verify", enabled: verifyAllowed(build, releases, intentNeeded) };
  else if (!aligning && failed && build.failedAction === "confirm") right = { label: "tryAgain", enabled: true };
  return { left, right };
}

/** A development build tag: dev-<branch>-<revision>-<run>-<attempt>. */
export function isDevelopmentTag(tag) {
  const value = typeof tag === "string" ? tag.trim() : "";
  return value.length <= 128 && /^dev-[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?-[0-9a-f]{7,40}-[1-9][0-9]*-[1-9][0-9]*$/.test(value);
}

/**
 * Which steps are open. Devices and Config need the confirmed build of a
 * running setup operation; Deployment also a saved config; Start a prepared
 * deployment.
 */
export function stepLocked(step, { confirmed, generatedReady, deploymentReady }) {
  if (step === "devices" || step === "config") return !confirmed;
  if (step === "deployment") return !confirmed || !generatedReady;
  if (step === "start") return !confirmed || !generatedReady || !deploymentReady;
  return false;
}

/** The seven stages a System Build passes through, as Classic's pipeline shows them. */
export const PIPELINE = Object.freeze(["select", "validate", "alignAdmin", "reconnect", "verifyResources", "installEms", "verifySystem"]);

const STAGE_INDEX = { selection_started: 0, validation_running: 1, validation_failed: 1, validated: 1, admin_update_pending: 2, admin_alignment_started: 2, admin_reconnect_pending: 3, admin_aligned: 4, resources_verified: 5, ems_operation_pending: 5, ems_operation_running: 5, healthcheck_pending: 6, completed: 6 };

/** The stage an answer names: the persisted transition's first. */
export function pipelineStage(data) {
  const payload = data && typeof data === "object" ? data : {};
  const transition = payload.transition && typeof payload.transition === "object" ? payload.transition : {};
  return transition.stage || payload.transition_stage || payload.stage || payload.status || null;
}

function adminRequired(payload, transition, stage) {
  if (typeof transition.admin_alignment_required === "boolean") return transition.admin_alignment_required;
  if (typeof payload.admin_alignment_required === "boolean") return payload.admin_alignment_required;
  if (typeof payload.admin_update_required === "boolean") return payload.admin_update_required;
  if (payload.alignment === "aligned") return false;
  if (["admin_update_pending", "admin_alignment_started", "admin_reconnect_pending"].includes(stage)) return true;
  return null;
}

/** Each pipeline stage as pending, active, done, skipped or failed, for an alignment status or validation answer. */
export function pipelineStates(data) {
  const payload = data && typeof data === "object" ? data : {};
  const transition = payload.transition && typeof payload.transition === "object" ? payload.transition : {};
  const stage = pipelineStage(payload);
  const failed = stage === "failed_recoverable";
  const effective = failed ? transition.resume_stage || stage : stage;
  const required = adminRequired(payload, transition, effective);
  const states = Array(PIPELINE.length).fill("pending");
  const through = (last, active) => {
    for (let index = 0; index <= last; index += 1) states[index] = "done";
    if (required === false && last >= 3) states[2] = states[3] = "skipped";
    if (active !== undefined) states[active] = "active";
  };
  switch (effective) {
    case "selection_started":
      states[0] = "active";
      break;
    case "validation_running":
      through(0, 1);
      break;
    case "validation_failed":
      through(0);
      states[1] = "failed";
      break;
    case "validated":
      through(1);
      if (required === false) states[2] = states[3] = "skipped";
      break;
    case "admin_update_pending":
    case "admin_alignment_started":
      through(1, 2);
      break;
    case "admin_reconnect_pending":
      through(2, 3);
      break;
    case "admin_aligned":
      through(3, 4);
      break;
    case "resources_verified":
    case "ems_operation_pending":
    case "ems_operation_running":
      through(4, 5);
      break;
    case "healthcheck_pending":
      through(5, 6);
      break;
    case "completed":
      through(6);
      break;
    default:
      if (typeof STAGE_INDEX[effective] === "number") through(STAGE_INDEX[effective] - 1, STAGE_INDEX[effective]);
  }
  if (failed) {
    const active = states.indexOf("active");
    if (active >= 0) states[active] = "failed";
  }
  return { stage, adminRequired: required, states };
}

/** Docker's state as the deployment plan reports it, and how Docker is reached. */
export const DOCKER_STATES = Object.freeze(["ready", "socket_missing", "client_missing", "permission_denied", "daemon_unreachable", "unavailable"]);
export const DOCKER_MODES = Object.freeze(["deployment_controller", "discovery_only"]);
const SERVICE_NAMES = { influxdb: "influxdb", ems: "ems", "ems-solarflow-admin": "admin" };

/** A preparation or start job's steps, for the step list; the labels are the server's. */
export function jobSteps(steps) {
  return (Array.isArray(steps) ? steps : []).map((step) => {
    const state = Object.hasOwn(STEP_STATES, step?.status) ? step.status : "pending";
    return { text: String(step?.label || ""), state, tone: STEP_STATES[state], icon: STEP_ICONS[state] };
  });
}

/** The words for a service the deployment runs, or null for one this console does not name. */
export function serviceKey(service) {
  return SERVICE_NAMES[service] || null;
}

/** The service a blocking container belongs to, by its name. */
export function conflictService(conflict) {
  return String(conflict?.container_name || "").toLowerCase().includes("influx") ? "influxdb" : "ems";
}

/** What the deployment step says now, in Classic's order. */
export function deploymentMessage(deploy, { tag, generatedReady }) {
  if (deploy.status === "running") return deploy.phase ? { raw: deploy.phase } : { key: "preparing" };
  if (deploy.ready) return { key: "ready" };
  if (deploy.status === "failed") return { key: "failed" };
  if (!tag) return { key: "noRelease" };
  if (!generatedReady) return { key: "noConfig" };
  if (!deploy.dockerReady) return deploy.docker?.message ? { raw: deploy.docker.message } : { key: "noDocker" };
  return { key: "auto" };
}

/** What the start step says now, in Classic's order. */
export function startMessage(start, { prepared, dockerReady, docker }) {
  if (start.status === "running") return start.phase ? { raw: start.phase } : { key: "starting" };
  if (start.conflict) return { key: "conflict" };
  if (start.status === "failed") return { key: "failed" };
  if (start.running) return { key: "running" };
  if (!prepared) return { key: "notPrepared" };
  if (!dockerReady) return docker?.message ? { raw: docker.message } : { key: "noDocker" };
  return { key: "ready" };
}

/** Why a stuck setup transition needs the reader, in Classic's order; the server's own error is shown as it is. */
export const RECOVERY_REASONS = Object.freeze(["workerUnknown", "expiredRunning", "running", "expired", "replacementGone", "partial"]);

/**
 * The recovery a setup transition offers once it failed, expired or lost its
 * Admin replacement: resume it when the server allows, or discard the setup
 * once its worker is proven stopped. Null when it needs none.
 */
export function setupRecovery(transition, payload = {}) {
  if (!transition || !SETUP_MODES.includes(transition.mode)) return null;
  const expired = transition.expired === true;
  const gone = transition.replacement_active === false;
  if (transition.stage !== "failed_recoverable" && !expired && !gone) return null;
  const running = transition.worker_active === true;
  const error = String(transition.error_message || payload?.error_message || payload?.message || "");
  let reason = null;
  if (transition.worker_status_available === false) reason = "workerUnknown";
  else if (running) reason = expired ? "expiredRunning" : "running";
  else if (!error) reason = expired ? "expired" : gone ? "replacementGone" : "partial";
  return {
    reason,
    error: reason ? "" : error,
    canResume: Boolean(transition.operation_id) && transition.resume_available === true,
    canDiscard: Boolean(transition.operation_id) && transition.cancel_available === true && transition.worker_active === false && transition.worker_status_available === true,
  };
}
