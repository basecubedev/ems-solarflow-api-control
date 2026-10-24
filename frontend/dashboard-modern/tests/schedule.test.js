// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { newDraft, rulePayload, draftFromRule, repeatEditable, presetOf, typedValue, shiftDate, PRESET_ORDER } from "../src/lib/schedule.js";

const overview = {
  actions: [
    { id: "total_output_limit", per_device: false, devices: [], value_type: "watts" },
    { id: "offgrid_socket", per_device: true, devices: ["WR1"], value_type: "choice", choices: ["off", "eco"] },
    { id: "winter_mode", per_device: false, devices: [], value_type: "switch" },
  ],
  presets: { daily: { freq: "daily" }, weekdays: { freq: "weekly", by_weekday: ["mo", "tu", "we", "th", "fr"] } },
};

test("a window whose end is not after its start ends the next day", () => {
  const draft = { ...newDraft(overview, new Date(2026, 9, 19)), action: "total_output_limit", value: "600", end_value: "800", from: "22:00", to: "06:00" };
  const payload = rulePayload(draft, overview);
  assert.equal(payload.when.start, "2026-10-19T22:00");
  assert.equal(payload.when.end, "2026-10-20T06:00");
  assert.deepEqual(payload.action, { id: "total_output_limit", value: 600, end_value: 800 });
  assert.deepEqual(payload.target, { scope: "system" });
  assert.deepEqual(payload.when.repeat, { freq: "daily" });
});

test("a per-device action targets its device", () => {
  const draft = { ...newDraft(overview), action: "offgrid_socket", device: "WR1", mode: "point", value: "eco", preset: "once" };
  const payload = rulePayload(draft, overview);
  assert.deepEqual(payload.target, { scope: "device", device: "WR1" });
  assert.equal(payload.when.end, undefined);
  assert.equal(payload.when.repeat, undefined);
  assert.equal(payload.action.value, "eco");
});

test("values are typed by the action", () => {
  assert.equal(typedValue({ value_type: "switch" }, "true"), true);
  assert.equal(typedValue({ value_type: "switch" }, "false"), false);
  assert.equal(typedValue({ value_type: "factor" }, "1.5"), 1.5);
  assert.equal(typedValue({ value_type: "watts" }, ""), null);
});

test("a rule round-trips through the editor unchanged", () => {
  const rule = {
    id: "r1", revision: 3, name: "Weekday cap", enabled: true,
    target: { scope: "system" },
    action: { id: "total_output_limit", value: 600, end_value: 800 },
    when: { start: "2026-10-19T22:00", end: "2026-10-20T06:00", repeat: { freq: "weekly", by_weekday: ["mo", "tu", "we", "th", "fr"], by_month: [10, 11] } },
  };
  const draft = draftFromRule(rule, overview);
  assert.equal(draft.preset, "weekdays");
  const payload = rulePayload(draft, overview);
  assert.deepEqual(payload.when, rule.when);
  assert.deepEqual(payload.action, rule.action);
});

test("a repeat the editor cannot express is kept as saved", () => {
  const repeat = { freq: "monthly", by_month_day: [1, 15] };
  assert.equal(repeatEditable(repeat), false);
  const draft = draftFromRule({ id: "x", name: "x", enabled: true, target: { scope: "system" }, action: { id: "winter_mode", value: true }, when: { start: "2026-01-01T00:00", repeat } }, overview);
  assert.equal(draft.preset, "kept");
  assert.deepEqual(rulePayload(draft, overview).when.repeat, repeat);
});

test("presets are recognised and dates shift across month ends", () => {
  assert.equal(presetOf(null, overview.presets), "once");
  assert.equal(presetOf({ freq: "daily" }, overview.presets), "daily");
  assert.equal(presetOf({ freq: "daily", interval: 3 }, overview.presets), "custom");
  assert.equal(shiftDate("2026-10-31", 1), "2026-11-01");
  assert.equal(PRESET_ORDER.at(-1), "custom");
});
