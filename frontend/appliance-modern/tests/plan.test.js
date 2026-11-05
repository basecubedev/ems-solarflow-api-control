// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from "node:test";
import assert from "node:assert/strict";
import { planBlocks, planDanger, planNeedsPrereleaseAck } from "../src/lib/plan.js";

test("the confirm button is red for what the plan itself says is risky", () => {
  assert.equal(planDanger({ type: "admin.rollback" }, {}), true);
  assert.equal(planDanger({ type: "admin.install", direction: "downgrade" }, {}), true);
  assert.equal(planDanger({ type: "admin.install", direction: "upgrade" }, {}), false);
  assert.equal(planDanger({ type: "network.hostname" }, {}), true);
  assert.equal(planDanger({ type: "ssh.key_add", account: "ems-shell" }, {}), true);
  assert.equal(planDanger({ type: "ssh.key_add", account: "ems-backup" }, {}), false);
  assert.equal(planDanger({ type: "manager.update", direction: "downgrade" }, {}), true);
  assert.equal(planDanger({ type: "updates.install" }, {}), false);
  assert.equal(planDanger({ type: "updates.install" }, { danger: true }), true);
});

test("a list or object the dialog has no block for is spelled out, not hidden", () => {
  const plan = {
    type: "updates.repair",
    dpkg_issues: ["libfoo half-configured", "bar unpacked"],
    keys: [{ fingerprint: "SHA256:abc", comment: "laptop" }],
    docker: { state: "running" },
    packages: [{ name: "openssl" }],
  };
  const basic = planBlocks(plan, false);
  assert.deepEqual(basic.map((block) => block.key), ["dpkg_issues", "keys"]);
  assert.deepEqual(basic[0].lines, ["libfoo half-configured, bar unpacked"]);
  assert.deepEqual(basic[1].lines, ["SHA256:abc · laptop"]);
  assert.deepEqual(planBlocks(plan, true).map((block) => block.key), ["dpkg_issues", "keys", "docker"]);
});

test("anything but a stable release is confirmed for this install on its own", () => {
  assert.equal(planNeedsPrereleaseAck({ track: "stable" }), false);
  assert.equal(planNeedsPrereleaseAck({ track: "rc" }), true);
  assert.equal(planNeedsPrereleaseAck({ track: "latest" }), true);
  assert.equal(planNeedsPrereleaseAck({ type: "admin.lifecycle" }), false);
});
