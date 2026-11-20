// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { serverSource, unwritten } from "../../shared/tests/serverSource.js";
import { BUSY_OPERATIONS, CONFIG_CONFLICTS, CONTINUITY, PLAN_CONFLICTS, SWITCH_REASONS, WORKFLOW_CONFLICTS } from "../src/lib/setupDraft.js";
import { WORKFLOW_BLOCKED_CODES } from "../src/lib/workflow.js";
import { BROKER_ISSUES } from "../src/lib/telemetry.js";

const SERVER = serverSource("admin", "ems");

test("every refusal, operation and reason the guided setup names is one the server writes", () => {
  for (const [name, codes] of Object.entries({ CONFIG_CONFLICTS, PLAN_CONFLICTS, WORKFLOW_CONFLICTS, BUSY_OPERATIONS, SWITCH_REASONS, CONTINUITY, WORKFLOW_BLOCKED_CODES })) {
    assert.deepEqual(unwritten(SERVER, codes), [], name);
  }
});

test("every broker issue the telemetry card names is one the EMS reports", () => {
  assert.deepEqual(unwritten(SERVER, BROKER_ISSUES), []);
});
