// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { normalizeDevice, deviceEntries, firmwareFacts, acPath, fleetSummary, normalizedDevices, assistView } from "../src/lib/devices.js";

test("a field the device did not report stays null instead of becoming 0", () => {
  const device = normalizeDevice("WR1", { online: true });
  assert.equal(device.pvW, null);
  assert.equal(device.outputW, null);
  assert.equal(device.batteryW, null);
  assert.equal(device.soc, null);
});

test("a device that reports no SoC has no SoC", () => {
  const device = normalizeDevice("E3DC", { online: true, soc: 0, soc_reported: false });
  assert.equal(device.socReported, false);
  assert.equal(device.soc, null);
});

test("SoC is clamped to 0..100", () => {
  assert.equal(normalizeDevice("x", { soc: 120 }).soc, 100);
  assert.equal(normalizeDevice("x", { soc: -3 }).soc, 0);
});

test("devices arrive as an object or an array", () => {
  assert.deepEqual(deviceEntries({ devices: { A: {}, B: {} } }).map(([name]) => name), ["A", "B"]);
  assert.deepEqual(deviceEntries({ devices: [{ device: "X" }, {}] }).map(([name]) => name), ["X", "Device 2"]);
});

test("the AC path uses acStatus for active states and acMode only for the standby direction", () => {
  assert.equal(acPath({ ac_status: 2, ac_mode: 2 }).key, "fw.acPath.charging");
  assert.equal(acPath({ ac_status: 1 }).key, "fw.acPath.output");
  assert.equal(acPath({ ac_status: 0, ac_mode: 1 }).key, "fw.acPath.chargeStandby");
  assert.equal(acPath({ ac_status: 9 }).key, "fw.unknownValue");
});

test("an unknown firmware value keeps its raw number", () => {
  const facts = firmwareFacts({ soc_limit: 7, pack_state: 1, ac_status: 0, dc_status: 2, grid_state: 1 });
  const guard = facts.find((fact) => fact.label === "fw.label.socGuard");
  assert.deepEqual(guard.value, { key: "fw.unknownValue", vars: { value: "7" } });
  assert.ok(!facts.some((fact) => fact.label === "fw.label.socCalibration"));
});

test("the fleet counts enabled devices only", () => {
  const fleet = fleetSummary(normalizedDevices({ devices: { A: { online: true }, B: { online: false }, C: { online: false, enabled: false } } }));
  assert.deepEqual(fleet, { total: 2, online: 1, offline: ["B"], disabled: ["C"] });
});

test("the full-charge assist names the facts Classic's card names for each status", () => {
  const labels = (assist) => assistView(assist).facts.map((fact) => fact.label);
  assert.deepEqual(labels({ status: "active", ac_mode: 1, ac_status: 2, restore_pending: true }), ["assist.started", "assist.acCharge", "assist.firmware", "assist.afterCharge"]);
  assert.deepEqual(assistView({ status: "active", ac_mode: 2, ac_status: 1 }).description, ["assist.desc.active"]);
  assert.deepEqual(labels({ status: "waiting" }), ["assist.started", "assist.firmware"]);
  assert.deepEqual(labels({ status: "unavailable" }), ["assist.last"]);
  assert.deepEqual(assistView({ status: "overdue", days_until_due: -3 }).facts[0], { label: "assist.overdueBy", days: 3 });
  assert.deepEqual(labels({ status: "window", days_until_due: 2 }), ["assist.dueIn", "assist.windowStarts"]);
  assert.deepEqual(labels({ status: "ok" }), ["assist.last", "assist.next"]);
  const restore = assistView({ status: "restore_pending", restore_pending: true, ac_mode_restore_pending: true });
  assert.deepEqual(restore.facts.map((fact) => fact.label), ["assist.last", "assist.next", "assist.maxSocRestore", "assist.acOutputMode"]);
  assert.deepEqual(restore.description, ["assist.desc.restoreMaxSoc", "assist.desc.restoreAcMode", "assist.desc.restoreWaits"]);
  assert.equal(assistView({ status: "something" }).status, "unknown");
});
