// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminGet, adminSend, auth, expectSessionEnd, loadAuth, replacingPage, sessionLost } from "./auth.svelte.js";
import { reload, system } from "./system.svelte.js";
import { followJob, isTransient } from "../lib/jobs.js";
import { noAnswer } from "../lib/errors.js";
import { REFUSED_BEFORE_WORK, activeUpgrade, defaultChoices, executeBody, migrationView, replacesAdmin, validateBody, verificationAccepted, verificationRefusal } from "../lib/upgrade.js";

export const POLL_MS = 1500;
export const REPLACEMENT_SLOW_MS = 120000;
export const REPLACEMENT_DEADLINE_MS = 15 * 60 * 1000;
export const START_WATCH_MS = 10 * 60 * 1000;
const MEMO_KEY = "ems-admin.upgrade";
const SIGN_IN_NOTICE = "upgrade.signInAgain";

/** Stages at which the Admin console is being replaced: only the new console moves the run on from there. */
const REPLACING_STAGES = Object.freeze(["admin_update_pending", "admin_reconnect_pending"]);

/** Stages whose EMS work waits for the normal route to claim it (system_alignment.recover_ems_operation). */
const CLAIMABLE_STAGES = Object.freeze(["resources_verified", "ems_operation_pending"]);

/** Refusals that mean "verify again": the pair or the migration review moved since this page read them. */
const VERIFY_AGAIN = Object.freeze(["system_build_verification_required", "system_build_verification_stale", "mqtt_migration_review_stale"]);

/** Refusals that mean a Guided Setup still holds the console. */
const SETUP_CONFLICTS = Object.freeze(["setup_abandon_required", "setup_cleanup_required"]);

/**
 * The one Guided Upgrade this console follows. It runs on the server whatever
 * page is open, so it is held here rather than by the Updates page, and the
 * server's durable transition is the authority: every decision about where
 * the run is comes from it (follow), and a page loaded from a replaced Admin
 * console finds the run there and follows it on. Its stage is one of
 * lib/upgrade.js UPGRADE_STAGES.
 */
class Upgrade {
  stage = $state("idle");
  release = $state.raw(null);
  verification = $state.raw(null);
  migration = $state.raw(null);
  choices = $state(defaultChoices());
  error = $state.raw(null);
  conflict = $state.raw(null);
  operationId = $state(null);
  adminRequired = $state(null);
  backupPlanned = $state(null);
  transition = $state.raw(null);
  jobId = $state(null);
  steps = $state.raw([]);
  result = $state.raw(null);
  lost = $state(false);
  slow = $state(false);
  unknownStart = $state(false);
  workDone = $state(false);
  returnNeedsClassic = $state(false);
  since = $state(0);
  finishedAt = $state(0);
  before = $state.raw(null);
  recovering = $state("");
  acknowledgeReturn = $state(false);
  detached = $state(false);
  settledStage = null;
  #generation = 0;

  get busy() {
    return ["verifying", "starting", "checking", "replacing", "running"].includes(this.stage) || Boolean(this.recovering);
  }

  /** A run the server still holds: the console cannot be closed on it, only recovered or followed. */
  get holding() {
    return activeUpgrade(this.transition) && this.operationId !== null && this.transition.operation_id === this.operationId;
  }

  get target() {
    return this.release?.tag || this.transition?.system_tag || null;
  }

  next() {
    this.#generation += 1;
    return this.#generation;
  }

  current(generation) {
    return generation === this.#generation;
  }
}

export const upgrade = new Upgrade();

function readMemo() {
  try {
    const memo = JSON.parse(globalThis.sessionStorage?.getItem(MEMO_KEY) || "null");
    return memo && typeof memo === "object" ? memo : null;
  } catch {
    return null;
  }
}

function writeMemo(memo) {
  try {
    if (memo) globalThis.sessionStorage?.setItem(MEMO_KEY, JSON.stringify(memo));
    else globalThis.sessionStorage?.removeItem(MEMO_KEY);
  } catch {
    return;
  }
}

function runningVersions() {
  const overview = system.overview.data;
  return {
    admin: overview?.components?.admin?.tag || system.alignment.data?.running_pair?.admin?.release || null,
    ems: overview?.containers?.ems?.tag || system.alignment.data?.running_pair?.ems?.release || null,
  };
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const codeOf = (error) => error?.payload?.error || error?.payload?.reason || error?.error || error?.reason || null;

function settle(stage) {
  upgrade.stage = stage;
  upgrade.finishedAt = upgrade.finishedAt || Date.now();
  upgrade.lost = false;
  upgrade.detached = false;
  upgrade.settledStage = stage === "failed" ? upgrade.transition?.expired === true ? "expired" : upgrade.transition?.stage || null : null;
  if (["succeeded", "cancelled", "refused", "returned"].includes(stage) || (stage === "failed" && !activeUpgrade(upgrade.transition))) writeMemo(null);
  else if (stage === "failed" && readMemo()) writeMemo({ ...readMemo(), stopped: true });
  reload("overview", "alignment", "lifecycle", "releases");
}

async function readStatus() {
  try {
    return await adminGet("/api/admin/system-alignment/status");
  } catch (error) {
    if (sessionLost(error)) throw error;
    return null;
  }
}

/** The transition of this run, if the status names it; null when it names none or another. */
function ours(transition) {
  if (!transition || transition.mode !== "guided_upgrade") return null;
  if (upgrade.operationId && transition.operation_id !== upgrade.operationId) return null;
  return transition;
}

/**
 * Sessions live in the Admin process, so a page reloaded from a replaced Admin
 * console meets the sign-in. When this tab started a run, the sign-in says
 * why and that the run goes on; signing in brings the reader back to it.
 */
export function explainSignInDuringUpgrade() {
  const memo = readMemo();
  if (memo?.operationId && !memo.stopped) expectSessionEnd(SIGN_IN_NOTICE);
}

/** Leave the run as it is on screen; a new choice starts from the release list. A run the server still holds is not left. */
export function closeUpgrade() {
  if (upgrade.busy || upgrade.holding) return;
  upgrade.next();
  reset();
}

function reset() {
  upgrade.stage = "idle";
  upgrade.release = null;
  upgrade.verification = null;
  upgrade.migration = null;
  upgrade.error = null;
  upgrade.conflict = null;
  upgrade.operationId = null;
  upgrade.adminRequired = null;
  upgrade.backupPlanned = null;
  upgrade.transition = null;
  upgrade.jobId = null;
  upgrade.steps = [];
  upgrade.result = null;
  upgrade.lost = false;
  upgrade.slow = false;
  upgrade.unknownStart = false;
  upgrade.workDone = false;
  upgrade.returnNeedsClassic = false;
  upgrade.before = null;
  upgrade.since = 0;
  upgrade.finishedAt = 0;
  upgrade.acknowledgeReturn = false;
  upgrade.detached = false;
  upgrade.choices = defaultChoices();
  writeMemo(null);
}

/**
 * Choose a release and verify it: the server downloads or reuses the Admin
 * and EMS images and checks the pair. Nothing on the host changes. The MQTT
 * migration review is read with it, so the confirmation shows what the run
 * will change.
 */
export async function chooseRelease(release) {
  if (upgrade.busy || upgrade.holding || !release?.tag) return;
  closeUpgrade();
  upgrade.release = release;
  await verify();
}

export async function verify() {
  const release = upgrade.release;
  if (!release || upgrade.busy || upgrade.holding) return;
  const generation = upgrade.next();
  upgrade.stage = "verifying";
  upgrade.error = null;
  upgrade.conflict = null;
  upgrade.verification = null;
  upgrade.migration = null;
  try {
    const data = await adminSend("/api/admin/maintenance/upgrade/validate", { body: validateBody(release) });
    if (!upgrade.current(generation)) return;
    if (!verificationAccepted(data)) {
      upgrade.error = verificationRefusal(data);
      upgrade.stage = "verifyFailed";
      return;
    }
    const migration = await adminGet("/api/admin/maintenance/zendure-mqtt/migration-review");
    if (!upgrade.current(generation)) return;
    if (migration?.status !== "ok") {
      upgrade.error = migration?.message ? { message: migration.message } : null;
      upgrade.stage = "verifyFailed";
      return;
    }
    upgrade.verification = data;
    upgrade.migration = migration;
    upgrade.stage = "ready";
  } catch (error) {
    if (!upgrade.current(generation)) return;
    if (SETUP_CONFLICTS.includes(codeOf(error))) {
      upgrade.conflict = error.payload;
      upgrade.stage = "blocked";
      return;
    }
    upgrade.error = error;
    upgrade.stage = "verifyFailed";
  }
}

/** The switch away from an unfinished Guided Setup, as the server previews it. */
export async function previewSetupDiscard() {
  return adminSend("/api/admin/workflow-lifecycle/switch/preview", { body: { target: "guided_upgrade" } });
}

/**
 * Discard the unfinished setup the preview named, then verify again. A state
 * that changed since the preview answers "preview again" (true), as Classic
 * asks once more; any other refusal stays on screen.
 */
export async function discardSetup(preview) {
  if (!preview?.fingerprint || upgrade.busy) return false;
  upgrade.recovering = "discard";
  upgrade.error = null;
  try {
    await adminSend("/api/admin/workflow-lifecycle/switch", { body: { target: "guided_upgrade", confirm: true, fingerprint: preview.fingerprint } });
  } catch (error) {
    upgrade.recovering = "";
    reload("lifecycle", "alignment");
    if (codeOf(error) === "workflow_lifecycle_changed") return true;
    upgrade.error = error;
    return false;
  }
  upgrade.recovering = "";
  reload("lifecycle", "alignment");
  await verify();
  return false;
}

/**
 * Start the run. The request itself checks the image, makes the backup and
 * applies an MQTT migration before it answers, which can take minutes; then
 * either the Admin console is replaced first, or the EMS job starts. A
 * refusal before any of that changed nothing; an answer that never came is
 * watched for in the transition.
 */
export async function startUpgrade() {
  const release = upgrade.release;
  const fingerprint = upgrade.verification?.selection_fingerprint;
  if (upgrade.stage !== "ready" || !release || !fingerprint) return;
  const generation = upgrade.next();
  upgrade.stage = "starting";
  upgrade.error = null;
  upgrade.result = null;
  upgrade.steps = [];
  upgrade.since = Date.now();
  upgrade.finishedAt = 0;
  upgrade.slow = false;
  upgrade.adminRequired = replacesAdmin(upgrade.verification);
  upgrade.backupPlanned = Boolean(upgrade.choices.backup || migrationView(upgrade.migration?.review).required);
  upgrade.before = runningVersions();
  upgrade.workDone = false;
  const before = await system.alignment.load();
  if (!upgrade.current(generation)) return;
  if (system.alignment.error) {
    upgrade.error = system.alignment.error;
    upgrade.stage = "verifyFailed";
    return;
  }
  const previous = before?.transition?.operation_id || null;
  writeMemo({ target: release.tag, before: upgrade.before, since: upgrade.since, adminRequired: upgrade.adminRequired, backupPlanned: upgrade.backupPlanned, instance: auth.instanceId, previous });
  let data;
  try {
    data = await adminSend("/api/admin/maintenance/upgrade/execute", {
      body: executeBody({ release, choices: upgrade.choices, migrationRevision: upgrade.migration?.revision, fingerprint }),
    });
  } catch (error) {
    if (upgrade.current(generation)) await notStarted(generation, error);
    return;
  }
  if (!upgrade.current(generation)) return;
  upgrade.operationId = data?.operation_id || data?.transition?.operation_id || null;
  writeMemo({ ...readMemo(), operationId: upgrade.operationId });
  if (data?.job_id) {
    await followJobOf(generation, data);
    return;
  }
  if (data?.reconnect || REPLACING_STAGES.includes(data?.stage)) {
    await waitForReplacement(generation);
    return;
  }
  await notStarted(generation, data);
}

/**
 * The start request did not answer with a run. A refusal leaves the choice
 * open: a verification or a review that moved asks to check again, a Guided
 * Setup that holds the console asks to discard it, and anything else says the
 * update did not start. Steps in the answer mean the check or backup ran and
 * stopped there. No answer at all means the server may still be checking and
 * backing up: the transition is watched for.
 */
async function notStarted(generation, error) {
  const payload = error?.payload || error;
  const code = codeOf(error);
  if (noAnswer(error)) {
    await watchForStart(generation);
    return;
  }
  if (VERIFY_AGAIN.includes(code)) {
    writeMemo(null);
    upgrade.verification = null;
    upgrade.error = error;
    upgrade.stage = "verifyFailed";
    return;
  }
  if (SETUP_CONFLICTS.includes(code)) {
    writeMemo(null);
    upgrade.conflict = payload;
    upgrade.stage = "blocked";
    return;
  }
  if (payload && Array.isArray(payload.steps) && payload.steps.length) {
    upgrade.result = payload;
    upgrade.error = null;
    settle("failed");
    return;
  }
  upgrade.error = error;
  if (REFUSED_BEFORE_WORK.includes(code)) {
    settle("refused");
    return;
  }
  upgrade.workDone = true;
  const status = await readStatus().catch(() => null);
  if (!upgrade.current(generation)) return;
  const started = startedTransition(status?.transition);
  if (started) {
    upgrade.operationId = started.operation_id;
    upgrade.transition = started;
    writeMemo({ ...readMemo(), operationId: upgrade.operationId });
    if (started.stage === "failed_recoverable") {
      settle("failed");
      return;
    }
    upgrade.error = null;
    await follow(generation, started);
    return;
  }
  settle("failed");
}

/** A Guided Upgrade transition for the chosen version that did not exist before this page started its run. */
function startedTransition(transition) {
  const memo = readMemo();
  if (!transition || transition.mode !== "guided_upgrade" || transition.system_tag !== upgrade.release?.tag) return null;
  return transition.operation_id && transition.operation_id !== memo?.previous ? transition : null;
}

/** After an unanswered start: wait for the run's transition to appear, then follow it; say so if none came. */
async function watchForStart(generation) {
  upgrade.stage = "checking";
  const deadline = Date.now() + START_WATCH_MS;
  while (upgrade.current(generation) && Date.now() < deadline) {
    let status = null;
    try {
      status = await readStatus();
    } catch {
      upgrade.detached = true;
      return;
    }
    const fresh = startedTransition(status?.transition);
    if (!upgrade.current(generation)) return;
    if (fresh) {
      upgrade.operationId = fresh.operation_id;
      writeMemo({ ...readMemo(), operationId: upgrade.operationId });
      await follow(generation, fresh);
      return;
    }
    await wait(POLL_MS * 2);
  }
  if (!upgrade.current(generation)) return;
  upgrade.unknownStart = true;
  settle("failed");
}

/**
 * Where the run goes from here, decided by its transition: the end it
 * reached, a failure to recover from, the Admin console still being replaced
 * (only the new console moves the run on), or the EMS work to follow.
 */
async function follow(generation, transition) {
  upgrade.transition = transition;
  if (!transition) {
    settle("failed");
    return;
  }
  if (transition.stage === "completed") {
    settle("succeeded");
    return;
  }
  if (transition.stage === "cancelled") {
    settle("cancelled");
    return;
  }
  if (transition.stage === "failed_recoverable" || transition.expired === true) {
    settle("failed");
    return;
  }
  if (REPLACING_STAGES.includes(transition.stage)) {
    if (reconnectDecided(transition)) await continueRun(generation);
    else await waitForReplacement(generation);
    return;
  }
  await continueRun(generation);
}

/**
 * Whether the Admin console's replacement can be asked to move on: this page
 * is answered by another console than the one the run started on (its own
 * startup attempt may have failed), or the replacement is proven gone, where
 * the server records the failure and offers its ways out.
 */
function reconnectDecided(transition) {
  if (transition.stage !== "admin_reconnect_pending") return false;
  if (transition.replacement_active === false) return true;
  const started = readMemo()?.instance;
  return Boolean(started && auth.instanceId && started !== auth.instanceId);
}

/**
 * The Admin console is being replaced by the target build. This page waits
 * for a different console to answer, and auth's adopt() then reloads it from
 * there; the new page finds the run in the transition and follows it on. The
 * page does not ask to resume while the replacement runs, since the old
 * console could record the swap as failed; it asks once another console
 * answers, once the replacement is proven gone, or after the deadline, when
 * the server decides. A failure, a cancellation, or a run that moved on (this
 * page already talks to the new console) ends the wait.
 */
async function waitForReplacement(generation) {
  moving();
  upgrade.stage = "replacing";
  const started = Date.now();
  while (upgrade.current(generation) && upgrade.stage === "replacing") {
    const elapsed = Date.now() - started;
    if (elapsed >= REPLACEMENT_SLOW_MS) upgrade.slow = true;
    if (elapsed >= REPLACEMENT_DEADLINE_MS) {
      await continueRun(generation);
      return;
    }
    await loadAuth();
    if (!upgrade.current(generation) || replacingPage()) return;
    if (!auth.authenticated && auth.reachable) {
      upgrade.detached = true;
      return;
    }
    if (auth.reachable) {
      let status = null;
      try {
        status = await readStatus();
      } catch {
        upgrade.detached = true;
        return;
      }
      if (!upgrade.current(generation)) return;
      const transition = status?.transition || null;
      if (transition && upgrade.operationId && transition.operation_id !== upgrade.operationId) {
        upgrade.transition = null;
        upgrade.error = { error: "operation_mismatch" };
        settle("failed");
        return;
      }
      if (transition) {
        upgrade.transition = transition;
        if (!REPLACING_STAGES.includes(transition.stage) || reconnectDecided(transition)) {
          await follow(generation, transition);
          return;
        }
      }
    }
    await wait(POLL_MS);
  }
}

/**
 * Follow the run's EMS job until it ends. Losing contact for longer than the
 * job poll tolerates is not a failure: the run goes on on the server, so the
 * page waits for an answer and asks the transition where the run is.
 */
async function followJobOf(generation, started) {
  upgrade.stage = "running";
  upgrade.jobId = started.job_id;
  upgrade.steps = started.steps || [];
  if (started.transition) upgrade.transition = started.transition;
  let final;
  try {
    final = await followJob(() => adminGet(`/api/admin/maintenance/upgrade/jobs/${encodeURIComponent(upgrade.jobId)}`), {
      onUpdate: (data) => {
        if (!upgrade.current(generation)) return;
        upgrade.steps = data.steps || [];
        if (data.transition) upgrade.transition = data.transition;
      },
      onLost: (value) => {
        if (upgrade.current(generation)) upgrade.lost = value;
      },
    });
  } catch (error) {
    if (!upgrade.current(generation)) return;
    if (sessionLost(error)) {
      upgrade.detached = true;
      return;
    }
    if (isTransient(error)) {
      await reattach(generation);
      return;
    }
    upgrade.error = error;
    const status = await readStatus().catch(() => null);
    if (!upgrade.current(generation)) return;
    upgrade.transition = status?.transition || upgrade.transition;
    settle("failed");
    return;
  }
  if (!upgrade.current(generation)) return;
  upgrade.lost = false;
  upgrade.result = final.result || { ok: final.status === "succeeded", steps: [] };
  if (final.transition) upgrade.transition = final.transition;
  if (final.status === "succeeded" && upgrade.result.ok !== false) {
    settle("succeeded");
    return;
  }
  const status = await readStatus().catch(() => null);
  if (!upgrade.current(generation)) return;
  upgrade.transition = status?.transition || upgrade.transition;
  settle("failed");
}

/** Contact came back after the job poll gave up: ask the transition, and follow the run from there. */
async function reattach(generation) {
  upgrade.lost = true;
  for (;;) {
    let status = null;
    try {
      status = await readStatus();
    } catch {
      upgrade.detached = true;
      return;
    }
    if (!upgrade.current(generation)) return;
    if (status) {
      upgrade.lost = false;
      await follow(generation, ours(status.transition));
      return;
    }
    await wait(POLL_MS * 2);
  }
}

/** Continue the run from its durable operation: the server re-attaches to its one job, or starts it. */
async function continueRun(generation) {
  if (!upgrade.current(generation)) return;
  moving();
  upgrade.stage = "running";
  let data;
  try {
    data = await adminSend("/api/admin/maintenance/upgrade/resume", { body: { operation_id: upgrade.operationId } });
  } catch (error) {
    if (!upgrade.current(generation)) return;
    if (sessionLost(error)) {
      upgrade.detached = true;
      return;
    }
    if (codeOf(error) === "admin_replacement_in_progress") {
      stillReplacing();
      await waitForReplacement(generation);
      return;
    }
    if (noAnswer(error)) {
      await wait(POLL_MS * 2);
      if (upgrade.current(generation)) await reattach(generation);
      return;
    }
    upgrade.error = error;
    const status = await readStatus().catch(() => null);
    if (!upgrade.current(generation)) return;
    upgrade.transition = status?.transition || upgrade.transition;
    settle("failed");
    return;
  }
  if (!upgrade.current(generation)) return;
  if (data?.reconnect) {
    stillReplacing();
    await waitForReplacement(generation);
    return;
  }
  if (data?.job_id) {
    await followJobOf(generation, data);
    return;
  }
  upgrade.error = data;
  settle("failed");
}

/** The run goes on: a sign-in during it may say so again. */
function moving() {
  if (readMemo()?.stopped) writeMemo({ ...readMemo(), stopped: false });
}

/** The console that answered is the one being replaced: only a different one may be asked to move the run on. */
function stillReplacing() {
  writeMemo({ ...readMemo(), instance: auth.instanceId });
}

/**
 * A page loaded while a Guided Upgrade holds the console -- or the Updates
 * page finding one this page lost hold of -- follows it on. After the Admin
 * console was replaced, that is this tab's own run, and it opens the Updates
 * page to show it. A run of this tab that ended while nobody watched is shown
 * with its end, and the record of it is dropped. One resume at a time: the
 * callers that ask while one runs share it.
 */
export function resumeUpgrade() {
  if (!resuming) resuming = resumeOnce().finally(() => (resuming = null));
  return resuming;
}

let resuming = null;

async function resumeOnce() {
  if (auth.notice === SIGN_IN_NOTICE) expectSessionEnd("");
  if (upgrade.stage !== "idle" && !upgrade.detached) return;
  const generation = upgrade.next();
  const status = await system.alignment.load();
  if (!upgrade.current(generation)) return;
  const transition = status?.transition || null;
  const memo = readMemo();
  const mine = Boolean(memo && transition && memo.target === transition.system_tag && (!memo.operationId || memo.operationId === transition.operation_id));
  if (!activeUpgrade(transition)) {
    const followed = transition && upgrade.operationId && transition.operation_id === upgrade.operationId;
    if (transition && ["completed", "cancelled"].includes(transition.stage) && ((mine && memo.operationId === transition.operation_id) || followed)) {
      adopt(transition, mine ? memo : null);
      upgrade.finishedAt = Date.parse(transition.updated_at) || Date.now();
      settle(transition.stage === "completed" ? "succeeded" : "cancelled");
      return;
    }
    writeMemo(null);
    if (upgrade.detached) {
      upgrade.next();
      reset();
    }
    return;
  }
  adopt(transition, mine ? memo : null);
  upgrade.detached = false;
  if (mine && !globalThis.location.hash.startsWith("#/updates")) globalThis.location.hash = "#/updates";
  await follow(generation, transition);
}

function adopt(transition, memo) {
  upgrade.transition = transition;
  upgrade.operationId = transition.operation_id;
  upgrade.release = upgrade.release?.tag === transition.system_tag ? upgrade.release : { tag: transition.system_tag };
  upgrade.adminRequired = typeof transition.admin_alignment_required === "boolean" ? transition.admin_alignment_required : memo ? memo.adminRequired : upgrade.adminRequired;
  upgrade.before = memo?.before || upgrade.before;
  upgrade.backupPlanned = memo && typeof memo.backupPlanned === "boolean" ? memo.backupPlanned : upgrade.backupPlanned;
  upgrade.since = memo?.since || upgrade.since || Date.parse(transition.created_at) || Date.now();
}

/**
 * A failed run on screen follows what the status says about it later:
 * another tab or Classic may retry or cancel it, or its time may run out. A
 * status that names another operation means the run was ended elsewhere: the
 * page lets go of it. The Updates page hands each new status read in here.
 */
export function noteStatus(transition) {
  if (upgrade.stage !== "failed" || upgrade.recovering || !transition) return;
  if (transition.operation_id !== upgrade.operationId) {
    if (upgrade.transition) {
      upgrade.transition = null;
      writeMemo(null);
    }
    return;
  }
  if (transition.stage === "failed_recoverable" || transition.expired === true) upgrade.settledStage = transition.expired === true ? "expired" : "failed_recoverable";
  const recovered = ["failed_recoverable", "expired"].includes(upgrade.settledStage);
  upgrade.transition = transition;
  if (transition.stage === "completed") {
    upgrade.finishedAt = Date.parse(transition.updated_at) || Date.now();
    settle("succeeded");
  } else if (transition.stage === "cancelled") {
    settle("cancelled");
  } else if (recovered && activeUpgrade(transition) && transition.stage !== "failed_recoverable" && transition.expired !== true) {
    upgrade.finishedAt = 0;
    upgrade.error = null;
    upgrade.result = null;
    upgrade.steps = [];
    upgrade.detached = true;
    resumeUpgrade();
  }
}

/**
 * The ways out of a run that stopped, as Classic sends them: try the
 * transition again, return the Admin console to the build that runs, or
 * cancel the run. The server says which are possible; each answer is
 * followed where it leads. The run stays busy until that is decided.
 */
export async function recover(kind) {
  const transition = upgrade.transition;
  const operationId = transition?.operation_id;
  if (!operationId || upgrade.recovering || resuming || upgrade.detached || ["starting", "checking", "replacing", "running"].includes(upgrade.stage)) return;
  const generation = upgrade.next();
  try {
    await recoverAs(kind, generation, transition, operationId);
  } finally {
    if (!upgrade.current(generation) && recoveringFor === generation) upgrade.recovering = "";
  }
}

let recoveringFor = 0;

/** One recovery request and where its answer leads; a newer action that supersedes it releases the busy mark. */
async function recoverAs(kind, generation, transition, operationId) {
  upgrade.recovering = kind;
  recoveringFor = generation;
  upgrade.error = null;
  upgrade.slow = false;
  try {
    if (kind === "retry") {
      const data = await adminSend("/api/admin/system-alignment/resume", { body: { operation_id: operationId, tag: transition.system_tag } });
      let next = data;
      if (data?.stage === "admin_aligned") next = await adminSend("/api/admin/system-alignment/verify-resources", { body: { operation_id: operationId } });
      if (!upgrade.current(generation)) return;
      upgrade.finishedAt = 0;
      upgrade.steps = [];
      upgrade.result = null;
      if (next?.reconnect || next?.status === "admin_alignment_started" || REPLACING_STAGES.includes(next?.stage)) {
        upgrade.recovering = "";
        stillReplacing();
        await waitForReplacement(generation);
        return;
      }
      const status = await readStatus();
      if (!upgrade.current(generation)) return;
      upgrade.recovering = "";
      if (!status) {
        await reattach(generation);
        return;
      }
      const after = ours(status.transition);
      if (after && CLAIMABLE_STAGES.includes(after.stage)) {
        upgrade.transition = after;
        await continueRun(generation);
        return;
      }
      await follow(generation, after);
      return;
    }
    if (kind === "return") {
      const acknowledge = upgrade.acknowledgeReturn;
      upgrade.acknowledgeReturn = false;
      const data = await adminSend("/api/admin/system-alignment/return-to-running-build", { body: { operation_id: operationId, confirm: true, acknowledge_risk: acknowledge } });
      if (!upgrade.current(generation)) return;
      upgrade.recovering = "";
      upgrade.transition = null;
      upgrade.operationId = data?.operation_id || null;
      upgrade.returnNeedsClassic = data?.reconnect !== true;
      settle("returned");
      if (data?.reconnect === true) await waitForAnotherConsole(generation);
      return;
    }
    if (kind === "cancel") {
      const data = await adminSend("/api/admin/system-alignment/cancel", { body: { operation_id: operationId, confirm: true } });
      if (!upgrade.current(generation)) return;
      upgrade.recovering = "";
      if (data?.stage !== "cancelled") {
        upgrade.error = data;
        const status = await readStatus();
        if (upgrade.current(generation)) upgrade.transition = status?.transition || upgrade.transition;
        return;
      }
      upgrade.transition = data.transition || { ...transition, stage: "cancelled" };
      settle("cancelled");
    }
  } catch (error) {
    if (!upgrade.current(generation)) return;
    upgrade.recovering = "";
    if (kind === "return" && codeOf(error) === "acknowledgement_required") {
      upgrade.acknowledgeReturn = true;
      return;
    }
    upgrade.error = error;
    const status = await readStatus().catch(() => null);
    if (!upgrade.current(generation)) return;
    upgrade.transition = status?.transition || upgrade.transition;
    settle("failed");
  }
}

/** The return replaces the Admin console: wait for the other console to answer, which reloads the page from it. */
async function waitForAnotherConsole(generation) {
  const started = Date.now();
  upgrade.slow = false;
  while (upgrade.current(generation) && Date.now() - started < REPLACEMENT_DEADLINE_MS) {
    if (Date.now() - started >= REPLACEMENT_SLOW_MS) upgrade.slow = true;
    await loadAuth();
    if (replacingPage()) return;
    await wait(POLL_MS);
  }
}
