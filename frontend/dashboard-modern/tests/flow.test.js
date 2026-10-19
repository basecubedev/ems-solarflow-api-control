// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { createFlowActivity, createFlowScale, createHouseLines, ribbonWidth, batteryDirection, gridDirection, gridHomeWatts, RIBBON_IDLE_PX, RIBBON_MAX_PX } from "../src/lib/flow.js";

test("a flow switches on at 8 W and off only below 3 W, as in Classic", () => {
  const activity = createFlowActivity();
  assert.equal(activity.isActive("pv", 7), false);
  assert.equal(activity.isActive("pv", 8), true);
  assert.equal(activity.isActive("pv", 4), true);
  assert.equal(activity.isActive("pv", 3), false);
  assert.equal(activity.isActive("pv", 7), false);
});

test("the scale follows the installation and holds inside half a rung", () => {
  const scale = createFlowScale();
  assert.equal(scale.update(800), 1000);
  assert.equal(scale.update(600), 1000);
  assert.equal(scale.update(2400), 3000);
  assert.equal(scale.update(1600), 3000);
  assert.equal(scale.update(1400), 2000);
});

test("thickness is magnitude: idle is thin, a full-scale flow is the widest", () => {
  assert.equal(ribbonWidth(500, false, 1000), RIBBON_IDLE_PX);
  assert.equal(ribbonWidth(1000, true, 1000), RIBBON_MAX_PX);
  assert.ok(ribbonWidth(700, true, 3000) < ribbonWidth(3000, true, 3000));
});

test("direction words follow the sign conventions of the snapshot", () => {
  assert.equal(batteryDirection(880), "charging");
  assert.equal(batteryDirection(-200), "discharging");
  assert.equal(batteryDirection(0), "idle");
  assert.equal(gridDirection(120), "importing");
  assert.equal(gridDirection(-120), "exporting");
  assert.equal(gridDirection(-5), "balanced");
  assert.equal(gridDirection(500, false), "offline");
});

test("the AC charge is not drawn twice on the grid to home line", () => {
  assert.equal(gridHomeWatts(900, 600), 300);
  assert.equal(gridHomeWatts(400, 600), 0);
  assert.equal(gridHomeWatts(-300, 0), 300);
});

test("a device's house line needs 8 W and twice the other side to change direction, as in Classic", () => {
  const lines = createHouseLines();
  assert.deepEqual(lines.direction("WR1", 0, 5, 1), { charging: false, watts: 0 });
  assert.equal(lines.direction("WR1", 300, 0, 2).charging, false);
  assert.equal(lines.direction("WR1", 300, 400, 3).charging, false);
  assert.equal(lines.direction("WR1", 300, 600, 4).charging, true);
  assert.equal(lines.direction("WR1", 400, 300, 5).charging, true);
  assert.equal(lines.direction("WR1", 700, 300, 6).charging, false);
});
