// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CHECK_IDS, CHECK_STATUSES, RUN_MODES, checkView, summaryTone, unavailableView } from "../src/lib/diagnostics.js";

const SERVER = readFileSync(new URL("../../../admin/ems_cli.py", import.meta.url), "utf8");

test("the checks, their statuses and the run modes are the server's", () => {
  const order = SERVER.match(/CHECK_ORDER = \(([^)]*)\)/)[1];
  assert.deepEqual([...order.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]), [...CHECK_IDS]);
  const buckets = SERVER.match(/_SUMMARY_BUCKET = \{([\s\S]*?)\n\}/)[1];
  const statuses = [...buckets.matchAll(/^\s+"([a-z_]+)":/gm)].map((match) => match[1]);
  assert.deepEqual(statuses.sort(), Object.keys(CHECK_STATUSES).sort());
  const modes = new Set([...SERVER.matchAll(/"mode": "([a-z]+)"/g)].map((match) => match[1]));
  assert.deepEqual([...modes].filter((mode) => !RUN_MODES.includes(mode)), []);
});

test("a check says in one line what happened, the server's sentence first", () => {
  assert.equal(checkView({ id: "influx_status", status: "disabled", message: "InfluxDB is disabled" }).message, "InfluxDB is disabled");
  assert.equal(checkView({ id: "runtime_status", status: "timeout" }).messageKey, "diagnostics.timedOut");
  assert.equal(checkView({ id: "x", status: "failed", stderr: "\n  boom\nmore", stdout: "out" }).message, "boom");
  assert.equal(checkView({ id: "x", status: "failed", stdout: "only out" }).message, "only out");
  assert.equal(checkView({ id: "x", status: "unavailable" }).messageKey, "diagnostics.couldNotRun");
  assert.equal(checkView({ id: "x", status: "failed", exit_code: 3 }).messageKey, "diagnostics.exitCode");
  assert.equal(checkView({ id: "x", status: "ok", stderr: "noise" }).message, "");
  assert.equal(checkView({ id: "x", status: "made_up" }).status, "not_run");
});

test("the EMS diagnosis inside the quick check is handed on, nothing else is", () => {
  const diagnosis = { status: "ok", sections: [] };
  assert.equal(checkView({ id: "quick_diagnose", status: "ok", json: { diagnosis } }).diagnosis, diagnosis);
  assert.equal(checkView({ id: "influx_status", status: "ok", json: { diagnosis } }).diagnosis, null);
  assert.equal(checkView({ id: "quick_diagnose", status: "failed", json: null }).diagnosis, null);
  assert.equal(checkView({ id: "x", status: "failed", stdout: "a", stderr: "b" }).raw, "a\n\nstderr:\nb");
  assert.equal(summaryTone({ status: "failed" }), "c-fault");
  assert.equal(summaryTone(null), "c-idle");
});

test("no EMS to check is said in words; a check runner that failed is not passed off as that", () => {
  assert.deepEqual(unavailableView("EMS CLI diagnostics are unavailable because no running EMS container or local emsctl.py was found."), { key: "diagnostics.unavailable", text: "" });
  assert.deepEqual(unavailableView(undefined), { key: "diagnostics.unavailable", text: "" });
  assert.deepEqual(unavailableView("EMS CLI diagnostics could not be run."), { key: null, text: "EMS CLI diagnostics could not be run." });
});
