// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { buildActions, buildMessage, conflictService, confirmedOperation, deploymentMessage, groupReleases, initialRelease, isDevelopmentTag, jobSteps, pipelineStates, serviceKey, setupRecovery, setupTransitionActive, startMessage, startTone, stepLocked, SETUP_STEP_LOOKS } from "../src/lib/setup.js";
import { pageSet } from "../src/app/pages.js";

const releases = [
  { tag: "latest", channel: "latest" },
  { tag: "v9.9.11", channel: "stable" },
  { tag: "v9.9.12-rc1", channel: "rc" },
  { tag: "dev-main-abc1234-1-1", channel: "development" },
  { tag: "odd", channel: "nightly" },
  { tag: "v0.6.0", channel: "stable", selectable: false },
];

test("the catalogue is grouped by channel and a build in an unknown channel is never offered", () => {
  const grouped = groupReleases(releases);
  assert.deepEqual(grouped.map((group) => [group.id, group.releases.map((release) => release.tag)]), [
    ["latest", ["latest"]],
    ["stable", ["v9.9.11", "v0.6.0"]],
    ["unstable", ["v9.9.12-rc1"]],
    ["experimental", ["dev-main-abc1234-1-1"]],
  ]);
  assert.equal(initialRelease(grouped, { restored: "odd", defaultTag: "v9.9.11" }).tag, "v9.9.11");
  assert.equal(initialRelease(grouped, { restored: "latest", defaultTag: "v9.9.11" }).tag, "latest");
  assert.equal(initialRelease(grouped, { defaultTag: "gone", preparedTag: "v0.6.0" }).tag, "latest");
  assert.ok(isDevelopmentTag("dev-main-abc1234-1-1"));
  assert.ok(!isDevelopmentTag("v9.9.11"));
});

test("a running setup is resumed on its build, and its operation counts once resources are verified", () => {
  const transition = { mode: "fresh_install", system_tag: "latest", stage: "resources_verified", operation_id: "op-1" };
  assert.ok(setupTransitionActive(transition));
  assert.deepEqual(confirmedOperation(transition), { operationId: "op-1", systemTag: "latest" });
  assert.equal(confirmedOperation({ ...transition, stage: "admin_aligned" }), null);
  assert.ok(!setupTransitionActive({ ...transition, mode: "guided_upgrade" }));
  assert.ok(!setupTransitionActive({ ...transition, stage: "completed" }));
});

test("exactly one way on: verify a chosen build, update an Admin that needs it, continue a ready one", () => {
  const chosen = { tag: "latest", state: "selected", result: null, locked: false };
  assert.deepEqual(buildActions(chosen, releases, false).right, { label: "verify", enabled: true });
  assert.equal(buildActions(chosen, releases, true).right.enabled, false);
  const update = { tag: "v9.9.11", state: "valid", result: { action_state: { admin_update_allowed: true } }, locked: false };
  assert.deepEqual([buildActions(update, releases, false).left.enabled, buildActions(update, releases, false).right.enabled], [true, false]);
  const ready = { tag: "latest", state: "valid", result: { action_state: { continue_allowed: true }, alignment: "aligned" }, locked: false };
  assert.deepEqual([buildActions(ready, releases, false).left.enabled, buildActions(ready, releases, false).right.enabled], [false, true]);
  assert.equal(buildMessage(ready), "aligned");
  const failed = { tag: "latest", state: "failed", failedAction: "confirm", result: {}, locked: false };
  assert.deepEqual([buildActions(failed, releases, false).left.enabled, buildActions(failed, releases, false).right.label], [false, "tryAgain"]);
  assert.equal(buildActions({ tag: "v0.6.0", state: "selected" }, releases, false).right.enabled, false);
});

test("the pipeline marks a skipped Admin update and the failed stage of a stopped transition", () => {
  assert.deepEqual(pipelineStates({ transition: { stage: "resources_verified", admin_alignment_required: false } }).states, ["done", "done", "skipped", "skipped", "done", "active", "pending"]);
  assert.deepEqual(pipelineStates({ transition: { stage: "failed_recoverable", resume_stage: "admin_reconnect_pending" } }).states, ["done", "done", "done", "failed", "pending", "pending", "pending"]);
  assert.deepEqual(pipelineStates(null).states, Array(7).fill("pending"));
});

test("later steps open only after a confirmed build, a saved config and a prepared deployment", () => {
  assert.equal(stepLocked("release", {}), false);
  assert.equal(stepLocked("devices", { confirmed: false }), true);
  assert.equal(stepLocked("config", { confirmed: true }), false);
  assert.equal(stepLocked("deployment", { confirmed: true, generatedReady: false }), true);
  assert.equal(stepLocked("start", { confirmed: true, generatedReady: true, deploymentReady: false }), true);
});

test("the setup page has an address but no place in the menu or the phone bar", () => {
  assert.ok(pageSet.pages.some((page) => page.id === "setup"));
  assert.ok(!pageSet.listed.some((page) => page.id === "setup"));
  assert.ok(!pageSet.pinnedPages("setup,overview").includes("setup"));
});

test("a deployment says what holds it up, in Classic's order", () => {
  const deploy = { status: "idle", ready: false, dockerReady: true, docker: null, phase: "" };
  assert.deepEqual(deploymentMessage({ ...deploy, status: "running", phase: "Pulling" }, { tag: "v1", generatedReady: true }), { raw: "Pulling" });
  assert.deepEqual(deploymentMessage({ ...deploy, ready: true }, { tag: "v1", generatedReady: true }), { key: "ready" });
  assert.deepEqual(deploymentMessage(deploy, { tag: "v1", generatedReady: false }), { key: "noConfig" });
  assert.deepEqual(deploymentMessage({ ...deploy, dockerReady: false, docker: { message: "No socket" } }, { tag: "v1", generatedReady: true }), { raw: "No socket" });
  assert.deepEqual(deploymentMessage(deploy, { tag: "v1", generatedReady: true }), { key: "auto" });
});

test("starting says a conflict before a failure, and a run before what is missing", () => {
  const start = { status: "idle", conflict: null, running: false, phase: "" };
  assert.deepEqual(startMessage({ ...start, status: "failed", conflict: {} }, { prepared: true, dockerReady: true }), { key: "conflict" });
  assert.deepEqual(startMessage({ ...start, running: true }, { prepared: false, dockerReady: false }), { key: "running" });
  assert.deepEqual(startMessage(start, { prepared: false, dockerReady: true }), { key: "notPrepared" });
  assert.deepEqual(startMessage(start, { prepared: true, dockerReady: true }), { key: "ready" });
});

test("the start card's mark never stays green beside a start that failed or cannot begin", () => {
  const start = { status: "idle", conflict: null, running: false, phase: "", error: "", errorCode: "" };
  const toneOf = (patch, context = { prepared: true, dockerReady: true }) => startTone({ ...start, ...patch }, startMessage({ ...start, ...patch }, context));
  assert.equal(toneOf({ status: "failed", error: "compose up exited 1" }), "c-fault");
  assert.equal(toneOf({ status: "failed", errorCode: "workspace_permission_denied", error: "Permission denied" }), "c-fault");
  assert.equal(toneOf({ status: "failed", conflict: {} }), "c-warn");
  assert.equal(toneOf({}, { prepared: false, dockerReady: true }), "c-warn");
  assert.equal(toneOf({ error: "Docker is not reachable" }, { prepared: true, dockerReady: false }), "c-warn");
  assert.equal(toneOf({ error: "Could not read container status from Docker Compose." }), "c-warn");
  assert.equal(toneOf({ error: "Docker is not reachable" }, { prepared: true, dockerReady: false, docker: { message: "No socket" } }), "c-warn");
  assert.equal(toneOf({ status: "running", phase: "Pulling" }), SETUP_STEP_LOOKS.start.tone);
  assert.equal(toneOf({}), SETUP_STEP_LOOKS.start.tone);
});

test("job steps keep the server's labels; an unknown state waits; services are named by what they are", () => {
  assert.deepEqual(jobSteps([{ label: "Pull EMS", status: "done" }, { label: "Odd", status: "weird" }]).map((step) => [step.text, step.state, step.icon]), [["Pull EMS", "done", "circle-check"], ["Odd", "pending", "circle"]]);
  assert.deepEqual([serviceKey("ems-solarflow-admin"), serviceKey("other")], ["admin", null]);
  assert.deepEqual([conflictService({ container_name: "ems-influxdb" }), conflictService({ container_name: "ems" })], ["influxdb", "ems"]);
});

test("a stuck setup transition offers resume when allowed and discard only with its worker proven stopped", () => {
  const stuck = { mode: "fresh_install", stage: "failed_recoverable", operation_id: "op", resume_available: true, cancel_available: true, worker_active: false, worker_status_available: true };
  assert.deepEqual(setupRecovery(stuck), { reason: "partial", error: "", canResume: true, canDiscard: true });
  assert.equal(setupRecovery({ ...stuck, mode: "guided_upgrade" }), null);
  assert.equal(setupRecovery({ ...stuck, stage: "admin_aligned" }), null);
  assert.deepEqual(setupRecovery({ ...stuck, stage: "validated", expired: true, worker_active: true }), { reason: "expiredRunning", error: "", canResume: true, canDiscard: false });
  assert.equal(setupRecovery({ ...stuck, worker_status_available: false }).canDiscard, false);
  assert.deepEqual(setupRecovery({ ...stuck, error_message: "Pull failed", resume_available: false }), { reason: null, error: "Pull failed", canResume: false, canDiscard: true });
  assert.equal(setupRecovery({ ...stuck, stage: "admin_reconnect_pending", replacement_active: false }).reason, "replacementGone");
});
