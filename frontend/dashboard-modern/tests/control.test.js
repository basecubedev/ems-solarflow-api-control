// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import { createFormat } from "../../shared/lib/format.js";
import assert from "node:assert/strict";
import {
  writeDecision, globalWrite, codeItems, decisionItem, finalTotal,
  rawTarget, deviceWeight, baseWeight, totalWeight, deviceShare, adjustedTarget, adjustmentDelta, outputDelta, limitValueText, noteText,
} from "../src/lib/control.js";

test("the EMS's own write decision wins", () => {
  assert.equal(writeDecision({ write_decision: "send", write_reason: "output_limit_update" }, {}).tone, "send");
  assert.equal(writeDecision({ write_decision: "blocked_offline" }, {}).tone, "blocked");
  assert.equal(writeDecision({ write_decision: "skip_deadband" }, {}).tone, "skip");
});

test("without one, the deadband decides as in Classic", () => {
  const explain = { deadband_w: 10 };
  assert.equal(writeDecision({ effective_target_w: 305, output_limit_w: 300 }, explain).tone, "skip");
  assert.equal(writeDecision({ effective_target_w: 400, output_limit_w: 300 }, explain).tone, "send");
  assert.equal(writeDecision({ online: false, effective_target_w: 400 }, explain).tone, "blocked");
});

test("one blocked device blocks the summary", () => {
  const devices = [["A", { write_decision: "send" }], ["B", { write_decision: "blocked" }]];
  assert.equal(globalWrite(devices, {}), "blocked");
  assert.equal(globalWrite([], {}), "none");
});

test("known codes become label keys and unknown codes keep their words", () => {
  assert.deepEqual(codeItems("pv_first, brand_new_code"), [{ key: "code.pv_first" }, { text: "brand new code" }]);
  assert.deepEqual(decisionItem("pv_first_allocation"), { key: "decision.pv_first_allocation" });
  assert.deepEqual(decisionItem("deadband").codes, [{ key: "code.deadband" }]);
});

test("the final total falls back through the explanation's fields", () => {
  assert.equal(finalTotal({ effective_target_total_w: 800, requested_total_w: 900 }), 800);
  assert.equal(finalTotal({ requested_total_w: 900 }), 900);
});

test("per-device decision figures follow Classic's fallbacks", () => {
  const device = { raw_target_w: 320, effective_weight: 1, pv_weight: 1.5, pv_priority_factor: 1.5, charge_balance_multiplier: 1, allocated_target_w: 320, output_w: 300 };
  assert.equal(rawTarget(device), 320);
  assert.equal(deviceWeight(device), 1);
  assert.equal(baseWeight(device), 1);
  assert.equal(totalWeight([["WR1", device], ["WR2", { effective_weight: 3 }]]), 4);
  assert.equal(deviceShare(device, { requested_total_w: 800 }, 4), 0.4);
  assert.equal(deviceShare({ effective_weight: 1 }, {}, 4), 0.25);
  assert.equal(deviceShare({ share_percent: 60 }, {}, null), 0.6);
  assert.equal(adjustedTarget(device), 320);
  assert.equal(adjustmentDelta(device), 0);
  assert.equal(outputDelta(device), 20);
});

test("a limit's value reads as watts or per device, never as a bare number", () => {
  const f = createFormat("en");
  const explain = { devices: { INV_1: {}, INV_2: {} }, device_order: ["INV_1", "INV_2"] };
  assert.equal(limitValueText({ name: "night_min_soc_idle", value: 70 }, explain, f), f.power(70));
  assert.equal(limitValueText({ name: "discharge_capacity", value: 0 }, explain, f), null);
  assert.equal(limitValueText({ name: "device_output_limits", value: "[35, 35]" }, explain, f), `INV_1 ${f.power(35)} / INV_2 ${f.power(35)}`);
  assert.equal(limitValueText({ name: "full_soc_pv_priority", value: "[1]" }, explain, f), "INV_2");
  assert.equal(limitValueText({ name: "pv_only_final_limits", value: "[]" }, explain, f), null);
  assert.equal(limitValueText({ name: "x", value: null }, explain, f), null);
});

test("a note the EMS writes reads in the reader's words, one it does not know stays its English", () => {
  const f = createFormat("en");
  const t = (key, vars) => `${key}${vars ? JSON.stringify(vars) : ""}`;
  assert.deepEqual(noteText("battery top-up was applied after PV-first allocation", t, f), { text: "note.topupApplied" });
  assert.deepEqual(noteText("120 W could not be distributed", t, f), { text: `note.undistributed${JSON.stringify({ w: f.power(120) })}` });
  assert.deepEqual(noteText("something new", t, f), { text: "something new", lang: "en" });
});

test("per-device limit values follow the controller's order, not the names' alphabet", () => {
  const f = createFormat("en");
  const explain = { devices: { Garage: {}, Terasse: {} }, device_order: ["Terasse", "Garage"] };
  assert.equal(limitValueText({ name: "device_output_limits", value: "[600, 0]" }, explain, f), `Terasse ${f.power(600)} / Garage ${f.power(0)}`);
  assert.equal(limitValueText({ name: "full_soc_pv_priority", value: "[0]" }, explain, f), "Terasse");
  assert.equal(limitValueText({ name: "full_soc_pv_priority", value: "[0]" }, { devices: { A: {} } }, f), "#1");
});
