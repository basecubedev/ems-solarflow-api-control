// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CONTROL_STATUSES, CONTROL_TRANSPORTS, controlView } from "../src/lib/control.js";

const SERVER = readFileSync(new URL("../../../admin/maintenance_config.py", import.meta.url), "utf8");

test("every control status the server can name has a tone here", () => {
  const body = SERVER.slice(SERVER.indexOf("def _control_state"));
  const statuses = new Set([...body.slice(0, body.indexOf("\ndef ", 10)).matchAll(/status = "([a-z_]+)"/g)].map((match) => match[1]));
  assert.ok(statuses.size >= 6);
  assert.deepEqual([...statuses].filter((status) => !Object.hasOwn(CONTROL_STATUSES, status)), []);
  const gates = SERVER.match(/CONTROL_GATE_ORDER = \(([^)]*)\)/)[1];
  assert.deepEqual([...gates.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]), [...CONTROL_TRANSPORTS]);
});

test("an unknown or missing state is unknown, never healthy", () => {
  for (const control of [null, undefined, [], {}, { status: "brand_new" }, { status: "unknown" }]) {
    const view = controlView(control);
    assert.equal(view.status, "unknown");
    assert.deepEqual(view.transports, []);
    assert.deepEqual(view.notes, [{ key: "control.note.provenance" }]);
  }
});

test("transports say whether they may write and how many devices use them", () => {
  const view = controlView({
    status: "may_control",
    state_reconciliation: true,
    placeholder_fields: ["devices[0].ip", "", 3],
    transports: [
      { control_gate: "api", armed: true, device_count: 2 },
      { control_gate: "mqtt_local", armed: true, device_count: 0 },
      { control_gate: "mqtt_zendure", armed: false, device_count: 1 },
      { control_gate: "carrier_pigeon", armed: true, device_count: 9 },
    ],
    envelope: { total_output_w: 1600, device_output_w: 800, soc_min: 10, soc_max: 100, soc_uniform: false },
  });
  assert.deepEqual(view.transports.map((entry) => [entry.gate, entry.state, entry.devices]), [["api", "writing", 2], ["mqtt_local", "idle", 0], ["mqtt_zendure", "blocked", 1]]);
  assert.deepEqual(view.envelope, { totalW: 1600, totalLimitW: 1600, deviceW: 800, socMin: 10, socMax: 100, socUniform: false });
  assert.deepEqual(view.notes.map((note) => note.key), ["control.note.placeholders", "control.note.singleController", "control.note.stateReconciliation", "control.note.provenance"]);
  assert.equal(view.notes[0].vars.fields, "devices[0].ip");
});

test("an envelope that is not a number is not shown as one", () => {
  const view = controlView({ status: "disabled", envelope: { total_output_w: "1600", device_output_w: null, soc_min: true } });
  assert.deepEqual(view.envelope, { totalW: null, totalLimitW: null, deviceW: null, socMin: null, socMax: null, socUniform: true });
});

test("the total shown is the operating value the EMS runs, never above its limit", () => {
  const control = { status: "may_control", enabled: true, transports: [{ control_gate: "api", armed: true, device_count: 2 }], envelope: { total_output_w: 1600 } };
  assert.deepEqual([controlView(control, { "system.max_total_power": 800 }).envelope.totalW, controlView(control, { "system.max_total_power": 800 }).envelope.totalLimitW], [800, 1600]);
  assert.equal(controlView(control, { "system.max_total_power": 2400 }).envelope.totalW, 1600);
  assert.equal(controlView(control, {}).envelope.totalW, 1600);
  assert.equal(controlView({ ...control, status: "disabled" }, { "system.max_total_power": 800 }).status, "disabled");
});

test("a device whose control is off on the dashboard is noted, since the counts come from the file", () => {
  const control = { status: "may_control", enabled: true, transports: [{ control_gate: "api", armed: true, device_count: 1 }], envelope: {} };
  assert.ok(controlView(control, { devices: { Terrace: { max_power: 800, control: false } } }).notes.some((note) => note.key === "control.note.devicesOff"));
  assert.ok(!controlView(control, { devices: { Terrace: { max_power: 800, control: true } } }).notes.some((note) => note.key === "control.note.devicesOff"));
});
