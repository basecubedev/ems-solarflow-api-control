// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { writeDecision, globalWrite, codeItems, decisionItem, finalTotal } from "../src/lib/control.js";

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
