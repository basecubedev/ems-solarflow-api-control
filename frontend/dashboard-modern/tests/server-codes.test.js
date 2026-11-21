// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { serverSource, unwritten } from "../../shared/tests/serverSource.js";
import { CONTROL_CODES, DECISION_SENTENCES } from "../src/lib/control.js";
import { PRESET_ORDER, STATUSES, WEEKDAYS } from "../src/lib/schedule.js";

const EMS = serverSource("ems");
const DASHBOARD = serverSource("dashboard");

test("every control code and decision sentence here is one the EMS writes", () => {
  const roles = [...EMS.matchAll(/^\s+AC_\w+ = "([a-z_]+)"/gm)].map((match) => `runtime_role_${match[1]}`);
  assert.ok(EMS.includes('f"runtime_role_{intent.role.value}"'));
  assert.deepEqual(unwritten(EMS, CONTROL_CODES).filter((code) => !roles.includes(code)), []);
  assert.deepEqual(unwritten(EMS, DECISION_SENTENCES), []);
});

test("the schedule's statuses are the API's, and its presets and weekdays are the EMS's", () => {
  const statuses = DASHBOARD.match(/^RULE_STATUSES = \(([^)]*)\)/m)[1];
  assert.deepEqual([...statuses.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]), [...STATUSES]);
  assert.deepEqual(unwritten(EMS, PRESET_ORDER), []);
  assert.deepEqual(unwritten(EMS, WEEKDAYS), []);
});
