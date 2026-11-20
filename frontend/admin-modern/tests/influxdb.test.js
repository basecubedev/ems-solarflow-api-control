// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { EXPLAINED_STATES, PLAN_STATES, RECORD_STATES, ROLLBACK_STEPS, STEP_NOTES, UPGRADE_STEPS, imageTag, influxCanRecheck, influxCardShown, influxNeedsAttention, influxOutcome, influxProgress, influxRun, influxSettled, influxView, lastView, rollbackView, stateWords, stepView } from "../src/lib/influxdb.js";
import { STRINGS } from "../src/i18n/strings.js";

const SERVER = readFileSync(new URL("../../../admin/influx_upgrade.py", import.meta.url), "utf8");
const WIDGET = readFileSync(new URL("../src/widgets/InfluxVersion.svelte", import.meta.url), "utf8");

function serverSteps(name) {
  const block = SERVER.match(new RegExp(`${name} = \\(([\\s\\S]*?)\\n\\)`))[1];
  return [...block.matchAll(/\("([a-z_]+)", "/g)].map((match) => match[1]);
}

test("the steps are the server's, in its order", () => {
  assert.deepEqual([...UPGRADE_STEPS], serverSteps("UPGRADE_STEPS"));
  assert.deepEqual([...ROLLBACK_STEPS], serverSteps("ROLLBACK_STEPS"));
});

test("every plan state and record state the server names has words", () => {
  const planStates = new Set([...SERVER.matchAll(/"state": "([a-z_]+)"/g)].map((match) => match[1]));
  planStates.add("unavailable");
  assert.deepEqual([...planStates].filter((state) => !PLAN_STATES.includes(state)), []);
  const recordStates = [...SERVER.matchAll(/^([A-Z_]+) = "([a-z_]+)"$/gm)].filter(([, name]) => !["RECORD_FILE", "STILL_STARTING", "PUTTING_BACK"].includes(name)).map((match) => match[2]);
  assert.deepEqual([...recordStates].sort(), [...RECORD_STATES].sort());
});

test("every note the server puts on a running step has words, shown only while that step runs", () => {
  const notes = [...SERVER.matchAll(/^(?:STILL_STARTING|PUTTING_BACK) = "([a-z_]+)"$/gm)].map((match) => match[1]);
  assert.deepEqual(Object.keys(STEP_NOTES), notes);
  for (const key of Object.values(STEP_NOTES)) {
    for (const lang of ["en", "de", "nl", "fr"]) assert.ok(STRINGS[key]?.[lang], `${key} ${lang}`);
  }
  const running = stepView("upgrade", { key: "verify", state: "running", note: "still_starting", message: null });
  assert.equal(running.detailKey, "influx.stillStarting");
  assert.equal(stepView("upgrade", { key: "verify", state: "done", note: "still_starting" }).detailKey, null);
  assert.equal(stepView("upgrade", { key: "verify", state: "running", note: "warp" }).detailKey, null);
});

test("the card beside the Updates list shows a run, its record and what needs explaining, never a mere offer", () => {
  const view = (plan) => influxView({ ok: true, current_image: "influxdb:2.7", target_image: "influxdb:2.9.1", ...plan });
  assert.equal(influxCardShown(view({ state: "available" })), false);
  assert.equal(influxCardShown(view({ state: "current" })), false);
  assert.equal(influxCardShown(view({ state: "newer" })), false);
  assert.equal(influxCardShown(view({ state: "available" }), { running: true }), true);
  assert.equal(influxCardShown(view({ state: "current" }), { finished: true }), true);
  const last = { state: "upgraded", from_image: "influxdb:2.7", to_image: "influxdb:2.9.1", backup: "b.tar.gz" };
  assert.equal(influxCardShown(view({ state: "current", last })), true);
  assert.equal(influxCardShown(view({ state: "current", rollback: { available: true, to_image: "influxdb:2.7" } })), true);
  for (const state of EXPLAINED_STATES) assert.equal(influxCardShown(view({ state })), true, state);
  for (const state of ["disabled", "external", "not_installed"]) assert.equal(influxCardShown(view({ state, last })), false, state);
  assert.equal(influxCardShown(null), false);
  assert.equal(influxCardShown(null, { unreadable: true }), true);
  assert.deepEqual(EXPLAINED_STATES.filter((state) => !PLAN_STATES.includes(state)), []);
});

test("every key the card can ask for exists in every language", () => {
  const keys = [
    ...PLAN_STATES.map((state) => `influx.state.${state}`),
    ...RECORD_STATES.map((state) => `influx.last.${state}`),
    ...UPGRADE_STEPS.map((step) => `influx.step.upgrade.${step}`),
    ...ROLLBACK_STEPS.map((step) => `influx.step.rollback.${step}`),
    ...["upgrade", "rollback"].flatMap((kind) => [`influx.progress.${kind}`, `influx.outcome.${kind}.done`, `influx.outcome.${kind}.failed`, `influx.working.${kind}`]),
    ...[...WIDGET.matchAll(/t\("(influx\.[A-Za-z.]+)"/g)].map((match) => match[1]),
  ];
  for (const key of keys) {
    assert.ok(STRINGS[key], key);
    for (const lang of ["en", "de", "nl", "fr"]) assert.ok(STRINGS[key][lang], `${key} ${lang}`);
  }
});

test("an upgrade is offered only where the server offers one, with an image to confirm", () => {
  const plan = { ok: true, state: "available", current_image: "influxdb:2.7", target_image: "influxdb:2.9.1", current_version: "2.7", target_version: "2.9.1" };
  assert.deepEqual(
    [influxView(plan).canUpgrade, influxView(plan).current, influxView(plan).target, influxView(plan).targetImage],
    [true, "2.7", "2.9.1", "influxdb:2.9.1"],
  );
  assert.equal(influxView({ ...plan, state: "current" }).canUpgrade, false);
  assert.equal(influxView({ ...plan, target_image: null }).canUpgrade, false);
  assert.equal(influxView({ ...plan, state: "warp" }).state, "unavailable");
  assert.equal(influxView({ ok: false }), null);
  assert.equal(influxView(null), null);
});

test("analytics that is off or external shows no card of its own", () => {
  assert.equal(influxView({ ok: true, state: "disabled" }).shown, false);
  assert.equal(influxView({ ok: true, state: "external" }).shown, false);
  assert.equal(influxView({ ok: true, state: "unmanaged" }).shown, true);
});

test("a way back is offered only with a version to go back to", () => {
  assert.deepEqual(rollbackView({ available: true, to_image: "influxdb:2.7", backup: "b.tar.gz", since: "2026-11-18T01:00:00Z" }), {
    available: true, to: "2.7", toImage: "influxdb:2.7", backup: "b.tar.gz", since: "2026-11-18T01:00:00Z", reason: "",
  });
  assert.equal(rollbackView({ available: true, to_image: "custom" }).available, false);
  assert.equal(rollbackView({ available: false, to_image: "influxdb:2.7", reason: "gone" }).reason, "gone");
  assert.equal(rollbackView(null), null);
});

test("the last run is read only in a state this build knows", () => {
  const record = { state: "rolled_back", from_image: "influxdb:2.7", to_image: "influxdb:2.9.1", backup: "b", started_at: "s", finished_at: "f", set_aside: "influxdb-2.9.1-set-aside-x" };
  assert.deepEqual(lastView({ ...record, kept: ["influxdb-2.9.1-set-aside-x", 3] }), {
    state: "rolled_back", from: "2.7", to: "2.9.1", backup: "b", at: "f", message: "", aside: "influxdb-2.9.1-set-aside-x", kept: ["influxdb-2.9.1-set-aside-x"],
  });
  assert.deepEqual(lastView(record).kept, []);
  assert.equal(lastView({ ...record, finished_at: null }).at, "s");
  assert.equal(lastView({ ...record, state: "beamed" }), null);
});

test("a step names its kind's words, and an unknown one keeps the server's", () => {
  assert.equal(stepView("upgrade", { key: "backup", state: "running" }).label, "influx.step.upgrade.backup");
  assert.equal(stepView("rollback", { key: "set_aside", state: "done", message: "data/x" }).detail, "data/x");
  const unknown = stepView("upgrade", { key: "beam", label: "Beam", state: "warp" });
  assert.deepEqual([unknown.label, unknown.text, unknown.state], [null, "Beam", "pending"]);
  assert.equal(imageTag("influxdb:2.9.1"), "2.9.1");
  assert.equal(imageTag("ghcr.io/x:1"), null);
});

test("a run's progress names the step under way and its place, so a line beside the version can say where the run is", () => {
  const steps = (states) => UPGRADE_STEPS.map((key, index) => ({ key, label: key, state: states[index] ?? "pending" }));
  const first = influxProgress("upgrade", steps([]));
  assert.deepEqual([first.step, first.total, first.current.label, first.share], [1, 7, "influx.step.upgrade.preflight", 0]);
  const backup = influxProgress("upgrade", steps(["done", "done", "done", "running"]));
  assert.deepEqual([backup.step, backup.total, backup.current.label, backup.share], [4, 7, "influx.step.upgrade.backup", 3 / 7]);
  const between = influxProgress("upgrade", steps(["done", "skipped", "done"]));
  assert.deepEqual([between.step, between.current.label], [4, "influx.step.upgrade.backup"]);
  const over = influxProgress("upgrade", steps(UPGRADE_STEPS.map(() => "done")));
  assert.deepEqual([over.step, over.share], [7, 1]);
  assert.equal(influxProgress("upgrade", steps(["done", "done", "failed"]), { ended: true }).step, 3);
  assert.equal(influxProgress("upgrade", steps(["done", "done", "failed"])).step, 4);
  assert.equal(influxProgress("upgrade", steps([]), { ended: true }).step, 1);
  const back = influxProgress("rollback", ROLLBACK_STEPS.map((key, index) => ({ key, state: index < 2 ? "done" : index === 2 ? "running" : "pending" })));
  assert.deepEqual([back.step, back.total, back.current.label], [3, ROLLBACK_STEPS.length, "influx.step.rollback.stop"]);
  const unknown = influxProgress("upgrade", [{ key: "beam", label: "Beam", state: "running" }]);
  assert.deepEqual([unknown.current.label, unknown.current.text], [null, "Beam"]);
  assert.equal(influxProgress("upgrade", []), null);
  assert.equal(influxProgress("upgrade", null), null);
});

test("a run this browser followed ends as done only when its result says so; a run under way or never started has no outcome", () => {
  const job = (fields) => ({ kind: "upgrade", running: false, result: null, error: null, ...fields });
  assert.deepEqual(influxOutcome(job({ result: { ok: true } })), { kind: "upgrade", done: true });
  assert.deepEqual(influxOutcome(job({ result: { ok: false, message: "x" } })), { kind: "upgrade", done: false });
  assert.deepEqual(influxOutcome(job({ result: {} })), { kind: "upgrade", done: false });
  assert.deepEqual(influxOutcome(job({ kind: "rollback", error: new Error("refused") })), { kind: "rollback", done: false });
  assert.equal(influxOutcome(job({ running: true, result: { ok: true } })), null);
  assert.equal(influxOutcome(job({})), null);
  assert.equal(influxOutcome(job({ kind: "", result: { ok: true } })), null);
  assert.equal(influxOutcome(null), null);
});

test("a line says a run it follows while it goes on, a run held elsewhere, and how the run ended only while the plan still shows what it did", () => {
  const view = (state, current = "2.9.1", extra = {}) => influxView({ ok: true, state, current_image: `influxdb:${current}`, target_image: "influxdb:2.9.1", ...extra });
  const job = (fields) => ({ kind: "upgrade", image: "influxdb:2.9.1", running: false, steps: [], result: null, error: null, ...fields });
  const running = influxRun(view("busy", "2.7"), job({ running: true, steps: [{ key: "backup", state: "running" }] }));
  assert.deepEqual([running.state, running.kind, running.progress.current.label], ["running", "upgrade", "influx.step.upgrade.backup"]);
  assert.equal(influxRun(view("busy", "2.7"), job({ running: true }), { followed: false }).state, "busy");
  assert.equal(influxRun(view("busy", "2.7"), job({})).state, "busy");
  assert.equal(influxRun(view("busy", "2.7"), job({ result: { ok: true } })).state, "busy");
  const ended = job({ result: { ok: true }, endedAt: 1000, steps: UPGRADE_STEPS.map((key) => ({ key, state: "done" })) });
  const readBack = influxRun(view("busy", "2.7"), ended, { askedAt: 999, reading: true });
  assert.deepEqual([readBack.state, readBack.progress.step], ["running", 7]);
  assert.equal(influxRun(view("busy", "2.7"), ended, { askedAt: 999 }).state, "busy");
  assert.equal(influxRun(view("busy", "2.7"), ended, { askedAt: 1000, reading: true }).state, "busy");
  assert.equal(influxRun(view("busy", "2.7"), ended, { followed: false, askedAt: 999, reading: true }).state, "busy");
  assert.deepEqual(influxRun(view("current"), ended, { askedAt: 999, reading: true }), { state: "done", kind: "upgrade" });
  const stopped = job({ result: { ok: false }, endedAt: 1000, steps: UPGRADE_STEPS.map((key, index) => ({ key, state: index < 3 ? "done" : index === 3 ? "failed" : "pending" })) });
  const stoppedBack = influxRun(view("busy", "2.7"), stopped, { askedAt: 999, reading: true });
  assert.deepEqual([stoppedBack.progress.step, stoppedBack.progress.current.label], [4, "influx.step.upgrade.backup"]);

  assert.deepEqual(influxRun(view("current"), job({ result: { ok: true } })), { state: "done", kind: "upgrade" });
  assert.equal(influxRun(view("current"), job({ result: { ok: true } }), { followed: false }), null);
  assert.equal(influxRun(view("current", "2.9.1", { last: { state: "failed", from_image: "influxdb:2.7", to_image: "influxdb:2.9.1", backup: "b" } }), job({ result: { ok: true } })), null);
  assert.equal(influxRun(view("available", "2.9.1", { target_image: "influxdb:2.10.0" }), job({ result: { ok: true } })), null);
  assert.equal(influxRun(view("current", "2.10.0", { target_image: "influxdb:2.10.0" }), job({ result: { ok: true } })), null);
  assert.equal(influxRun(view("not_running"), job({ result: { ok: true } })), null);

  assert.deepEqual(influxRun(view("available", "2.7"), job({ result: { ok: false } })), { state: "failed", kind: "upgrade" });
  assert.deepEqual(influxRun(view("available", "2.7"), job({ error: new Error("refused") })), { state: "failed", kind: "upgrade" });
  assert.equal(influxRun(view("available", "2.7", { target_image: "influxdb:2.10.0" }), job({ result: { ok: false } })), null);
  assert.equal(influxRun(view("current"), job({ result: { ok: false } })), null);

  assert.equal(influxRun(view("available", "2.7"), job({ kind: "rollback", image: "influxdb:2.7", result: { ok: true } })), null);
  assert.equal(influxRun(view("current"), job({ result: null, error: null })), null);
  assert.equal(influxRun(null, job({ result: { ok: true } })), null);
});

test("a step under way that says more than its name says that instead", () => {
  const putBack = influxProgress("rollback", ROLLBACK_STEPS.map((key) => ({ key, state: key === "restore" ? "running" : "done", note: key === "restore" ? "putting_back" : null })));
  assert.deepEqual([putBack.current.label, putBack.current.detailKey], ["influx.step.rollback.restore", "influx.puttingBack"]);
});

test("up to date means no run left to look at; an unfinished one asks to be looked at while nothing runs", () => {
  const view = (state, record) => influxView({ ok: true, state, current_image: "influxdb:2.9.1", target_image: "influxdb:2.9.1", last: record ? { state: record, from_image: "influxdb:2.7", to_image: "influxdb:2.9.1", backup: "b.tar.gz" } : null });
  assert.equal(influxSettled(view("current")), true);
  assert.equal(influxSettled(view("newer")), true);
  assert.equal(influxSettled(view("current", "upgraded")), true);
  assert.equal(influxSettled(view("current", "rolled_back")), true);
  for (const record of ["failed", "running", "rolling_back", "rollback_failed"]) {
    assert.equal(influxSettled(view("current", record)), false, record);
    assert.equal(influxNeedsAttention(view("current", record)), true, record);
    assert.equal(influxNeedsAttention(view("busy", record)), false, `${record} while busy`);
  }
  assert.equal(influxNeedsAttention(view("current", "upgraded")), false);
  assert.equal(influxNeedsAttention(view("available", "failed")), false);
  assert.equal(influxNeedsAttention(view("disabled", "failed")), false);
  assert.equal(influxSettled(view("available")), false);
  assert.equal(influxSettled(null), false);
});

test("a run under way has its own words, and the stop of a long wait is said in every language", () => {
  for (const state of ["running", "rolling_back"]) {
    for (const lang of ["en", "de", "nl", "fr"]) assert.ok(STRINGS[`influx.last.${state}.now`]?.[lang], `${state} ${lang}`);
  }
  for (const key of ["influx.stopWaiting", "influx.confirmStopTitle", "influx.confirmStop.ends", "influx.confirmStop.back", "influx.confirmStop.endsBack", "influx.confirmStop.putBack"]) {
    for (const lang of ["en", "de", "nl", "fr"]) assert.ok(STRINGS[key]?.[lang], `${key} ${lang}`);
  }
  assert.equal(influxView({ ok: true, state: "busy", slow: true }).slow, true);
  assert.equal(influxView({ ok: true, state: "busy" }).slow, false);
  assert.equal(influxView({ ok: true, state: "busy", live: true }).live, true);
  assert.equal(influxView({ ok: true, state: "busy" }).live, false);
});

test("checking again is offered exactly where the server offers it", () => {
  const view = (recheck) => influxView({ ok: true, state: "current", current_image: "influxdb:2.9.1", target_image: "influxdb:2.9.1", recheck });
  assert.equal(influxCanRecheck(view({ available: true, reason: null })), true);
  assert.equal(influxCanRecheck(view({ available: false, reason: "docker-compose.yml names influxdb:2.7" })), false);
  assert.equal(influxCanRecheck(view(null)), false);
  assert.equal(influxCanRecheck(view({ available: "yes" })), false);
  assert.equal(influxCanRecheck(null), false);
  for (const lang of ["en", "de", "nl", "fr"]) {
    for (const key of ["influx.recheck", "influx.recheck.done", "influx.recheck.failed"]) assert.ok(STRINGS[key]?.[lang], `${key} ${lang}`);
  }
});

test("a way back under way is dated from its own start, not from the upgrade it undoes", () => {
  const record = { from_image: "influxdb:2.7", to_image: "influxdb:2.9.1", backup: "b", started_at: "2026-11-01T10:00:00Z", finished_at: "2026-11-01T10:20:00Z", rollback_started_at: "2026-11-18T09:00:00Z" };
  assert.equal(lastView({ ...record, state: "rolling_back" }).at, "2026-11-18T09:00:00Z");
  assert.equal(lastView({ ...record, state: "upgraded" }).at, "2026-11-01T10:20:00Z");
  assert.equal(lastView({ ...record, state: "rolling_back", rollback_started_at: null }).at, "2026-11-01T10:20:00Z");
});

test("an upgrade held back for want of memory is shown as offered, with the figures to fill in", () => {
  const plan = { ok: true, state: "low_memory", current_image: "influxdb:2.7", target_image: "influxdb:2.9.1", current_version: "2.7", target_version: "2.9.1", memory: { influx_mb: 372, needed_mb: 139, available_mb: 70 } };
  const view = influxView(plan);
  assert.equal(view.canUpgrade, false);
  assert.equal(view.offered, true);
  assert.deepEqual(view.memory, { held: 372, needed: 139, available: 70 });
  assert.deepEqual(stateWords(view), { target: "2.9.1", needed: 139, available: 70, held: 372 });
  assert.equal(influxCardShown(view), true);
  assert.equal(influxNeedsAttention(influxView({ ...plan, last: { state: "failed", from_image: "influxdb:2.7", to_image: "influxdb:2.9.1" } })), false);
  assert.equal(influxView({ ...plan, memory: { influx_mb: 372, needed_mb: "x", available_mb: 70 } }).memory, null);
  assert.deepEqual(stateWords(influxView({ ...plan, memory: null })), { target: "2.9.1", needed: "—", available: "—", held: "—" });
});

test("the low-memory sentence names the version and both figures in every language", () => {
  for (const words of Object.values(STRINGS["influx.state.low_memory"])) {
    for (const slot of ["{target}", "{needed}", "{available}"]) assert.ok(words.includes(slot), `${slot} in ${words}`);
  }
});
