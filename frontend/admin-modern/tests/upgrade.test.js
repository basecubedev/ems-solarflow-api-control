// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  UPGRADE_STEPS,
  PHASES,
  OPTION_GROUPS,
  defaultChoices,
  upgradeOptions,
  isDevelopment,
  validateBody,
  executeBody,
  verificationAccepted,
  verificationRefusal,
  replacesAdmin,
  migrationView,
  activeUpgrade,
  phaseStates,
  failedStep,
  backupArchive,
  rateLimited,
  RATE_LIMITED,
  clock,
  lastRun,
  nothingChanged,
  phaseOfStage,
  REFUSED_BEFORE_WORK,
  UPGRADE_STAGES,
  WORKING_STAGES,
  STAGE_TONES,
  PHASE_TONES,
  phaseProgress,
} from "../src/lib/upgrade.js";

const read = (path) => readFileSync(new URL(`../../../admin/${path}`, import.meta.url), "utf8");
const CLASSIC = readFileSync(new URL("../../../admin/static/admin.js", import.meta.url), "utf8");

test("the steps are the executor's, in its order, and every one belongs to exactly one phase", () => {
  const source = read("guided_upgrade.py");
  const labels = source.slice(source.indexOf("_PLAN_LABELS = {"), source.indexOf("}", source.indexOf("_PLAN_LABELS = {")));
  assert.deepEqual([...labels.matchAll(/^\s+"([a-z_]+)":/gm)].map((match) => match[1]), [...UPGRADE_STEPS]);
  const placed = PHASES.flatMap((phase) => phase.steps);
  assert.deepEqual([...placed].sort(), [...UPGRADE_STEPS].sort());
  assert.equal(new Set(placed).size, placed.length);
  assert.deepEqual(placed, [...UPGRADE_STEPS], "phases follow the run's order");
});

test("the owner's choices cover every option the executor takes, and the deploy is always on", () => {
  const source = read("guided_upgrade.py");
  const all = [...source.slice(source.indexOf("ALL_OPTIONS = ("), source.indexOf(")", source.indexOf("ALL_OPTIONS = ("))).matchAll(/"([a-z_]+)"/g)].map((match) => match[1]);
  const mandatory = [...source.match(/MANDATORY_OPTIONS = \(([^)]*)\)/)[1].matchAll(/"([a-z_]+)"/g)].map((match) => match[1]);
  const offered = Object.values(OPTION_GROUPS).flat();
  assert.deepEqual([...offered, ...mandatory].sort(), [...all].sort());
  assert.deepEqual(Object.keys(upgradeOptions(defaultChoices())).sort(), [...all].sort());
  const off = upgradeOptions({ backup: false, settings: false, diagnostics: false });
  assert.deepEqual(Object.entries(off).filter(([, on]) => on).map(([key]) => key).sort(), [...mandatory].sort());
  assert.ok(Object.values(upgradeOptions(defaultChoices())).every(Boolean));
});

test("a development build is recognised as Classic recognises it, and only it carries the acknowledgement", () => {
  const classic = CLASSIC.slice(CLASSIC.indexOf("function isImmutableDevelopmentBuildTag("), CLASSIC.indexOf("\n}\n", CLASSIC.indexOf("function isImmutableDevelopmentBuildTag(")) + 2);
  const isDev = new Function(`${classic}\nreturn isImmutableDevelopmentBuildTag;`)();
  for (const tag of ["dev-feature-x-abcdef1-12-1", "dev-a-0123456789abcdef-3-2", "v0.9.0", "latest", "dev-x-abc-1-1", "dev--abcdef1-1-1", ` dev-feature-x-abcdef1-12-1 `]) {
    assert.equal(isDevelopment({ tag }), isDev(tag), tag);
  }
  assert.equal(isDevelopment({ tag: "v1", channel: "development" }), true);
  assert.deepEqual(validateBody({ tag: "v0.9.0", channel: "stable" }), { tag: "v0.9.0" });
  assert.deepEqual(validateBody({ tag: "dev-feature-x-abcdef1-12-1" }), { tag: "dev-feature-x-abcdef1-12-1", acknowledge_risk: true });
});

test("the run is sent as Classic sends it: confirmed, bound to the verified pair and the reviewed migration", () => {
  const server = read("server.py");
  const fields = server.slice(server.indexOf("def _handle_maintenance_upgrade_execute"), server.indexOf("unsupported upgrade field"));
  const allowed = [...fields.matchAll(/"([a-z_]+)",/g)].map((match) => match[1]);
  const body = executeBody({ release: { tag: "v0.9.0" }, choices: defaultChoices(), migrationRevision: "rev-1", fingerprint: "fp" });
  assert.deepEqual(body, { confirm: true, target_release: "v0.9.0", options: upgradeOptions(defaultChoices()), migration_revision: "rev-1", selection_fingerprint: "fp" });
  const dev = executeBody({ release: { tag: "x", channel: "development" }, choices: defaultChoices(), migrationRevision: undefined, fingerprint: "fp" });
  assert.equal(dev.acknowledge_risk, true);
  assert.equal(dev.migration_revision, null);
  assert.deepEqual(Object.keys(dev).filter((key) => !allowed.includes(key)), []);
});

test("a verification counts only with the server's yes and its fingerprint, and a no says why", () => {
  assert.equal(verificationAccepted({ valid: true, selection_fingerprint: "fp" }), true);
  assert.equal(verificationAccepted({ valid: true, upgrade_allowed: false, selection_fingerprint: "fp" }), false);
  assert.equal(verificationAccepted({ valid: true, selection_fingerprint: "" }), false);
  assert.equal(verificationAccepted({ valid: false, selection_fingerprint: "fp" }), false);
  assert.equal(verificationAccepted(null), false);
  assert.deepEqual(verificationRefusal({ upgrade_direction: { reason: "Older than the running build." } }), { message: "Older than the running build." });
  assert.deepEqual(verificationRefusal({ terminal_error: { code: "x", message: "Pull failed." } }), { message: "Pull failed." });
  assert.deepEqual(verificationRefusal({ error: "system_build_registry_rate_limited" }), { error: "system_build_registry_rate_limited" });
  assert.equal(verificationRefusal({ valid: true }), null);
  assert.equal(replacesAdmin({ admin_update_required: true }), true);
  assert.equal(replacesAdmin({ admin_update_required: false, alignment: "retag_required" }), false);
  assert.equal(replacesAdmin(null), false);
});

test("the migration review says how many devices change and how many lose control", () => {
  assert.deepEqual(migrationView({ needs_migration: true, changes: [{ disables_control: true }, {}] }), { required: true, devices: 2, losing: 1 });
  assert.deepEqual(migrationView({ needs_migration: false, changes: [] }), { required: false, devices: 0, losing: 0 });
  assert.deepEqual(migrationView(null), { required: false, devices: 0, losing: 0 });
});

test("only a guided upgrade that has not ended is one to follow", () => {
  assert.equal(activeUpgrade({ mode: "guided_upgrade", stage: "admin_reconnect_pending" }), true);
  assert.equal(activeUpgrade({ mode: "guided_upgrade", stage: "failed_recoverable" }), true);
  assert.equal(activeUpgrade({ mode: "guided_upgrade", stage: "completed" }), false);
  assert.equal(activeUpgrade({ mode: "guided_upgrade", stage: "cancelled" }), false);
  assert.equal(activeUpgrade({ mode: "fresh_install", stage: "ems_operation_running" }), false);
  assert.equal(activeUpgrade(null), false);
});

const states = (input) => Object.fromEntries(phaseStates(input).map((phase) => [phase.id, phase.state]));

test("while the start request checks and backs up, the first two phases run and the rest wait", () => {
  assert.deepEqual(states({ starting: true, adminRequired: true, backup: true }), { check: "running", backup: "running", admin: "pending", install: "pending", verify: "pending" });
  assert.deepEqual(states({ starting: true, adminRequired: false, backup: false }), { check: "running", backup: "skipped", admin: "skipped", install: "pending", verify: "pending" });
});

test("while the Admin console is replaced, what came before it is done and the rest waits", () => {
  assert.deepEqual(states({ stage: "admin_reconnect_pending", adminRequired: true, backup: true }), { check: "done", backup: "done", admin: "running", install: "pending", verify: "pending" });
  assert.deepEqual(states({ stage: null, adminRequired: true, backup: true }), { check: "pending", backup: "pending", admin: "pending", install: "pending", verify: "pending" });
});

test("checking that everything runs follows the health check when no diagnostics were chosen", () => {
  const steps = [{ key: "verify_image", state: "done" }, { key: "recreate_ems", state: "done" }];
  assert.equal(states({ steps, stage: "healthcheck_pending" }).verify, "running");
  assert.equal(states({ steps, stage: "completed" }).verify, "done");
  assert.equal(states({ steps, stage: "ems_operation_running" }).verify, "pending");
  assert.equal(states({ steps: [...steps, { key: "diagnostics", state: "done" }], stage: "healthcheck_pending" }).verify, "running");
  assert.equal(states({ steps: [...steps, { key: "diagnostics", state: "failed" }], stage: "healthcheck_pending" }).verify, "failed");
});

test("the Admin replacement is told by the transition, and a run that keeps the Admin skips it", () => {
  assert.equal(states({ stage: "admin_reconnect_pending", adminRequired: true }).admin, "running");
  assert.equal(states({ stage: "ems_operation_running", adminRequired: true }).admin, "done");
  assert.equal(states({ stage: null, adminRequired: true, steps: [{ key: "pull_image", state: "running" }] }).admin, "done");
  assert.equal(states({ stage: null, adminRequired: true, steps: [{ key: "backup", state: "done" }] }).admin, "pending");
  assert.equal(states({ stage: "admin_reconnect_pending", adminRequired: false }).admin, "skipped");
});

test("each phase follows its steps: any failed fails it, any running runs it, all done or skipped finishes it", () => {
  const steps = [
    { key: "verify_image", state: "done" },
    { key: "preflight", state: "done" },
    { key: "migration_review", state: "done" },
    { key: "backup", state: "done" },
    { key: "mqtt_migration", state: "skipped" },
    { key: "target_resources", state: "done" },
    { key: "pull_image", state: "running" },
    { key: "update_compose", state: "pending" },
    { key: "recreate_ems", state: "pending" },
    { key: "diagnostics", state: "pending" },
  ];
  assert.deepEqual(states({ steps, stage: "ems_operation_running", adminRequired: true }), { check: "done", backup: "done", admin: "done", install: "running", verify: "pending" });
  const failed = steps.map((step) => (step.key === "pull_image" ? { ...step, state: "failed" } : step));
  assert.equal(states({ steps: failed, stage: "failed_recoverable", adminRequired: false }).install, "failed");
  const noBackup = steps.filter((step) => step.key !== "backup" && step.key !== "mqtt_migration");
  assert.equal(states({ steps: noBackup }).backup, "skipped");
});

test("a finished run shows every phase it went through as done", () => {
  const steps = [{ key: "verify_image", state: "done" }, { key: "recreate_ems", state: "done" }, { key: "diagnostics", state: "running" }];
  assert.deepEqual(states({ steps, adminRequired: false, finished: "succeeded" }), { check: "done", backup: "skipped", admin: "skipped", install: "done", verify: "done" });
  assert.equal(states({ steps: steps.slice(0, 2), adminRequired: false, finished: "succeeded" }).verify, "done");
});

test("the step that stopped a run comes from the live steps first, then the result", () => {
  assert.deepEqual(failedStep({ steps: [{ key: "backup", state: "failed", message: "disk full" }] }), { key: "backup", detail: "disk full" });
  assert.deepEqual(failedStep({ steps: [], result: { steps: [{ id: "pull_image", status: "error", detail: "manifest unknown" }] } }), { key: "pull_image", detail: "manifest unknown" });
  assert.equal(failedStep({ steps: [{ key: "backup", state: "done" }], result: { steps: [{ id: "backup", status: "ok" }] } }), null);
  assert.equal(failedStep(null), null);
});

test("the backup a run wrote is named by its archive, never by another sentence", () => {
  assert.equal(backupArchive({ steps: [{ id: "backup", status: "ok", archive: "/opt/ems/backups/ems-backup-20261021.tar.gz" }] }), "ems-backup-20261021.tar.gz");
  assert.equal(backupArchive({ steps: [{ id: "backup", status: "ok", detail: "Completed before Admin alignment." }] }), null);
  assert.equal(backupArchive({ steps: [{ id: "backup", status: "error", archive: "x.tar.gz" }] }), null);
  assert.equal(backupArchive(null), null);
});

test("a registry rate limit is recognised by its reason or by the pull step's code, as in Classic", () => {
  const classic = CLASSIC.match(/const UPGRADE_REGISTRY_RATE_LIMIT_REASONS = new Set\(\[([^\]]*)\]\)/)[1];
  assert.deepEqual([...classic.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]).sort(), [...RATE_LIMITED].sort());
  assert.equal(rateLimited({ reason: "image_pull_rate_limited" }), true);
  assert.equal(rateLimited({ steps: [{ id: "pull_image", status: "error", code: "system_build_registry_rate_limited" }] }), true);
  assert.equal(rateLimited({ steps: [{ id: "backup", status: "error", code: "image_pull_rate_limited" }] }), false);
  assert.equal(rateLimited(null), false);
});

test("the clock reads minutes and seconds, and hours once there are any", () => {
  assert.equal(clock(0), "0:00");
  assert.equal(clock(65_400), "1:05");
  assert.equal(clock(3_725_000), "1:02:05");
  assert.equal(clock(-5), "0:00");
  assert.equal(clock(undefined), "0:00");
});

test("the last run is named once it ended, never while it runs", () => {
  assert.deepEqual(lastRun({ mode: "guided_upgrade", stage: "completed", system_tag: "v0.9.0", updated_at: "2026-10-21T10:00:00Z" }), { outcome: "completed", tag: "v0.9.0", at: "2026-10-21T10:00:00Z" });
  assert.equal(lastRun({ mode: "guided_upgrade", stage: "cancelled", system_tag: "v0.9.0" }).outcome, "cancelled");
  assert.equal(lastRun({ mode: "guided_upgrade", stage: "ems_operation_running", system_tag: "v0.9.0" }), null);
  assert.equal(lastRun({ mode: "fresh_install", stage: "completed", system_tag: "v0.9.0" }), null);
  assert.equal(lastRun(null), null);
});

test("a stopped run left the device as it was only before its transition and only in a check or the backup", () => {
  assert.equal(nothingChanged({ started: false, steps: [], result: null }), true);
  assert.equal(nothingChanged({ started: false, steps: [], result: { steps: [{ id: "backup", status: "error" }] } }), true);
  assert.equal(nothingChanged({ started: false, steps: [], result: { steps: [{ id: "mqtt_migration", status: "error" }] } }), false);
  assert.equal(nothingChanged({ started: true, steps: [{ key: "verify_image", state: "failed" }] }), false);
  assert.equal(nothingChanged({ started: true, steps: [], result: { reason: "image_pull_rate_limited", steps: [{ id: "pull_image", status: "error", code: "image_pull_rate_limited" }] } }), false);
});

test("a failure no step names is placed by the transition's stage", () => {
  assert.equal(phaseOfStage("admin_reconnect_pending"), "admin");
  assert.equal(phaseOfStage("admin_update_pending"), "admin");
  assert.equal(phaseOfStage("ems_operation_pending"), "install");
  assert.equal(phaseOfStage("resources_verified"), "install");
  assert.equal(phaseOfStage("healthcheck_pending"), "verify");
  assert.equal(phaseOfStage("validated"), null);
  assert.equal(phaseOfStage(null), null);
});

test("every refusal that may say nothing changed is one the start request gives before any work", () => {
  const server = read("server.py");
  const execute = server.slice(server.indexOf("def _handle_maintenance_upgrade_execute"), server.indexOf("prepare_alignment(run_context)")) +
    server.slice(server.indexOf("def _refuse_blocked_upgrade_direction"), server.indexOf("def _start_guided_upgrade_job"));
  const preflight = read("guided_upgrade.py");
  const before = preflight.slice(preflight.indexOf("    def preflight("), preflight.indexOf("    def _runtime_image("));
  for (const code of REFUSED_BEFORE_WORK) {
    assert.ok(execute.includes(`"${code}"`) || before.includes(`"${code}"`), code);
  }
});

test("every stage but idle wears a tone, and the working stages are stages that hold the console", () => {
  assert.deepEqual(Object.keys(STAGE_TONES).sort(), UPGRADE_STAGES.filter((stage) => stage !== "idle").sort());
  for (const stage of WORKING_STAGES) {
    assert.ok(UPGRADE_STAGES.includes(stage), stage);
    assert.equal(STAGE_TONES[stage], "c-out", stage);
  }
  assert.equal(STAGE_TONES.ready, "c-ok");
  assert.equal(STAGE_TONES.succeeded, "c-ok");
  for (const state of ["done", "running", "pending", "failed"]) assert.match(PHASE_TONES[state], /^c-/, state);
  assert.equal(PHASE_TONES.running, STAGE_TONES.running);
});

test("the progress names the phase at work, its place and the share behind it", () => {
  const phases = (states) => states.map((state, index) => ({ id: PHASES[index].id, state }));
  assert.deepEqual(phaseProgress(phases(["done", "done", "running", "pending", "pending"])), { current: "admin", step: 3, total: 5, share: 0.5 });
  assert.deepEqual(phaseProgress(phases(["done", "pending", "pending"])), { current: "backup", step: 2, total: 3, share: 1 / 3 });
  assert.deepEqual(phaseProgress(phases(["done", "failed", "pending"])), { current: "backup", step: 2, total: 3, share: 1 / 3 });
  assert.deepEqual(phaseProgress(phases(["done", "done", "done"])), { current: null, step: 3, total: 3, share: 1 });
  assert.deepEqual(phaseProgress(phases(["running", "running", "pending", "pending"])), { current: "check", step: 1, total: 4, share: 0.25 });
  assert.deepEqual(phaseProgress([]), { current: null, step: 0, total: 0, share: 0 });
  assert.deepEqual(phaseProgress(null), { current: null, step: 0, total: 0, share: 0 });
});
