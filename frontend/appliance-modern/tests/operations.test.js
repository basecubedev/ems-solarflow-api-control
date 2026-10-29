// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { acknowledgeable, archiveHref, cancellable, currentOperation, operationDetails, operationStage, planRequestId, recentProgress, settledSince, stateOf } from "../src/lib/operations.js";

const ID = "0123456789abcdef0123456789abcdef";

test("the banner shows the running operation, else the newest unacknowledged one", () => {
  assert.equal(currentOperation({ active: { operation_id: "a" }, unacknowledged: [{ operation_id: "b" }] }).operation_id, "a");
  assert.equal(currentOperation({ active: null, unacknowledged: [{ operation_id: "b" }] }).operation_id, "b");
  assert.equal(currentOperation({ active: null, unacknowledged: [] }), null);
});

test("the stage is shown only when it says more than the state", () => {
  assert.equal(operationStage({ state: "running", stage: "running" }), "");
  assert.equal(operationStage({ state: "running", stage: "pulling_image" }), "pulling image");
});

test("a known state is translated; an unknown one is spelled out in the tone of its settledness", () => {
  assert.deepEqual(stateOf({ state: "succeeded" }), { key: "opstate.succeeded", tone: "c-ok" });
  assert.deepEqual(stateOf({ state: "weird_new", settled: true }), { text: "weird new", lang: "en", tone: "c-fault" });
  assert.equal(stateOf({ state: "weird_new", settled: false }).tone, "c-warn");
});

test("cancel is offered only before a plan starts, acknowledge only on a settled outcome", () => {
  assert.equal(cancellable({ state: "awaiting_confirmation" }), true);
  assert.equal(cancellable({ state: "running" }), false);
  assert.equal(cancellable({ state: "failed_recoverable" }), true);
  assert.equal(acknowledgeable({ settled: true, acknowledged: false }), true);
  assert.equal(acknowledgeable({ settled: true, acknowledged: true }), false);
  assert.equal(acknowledgeable({ settled: false }), false);
});

test("the support archive is downloadable from a succeeded archive operation with a well-formed id only", () => {
  assert.equal(archiveHref({ type: "support.archive", state: "succeeded", operation_id: ID }), `/api/support/archive/${ID}`);
  assert.equal(archiveHref({ type: "support.archive", state: "running", operation_id: ID }), null);
  assert.equal(archiveHref({ type: "support.archive", state: "succeeded", operation_id: "../x" }), null);
  assert.equal(archiveHref({ type: "admin.install", state: "succeeded", operation_id: ID }), null);
});

test("the progress list keeps the last six steps", () => {
  const progress = Array.from({ length: 9 }, (_, index) => ({ stage: `step_${index}` }));
  assert.deepEqual(recentProgress({ progress }).map((step) => step.stage), ["step 3", "step 4", "step 5", "step 6", "step 7", "step 8"]);
});

test("a result with nothing beyond done says nothing; a failed verification says why", () => {
  assert.equal(operationDetails({ result: { verification: { verified: true } } }).any, false);
  const failed = operationDetails({ result: { verification: { verified: false, failures: ["api_unreachable"] }, manual_actions: ["do x"] } });
  assert.equal(failed.any, true);
  assert.deepEqual(failed.verification.failures, ["api_unreachable"]);
  assert.deepEqual(failed.manual, ["do x"]);
});

test("one more settled record, or a later finish, is a settle; neither is not", () => {
  const seen = { count: 1, finishedAt: 100 };
  assert.equal(settledSince({ unacknowledged: [{ finished_at: 100 }] }, seen).settled, false);
  assert.equal(settledSince({ unacknowledged: [{ finished_at: 100 }, { finished_at: 50 }] }, seen).settled, true);
  assert.equal(settledSince({ unacknowledged: [{ finished_at: 120 }] }, seen).settled, true);
  assert.deepEqual(settledSince({ unacknowledged: [] }, seen), { settled: false, count: 0, finishedAt: 100 });
});

test("a plan request id is 32 hex characters", () => {
  const random = { getRandomValues: (bytes) => bytes.fill(171) };
  assert.equal(planRequestId(random), "ab".repeat(16));
});
