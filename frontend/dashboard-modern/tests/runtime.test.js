// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { changedPayload, changeWarning, deviceEndpoint, endpointDevice, acRole, deviceSettings, reseed, withDefault, SYSTEM_ENDPOINT } from "../src/lib/runtime.js";

const fields = [
  { name: "enabled", type: "toggle" },
  { name: "max_power", type: "number" },
  { name: "pv_priority_factor", type: "number" },
  { name: "offgrid_socket_mode", type: "select" },
  { name: "_role_reason", type: "readonly" },
];

test("only changed fields are sent, numbers as numbers", () => {
  const initial = { enabled: true, max_power: 800, pv_priority_factor: 1, offgrid_socket_mode: "off" };
  assert.deepEqual(changedPayload(fields, { ...initial }, initial), {});
  assert.deepEqual(changedPayload(fields, { ...initial, max_power: "600", pv_priority_factor: 1.25 }, initial), { max_power: 600, pv_priority_factor: 1.25 });
  assert.deepEqual(changedPayload(fields, { ...initial, enabled: false }, initial), { enabled: false });
});

test("an emptied number is not sent", () => {
  const initial = { max_power: 800 };
  assert.deepEqual(changedPayload(fields, { max_power: "" }, initial), {});
});

test("emptying the AC charge power asks first, as the device then charges at its maximum", () => {
  const runtime = { system: { enabled: true }, devices: { WR1: { runtime_role: "ac_input", ac_charge_power_w: 300 } } };
  assert.equal(changeWarning(deviceEndpoint("WR1"), { ac_charge_power_w: null }, runtime)[0].key, "runtime.warn.acPowerMax");
  assert.equal(changeWarning(deviceEndpoint("WR1"), { ac_charge_power_w: 500 }, runtime), null);
  assert.equal(changeWarning(deviceEndpoint("WR1"), { runtime_role: "ac_input", ac_charge_power_w: null }, runtime)[0].key, "runtime.warn.acInput");
  assert.equal(changeWarning(deviceEndpoint("WR1"), { runtime_role: "ac_output", ac_charge_power_w: null }, runtime), null);
  const output = { system: { enabled: true }, devices: { WR1: { runtime_role: "ac_output", ac_charge_power_w: 300 } } };
  assert.equal(changeWarning(deviceEndpoint("WR1"), { ac_charge_power_w: null }, output), null);
  const both = changeWarning(deviceEndpoint("WR1"), { ac_charge_power_w: null, offgrid_socket_mode: "standard" }, runtime).map((item) => item.key);
  assert.deepEqual(both, ["runtime.warn.offgrid", "runtime.warn.acPowerMax"]);
});

test("an emptied number with a default goes out as that default, and not at all where it already holds it", () => {
  const minimum = [withDefault({ name: "min_output_limit", type: "number", min: 0, max: 800 }, 35)];
  assert.deepEqual(changedPayload(minimum, { min_output_limit: "" }, { min_output_limit: 40 }), { min_output_limit: 35 });
  assert.deepEqual(changedPayload(minimum, { min_output_limit: null }, { min_output_limit: 40 }), { min_output_limit: 35 });
  assert.deepEqual(changedPayload(minimum, { min_output_limit: "" }, { min_output_limit: 35 }), {});
  assert.deepEqual(changedPayload(minimum, { min_output_limit: "50" }, { min_output_limit: 40 }), { min_output_limit: 50 });
});

test("a number with no stored value that is left empty is not sent, default or not", () => {
  const care = [{ name: "enabled", type: "toggle" }, withDefault({ name: "min_discharge_w", type: "number", min: 0, max: 5000 }, 50)];
  assert.deepEqual(changedPayload(care, { enabled: false, min_discharge_w: "" }, { enabled: true }), { enabled: false });
  assert.deepEqual(changedPayload(care, { enabled: true, min_discharge_w: "" }, { enabled: true, min_discharge_w: null }), {});
});

test("a field takes a default only when it is a number within its bounds", () => {
  const total = { name: "max_total_power", type: "number", min: 0, max: 600 };
  assert.equal(withDefault(total, 800), total);
  assert.equal(withDefault(total, undefined), total);
  assert.equal(withDefault(total, "35"), total);
  assert.equal(withDefault({ ...total, min: 100 }, 50).default, undefined);
  assert.equal(withDefault(total, 600).default, 600);
  assert.equal(withDefault({ name: "pv_priority_factor", type: "number", min: 0.01, max: 100 }, 1).default, 1);
});

test("an emptied clearable number goes out as null, and an empty one left empty not at all", () => {
  const power = [{ name: "ac_charge_power_w", type: "number", clearable: true }];
  assert.deepEqual(changedPayload(power, { ac_charge_power_w: "" }, { ac_charge_power_w: 600 }), { ac_charge_power_w: null });
  assert.deepEqual(changedPayload(power, { ac_charge_power_w: "" }, { ac_charge_power_w: null }), {});
  assert.deepEqual(changedPayload(power, { ac_charge_power_w: "0" }, { ac_charge_power_w: null }), { ac_charge_power_w: 0 });
  assert.deepEqual(changedPayload([withDefault(power[0], 300)], { ac_charge_power_w: "" }, { ac_charge_power_w: 600 }), { ac_charge_power_w: null });
  assert.equal(withDefault(power[0], 300).default, undefined);
});

test("a stored value that changes under an untouched field is shown and never written back", () => {
  const before = { enabled: true, max_power: 800, pv_priority_factor: 1, offgrid_socket_mode: "off" };
  const seeded = reseed(fields, { initial: before, fresh: true });
  const edited = { ...seeded.values, pv_priority_factor: 3 };
  const after = { ...before, enabled: false, max_power: 600 };
  const shown = reseed(fields, { values: edited, baseline: seeded.baseline, initial: after });
  assert.equal(shown.values.enabled, false);
  assert.equal(shown.values.max_power, 600);
  assert.equal(shown.values.pv_priority_factor, 3);
  assert.deepEqual(changedPayload(fields, shown.values, shown.baseline), { pv_priority_factor: 3 });
});

test("an edited field keeps the edit when its stored value changes too", () => {
  const before = { max_power: 800 };
  const seeded = reseed(fields, { initial: before, fresh: true });
  const shown = reseed(fields, { values: { ...seeded.values, max_power: "700" }, baseline: seeded.baseline, initial: { max_power: 600 } });
  assert.equal(shown.values.max_power, "700");
  assert.deepEqual(changedPayload(fields, shown.values, shown.baseline), { max_power: 700 });
});

test("device names survive the endpoint round trip", () => {
  assert.equal(endpointDevice(deviceEndpoint("WR 1/Garage")), "WR 1/Garage");
});

test("legacy AC role names map to input or output", () => {
  assert.equal(acRole({ runtime_role: "ac_input_charge" }), "ac_input");
  assert.equal(acRole({ runtime_role: "reserved" }), "ac_input");
  assert.equal(acRole({}), "ac_output");
});

test("switching the EMS off always asks first", () => {
  const items = changeWarning(SYSTEM_ENDPOINT, { enabled: false }, { devices: {} });
  assert.equal(items[0].key, "runtime.warn.systemOff");
  assert.equal(changeWarning(SYSTEM_ENDPOINT, { max_total_power: 600 }, {}), null);
});

test("taking a device out of control, AC input and the off-grid socket ask first", () => {
  const runtime = { system: { enabled: true }, devices: { WR1: { enabled: true } } };
  assert.equal(changeWarning(deviceEndpoint("WR1"), { enabled: false }, runtime)[0].key, "runtime.warn.deviceOff");
  assert.equal(changeWarning(deviceEndpoint("WR1"), { runtime_role: "ac_input" }, runtime)[0].key, "runtime.warn.acInput");
  assert.equal(changeWarning(deviceEndpoint("WR1"), { offgrid_socket_mode: "eco" }, runtime)[0].key, "runtime.warn.offgrid");
  assert.equal(changeWarning(deviceEndpoint("WR1"), { max_power: 600 }, runtime), null);
});

test("a change while the EMS is off says when it takes effect", () => {
  const runtime = { system: { enabled: false }, devices: { WR1: { enabled: true } } };
  const keys = changeWarning(deviceEndpoint("WR1"), { offgrid_socket_mode: "eco" }, runtime).map((item) => item.key);
  assert.ok(keys.includes("runtime.warn.emsOff"));
});

test("device settings: offered ones, plus any the device still has a value for", () => {
  const catalogue = [{ key: "light", values: [] }, { key: "fan", values: [] }, { key: "buzzer", values: [] }];
  const settings = deviceSettings({ buzzer: "on" }, catalogue, ["light"]);
  assert.deepEqual(settings.map((s) => [s.key, s.offered]), [["light", true], ["buzzer", false]]);
});
