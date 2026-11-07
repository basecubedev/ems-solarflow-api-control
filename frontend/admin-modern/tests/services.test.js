// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ApiError } from "../../shared/lib/api.js";
import { DISPLAY_STATES, SYNC_ACTIONS, CONFIG_STATES, serviceView, plannedChanges, canSync, partlyDone, planMessage, stepView, serviceOutcome, inEffectAfter, planRefusal, restartAfter } from "../src/lib/services.js";

const SERVER = readFileSync(new URL("../../../admin/container_actions.py", import.meta.url), "utf8");

test("every display state the plan can name is known here", () => {
  const named = new Set([...SERVER.matchAll(/"display_state": "([a-z_]+)"/g)].map((match) => match[1]));
  for (const state of ["running", "stopped", "missing"]) named.add(state);
  assert.deepEqual([...named].filter((state) => !DISPLAY_STATES.includes(state)).sort(), []);
  const configStates = [...SERVER.matchAll(/^CONFIG_STATE_\w+ = "([a-z]+)"/gm)].map((match) => match[1]);
  assert.deepEqual(configStates.sort(), [...CONFIG_STATES].sort());
});

test("every action a sync or a repair runs has words here", () => {
  const actions = new Set([...SERVER.matchAll(/ServiceAction\(\s*(\w+_SERVICE),\s*"([a-z]+)"/g)].map((match) => `${match[1] === "EMS_SERVICE" ? "ems" : "influxdb"}:${match[2]}`));
  for (const match of SERVER.matchAll(/self\._run_analytics_step\(\s*"([a-z]+)"/g)) actions.add(`influxdb:${match[1]}`);
  actions.add("influxdb:repair");
  const real = [...actions].filter((id) => !/:(none|unavailable)$/.test(id));
  assert.ok(real.length >= 4, real.join());
  assert.deepEqual(real.filter((id) => !SYNC_ACTIONS.includes(id)).sort(), []);
});

test("planned changes in the server's order, the EMS one worded by what the settings state says", () => {
  const plan = {
    available: true,
    config_state: { state: "pending" },
    actions: [
      { service: "influxdb", action: "start" },
      { service: "ems", action: "recreate" },
      { service: "influxdb", action: "teleport", label: "Teleport" },
      { service: "ems", action: "none" },
    ],
  };
  assert.deepEqual(plannedChanges(plan), [{ key: "services.change.influxdb:start" }, { key: "services.change.ems:recreate.pending" }, { text: "Teleport" }]);
  assert.equal(canSync(plan), true);
  assert.equal(canSync({ ...plan, available: false }), false);
  assert.equal(canSync({ available: true, actions: [] }), false);
  assert.deepEqual(plannedChanges({ actions: [{ service: "ems", action: "recreate" }] }), [{ key: "services.change.ems:recreate.unknown" }]);
});

test("a service view keeps the server's label only for a state it does not know", () => {
  const plan = { services: { ems: { display_state: "running" }, influxdb: { display_state: "warp", display_label: "Warp" } }, current: { ems: { running: true, name: "ems", started_at: "t" } } };
  assert.deepEqual(serviceView(plan, "ems"), { service: "ems", state: "running", label: null, tone: "c-ok", name: "ems", startedAt: "t", configured: true });
  const influx = serviceView(plan, "influxdb");
  assert.equal(influx.state, "unknown");
  assert.equal(influx.label, "Warp");
  assert.deepEqual(stepView({ service: "ems", action: "recreate", status: "ok" }), { key: "services.step.ems:recreate", text: "", ok: true, detail: "" });
  assert.deepEqual(stepView({ service: "x", action: "y", status: "error", detail: "boom" }), { key: null, text: "x:y", ok: false, detail: "boom" });
});

test("a finished run that warned or had a step fail is only partly done", () => {
  assert.equal(partlyDone({ status: "completed", steps: [{ status: "ok" }], warnings: [] }), false);
  assert.equal(partlyDone({ status: "completed_with_warnings", steps: [{ status: "ok" }], warnings: [] }), true);
  assert.equal(partlyDone({ status: "completed", steps: [{ status: "ok" }, { status: "error" }] }), true);
  assert.equal(partlyDone({ status: "completed", warnings: ["Could not start bundled InfluxDB"] }), true);
});

test("why a plan cannot run: the Docker sentence in words, any other cause as the server wrote it", () => {
  assert.deepEqual(planMessage("Docker/Compose is not available. Re-check the Admin deployment."), { key: "services.unavailable", text: "Docker/Compose is not available. Re-check the Admin deployment." });
  const broken = "config/config.json cannot be read: Expecting value. Fix the file or restore a backup; containers are left as they are.";
  assert.deepEqual(planMessage(broken), { key: null, text: broken });
  assert.equal(planMessage(undefined), null);
});

test("a finished restart is said by its tone: done, partly done, refused or without an answer", () => {
  const t = (key) => key;
  assert.equal(serviceOutcome(null, t), null);
  const done = serviceOutcome({ kind: "sync", result: { status: "completed", steps: [{ service: "ems", action: "recreate", status: "ok" }] }, error: null, at: 5 }, t);
  assert.deepEqual([done.tone, done.message, done.at, done.steps.length], ["ok", "services.synced", 5, 1]);
  assert.equal(serviceOutcome({ kind: "sync", result: { status: "completed", warnings: ["late"] }, error: null }, t).tone, "partial");
  const refused = serviceOutcome({ kind: "sync", result: { status: "busy" }, error: new ApiError(409, { status: "busy" }) }, t);
  assert.deepEqual([refused.tone, refused.message], ["fault", "services.status.busy"]);
  assert.equal(serviceOutcome({ kind: "repair", result: null, error: new TypeError("Failed to fetch") }, t).message, "error.noAnswer");
});

test("only a plan asked for after a save or a restart can say whether the settings are in effect", () => {
  const current = { config_state: { state: "current" } };
  assert.equal(inEffectAfter(current, 200, 100), true);
  assert.equal(inEffectAfter({ config_state: { state: "pending" } }, 200, 100), false);
  assert.equal(inEffectAfter({ config_state: { state: "unknown" } }, 200, 100), null);
  assert.equal(inEffectAfter(current, 100, 100), null);
  assert.equal(inEffectAfter(current, 0, 100), null);
});

test("a change counts only a restart started after it as its own, and not a success a later plan says left it out of effect", () => {
  const t = (key) => key;
  const done = { kind: "sync", result: { status: "completed", steps: [] }, error: null, startedAt: 150, at: 300 };
  const pending = { config_state: { state: "pending" } };
  const current = { config_state: { state: "current" } };
  assert.equal(restartAfter(done, { since: 200, shown: 0 }, current, 400, t), null);
  assert.equal(restartAfter(done, { since: 100 }, current, 400, t).tone, "ok");
  assert.equal(restartAfter(done, { since: 100 }, pending, 400, t), null);
  assert.equal(restartAfter(done, { since: 100 }, pending, 250, t).tone, "ok");
  assert.equal(restartAfter({ ...done, kind: "repair" }, { since: 100 }, current, 400, t), null);
  const failed = { kind: "sync", result: { status: "busy" }, error: new ApiError(409, { status: "busy" }), startedAt: 150, at: 300 };
  assert.equal(restartAfter(failed, { since: 100 }, pending, 400, t).tone, "fault");
  assert.equal(restartAfter(null, { since: 100 }, current, 400, t), null);
});

test("where no change is known, an offer counts the restart that ended after it appeared, one already running included", () => {
  const t = (key) => key;
  const failed = { kind: "sync", result: { status: "busy" }, error: new ApiError(409, { status: "busy" }), startedAt: 150, at: 300 };
  assert.equal(restartAfter(failed, { shown: 200 }, null, 0, t).tone, "fault");
  assert.equal(restartAfter(failed, { shown: 300 }, null, 0, t), null);
  const done = { ...failed, result: { status: "completed", steps: [] }, error: null };
  assert.equal(restartAfter(done, { shown: 200 }, { config_state: { state: "current" } }, 400, t).tone, "ok");
  assert.equal(restartAfter(done, { shown: 200 }, { config_state: { state: "pending" } }, 400, t), null);
});

test("a plan that cannot restart anything says why, in the server's words when they are not the Docker sentence", () => {
  const t = (key) => key;
  assert.equal(planRefusal(null, t), null);
  assert.equal(planRefusal({ available: true }, t), null);
  assert.deepEqual(planRefusal({ available: false, message: "Docker/Compose is not available. Re-check the Admin deployment." }, t), { text: "services.unavailable" });
  const broken = "config/config.json cannot be read: Expecting value. Fix the file or restore a backup; containers are left as they are.";
  assert.deepEqual(planRefusal({ available: false, message: broken }, t), { text: broken, lang: "en" });
});
