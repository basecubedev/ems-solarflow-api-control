// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { nextStep, acknowledgeable, findOperation, archiveHref, cancellable, currentOperation, operationDetails, operationStage, operationTitleKey, planRequestId, managerReconnectState, recentProgress, settledSince, stateOf } from "../src/lib/operations.js";

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
  assert.equal(cancellable({ state: "failed_recoverable" }), false);
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

test("a refresh of the package index is not titled a repair, in the banner or in its plan", () => {
  assert.equal(operationTitleKey({ type: "updates.repair", requested_target: { action: "refresh_index" } }), "op.updates.refresh_index");
  assert.equal(operationTitleKey({ type: "updates.repair" }, "refresh_index"), "op.updates.refresh_index");
  assert.equal(operationTitleKey({ type: "updates.repair", requested_target: { action: "fix_broken" } }), "op.updates.repair");
  assert.equal(operationTitleKey({ type: "not.a.type" }), null);
});

test("the refresh leaves the recovery repairs and takes nothing else with it", async () => {
  const { RECOVERY_ACTIONS, REFRESH_INDEX_ACTION, REPAIR_ACTIONS, refreshIndexPlan } = await import("../src/lib/plans.js");
  assert.deepEqual([...RECOVERY_ACTIONS, REFRESH_INDEX_ACTION].sort(), [...REPAIR_ACTIONS].sort());
  assert.ok(!RECOVERY_ACTIONS.includes(REFRESH_INDEX_ACTION));
  assert.deepEqual(refreshIndexPlan().body, { action: REFRESH_INDEX_ACTION });
});

test("a Manager install that has only started does not read as completed", () => {
  for (const type of ["manager.update", "manager.revert"]) {
    assert.deepEqual(stateOf({ type, state: "succeeded" }), { key: "op.managerStarted", tone: "c-idle" });
  }
  assert.equal(stateOf({ type: "admin.install", state: "succeeded" }).key, "opstate.succeeded");
});

test("the wait on a Manager install ends on a restart, not on the old service answering", () => {
  const before = "0.4.1";
  const old = { authenticated: true, appliance_version: "0.4.1" };
  const started = { state: "succeeded", settled: true };
  assert.equal(managerReconnectState({ before, session: old, manager: { outcome: { outcome: "pending" } }, operation: started }), "waiting");
  assert.equal(managerReconnectState({ before, session: { ...old, appliance_version: "0.4.2" } }), "back");
  assert.equal(managerReconnectState({ before, session: { authenticated: false, appliance_version: "0.4.1" } }), "back");
  assert.equal(managerReconnectState({ before, outage: true, session: old }), "back");
  assert.equal(managerReconnectState({ before, session: null }), "waiting");
});

test("an install that ended without a restart ends the wait", () => {
  const old = { authenticated: true, appliance_version: "0.4.1" };
  const started = { state: "succeeded", settled: true };
  for (const outcome of ["not_started", "no_request", "request_invalid", "install_failed", "revert_failed"]) {
    assert.equal(managerReconnectState({ before: "0.4.1", session: old, manager: { outcome: { outcome } }, operation: started }), "unchanged");
  }
  assert.equal(managerReconnectState({ before: "0.4.1", session: old, manager: { outcome: { outcome: "pending" } }, operation: started }), "waiting");
  assert.equal(managerReconnectState({ before: "0.4.1", session: old, manager: {}, operation: started }), "waiting");
});

test("a Manager operation that fails after the confirm ends the wait", () => {
  const old = { authenticated: true, appliance_version: "0.4.1" };
  const failed = { state: "failed_terminal", settled: true };
  assert.equal(managerReconnectState({ before: "0.4.1", session: old, operation: failed }), "unchanged");
});

test("the last install's outcome does not end the wait for this one", () => {
  const old = { authenticated: true, appliance_version: "0.4.1" };
  const downloading = { state: "running", settled: false };
  const stale = { outcome: { outcome: "not_started" } };
  assert.equal(managerReconnectState({ before: "0.4.1", session: old, manager: stale, operation: downloading }), "waiting");
  assert.equal(managerReconnectState({ before: "0.4.1", session: old, manager: stale, operation: null }), "waiting");
});

test("the waited-on operation is found wherever the answer lists it", () => {
  const record = { operation_id: "b", state: "running" };
  assert.equal(findOperation({ active: record, unacknowledged: [], recent: [] }, "b"), record);
  assert.equal(findOperation({ active: null, unacknowledged: [], recent: [record] }, "b"), record);
  assert.equal(findOperation({ active: null, unacknowledged: [], recent: [] }, "b"), null);
  assert.equal(findOperation(null, "b"), null);
});

test("the banner title names the action or the way a switch goes", () => {
  assert.equal(operationTitleKey({ type: "admin.lifecycle", requested_target: { action: "restart" } }), "op.admin.lifecycle.restart");
  assert.equal(operationTitleKey({ type: "ssh.service", requested_target: { enabled: false } }), "op.ssh.service.off");
  assert.equal(operationTitleKey({ type: "ssh.shell_access", requested_target: { enabled: true } }), "op.ssh.shell_access.on");
  assert.equal(operationTitleKey({ type: "ssh.service", requested_target: {} }), "op.ssh.service");
});

test("an operation that did not finish names where it is taken up again", () => {
  assert.deepEqual(nextStep({ type: "updates.install", state: "failed_recoverable" }), { key: "op.next.updates", page: "updates" });
  assert.deepEqual(nextStep({ type: "admin.lifecycle", state: "manual_action_required" }), { key: "op.next.admin", page: "admin" });
  assert.deepEqual(nextStep({ type: "ssh.service", state: "failed_terminal" }), { key: "op.next.diagnostics", page: "diagnostics" });
  assert.equal(nextStep({ type: "updates.install", state: "succeeded" }), null);
});
