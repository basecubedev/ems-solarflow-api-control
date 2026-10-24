// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { createFlowActivity, createFlowScale, createHouseLines, createFleetLine, busFlows, ribbonWidth, batteryDirection, gridDirection, flowBucket, FLOW_BUCKETS, RIBBON_IDLE_PX, RIBBON_MAX_PX } from "../src/lib/flow.js";

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

test("a flow moves faster and shines brighter from 150 W and again from 600 W, as in Classic", () => {
  assert.equal(flowBucket(149), "low");
  assert.equal(flowBucket(-150), "medium");
  assert.equal(flowBucket(599), "medium");
  assert.equal(flowBucket(600), "high");
  const order = ["low", "medium", "high"].map((name) => FLOW_BUCKETS[name]);
  for (let i = 1; i < order.length; i += 1) {
    assert.ok(order[i].speed > order[i - 1].speed);
    assert.ok(order[i].alpha > order[i - 1].alpha);
    assert.ok(order[i].glow > order[i - 1].glow);
  }
});

test("the System line carries output minus charge, and rests while the two are about even", () => {
  const line = createFleetLine();
  let sequence = 0;
  const watts = (output, charge) => line.watts(output, charge, (sequence += 1));
  assert.equal(watts(800, 0), 800);
  assert.equal(watts(800, 600), 200);
  assert.equal(watts(410, 400), 0);
  assert.equal(watts(395, 410), 0);
  assert.equal(watts(380, 410), -30);
  assert.equal(watts(400, 412), -12);
  assert.equal(watts(400, 405), 0);
  assert.equal(watts(400, 799), -399);
  assert.equal(watts(400, 800), -400);
  assert.equal(watts(0, 600), -600);
  assert.equal(watts(0, 0), 0);
});

test("the System line needs 5 % of the larger side to show and rests below half of that", () => {
  const line = createFleetLine();
  let sequence = 0;
  const watts = (output, charge) => line.watts(output, charge, (sequence += 1));
  assert.equal(watts(1000, 951), 0);
  assert.equal(watts(1000, 950), 50);
  assert.equal(watts(1000, 975), 25);
  assert.equal(watts(1000, 976), 0);
  assert.equal(watts(951, 1000), 0);
  assert.equal(watts(950, 1000), -50);
});

test("the System line needs 8 W to show, as every flow does, and reads a missing figure as nothing", () => {
  const line = createFleetLine();
  assert.equal(line.watts(7, 0, 1), 0);
  assert.equal(line.watts(8, 0, 2), 8);
  assert.equal(line.watts(3, 0, 3), 3);
  assert.equal(line.watts(2, 0, 4), 0);
  assert.equal(line.watts(null, 600, 5), -600);
  assert.equal(line.watts(Number.NaN, undefined, 6), 0);
});

test("a full reversal between two snapshots turns the System line straight round", () => {
  const line = createFleetLine();
  assert.equal(line.watts(800, 0, 1), 800);
  assert.equal(line.watts(0, 800, 2), -800);
  assert.equal(line.watts(800, 0, 3), 800);
});

test("the System line forgets a direction older than one snapshot", () => {
  const line = createFleetLine();
  assert.equal(line.watts(0, 600, 1), -600);
  assert.equal(line.watts(400, 412, 2), -12);
  assert.equal(line.watts(400, 412, 9), 0);
});

test("a device's house line needs 8 W and twice the other side to change direction, as in Classic", () => {
  const lines = createHouseLines();
  assert.deepEqual(lines.direction("WR1", 0, 5, 1), { charging: false, active: false, watts: 0 });
  assert.equal(lines.direction("WR1", 300, 0, 2).charging, false);
  assert.equal(lines.direction("WR1", 300, 400, 3).charging, false);
  assert.equal(lines.direction("WR1", 300, 600, 4).charging, true);
  assert.equal(lines.direction("WR1", 400, 300, 5).charging, true);
  assert.equal(lines.direction("WR1", 700, 300, 6).charging, false);
});

test("the bus adds the inverters' lines toward the home and the trunk carries them all", () => {
  const joined = busFlows([300, -200, 400, 0], { above: [0, 1], below: [2] });
  assert.deepEqual(joined.segments, { "bus-above0": 300, "bus-above1": 100, "bus-below0": 400 });
  assert.equal(joined.total, 500);
  assert.deepEqual(busFlows([250], {}), { segments: {}, total: 250 });
});
