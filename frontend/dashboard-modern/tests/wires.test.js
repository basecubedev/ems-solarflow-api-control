// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { runs, elbow, CORNER_RADIUS } from "../src/lib/wires.js";

test("dash runs stop where the track bends, so no dash leaves a rounded corner", () => {
  const points = elbow(0, 0, 100, 60);
  const [first, middle, last] = runs(points);
  assert.equal(first.x, 0);
  assert.equal(first.length, 50 - CORNER_RADIUS);
  assert.equal(middle.y, CORNER_RADIUS);
  assert.equal(middle.length, 60 - 2 * CORNER_RADIUS);
  assert.equal(last.x, 50 + CORNER_RADIUS);
  assert.equal(last.x + last.length, 100);
});

test("a straight wire keeps its whole length", () => {
  assert.deepEqual(runs([[0, 10], [80, 10]]), [{ x: 0, y: 10, length: 80, angle: 0, before: 0 }]);
});

test("each run knows how far along the rounded track it starts, so the dashes keep their spacing round a corner", () => {
  const [first, middle, last] = runs(elbow(0, 0, 100, 60));
  const corner = (Math.PI / 2) * CORNER_RADIUS;
  assert.equal(first.before, 0);
  assert.ok(Math.abs(middle.before - (first.length + corner)) < 1e-9);
  assert.ok(Math.abs(last.before - (middle.before + middle.length + corner)) < 1e-9);
});
