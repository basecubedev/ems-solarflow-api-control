// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import {
  writeDecision, globalWrite, codeItems, decisionItem, finalTotal,
  rawTarget, deviceWeight, baseWeight, totalWeight, deviceShare, adjustedTarget, adjustmentDelta, outputDelta,
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
