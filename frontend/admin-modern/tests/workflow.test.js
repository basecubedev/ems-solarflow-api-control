// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { WORKFLOW_OWNERS, WORKFLOW_STATES, STALE_REASONS, activeWorkflow, alignmentView, recoveryReason, recoveryWanted, shortReference, workflowReference, workflowSince, staleReasonKey } from "../src/lib/workflow.js";

const SERVER = readFileSync(new URL("../../../admin/workflow_lifecycle.py", import.meta.url), "utf8");

test("owners, states and stale reasons are the server's", () => {
  const owners = [...SERVER.matchAll(/^OWNER_\w+ = "([a-z_]+)"/gm)].map((match) => match[1]);
  assert.deepEqual(owners.filter((owner) => !WORKFLOW_OWNERS.includes(owner)), []);
  assert.ok(WORKFLOW_OWNERS.includes("align_existing_install"));
  const states = [...SERVER.matchAll(/^STATE_\w+ = "([a-z_]+)"/gm)].map((match) => match[1]);
  assert.deepEqual(states.sort(), [...WORKFLOW_STATES].sort());
  const reasons = new Set([...SERVER.matchAll(/"reason": "([a-z_]+)"/g)].map((match) => match[1]));
  assert.deepEqual([...reasons].filter((reason) => !STALE_REASONS.includes(reason)).sort(), []);
});

test("only a workflow that holds the console raises the banner, and it opens the page that continues it", () => {
  assert.equal(activeWorkflow(null), null);
  assert.equal(activeWorkflow({ ok: false, owner: "guided_setup", state: "active" }), null);
  assert.equal(activeWorkflow({ ok: true, owner: "none", state: "idle" }), null);
  assert.equal(activeWorkflow({ ok: true, owner: "guided_upgrade", state: "idle" }), null);
  assert.deepEqual(activeWorkflow({ ok: true, owner: "guided_setup", state: "active" }), { owner: "guided_setup", state: "active", classic: "setup", native: null, label: null, recoverable: false });
  assert.equal(activeWorkflow({ ok: true, owner: "guided_upgrade", state: "cleanup_pending", recoverable: true }).classic, "maintenance-upgrade");
  assert.equal(activeWorkflow({ ok: true, owner: "guided_upgrade", state: "active" }).native, "updates");
  assert.deepEqual(
    ["align_existing_install", "conflict", "unknown", "teleport"].map((owner) => activeWorkflow({ ok: true, owner, state: "active" })).map((view) => [view.native, view.label]),
    [["updates", "workflow.showAlignment"], ["system", "workflow.openRecovery"], ["system", "workflow.openRecovery"], ["system", "workflow.openRecovery"]],
  );
});

test("the recovery card speaks unless the answer is a proven idle state with nothing to reset", () => {
  assert.equal(recoveryWanted(null), true);
  assert.equal(recoveryWanted({ ok: false }), true);
  assert.equal(recoveryWanted({ ok: true, lifecycle: { state: "idle" }, safe: { available: false }, advanced: { available: false } }), false);
  assert.equal(recoveryWanted({ ok: true, lifecycle: { state: "idle" }, safe: { available: true }, advanced: {} }), true);
  assert.equal(recoveryWanted({ ok: true, lifecycle: { state: "cleanup_pending" } }), true);
});

test("the recorded reason is chosen by the state, never typed", () => {
  assert.equal(recoveryReason({ lifecycle: { state: "cleanup_pending", blocking_reason: "cleanup_pending" } }), "maintenance release: cleanup_pending/cleanup_pending");
  assert.equal(recoveryReason({}), "maintenance release: unknown/none");
  assert.equal(shortReference("abcdefghijklmnop"), "abcdefghijkl…");
  assert.equal(shortReference(""), "—");
  assert.deepEqual(workflowReference({ transition: { operation_id: "op-1" } }), { kind: "operation", id: "op-1" });
  assert.deepEqual(workflowReference({ setup: { workflow_id: "wf-1" } }), { kind: "workflow", id: "wf-1" });
  assert.equal(workflowReference({}), null);
  assert.equal(workflowSince({ setup: { updated_at: "t" } }), "t");
  assert.equal(staleReasonKey("unreadable_state"), "workflow.stale.unreadable_state");
  assert.equal(staleReasonKey("nope"), null);
});

test("a system alignment says where it stands and offers Continue only where the server's resume moves it", () => {
  const at = (stage, extra = {}) => alignmentView({ mode: "align_existing_install", operation_id: "op-1", system_tag: "v1", stage, ...extra });
  assert.equal(alignmentView({ mode: "guided_upgrade", operation_id: "op-1", stage: "admin_aligned" }), null);
  assert.equal(at("completed"), null);
  assert.deepEqual(at("admin_aligned"), { operationId: "op-1", tag: "v1", stage: "admin_aligned", error: "", canContinue: true, release: false });
  assert.deepEqual([at("resources_verified").canContinue, at("resources_verified").release], [false, true]);
  assert.deepEqual([at("admin_reconnect_pending").canContinue, at("admin_reconnect_pending", { replacement_active: true }).canContinue], [true, false]);
  assert.equal(at("admin_reconnect_pending", { replacement_active: true }).stage, "admin_update_pending");
  assert.equal(at("admin_update_pending").canContinue, false);
  assert.equal(at("teleporting").stage, "other");
  assert.deepEqual([at("failed_recoverable", { resume_available: true, error_message: "x" }).canContinue, at("failed_recoverable").canContinue], [true, false]);
  assert.equal(at("failed_recoverable", { error_message: "Pull failed." }).error, "Pull failed.");
});
