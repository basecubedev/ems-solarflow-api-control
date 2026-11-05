// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from "node:test";
import assert from "node:assert/strict";
import { planDanger } from "../src/lib/plan.js";

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
