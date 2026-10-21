// SPDX-License-Identifier: AGPL-3.0-or-later
import { ApiError } from "../../../shared/lib/api.js";
import { adminGet, adminSend, adminUpload, expectSessionEnd, replacingPage, sessionLost } from "./auth.svelte.js";
import { reload } from "./system.svelte.js";
import { followJob, isTransient } from "../lib/jobs.js";
import { errorKey, noAnswer } from "../lib/errors.js";

/**
 * One server-side job as this browser follows it: its id once the server
 * started it, the server's steps and result, or what went wrong. Once it has
 * ended, the reads it changes are loaded again.
 */
class Job {
  reloads;
  jobId = $state(null);
  running = $state(false);
  steps = $state.raw([]);
  lost = $state(false);
  result = $state.raw(null);
  error = $state.raw(null);

  constructor(reloads) {
    this.reloads = reloads;
  }

  reset() {
    this.jobId = null;
    this.steps = [];
    this.lost = false;
    this.result = null;
    this.error = null;
  }
}

/**
 * The backup and the restore this browser started. They run on the server
 * whatever page is open, so they are followed here rather than by a page:
 * leaving Backups and coming back finds each job where it is, and the page
 * stays busy -- no second backup or restore on top -- until it has ended.
 */
class BackupJobs {
  create = new Job(["backups", "overview"]);
  restore = new Job(["backups", "overview", "plan"]);
  restoreTarget = $state.raw(null);
  restorePlan = $state.raw(null);
  upload = $state.raw(null);

  get uploading() {
    return this.upload?.state === "busy";
  }

  get busy() {
    return this.create.running || this.restore.running;
  }
}

export const backupJobs = new BackupJobs();

const jobPath = (id) => `/api/admin/maintenance/backups/jobs/${encodeURIComponent(id)}`;

async function followFrom(job) {
  job.running = true;
  job.error = null;
  try {
    const final = await followJob(() => adminGet(jobPath(job.jobId)), {
      onUpdate: (data) => (job.steps = data.steps || []),
      onLost: (value) => (job.lost = value),
    });
    job.result = final.result || {};
  } catch (error) {
    job.error = error;
  } finally {
    job.lost = false;
    job.running = false;
    reload(...job.reloads);
  }
}

async function follow(job, start) {
  job.reset();
  job.running = true;
  try {
    const started = await start();
    job.jobId = started.job_id;
    job.steps = started.steps || [];
  } catch (error) {
    job.error = error;
    job.running = false;
    if (noAnswer(error)) reload(...job.reloads);
    return;
  }
  await followFrom(job);
}

export async function createBackup(scope) {
  if (backupJobs.busy) return;
  await follow(backupJobs.create, () => adminSend("/api/admin/maintenance/backups/create", { body: { scope } }));
}

const sessionError = (error) => error instanceof ApiError && (error.status === 401 || error.status === 403);

/**
 * Whether following a started job broke off for want of an answer: the
 * server may still be running it, so it is neither done nor failed yet.
 */
export function contactLost(job) {
  return Boolean(job.jobId) && job.result === null && job.error !== null && isTransient(job.error);
}

/**
 * A session that ends while a restore runs most likely ended because the
 * restore put back the password file -- but only a restore the server had
 * started can have done that; a refused start is just a refused start.
 */
export function restoreEndedSession(job) {
  return Boolean(job.jobId) && job.result === null && sessionError(job.error);
}

export async function runRestore(target, plan) {
  if (backupJobs.busy || !plan?.plan_id || plan.blocked) return;
  backupJobs.restoreTarget = target;
  backupJobs.restorePlan = plan;
  expectSessionEnd("restore.sessionEnded");
  await follow(backupJobs.restore, () => adminSend("/api/admin/maintenance/backups/restore/execute", { body: { plan_id: plan.plan_id, confirm: true } }));
  if (!restoreEndedSession(backupJobs.restore)) expectSessionEnd("");
}

/**
 * A start that got no answer: the server may have started the job or not,
 * and without its id there is nothing to follow, so the lists are read again.
 */
export function startUnanswered(job) {
  return !job.jobId && job.result === null && noAnswer(job.error);
}

/** Follow a job again whose following broke off; the server kept running it. */
export function followAgain(job) {
  if (!job.running && (contactLost(job) || restoreEndedSession(job))) followFrom(job);
}

/**
 * Once the console answers and the session holds again, every job whose
 * following was cut short is followed on: its result is still to learn. A
 * start or an upload the ended session refused never ran, so it is forgotten
 * rather than shown as a failure next to a session that holds.
 */
export function resumeJobs() {
  followAgain(backupJobs.create);
  followAgain(backupJobs.restore);
  for (const job of [backupJobs.create, backupJobs.restore]) {
    if (!job.jobId && !job.running && sessionLost(job.error)) job.reset();
  }
  if (backupJobs.upload?.state === "fault" && sessionLost(backupJobs.upload.error)) backupJobs.upload = null;
}

/**
 * A backup file brought back from this computer. An upload can be large and
 * slow, so it is held here like the jobs: leaving the page neither loses its
 * result nor frees the button for a second one on top.
 */
export async function uploadBackup(file) {
  if (backupJobs.uploading || !file) return;
  backupJobs.upload = { state: "busy", name: file.name, error: null };
  try {
    const data = await adminUpload("/api/admin/maintenance/backups/import", file);
    backupJobs.upload = { state: "ok", name: data.name || file.name, error: null };
  } catch (error) {
    backupJobs.upload = { state: "fault", name: file.name, error };
  } finally {
    reload("backups", "overview");
  }
}

/**
 * A restore, a backup or an upload runs on while this page is open, but its
 * result reaches only this page, and an upload ends with it. Leaving the
 * console then -- a hand-off to Classic is a full page change -- asks first.
 */
export function guardLeaving(isBusy) {
  globalThis.addEventListener("beforeunload", (event) => {
    if (replacingPage() || !(backupJobs.busy || backupJobs.uploading || isBusy())) return;
    event.preventDefault();
    event.returnValue = true;
  });
}

/** A backup another tab or a cleanup removed: the list is read again so it no longer offers it. */
export function reloadIfGone(error) {
  if (errorKey(error) === "error.backup_gone") reload("backups");
}

/** Forget a finished restore, so the next one starts from its own preview. */
export function dismissRestore() {
  if (backupJobs.restore.running) return;
  backupJobs.restore.reset();
  backupJobs.restoreTarget = null;
  backupJobs.restorePlan = null;
}
