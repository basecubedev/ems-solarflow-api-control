// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { WORKFLOW_OWNERS, WORKFLOW_STATES, STALE_REASONS, activeWorkflow, recoveryReason, recoveryWanted, shortReference, workflowReference, workflowSince, staleReasonKey } from "../src/lib/workflow.js";

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
  assert.deepEqual(activeWorkflow({ ok: true, owner: "guided_setup", state: "active" }), { owner: "guided_setup", state: "active", classic: "setup", recoverable: false });
  assert.equal(activeWorkflow({ ok: true, owner: "guided_upgrade", state: "cleanup_pending", recoverable: true }).classic, "maintenance-upgrade");
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
