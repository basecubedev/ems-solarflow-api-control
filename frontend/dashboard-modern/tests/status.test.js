// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { deriveSystemStatus, staleAfterMs, ruleStates, RULES } from "../src/lib/status.js";

const NOW = 1_800_000_000_000;
const base = (extra = {}) => ({
  timestamp: "t",
  controller: { enabled: true },
  devices: { WR1: { online: true, enabled: true }, WR2: { online: true, enabled: true } },
  ...extra,
});
const derive = (snapshot, changedAt = NOW) => deriveSystemStatus({ snapshot, changedAt, now: NOW, staleAfter: staleAfterMs(5) });

test("no snapshot is waiting, not normal", () => {
  assert.equal(derive(null).level, "waiting");
});

test("stale data outranks everything, even a healthy last snapshot", () => {
  const status = derive(base(), NOW - 31_000);
  assert.equal(status.level, "stale");
  assert.equal(status.ageSeconds, 31);
});

test("the stale limit is three loops but never under thirty seconds", () => {
  assert.equal(staleAfterMs(5), 30_000);
  assert.equal(staleAfterMs(20), 60_000);
  assert.equal(staleAfterMs(undefined), 30_000);
});

test("a meter that does not answer is a fault", () => {
  assert.equal(derive(base({ grid_power_valid: false })).level, "fault");
});

test("an offline device is attention, a disabled one is not counted", () => {
  const offline = derive(base({ devices: { WR1: { online: false, enabled: true }, WR2: { online: true } } }));
  assert.equal(offline.level, "attention");
  assert.deepEqual(offline.fleet.offline, ["WR1"]);
  const disabled = derive(base({ devices: { WR1: { online: false, enabled: false }, WR2: { online: true } } }));
  assert.equal(disabled.level, "ok");
  assert.equal(disabled.fleet.total, 1);
});

test("every enabled device offline is a fault", () => {
  assert.equal(derive(base({ devices: { WR1: { online: false } } })).level, "fault");
});

test("switched-off control is paused, not normal", () => {
  assert.equal(derive(base({ controller: { enabled: false } })).level, "paused");
});

test("a healthy fresh snapshot is normal", () => {
  assert.equal(derive(base()).level, "ok");
});

test("rules keep their order and a missing rule is inactive", () => {
  const rules = ruleStates({ rules: { pv_priority_balancing: { active: true, reason: "x" } } });
  assert.deepEqual(rules.map((rule) => rule.key), RULES.map((rule) => rule.key));
  assert.equal(rules.find((rule) => rule.key === "pv_priority_balancing").active, true);
  assert.equal(rules.find((rule) => rule.key === "ems_enabled").active, false);
});

test("a rule's reason in the dashboard's own words is translated, a list of device names is not", () => {
  const rules = ruleStates({
    rules: {
      offline_devices: { active: false, reason: "all devices online" },
      full_charge_assist_active: { active: true, reason: "WR1, WR2" },
    },
  });
  const byKey = Object.fromEntries(rules.map((rule) => [rule.key, rule]));
  assert.equal(byKey.offline_devices.reasonKey, "rule.reason.offline_devices");
  assert.equal(byKey.full_charge_assist_active.reasonKey, null);
  assert.equal(byKey.full_charge_assist_active.reason, "WR1, WR2");
});
