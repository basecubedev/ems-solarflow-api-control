// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminGet, adminSend } from "./auth.svelte.js";
import { setup } from "./setup.svelte.js";
import { reload } from "./system.svelte.js";

const GENERATED_PATH = "/data/generated/config.json";
const PREPARE_FIRST_MS = 400;
const PREPARE_POLL_MS = 800;
const START_FIRST_MS = 500;
const START_POLL_MS = 900;

function initialStart() {
  return { status: "idle", phase: "", steps: [], services: [], error: "", errorCode: null, errorDetail: "", running: false, dashboardUrl: null, conflict: null, resolving: false, repairing: false };
}

/**
 * Guided Setup's last two steps as Classic runs them: the deployment the
 * server prepares from the saved config, and the start of that deployment.
 * The server plans, prepares and starts; this keeps its answers.
 */
class SetupDeploy {
  generatedPath = $state(GENERATED_PATH);
  plan = $state.raw(null);
  prepared = $state(false);
  workspace = $state("");
  bootstrap = $state.raw(null);
  images = $state.raw([]);
  steps = $state.raw([]);
  phase = $state("");
  status = $state("idle");
  error = $state("");
  errorDetail = $state("");
  workspaceConflict = $state(false);
  existing = $state.raw(null);
  docker = $state.raw(null);
  start = $state.raw(initialStart());
  autoAttempted = false;
  jobId = "";
  generation = 0;
  prepareTimer = null;
  startTimer = null;

  dockerReady = $derived(!this.docker || this.docker.state === "ready");
  ready = $derived(this.prepared && this.status === "succeeded");
}

export const setupDeploy = new SetupDeploy();

const text = (value) => String(value ?? "").trim();
const message = (payload, error, fallback) => text(payload?.message) || text(payload?.error) || text(error?.message) || fallback;

function sync() {
  setup.deploymentReady = setupDeploy.ready;
}

function patchStart(change) {
  setupDeploy.start = { ...setupDeploy.start, ...change };
}

/** Whether a generated config is saved; it opens the deployment step after a reload too. */
export async function loadConfigStatus() {
  try {
    const data = await adminGet("/api/setup/config/status");
    setup.generatedReady = Boolean(data?.exists);
    if (data?.path) setupDeploy.generatedPath = data.path;
  } catch {
    return;
  }
}

/** The saved config just written: the deployment starts over from it. */
export function configWritten(data) {
  setup.generatedReady = true;
  setupDeploy.generatedPath = data?.path || setupDeploy.generatedPath;
  setupDeploy.prepared = false;
  if (setupDeploy.status === "succeeded") setupDeploy.status = "idle";
  setupDeploy.autoAttempted = false;
  sync();
}

/**
 * What the deployment will prepare, read before anything happens; the
 * prepared marker survives a reload. On the deployment step preparation then
 * starts on its own once.
 */
export async function loadPlan({ prepare = false } = {}) {
  let data;
  try {
    data = await adminGet("/api/setup/deployment/plan");
  } catch {
    return;
  }
  setupDeploy.plan = data || null;
  if (data?.generated_config) {
    setup.generatedReady = Boolean(data.generated_config.ready);
    setupDeploy.generatedPath = data.generated_config.path || setupDeploy.generatedPath;
  }
  setupDeploy.workspace = data?.workspace || setupDeploy.workspace;
  setupDeploy.bootstrap = data?.bootstrap_source || null;
  setupDeploy.docker = data?.docker || null;
  setupDeploy.images = Array.isArray(data?.images) ? data.images : [];
  setupDeploy.prepared = Boolean(data?.prepared);
  if (setupDeploy.prepared) {
    if (setupDeploy.status !== "running") setupDeploy.status = "succeeded";
  } else if (setupDeploy.status === "succeeded") setupDeploy.status = "idle";
  sync();
  if (prepare) autoPrepare();
}

function autoPrepare() {
  const deploy = setupDeploy;
  if (deploy.autoAttempted || ["running", "failed"].includes(deploy.status) || deploy.workspaceConflict || deploy.existing || deploy.ready || !setup.generatedReady || !deploy.plan?.can_prepare || !deploy.dockerReady) return;
  deploy.autoAttempted = true;
  prepare(false);
}

/**
 * Prepares the workspace and pulls the images. An existing installation or a
 * workspace prepared for another config is never replaced without the
 * reader's yes, which is ``overwrite``.
 */
export async function prepare(overwrite) {
  const deploy = setupDeploy;
  deploy.status = "running";
  deploy.error = "";
  deploy.errorDetail = "";
  deploy.workspaceConflict = false;
  deploy.existing = null;
  deploy.autoAttempted = true;
  deploy.steps = [];
  sync();
  try {
    const job = await adminSend("/api/setup/deployment/prepare", { body: { setup_workflow_id: setup.workflowId, overwrite: Boolean(overwrite) } });
    if (job?.ok === false) throw Object.assign(new Error("prepare"), { payload: job });
    applyPrepareJob(job || {});
    if (job?.job_id) followPrepare(job.job_id);
  } catch (error) {
    const payload = error?.payload;
    if (error?.status === 409 && payload?.reason === "existing_install_conflict") {
      deploy.status = "idle";
      deploy.existing = payload;
    } else if (error?.status === 409 && payload?.reason === "workspace_conflict") {
      deploy.status = "idle";
      deploy.workspaceConflict = true;
      deploy.error = message(payload, null, "");
    } else {
      deploy.status = "failed";
      deploy.error = message(payload, error, "");
    }
    sync();
  }
}

function applyPrepareJob(job) {
  const deploy = setupDeploy;
  deploy.jobId = job.job_id || deploy.jobId;
  deploy.status = job.status || deploy.status;
  deploy.phase = job.phase || deploy.phase;
  deploy.steps = Array.isArray(job.steps) ? job.steps : [];
  if (Array.isArray(job.images) && job.images.length) deploy.images = job.images;
  if (job.workspace) deploy.workspace = job.workspace;
  if (job.status === "succeeded") {
    deploy.prepared = true;
    deploy.error = "";
    deploy.errorDetail = "";
  } else if (job.status === "failed") {
    deploy.prepared = false;
    deploy.error = text(job.error?.message);
    deploy.errorDetail = text(job.error?.detail);
  }
  sync();
}

function followPrepare(jobId) {
  clearTimeout(setupDeploy.prepareTimer);
  const generation = setupDeploy.generation;
  const tick = async () => {
    try {
      const job = await adminGet(`/api/setup/deployment/jobs/${encodeURIComponent(jobId)}`);
      if (generation !== setupDeploy.generation) return;
      applyPrepareJob(job || {});
      if (job?.status === "running") setupDeploy.prepareTimer = setTimeout(tick, PREPARE_POLL_MS);
      else loadPlan();
    } catch (error) {
      if (generation !== setupDeploy.generation) return;
      setupDeploy.status = "failed";
      setupDeploy.error = message(error?.payload, error, "");
      sync();
    }
  };
  setupDeploy.prepareTimer = setTimeout(tick, PREPARE_FIRST_MS);
}

/** The containers as they are now: running or not, their services, and a conflict that blocks starting. */
export async function loadStatus() {
  let data;
  try {
    data = await adminGet("/api/setup/deployment/status");
  } catch {
    return;
  }
  setupDeploy.prepared = Boolean(data?.prepared);
  if (data?.docker) setupDeploy.docker = data.docker;
  if (!setupDeploy.prepared && setupDeploy.status === "succeeded") setupDeploy.status = "idle";
  sync();
  const start = setupDeploy.start;
  const running = Boolean(data?.running);
  const errors = Array.isArray(data?.errors) ? data.errors : [];
  const change = { running, services: Array.isArray(data?.services) ? data.services : [], dashboardUrl: data?.dashboard_url || start.dashboardUrl };
  if (data?.conflict) change.conflict = data.conflict;
  else if (start.status !== "failed") change.conflict = null;
  if (running) Object.assign(change, { status: "succeeded", error: "", errorCode: null, errorDetail: "" });
  else if (start.status !== "running" && start.status !== "failed") {
    Object.assign(change, { status: "idle", error: text(errors[0]?.message), errorDetail: text(errors[0]?.detail), errorCode: errors[0]?.code || null });
  }
  patchStart(change);
}

/** Starts the prepared deployment and follows the start job. */
export async function startEms() {
  patchStart({ status: "running", error: "", errorCode: null, errorDetail: "", conflict: null, steps: [] });
  try {
    const data = await adminSend("/api/setup/deployment/start", { body: { setup_workflow_id: setup.workflowId } });
    reload("alignment");
    if (data?.ok === false) throw Object.assign(new Error("start"), { payload: data });
    applyStartJob(data || {});
    if (data?.job_id) followStart(data.job_id);
  } catch (error) {
    const payload = error?.payload;
    reload("alignment");
    patchStart({ status: "failed", errorCode: payload?.reason || null, error: message(payload, error, ""), errorDetail: text(payload?.detail) });
  }
}

function applyStartJob(job) {
  const change = { status: job.status || setupDeploy.start.status, phase: job.phase || setupDeploy.start.phase, steps: Array.isArray(job.steps) ? job.steps : [] };
  if (Array.isArray(job.services)) change.services = job.services;
  if (job.dashboard_url) change.dashboardUrl = job.dashboard_url;
  if (job.status === "succeeded") Object.assign(change, { running: true, error: "", errorCode: null });
  else if (job.status === "failed") Object.assign(change, { running: false, errorCode: job.error?.code || null, error: text(job.error?.message), errorDetail: text(job.error?.detail), conflict: job.conflict || null });
  if (job.transition) reload("alignment");
  patchStart(change);
}

function followStart(jobId) {
  clearTimeout(setupDeploy.startTimer);
  const generation = setupDeploy.generation;
  const tick = async () => {
    try {
      const job = await adminGet(`/api/setup/deployment/start/jobs/${encodeURIComponent(jobId)}`);
      if (generation !== setupDeploy.generation) return;
      applyStartJob(job || {});
      if (job?.status === "running") setupDeploy.startTimer = setTimeout(tick, START_POLL_MS);
      else loadStatus();
    } catch (error) {
      if (generation !== setupDeploy.generation) return;
      patchStart({ status: "failed", error: message(error?.payload, error, "") });
    }
  };
  setupDeploy.startTimer = setTimeout(tick, START_FIRST_MS);
}

/** Gives the workspace to the container's user, then starts again. */
export async function repairPermissions() {
  if (setupDeploy.start.errorCode !== "workspace_permission_denied") return;
  patchStart({ repairing: true });
  try {
    const data = await adminSend("/api/setup/deployment/repair-permissions", { body: { setup_workflow_id: setup.workflowId } });
    if (data?.ok === false) throw Object.assign(new Error("repair"), { payload: data });
    patchStart({ repairing: false, error: "", errorCode: null, errorDetail: "" });
    await startEms();
  } catch (error) {
    const payload = error?.payload;
    patchStart({ status: "failed", errorCode: payload?.reason || "workspace_permission_repair_failed", error: message(payload, error, ""), errorDetail: text(payload?.detail) });
  } finally {
    patchStart({ repairing: false });
  }
}

/** Removes a stopped container, or replaces a running one, that holds the name; then starts when the server says so. */
export async function resolveConflict() {
  const conflict = setupDeploy.start.conflict;
  const replace = conflict?.replace_available === true;
  if (!conflict || (!replace && conflict.safe_fix_available !== true)) return;
  patchStart({ resolving: true, error: "" });
  try {
    const data = await adminSend("/api/setup/deployment/resolve-container-conflict", { body: { setup_workflow_id: setup.workflowId, container_name: conflict.container_name, action: replace ? "replace_running_and_continue" : "remove_stopped_and_continue" } });
    if (data?.ok === false) throw Object.assign(new Error("resolve"), { payload: data });
    if (data?.transition) reload("alignment");
    patchStart({ conflict: data?.conflict || null, resolving: false, error: "", errorCode: null, errorDetail: "" });
    if (data?.continue !== true) {
      patchStart({ status: "idle" });
      return;
    }
    await startEms();
  } catch (error) {
    patchStart({ resolving: false, status: "failed", error: message(error?.payload, error, "") });
  }
}

/** Everything back to a first visit, for a setup started over; a poll in flight is dropped. */
export function resetDeploy() {
  setupDeploy.generation += 1;
  clearTimeout(setupDeploy.prepareTimer);
  clearTimeout(setupDeploy.startTimer);
  Object.assign(setupDeploy, { generatedPath: GENERATED_PATH, plan: null, prepared: false, workspace: "", bootstrap: null, images: [], steps: [], phase: "", status: "idle", error: "", errorDetail: "", workspaceConflict: false, existing: null, docker: null, start: initialStart(), autoAttempted: false, jobId: "" });
  setup.generatedReady = false;
  sync();
}
