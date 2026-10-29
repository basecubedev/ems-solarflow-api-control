// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Guided Upgrade as the owner follows it (admin/guided_upgrade.py,
 * admin/system_alignment.py). The server verifies, plans and runs every step
 * and decides what may happen; this turns its steps and stages into the few
 * phases an owner can follow, and builds the requests Classic sends.
 */

/** Where the console is with a Guided Upgrade (state/upgrade.svelte.js). */
export const UPGRADE_STAGES = Object.freeze(["idle", "verifying", "verifyFailed", "blocked", "ready", "starting", "checking", "replacing", "running", "succeeded", "failed", "refused", "cancelled", "returned"]);

/** The stages at which the device is at work for the owner: a version is fetched and checked, or the update runs. */
export const WORKING_STAGES = Object.freeze(["verifying", "starting", "checking", "replacing", "running"]);

/** The tone each stage wears, from its mark to its banner: working, ready, held up, or how the run ended. */
export const STAGE_TONES = Object.freeze({
  verifying: "c-out",
  verifyFailed: "c-warn",
  blocked: "c-warn",
  ready: "c-ok",
  starting: "c-out",
  checking: "c-out",
  replacing: "c-out",
  running: "c-out",
  succeeded: "c-ok",
  failed: "c-fault",
  refused: "c-fault",
  cancelled: "c-fault",
  returned: "c-soc",
});

/**
 * Refusals the start request gives before it checks, backs up or migrates
 * anything (admin/server.py _handle_maintenance_upgrade_execute and
 * GuidedUpgradeExecutor.preflight): only these may say that nothing changed.
 */
export const REFUSED_BEFORE_WORK = Object.freeze([
  "confirm_required",
  "target_required",
  "config_missing",
  "config_invalid",
  "compose_missing",
  "upgrade_direction_blocked",
  "upgrade_direction_unavailable",
  "acknowledgement_required",
]);

/** The run's steps in the order the server emits them (guided_upgrade._PLAN_LABELS). */
export const UPGRADE_STEPS = Object.freeze([
  "verify_image",
  "preflight",
  "migration_review",
  "backup",
  "mqtt_migration",
  "target_resources",
  "config_check",
  "config_write",
  "pull_image",
  "update_compose",
  "recreate_ems",
  "diagnostics",
]);

/**
 * What the owner follows: each phase gathers the server's steps that belong to
 * it. "admin" has no step of its own: the Admin console is replaced between
 * the steps before it and the ones after, and the transition's stage says so.
 */
export const PHASES = Object.freeze([
  { id: "check", steps: ["verify_image", "preflight", "migration_review"] },
  { id: "backup", steps: ["backup", "mqtt_migration"] },
  { id: "admin", steps: [] },
  { id: "install", steps: ["target_resources", "config_check", "config_write", "pull_image", "update_compose", "recreate_ems"] },
  { id: "verify", steps: ["diagnostics"] },
]);

/** The choices the owner makes; deploying the build (pull_image, recreate) is not one (guided_upgrade.MANDATORY_OPTIONS). */
export const OPTION_GROUPS = Object.freeze({
  backup: ["backup"],
  settings: ["config_check", "config_add_keys", "config_comments"],
  diagnostics: ["diagnostics"],
});

export function defaultChoices() {
  return { backup: true, settings: true, diagnostics: true };
}

/** The executor's options for the owner's choices, the mandatory deploy included. */
export function upgradeOptions(choices) {
  const options = { pull_image: true, recreate: true };
  for (const [group, keys] of Object.entries(OPTION_GROUPS)) {
    for (const key of keys) options[key] = Boolean(choices?.[group]);
  }
  return options;
}

const DEVELOPMENT_TAG = /^dev-[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?-[0-9a-f]{7,40}-[1-9][0-9]*-[1-9][0-9]*$/;

/** A build that has not been released: the catalogue says so, or its tag is an immutable development tag (admin.js isImmutableDevelopmentBuildTag). */
export function isDevelopment(release) {
  const tag = typeof release?.tag === "string" ? release.tag.trim() : "";
  return release?.channel === "development" || (tag.length <= 128 && DEVELOPMENT_TAG.test(tag));
}

/** The verification request; choosing a development build is its acknowledgement, as in Classic. */
export function validateBody(release) {
  return isDevelopment(release) ? { tag: release.tag, acknowledge_risk: true } : { tag: release.tag };
}

/** The confirmed run, bound to the verified pair and the migration review the owner saw. */
export function executeBody({ release, choices, migrationRevision, fingerprint }) {
  const body = {
    confirm: true,
    target_release: release.tag,
    options: upgradeOptions(choices),
    migration_revision: migrationRevision ?? null,
    selection_fingerprint: fingerprint,
  };
  if (isDevelopment(release)) body.acknowledge_risk = true;
  return body;
}

/** A verification counts only with the server's yes and its fingerprint (admin.js upgradeValidationAccepted). */
export function verificationAccepted(data) {
  return Boolean(data && data.valid && data.upgrade_allowed !== false && typeof data.selection_fingerprint === "string" && data.selection_fingerprint);
}

/**
 * Why a verification was not accepted, as the server put it: the direction's
 * reason (a downgrade, an unproven move), the terminal error, or its message;
 * null when it said nothing, so the generic sentence is shown.
 */
export function verificationRefusal(data) {
  const message = data?.upgrade_direction?.reason || data?.terminal_error?.message || data?.message;
  if (typeof message === "string" && message.trim()) return { message: message.trim() };
  return data?.error ? { error: data.error } : null;
}

/** Whether the run replaces the Admin console too: the verification's own verdict (system_build.ALIGNMENT_UPDATE_DECISIONS). */
export function replacesAdmin(verification) {
  return verification?.admin_update_required === true;
}

/** What the MQTT migration review says the run will change. */
export function migrationView(review) {
  const changes = Array.isArray(review?.changes) ? review.changes : [];
  const losing = changes.filter((change) => change?.disables_control).length;
  return { required: review?.needs_migration === true || changes.length > 0, devices: changes.length, losing };
}

/** Admin stages of a guided_upgrade transition, and where each leaves the "admin" phase. */
const ADMIN_STAGES = Object.freeze({
  admin_update_pending: "running",
  admin_alignment_started: "running",
  admin_reconnect_pending: "running",
  admin_aligned: "done",
  resources_verified: "done",
  ems_operation_pending: "done",
  ems_operation_running: "done",
  healthcheck_pending: "done",
  completed: "done",
});

/** A transition that belongs to a Guided Upgrade and has not ended. */
export function activeUpgrade(transition) {
  return Boolean(transition && transition.mode === "guided_upgrade" && transition.stage && !["completed", "cancelled"].includes(transition.stage));
}

/** Where the Admin console is in its replacement: "skipped" when the run keeps it. */
function adminState({ adminRequired, stage, steps }) {
  if (adminRequired === false) return "skipped";
  if (Object.hasOwn(ADMIN_STAGES, stage)) return ADMIN_STAGES[stage];
  const after = steps.some((step) => PHASES[3].steps.includes(step.key) && step.state !== "pending");
  return after ? "done" : "pending";
}

function gather(states) {
  if (states.includes("failed")) return "failed";
  if (states.includes("running")) return "running";
  if (states.every((state) => state === "skipped")) return "skipped";
  if (states.every((state) => state === "done" || state === "skipped")) return "done";
  if (states.some((state) => state === "done")) return "running";
  return "pending";
}

/** Where the run is without a step to say so: before its job exists, or while the Admin console is replaced. */
function withoutSteps(id, { stage, starting, backup }) {
  const past = Object.hasOwn(ADMIN_STAGES, stage);
  if (id === "backup" && backup === false) return "skipped";
  if (id === "check" || id === "backup") return starting ? "running" : past ? "done" : "pending";
  if (id === "verify") return stage === "healthcheck_pending" ? "running" : stage === "completed" ? "done" : "pending";
  return "pending";
}

/**
 * The phases with their states, from the job's steps (key, state) and the
 * transition's stage. Before the job exists -- while the request that checks
 * and backs up is open, or while the Admin console is replaced -- the stage
 * and the owner's choice of a backup say where the run is. A finished run
 * shows every phase it went through as done; checking that everything runs
 * is always part of it, with or without the diagnostics.
 */
export function phaseStates({ steps = [], stage = null, adminRequired = null, backup = null, starting = false, finished = null } = {}) {
  const list = Array.isArray(steps) ? steps : [];
  const byKey = new Map(list.map((step) => [step.key, step.state]));
  const states = PHASES.map((phase) => {
    if (phase.id === "admin") return adminState({ adminRequired, stage, steps: list });
    const known = phase.steps.filter((key) => byKey.has(key)).map((key) => byKey.get(key) || "pending");
    if (known.length) {
      const state = gather(known);
      if (phase.id === "verify" && state !== "failed" && stage === "healthcheck_pending") return "running";
      return state;
    }
    if (list.length && phase.id === "backup") return "skipped";
    return withoutSteps(phase.id, { stage, starting: starting && !list.length, backup });
  });
  if (finished === "succeeded") return PHASES.map((phase, index) => ({ id: phase.id, state: ["pending", "running"].includes(states[index]) ? "done" : states[index] }));
  return PHASES.map((phase, index) => ({ id: phase.id, state: states[index] }));
}

/**
 * Where a run stands among the phases it shows: the phase at work (the first
 * running or failed one, else the next to come), its place in the list, and
 * the share behind it, each running phase counting half.
 */
export function phaseProgress(phases) {
  const list = Array.isArray(phases) ? phases : [];
  const done = list.filter((phase) => phase.state === "done").length;
  const running = list.filter((phase) => phase.state === "running").length;
  const current = list.find((phase) => phase.state === "running" || phase.state === "failed") || list.find((phase) => phase.state === "pending") || null;
  return {
    current: current?.id || null,
    step: current ? list.indexOf(current) + 1 : list.length,
    total: list.length,
    share: list.length ? Math.min(1, (done + running / 2) / list.length) : 0,
  };
}

/** The tone each phase state wears in the list of phases. */
export const PHASE_TONES = Object.freeze({ done: "c-ok", running: "c-out", pending: "c-idle", failed: "c-fault" });

/** The step that stopped a run, from the job's steps or the result's. */
export function failedStep(job) {
  const steps = Array.isArray(job?.steps) ? job.steps : [];
  const live = steps.find((step) => step?.state === "failed");
  if (live) return { key: live.key, detail: live.message || null };
  const results = Array.isArray(job?.result?.steps) ? job.result.steps : [];
  const result = results.find((step) => step?.status === "error");
  return result ? { key: result.id, detail: result.detail || null } : null;
}

/** The steps after which the run has changed nothing on the device: checks and the backup. */
const HARMLESS_STEPS = Object.freeze(["verify_image", "preflight", "migration_review", "backup"]);

/**
 * Whether a run that stopped left the device as it was. Only a run that never
 * reached its transition can say so, and only when it stopped in a check or
 * the backup; an MQTT migration, a replaced Admin console or any install step
 * may already have changed something.
 */
export function nothingChanged({ started, steps, result }) {
  if (started) return false;
  const stopped = failedStep({ steps, result });
  return !stopped || HARMLESS_STEPS.includes(stopped.key);
}

/** The phase a transition's failed stage belongs to, for a failure no step names. */
export function phaseOfStage(stage) {
  if (typeof stage !== "string") return null;
  if (stage.startsWith("admin_")) return "admin";
  if (stage === "resources_verified" || stage.startsWith("ems_")) return "install";
  if (stage === "healthcheck_pending") return "verify";
  return null;
}

/** The archive a run's backup wrote, as the server named it. */
export function backupArchive(result) {
  const steps = Array.isArray(result?.steps) ? result.steps : [];
  const step = steps.find((item) => item?.id === "backup" && item.status === "ok");
  const name = step?.archive || step?.detail;
  return typeof name === "string" && /\.tar\.gz(\.enc)?$/.test(name) ? name.split("/").pop() : null;
}

export const RATE_LIMITED = Object.freeze(["system_build_registry_rate_limited", "image_pull_rate_limited"]);

/** A run the registry turned away: nothing was changed, and waiting is the remedy (admin.js upgradeFailureReason). */
export function rateLimited(result) {
  if (typeof result?.reason === "string" && RATE_LIMITED.includes(result.reason)) return true;
  const steps = Array.isArray(result?.steps) ? result.steps : [];
  return steps.some((step) => step?.id === "pull_image" && step.status === "error" && RATE_LIMITED.includes(step.code));
}

/** "m:ss" for a running clock; hours only once there are any. */
export function clock(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
}

/** The last Guided Upgrade the transition record names, once it has ended; null while one runs or none is recorded. */
export function lastRun(transition) {
  if (transition?.mode !== "guided_upgrade" || !["completed", "cancelled"].includes(transition.stage) || !transition.system_tag) return null;
  return { outcome: transition.stage, tag: String(transition.system_tag), at: transition.updated_at || null };
}
