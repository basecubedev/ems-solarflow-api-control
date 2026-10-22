// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminGet, adminSend, loadAuth, replacingPage } from "./auth.svelte.js";
import { ApiError } from "../../../shared/lib/api.js";
import { reload, system } from "./system.svelte.js";
import { STALE_INTENT, SETUP_STEPS, actionState, buildBusy, pipelineStage, confirmedOperation, continueAllowed, groupReleases, initialRelease, setupTransitionActive, stepLocked, updateAllowed, verifyAllowed } from "../lib/setup.js";

/** Shared with Classic, so a setup started in one console continues in the other. */
const WORKFLOW_KEY = "ems-admin-setup-workflow";
const STEP_KEY = "ems-admin-setup-step";
const WORKFLOW_CONFLICTS = new Set(["setup_workflow_required", "setup_workflow_not_active", "setup_intent_workflow_mismatch", "setup_transition_owner_unproven", "setup_transition_context_mismatch"]);
const POLL_MS = 1500;
const RECONNECT_LIMIT_MS = 15 * 60 * 1000;

function readJson(key) {
  try {
    return JSON.parse(globalThis.localStorage?.getItem(key) || "null");
  } catch {
    return null;
  }
}

function writeJson(key, value) {
  try {
    if (value === null || value === undefined) globalThis.localStorage?.removeItem(key);
    else globalThis.localStorage?.setItem(key, typeof value === "string" ? value : JSON.stringify(value));
  } catch {
    return;
  }
}

function storedWorkflow() {
  const value = readJson(WORKFLOW_KEY);
  return value && typeof value.workflow_id === "string" && value.workflow_id ? { workflowId: value.workflow_id, previewId: typeof value.preview_id === "string" && value.preview_id ? value.preview_id : null } : { workflowId: null, previewId: null };
}

function storedStep() {
  try {
    const step = globalThis.localStorage?.getItem(STEP_KEY);
    return SETUP_STEPS.includes(step) ? step : "release";
  } catch {
    return "release";
  }
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * What an error shows the reader: the server's answer when there was one, a
 * request that got none as such, and nothing for this page's own checks,
 * whose step message already says what failed.
 */
function shown(error) {
  if (error instanceof ApiError) return error;
  if (error?.payload && typeof error.payload === "object" && Object.keys(error.payload).length) return error.payload;
  return error instanceof TypeError ? error : null;
}

/**
 * Guided Setup. The server owns it: the workflow identity it issued, the
 * one-shot intent that authorizes the first mutation, the transition the
 * confirmed build created. This console keeps them only to name them back.
 */
class Setup {
  workflowId = $state(storedWorkflow().workflowId);
  previewId = $state(storedWorkflow().previewId);
  intentId = $state(null);
  intentNeeded = $state(false);
  stale = $state(false);
  cleanup = $state.raw(null);
  step = $state("release");
  starting = $state(false);
  startError = $state.raw(null);
  confirmReplace = $state(false);
  restarting = $state(false);
  restartError = $state.raw(null);
  recovering = $state("");
  recoveryError = $state.raw(null);
  releases = $state.raw([]);
  warnings = $state.raw([]);
  releasesLoading = $state(false);
  releasesError = $state.raw(null);
  template = $state.raw(null);
  operation = $state.raw(null);
  generatedReady = $state(false);
  deploymentReady = $state(false);
  build = $state.raw({ tag: null, state: "idle", result: null, error: null, failedAction: null, locked: false });
  generation = 0;
  validation = 0;

  confirmed = $derived(Boolean(this.operation?.operationId) && this.operation.systemTag === this.build.tag && this.template?.tag === this.build.tag);
}

export const setup = new Setup();

function saveWorkflow() {
  writeJson(WORKFLOW_KEY, setup.workflowId ? { workflow_id: setup.workflowId, preview_id: setup.previewId } : null);
}

export function setWorkflow(workflowId) {
  setup.workflowId = typeof workflowId === "string" && workflowId ? workflowId : null;
  if (!setup.workflowId) setup.previewId = null;
  setup.stale = false;
  saveWorkflow();
}

export function setPreviewId(previewId) {
  setup.previewId = previewId || null;
  saveWorkflow();
}

function patchBuild(change) {
  setup.build = { ...setup.build, ...change };
}

function intentHeaders() {
  return setup.intentId ? { "X-Setup-Intent-ID": setup.intentId } : {};
}

/** Whether a step can be opened now. */
export function locked(step) {
  return stepLocked(step, { confirmed: setup.confirmed, generatedReady: setup.generatedReady, deploymentReady: setup.deploymentReady });
}

export function goTo(step) {
  const next = SETUP_STEPS.includes(step) && !locked(step) ? step : "release";
  setup.step = next;
  try {
    globalThis.localStorage?.setItem(STEP_KEY, next);
  } catch {
    return;
  }
}

/** The server's record of the setup workflow, or null. */
export async function workflowSnapshot() {
  try {
    const data = await adminGet("/api/setup/workflow");
    return typeof data?.workflow?.workflow_id === "string" ? data.workflow : null;
  } catch {
    return null;
  }
}

function cleanupBlocks(workflow) {
  return workflow?.cleanup?.blocking === true;
}

/**
 * After a reload or an Admin restart the server's record is the workflow:
 * adopt the active one, drop an identity the server no longer has, and keep
 * a finished one whose cleanup is still pending, because only it can retry.
 */
export async function restoreWorkflow() {
  const workflow = await workflowSnapshot();
  if (workflow && cleanupBlocks(workflow)) {
    setup.workflowId = workflow.workflow_id;
    setup.previewId = null;
    saveWorkflow();
    setup.cleanup = workflow.cleanup;
    return;
  }
  setup.cleanup = null;
  const current = workflow?.status === "active" ? workflow.workflow_id : null;
  if (current !== setup.workflowId) setWorkflow(current);
}

/** A request the server refused for this tab's workflow: stop acting and let the reader rejoin or discard. */
export function workflowRefused(error) {
  const code = error?.payload?.error || error?.error;
  if (!WORKFLOW_CONFLICTS.has(code)) return false;
  setup.stale = true;
  setup.intentId = null;
  setPreviewId(null);
  return true;
}

function intentRefused(error) {
  const code = error?.payload?.error;
  if (!STALE_INTENT.includes(code)) return false;
  setup.intentId = null;
  setup.operation = null;
  setup.intentNeeded = true;
  patchBuild({ state: "idle", error: shown(error), locked: false });
  goTo("release");
  return true;
}

/** Rejoins the server's current workflow, or drops the identity when there is none. */
export async function rejoin() {
  const workflow = await workflowSnapshot();
  if (workflow && cleanupBlocks(workflow)) {
    setup.workflowId = workflow.workflow_id;
    saveWorkflow();
    setup.cleanup = workflow.cleanup;
    return;
  }
  setWorkflow(workflow?.status === "active" ? workflow.workflow_id : null);
}

/**
 * Opens Guided Setup from the start gate: a switch away from another guided
 * workflow when one holds the console, otherwise the start path, which may
 * ask once more over an existing installation.
 */
export async function startSetup({ confirm = false, lifecycle = null } = {}) {
  if (setup.starting) return null;
  setup.starting = true;
  setup.startError = null;
  setup.intentId = null;
  setup.intentNeeded = false;
  try {
    if (lifecycle) {
      const result = await adminSend("/api/admin/workflow-lifecycle/switch", { body: { target: "guided_setup", confirm: true, fingerprint: lifecycle.fingerprint } });
      if (result?.ok !== true || !result.setup_workflow_id || !result.setup_intent_id) throw Object.assign(new Error("not recorded"), { payload: result || {} });
      setup.intentId = result.setup_intent_id;
      setWorkflow(result.setup_workflow_id);
      return "setup";
    }
    const result = await startPath(confirm);
    if (result?.requires_confirmation) {
      setup.confirmReplace = true;
      return "confirm";
    }
    if (result?.ok && result.route === "setup" && result.setup_intent_id && result.setup_workflow_id) {
      setup.confirmReplace = false;
      setup.intentId = result.setup_intent_id;
      setWorkflow(result.setup_workflow_id);
      return "setup";
    }
    throw Object.assign(new Error("not started"), { payload: result || {} });
  } catch (error) {
    setup.startError = shown(error) || { error: "setup_not_started" };
    return null;
  } finally {
    setup.starting = false;
  }
}

/** The start path's answer, a refusal that asks for a confirmation included: the server sends that one as 409. */
async function startPath(confirm) {
  try {
    return await adminSend("/api/admin/start-path", { body: { choice: "setup_new", confirm } });
  } catch (error) {
    if (error?.payload?.requires_confirmation === true) return error.payload;
    throw error;
  }
}

/** The lifecycle's answer to a switch to Guided Setup: blocked, or a plan to confirm. */
export async function switchPlan() {
  try {
    return await adminSend("/api/admin/workflow-lifecycle/switch/preview", { body: { target: "guided_setup" } });
  } catch (error) {
    return { ok: false, ...(error?.payload || {}), failure: error };
  }
}

async function loadTemplate(tag) {
  const data = await adminGet("/api/setup/config-template");
  if (!data || data.tag !== tag || !data.template || typeof data.template !== "object" || Array.isArray(data.template)) throw new Error("template");
  setup.template = { tag: data.tag, template: data.template, dockerImage: data.docker_image || null };
}

/** The System Build catalogue, starting on the build a running setup targets. */
export async function loadReleases(restored = null) {
  setup.releasesLoading = true;
  setup.releasesError = null;
  try {
    const data = await adminGet("/api/setup/releases");
    setup.releases = Array.isArray(data?.releases) ? data.releases : [];
    setup.warnings = Array.isArray(data?.warnings) ? data.warnings : [];
    const chosen = initialRelease(groupReleases(setup.releases), { restored: restored || setup.build.tag, defaultTag: data?.default_release, preparedTag: data?.prepared_release });
    if (!chosen) throw new Error("none");
    if (restored) {
      patchBuild({ tag: chosen.tag });
      await validate({ tag: chosen.tag });
    } else if (setup.build.tag !== chosen.tag) {
      present(chosen.tag);
    } else if (!buildBusy(setup.build) && setup.build.state !== "validating") {
      setup.validation += 1;
      patchBuild({ state: "selected", result: null, error: null, failedAction: null });
    }
  } catch (error) {
    setup.releasesError = error;
  } finally {
    setup.releasesLoading = false;
  }
}

function present(tag) {
  setup.validation += 1;
  setup.template = null;
  setup.operation = null;
  patchBuild({ tag: tag || null, state: tag ? "selected" : "idle", result: null, error: null, failedAction: null });
}

/**
 * Another build chosen: side-effect free. A setup transition for a different
 * build is retired first, which issues a new workflow and intent.
 */
export async function choose(tag) {
  const previous = setup.build.tag;
  present(tag);
  const release = setup.releases.find((item) => item.tag === tag);
  if (release && release.selectable === false) {
    patchBuild({ state: "invalid", error: release.reason ? { message: release.reason } : null });
    return;
  }
  if (!tag || tag === previous || !setup.workflowId) return;
  try {
    const status = await adminGet("/api/admin/system-alignment/status");
    const transition = status?.transition;
    if (!transition || transition.system_tag === tag || ["completed", "cancelled"].includes(transition.stage) || !["fresh_install", "automated_setup"].includes(transition.mode)) return;
    const data = await adminSend("/api/setup/system-build/supersede", { body: { setup_workflow_id: setup.workflowId, tag } });
    if (data?.ok !== true || !data.setup_workflow_id) throw Object.assign(new Error("supersede"), { payload: data || {} });
    setup.operation = null;
    setup.intentId = data.setup_intent_id || null;
    setup.intentNeeded = false;
    setWorkflow(data.setup_workflow_id);
    reload("alignment");
  } catch (error) {
    if (workflowRefused(error)) return;
    patchBuild({ state: "failed", failedAction: "validate", error: shown(error) });
  }
}

/**
 * Downloads and verifies the selected build; the server's action state then
 * says whether the Admin console needs an update or setup may continue. A
 * newer selection or validation makes an older answer void.
 */
export async function validate({ tag = setup.build.tag, internal = false } = {}) {
  if (!tag) return;
  const generation = ++setup.validation;
  patchBuild({ tag, state: "validating", result: null, error: null, ...(internal ? {} : { failedAction: null }) });
  try {
    const data = await adminSend("/api/admin/system-alignment/validate", { body: { tag } });
    if (generation !== setup.validation || tag !== setup.build.tag) return;
    if (data?.action_state?.terminal_error) {
      settleTerminal(data, internal);
      return;
    }
    if (!data?.action_state || typeof data.action_state !== "object" || data.valid === false) throw Object.assign(new Error("validation"), { payload: data || {}, invalid: data?.valid === false });
    if (data.action_state.selected_build?.tag !== data.system_build?.canonical_tag) throw new Error("mismatch");
    patchBuild({ result: data, state: "valid", failedAction: null });
    reload("alignment");
    if (data.action_state.busy === true && data.action_state.transition_stage === "admin_aligned" && data.action_state.operation_id) await resumeResources(data.action_state.operation_id, tag, generation);
  } catch (error) {
    if (generation !== setup.validation) return;
    if (error?.payload?.action_state?.terminal_error) {
      settleTerminal(error.payload, internal);
      return;
    }
    const transient = !error?.invalid && !(error?.status >= 400 && error?.status < 500);
    patchBuild({ state: transient ? "failed" : "invalid", error: shown(error), ...(!internal && transient ? { failedAction: "validate" } : {}) });
  }
}

/** A verdict that ends this build here; a registry rate limit is the one that may be tried again. */
function settleTerminal(data, internal) {
  const error = data.action_state.terminal_error;
  const limited = error.code === "system_build_registry_rate_limited";
  patchBuild({ result: data, state: limited ? "failed" : "invalid", error, ...(limited && !internal ? { failedAction: "validate" } : {}) });
}

async function resumeResources(operationId, tag, generation) {
  patchBuild({ locked: true });
  try {
    await adminSend("/api/admin/system-alignment/resume", { body: { operation_id: operationId, tag } });
    if (generation !== setup.validation || tag !== setup.build.tag) return;
    patchBuild({ locked: false });
    await validate({ tag, internal: true });
  } catch (error) {
    if (generation !== setup.validation || tag !== setup.build.tag) return;
    if (error?.payload?.error === "resource_verification_in_progress") return;
    patchBuild({ state: "failed", failedAction: "confirm", error: shown(error) });
  } finally {
    patchBuild({ locked: false });
  }
}

/** Confirms the verified build: the server creates the setup transition and the template of that build loads. */
export async function confirmBuild() {
  const tag = setup.build.tag;
  if (!tag || !continueAllowed(setup.build, setup.intentNeeded) || setup.build.locked) return;
  const result = setup.build.result || {};
  if (result.resources_verified === true && result.confirmation_allowed === false && result.operation_id) {
    setup.operation = { operationId: String(result.operation_id), systemTag: tag };
    try {
      await loadTemplate(tag);
      goTo("devices");
    } catch (error) {
      patchBuild({ state: "failed", failedAction: "confirm", error: shown(error) });
    }
    return;
  }
  if (result.confirmation_allowed !== true) return;
  patchBuild({ locked: true, state: "confirming", failedAction: null, error: null });
  try {
    const data = await adminSend("/api/setup/system-build/confirm", { body: { tag, setup_workflow_id: setup.workflowId }, headers: intentHeaders() });
    if (tag !== setup.build.tag) throw new Error("changed");
    if (data?.resources_verified !== true) throw Object.assign(new Error("confirm"), { payload: data || {} });
    const confirmedTag = data.system_build?.canonical_tag || data.system_tag || tag;
    if (confirmedTag !== tag || !data.operation_id) throw new Error("context");
    setup.intentId = null;
    setup.operation = { operationId: String(data.operation_id), systemTag: confirmedTag };
    await loadTemplate(confirmedTag);
    patchBuild({ result: { ...setup.build.result, ...data }, state: "valid", failedAction: null, locked: false });
    reload("alignment", "lifecycle");
    goTo("devices");
  } catch (error) {
    if (workflowRefused(error) || intentRefused(error)) {
      patchBuild({ locked: false, state: "idle" });
      return;
    }
    const terminal = Boolean(error?.payload?.action_state?.terminal_error);
    patchBuild({ locked: false, state: terminal ? "invalid" : "failed", failedAction: terminal ? null : "confirm", error: shown(error), ...(terminal ? { result: error.payload } : {}) });
    goTo("release");
  }
}

/**
 * Updates the Admin console to the selected build. When the server replaces
 * it, this page waits for the new one and reloads from it; the setup then
 * resumes from the server's transition.
 */
export async function updateAdmin() {
  const tag = setup.build.tag;
  if (!tag || setup.build.locked || ["updating", "reconnecting"].includes(setup.build.state)) return;
  patchBuild({ locked: true, failedAction: null, error: null });
  await validate({ tag, internal: true });
  if (tag !== setup.build.tag || !updateAllowed(setup.build, setup.intentNeeded)) {
    patchBuild({ locked: false, ...(setup.build.state === "failed" ? { failedAction: "align" } : {}) });
    return;
  }
  patchBuild({ state: "updating" });
  try {
    const data = await adminSend("/api/setup/system-build/update-admin", { body: { tag, setup_workflow_id: setup.workflowId }, headers: intentHeaders() });
    setup.intentId = null;
    if (data?.reconnect || data?.status === "admin_alignment_started") {
      if (!(data.operation_id || data.transition?.operation_id)) throw new Error("operation");
      patchBuild({ state: "reconnecting" });
      if (await awaitReplacement()) return;
      patchBuild({ locked: false, state: "failed", failedAction: "align", error: null });
      return;
    }
    patchBuild({ locked: false });
    await validate({ tag, internal: true });
  } catch (error) {
    if (workflowRefused(error) || intentRefused(error)) {
      patchBuild({ locked: false, state: "idle" });
      return;
    }
    patchBuild({ locked: false, state: "failed", failedAction: "align", error: shown(error) });
  }
}

/** Waits for the new Admin console, which reloads this page; answers whether it came. */
async function awaitReplacement() {
  const started = Date.now();
  while (Date.now() - started < RECONNECT_LIMIT_MS) {
    await loadAuth();
    if (replacingPage()) return true;
    await wait(POLL_MS);
  }
  return false;
}

/**
 * Resumes a stuck setup transition as Classic's recovery does: the server's
 * resume, its resource check once the Admin console is aligned, then the
 * setup picks up where the transition stands.
 */
export async function resumeTransition(transition) {
  if (setup.recovering || !transition?.operation_id) return;
  setup.recovering = "resume";
  setup.recoveryError = null;
  try {
    let data = await adminSend("/api/admin/system-alignment/resume", { body: { operation_id: transition.operation_id, tag: transition.system_tag } });
    if (pipelineStage(data) === "admin_aligned") data = await adminSend("/api/admin/system-alignment/verify-resources", { body: { operation_id: transition.operation_id } });
    if (data?.reconnect || data?.status === "admin_alignment_started") {
      patchBuild({ state: "reconnecting", locked: true });
      if (await awaitReplacement()) return;
      patchBuild({ state: "failed", failedAction: "align", locked: false, error: null });
      return;
    }
    const status = await system.alignment.load();
    await resumeFromTransition(status?.transition);
  } catch (error) {
    setup.recoveryError = shown(error) || { error: "setup_recovery_failed" };
    reload("alignment");
  } finally {
    setup.recovering = "";
  }
}

/**
 * Discards the setup a stuck transition belongs to, by the workflow the
 * server has; the installed system stays. Answers whether it was discarded.
 */
export async function discardSetup() {
  if (setup.recovering) return false;
  setup.recovering = "discard";
  setup.recoveryError = null;
  try {
    const current = (await workflowSnapshot())?.workflow_id || null;
    const data = await adminSend("/api/setup/abandon", { body: current ? { setup_workflow_id: current } : {} });
    if (data?.cleanup?.blocking === true) {
      setup.cleanup = data.cleanup;
      return false;
    }
    if (data?.ok !== true) throw Object.assign(new Error("discard"), { payload: data || {} });
    setWorkflow(null);
    setup.generatedReady = false;
    setup.deploymentReady = false;
    setup.cleanup = null;
    present(setup.build.tag);
    goTo("release");
    return true;
  } catch (error) {
    if (error?.payload?.cleanup) setup.cleanup = error.payload.cleanup;
    setup.recoveryError = shown(error) || { error: "setup_recovery_failed" };
    return false;
  } finally {
    setup.recovering = "";
    reload("alignment", "lifecycle");
  }
}

/** The left action: update the Admin console, or retry what failed before it. */
export async function leftAction() {
  if (setup.build.state === "failed") {
    if (setup.build.failedAction === "validate") return validate();
    if (setup.build.failedAction === "confirm") return undefined;
  }
  return updateAdmin();
}

/** The right action: verify a chosen build, continue once ready, or retry a failed confirmation. */
export async function rightAction() {
  if (setup.build.state === "selected") return validate();
  if (setup.build.state === "failed" && setup.build.failedAction === "confirm" && setup.build.result) patchBuild({ state: "valid", failedAction: null });
  return confirmBuild();
}

export function canVerify() {
  return verifyAllowed(setup.build, setup.releases, setup.intentNeeded);
}

/**
 * A setup the server still runs: resumed on the build its transition targets,
 * which after a reconnect is first carried past the Admin replacement.
 * Answers whether there was one.
 */
export async function resumeFromTransition(transition) {
  if (!setupTransitionActive(transition)) return false;
  setup.operation = confirmedOperation(transition);
  if (["admin_reconnect_pending", "admin_aligned"].includes(transition.stage)) {
    try {
      await adminSend("/api/admin/system-alignment/resume", { body: { operation_id: transition.operation_id, tag: transition.system_tag } });
    } catch {
      setup.operation = null;
    }
  }
  if (setup.operation) {
    try {
      await loadTemplate(transition.system_tag);
    } catch {
      setup.template = null;
    }
  }
  await loadReleases(transition.system_tag);
  goTo(storedStep());
  return true;
}

/**
 * Starts Guided Setup over: the server abandons the workflow first, which
 * removes its own draft, generated config and deployment plan but never the
 * installed system, then a new workflow begins at step one.
 */
export async function restart(onCleared) {
  if (setup.restarting) return false;
  setup.restarting = true;
  setup.restartError = null;
  setup.generation += 1;
  try {
    const workflowId = setup.workflowId || (await workflowSnapshot())?.workflow_id || null;
    await adminSend("/api/setup/abandon", { body: workflowId ? { setup_workflow_id: workflowId } : {} });
  } catch (error) {
    setup.restartError = shown(error) || { error: "setup_restart_failed" };
    if (error?.payload?.cleanup) setup.cleanup = error.payload.cleanup;
    setup.restarting = false;
    return false;
  }
  setWorkflow(null);
  setup.operation = null;
  setup.template = null;
  setup.generatedReady = false;
  setup.deploymentReady = false;
  onCleared?.();
  try {
    const result = await startPath(false);
    if (result?.ok && result.setup_workflow_id) {
      setup.intentId = result.setup_intent_id || null;
      setup.intentNeeded = false;
      setWorkflow(result.setup_workflow_id);
    } else {
      setup.intentNeeded = true;
    }
  } catch {
    setup.intentNeeded = true;
  }
  goTo("release");
  present(setup.build.tag);
  setup.restarting = false;
  reload("alignment", "lifecycle");
  return true;
}

export { actionState };
