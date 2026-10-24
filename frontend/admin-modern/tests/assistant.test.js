// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ASSISTANT_STATES,
  STATE_LOOKS,
  STEP_IDS,
  STEP_LOOKS,
  TASKS,
  WELCOME_KEY,
  assistantSteps,
  assistantTasks,
  attentionCount,
  bareAddress,
  dashboardStep,
  installKind,
  installed,
  latestUsableBackup,
  managesExisting,
  opensOnAssistant,
  stepProgress,
} from "../src/lib/assistant.js";
import { HEALTHY_INSTALL_STATES, INSTALL_STATES, healthVerdict } from "../src/lib/health.js";
import { PAGES } from "../src/app/pages.js";

const CONFIG_SERVER = readFileSync(new URL("../../../admin/maintenance_config.py", import.meta.url), "utf8");
const SETTINGS_PAGE = readFileSync(new URL("../src/pages/Settings.svelte", import.meta.url), "utf8");

const overview = (state, patch = {}) => ({
  install_state: { state },
  docker: { available: true },
  containers: { ems: { found: true, running: true } },
  ...patch,
});
const config = (count) => ({ status: "ok", summary: { device_count: count } });
const backups = (latest) => ({ summary: { latest_created_at: latest }, backups: latest ? [{ created_at: latest, valid: true }] : [] });
const states = (steps) => Object.fromEntries(steps.map((step) => [step.id, step.state]));
const pick = (step) => ({ state: step.state, reason: step.reason });

test("installed means one of the healthy install states, and an answer that has not come means neither", () => {
  for (const state of INSTALL_STATES) assert.equal(installed(overview(state)), HEALTHY_INSTALL_STATES.includes(state), state);
  assert.equal(installed(null), null);
  assert.equal(installed({ install_state: {} }), null);
  assert.equal(installKind(overview("none")), "none");
  assert.equal(installKind(overview("partial_install")), "partial");
  assert.equal(installKind(overview("compose_only")), "partial");
  assert.equal(installKind(overview("standard_install")), "installed");
  assert.equal(installKind(null), "unknown");
  assert.equal(installKind(overview("legacy_root_config", { install_state: { state: "legacy_root_config", legacy_migration_available: true } })), "legacy");
  assert.equal(installKind(overview("standard_install", { containers: { ems: { found: false } } })), "missing");
  assert.equal(installKind(overview("standard_install", { docker: { available: false }, containers: {} })), "installed");
  assert.equal(managesExisting(overview("partial_install", { install_state: { state: "partial_install", recommended_path: "manage_existing" } })), true);
  assert.equal(managesExisting(overview("partial_install", { install_state: { state: "partial_install", recommended_path: "setup_new" } })), false);
});

test("the server's recommended paths and the legacy flag are the ones install_state.py sends", () => {
  const server = readFileSync(new URL("../../../admin/install_state.py", import.meta.url), "utf8");
  assert.match(server, /PATH_MANAGE_EXISTING = "manage_existing"/);
  assert.match(server, /"legacy_migration_available": self\.legacy_migration_available/);
  assert.match(server, /legacy_migration_available = state == STATE_LEGACY_ROOT_CONFIG/);
});

test("with nothing installed, the installation is the next step and the others wait for it", () => {
  const steps = assistantSteps({ overview: overview("none"), config: null, backups: null });
  assert.deepEqual(steps.map((step) => step.id), STEP_IDS);
  assert.deepEqual(states(steps), { install: "todo", devices: "later", backup: "later" });
  assert.deepEqual(steps.filter((step) => step.next).map((step) => step.id), ["install"]);
});

test("an installation without devices or a backup has those steps to do, devices first", () => {
  const steps = assistantSteps({ overview: overview("standard_install"), config: config(0), backups: backups(null) });
  assert.deepEqual(states(steps), { install: "done", devices: "todo", backup: "todo" });
  assert.deepEqual(steps.filter((step) => step.next).map((step) => step.id), ["devices"]);
  assert.deepEqual(stepProgress(steps), { done: 1, total: 3 });
});

test("devices and a backup on an installation make every step done, with the facts that decided them", () => {
  const steps = assistantSteps({ overview: overview("admin_prepared_install"), config: config(2), backups: backups("2026-10-24T08:00:00Z") });
  assert.deepEqual(states(steps), { install: "done", devices: "done", backup: "done" });
  assert.equal(steps.find((step) => step.id === "devices").count, 2);
  assert.equal(steps.find((step) => step.id === "backup").at, "2026-10-24T08:00:00Z");
  assert.ok(!steps.some((step) => step.next));
  assert.deepEqual(stepProgress(steps), { done: 3, total: 3 });
});

test("settings files without an EMS container are not an installation: the step stays to do, devices and backup still count", () => {
  const gone = overview("standard_install", { containers: { ems: { found: false, running: false } } });
  const steps = assistantSteps({ overview: gone, config: config(2), backups: backups("2026-10-24T08:00:00Z") });
  assert.deepEqual(states(steps), { install: "todo", devices: "done", backup: "done" });
  assert.equal(steps[0].kind, "missing");
  assert.equal(steps[0].next, true);
  assert.deepEqual(stepProgress(steps), { done: 2, total: 3 });
  const unasked = overview("standard_install", { docker: { available: false }, containers: {} });
  assert.equal(assistantSteps({ overview: unasked, config: config(1), backups: backups(null) })[0].state, "done");
});

test("a step whose answer has not come, or could not be read, is unknown and never done", () => {
  assert.deepEqual(states(assistantSteps({ overview: null, config: config(3), backups: backups("2026-10-24T08:00:00Z") })), { install: "unknown", devices: "unknown", backup: "unknown" });
  const installedSteps = assistantSteps({ overview: overview("standard_install"), config: { status: "invalid" }, backups: null });
  assert.deepEqual(states(installedSteps), { install: "done", devices: "unknown", backup: "unknown" });
  assert.equal(states(assistantSteps({ overview: overview("standard_install"), config: { status: "ok", summary: {} }, backups: backups(null) })).devices, "unknown");
});

test("the device counts are the ones the config summary sends", () => {
  const summary = CONFIG_SERVER.match(/def _summary\(config, draft\):[\s\S]*?\n\n\n/);
  assert.ok(summary, "admin/maintenance_config.py _summary is not found");
  assert.match(summary[0], /"device_count": len\(controlled\)/);
  assert.match(summary[0], /"dashboard_enabled": /);
  assert.match(summary[0], /"control": _control_state\(config\)/);
  assert.match(CONFIG_SERVER, /"placeholder_fields": /);
});

test("devices, a grid meter or a broker that still carry the template's example values are not set up", () => {
  const withFields = (fields) => ({ status: "ok", summary: { device_count: 2, control: { placeholder_fields: fields } } });
  const steps = assistantSteps({ overview: overview("standard_install"), config: withFields(["INV_1: devices[0].ip", "grid_meter.ip"]), backups: backups("2026-10-24T08:00:00Z") });
  assert.equal(states(steps).devices, "todo");
  assert.deepEqual(steps[1].placeholders, ["INV_1: devices[0].ip", "grid_meter.ip"]);
  assert.equal(steps[1].next, true);
  assert.equal(states(assistantSteps({ overview: overview("standard_install"), config: withFields(["grid_meter.ip"]), backups: backups(null) })).devices, "todo");
  assert.equal(states(assistantSteps({ overview: overview("standard_install"), config: withFields([]), backups: backups(null) })).devices, "done");
  assert.equal(states(assistantSteps({ overview: overview("standard_install"), config: config(1), backups: backups(null) })).devices, "done");
});

test("only a backup that can be read counts, the newest of them", () => {
  assert.equal(latestUsableBackup({ backups: [{ created_at: "2026-10-20T08:00:00Z" }, { created_at: "2026-10-24T08:00:00Z", valid: false }] }), "2026-10-20T08:00:00Z");
  assert.equal(latestUsableBackup({ backups: [{ created_at: "2026-10-24T08:00:00Z", valid: false }] }), null);
  assert.equal(latestUsableBackup({ backups: [{ created_at: "2026-10-21T08:00:00Z" }, { created_at: "2026-10-23T08:00:00Z" }, { valid: true }] }), "2026-10-23T08:00:00Z");
  assert.equal(latestUsableBackup(null), null);
  const broken = { summary: { latest_created_at: "2026-10-24T08:00:00Z" }, backups: [{ created_at: "2026-10-24T08:00:00Z", valid: false }] };
  assert.equal(states(assistantSteps({ overview: overview("standard_install"), config: config(1), backups: broken })).backup, "todo");
});

test("the listing's records carry the validity and creation time the backup step reads", () => {
  const server = readFileSync(new URL("../../../admin/backup_restore.py", import.meta.url), "utf8");
  assert.match(server, /"valid": self\.valid/);
  assert.match(server, /"backups": \[r\.to_dict\(\) for r in records\]/);
  assert.match(server, /"created_at": /);
});

test("the dashboard is ready once EMS runs, its address is known and the settings leave it on, never done", () => {
  const on = { status: "ok", summary: { dashboard_enabled: true } };
  assert.deepEqual(pick(dashboardStep(overview("standard_install"), "http://host:8080/", on)), { state: "ready", reason: null });
  assert.deepEqual(pick(dashboardStep(overview("standard_install"), "http://host:8080/", null)), { state: "ready", reason: null });
  assert.deepEqual(pick(dashboardStep(overview("standard_install", { containers: { ems: { found: true, running: false } } }), null, on)), { state: "later", reason: "stopped" });
  assert.deepEqual(pick(dashboardStep(overview("standard_install", { containers: { ems: { running: false } } }), "http://host:8080/", on)), { state: "later", reason: "stopped" });
  const off = { status: "ok", summary: { dashboard_enabled: false } };
  assert.deepEqual(pick(dashboardStep(overview("standard_install"), "http://host:8080/", off)), { state: "off", reason: "off" });
  assert.deepEqual(pick(dashboardStep(overview("standard_install"), "http://host:8080/", { status: "invalid", summary: { dashboard_enabled: false } })), { state: "ready", reason: null });
  const unasked = overview("standard_install", { docker: { available: false }, containers: { ems: { running: false, status: "unknown" } } });
  assert.deepEqual(pick(dashboardStep(unasked, "http://host:8080/", on)), { state: "unknown", reason: "docker" });
  assert.deepEqual(pick(dashboardStep(unasked, "http://host:8080/", off)), { state: "off", reason: "off" });
  assert.deepEqual(pick(dashboardStep(unasked, null, on)), { state: "unknown", reason: "noAddress" });
  const unreadableSettings = overview("partial_install", { containers: { ems: { found: true, running: true } } });
  assert.deepEqual(pick(dashboardStep(unreadableSettings, null, null)), { state: "unknown", reason: "noAddress" });
  assert.deepEqual(pick(dashboardStep(overview("none", { containers: { ems: { found: false, running: false } } }), null, null)), { state: "later", reason: "stopped" });
  assert.equal(dashboardStep(null, null, on).state, "unknown");
  const emptyUnasked = overview("none", { docker: { available: false }, containers: { ems: { running: false, status: "unknown" } } });
  assert.deepEqual(pick(dashboardStep(emptyUnasked, null, null)), { state: "later", reason: "stopped" });
  assert.deepEqual(pick(dashboardStep(overview("compose_only", { docker: { available: false }, containers: {} }), null, null)), { state: "later", reason: "stopped" });
});

test("every state and every step has its look, and every look names a tone", () => {
  assert.deepEqual(Object.keys(STATE_LOOKS).sort(), [...ASSISTANT_STATES].sort());
  assert.deepEqual(Object.keys(STEP_LOOKS).sort(), [...STEP_IDS, "dashboard"].sort());
  for (const look of [...Object.values(STATE_LOOKS), ...Object.values(STEP_LOOKS), ...TASKS]) assert.match(look.tone, /^c-[a-z]+$/);
});

test("each task opens a page of this console, and a tab it names is one the settings page has", () => {
  const ids = PAGES.map((page) => page.id);
  for (const task of TASKS) assert.ok(ids.includes(task.page), task.id);
  const tabs = SETTINGS_PAGE.match(/const TABS = \[\.\.\.EDIT_TABS, ([^\]]*)\]/);
  assert.ok(tabs, "the settings tabs are not found");
  for (const task of TASKS.filter((entry) => entry.tab)) assert.ok(tabs[1].includes(`"${task.tab}"`), task.id);
  assert.equal(TASKS.find((task) => task.id === "reinstall").tone, "c-warn");
});

test("the tasks need the settings in place, and setting up again needs an EMS to set up over", () => {
  assert.deepEqual(assistantTasks(overview("standard_install")), TASKS);
  assert.deepEqual(assistantTasks(overview("none")), []);
  assert.deepEqual(assistantTasks(overview("partial_install")), []);
  assert.deepEqual(assistantTasks(null), []);
  const gone = assistantTasks(overview("standard_install", { containers: { ems: { found: false } } }));
  assert.deepEqual(gone.map((task) => task.id), TASKS.filter((task) => task.id !== "reinstall").map((task) => task.id));
});

test("the console opens on the assistant always while nothing is installed, otherwise until the reader turns it off", () => {
  assert.equal(opensOnAssistant(overview("none"), "off"), true);
  assert.equal(opensOnAssistant(overview("none"), "on"), true);
  assert.equal(opensOnAssistant(overview("standard_install"), "on"), true);
  assert.equal(opensOnAssistant(overview("standard_install"), "off"), false);
  assert.equal(opensOnAssistant(overview("partial_install"), "off"), false);
  assert.equal(opensOnAssistant(null, "off"), false);
  assert.equal(opensOnAssistant(null, "on"), true);
  assert.ok(WELCOME_KEY.startsWith("ems-modern."));
});

test("only the console's bare address counts as opening it; a page in the address is what the reader asked for", () => {
  for (const hash of ["", "#", "#/", undefined, null]) assert.equal(bareAddress(hash), true, String(hash));
  for (const hash of ["#/overview", "#/updates", "#/settings/devices", "#/assistant"]) assert.equal(bareAddress(hash), false, hash);
});

test("the attention count is the overview headline's: every finding once the worst is a warning or an error, none for notes alone", () => {
  const verdict = (findings, state = "standard_install") => healthVerdict({ install_state: { state }, health: { findings } }, false);
  assert.equal(attentionCount(verdict([])), 0);
  assert.equal(attentionCount(verdict([{ code: "ems_safe_mode", severity: "warning" }, { code: "settings_await_restart", severity: "info" }])), 2);
  assert.equal(attentionCount(verdict([{ code: "ems_stopped", severity: "error" }, { code: "installation_warning", severity: "warning" }, { code: "x", severity: "info" }])), 3);
  assert.equal(attentionCount(verdict([{ code: "settings_await_restart", severity: "info" }])), 0);
  assert.equal(attentionCount(verdict([{ code: "installation_incomplete", severity: "error" }], "none")), 0);
  assert.equal(attentionCount(verdict([{ code: "docker_unavailable", severity: "error" }], "none")), 1);
  assert.equal(attentionCount(verdict([{ code: "x", severity: "info" }], "none")), 0);
  assert.equal(attentionCount(null), 0);
});

test("the overview headline counts the same way, so the two never disagree", () => {
  const page = readFileSync(new URL("../src/pages/Overview.svelte", import.meta.url), "utf8");
  assert.match(page, /if \(verdict\.level === "warning" \|\| verdict\.level === "error"\) return tn\("overview\.attention", verdict\.findings\.length\)/);
});
