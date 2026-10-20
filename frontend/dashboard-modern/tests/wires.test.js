// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { runs, elbow, CORNER_RADIUS } from "../src/lib/wires.js";

test("dot runs stop where the track bends, so no dot leaves a rounded corner", () => {
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
  assert.deepEqual(runs([[0, 10], [80, 10]]), [{ x: 0, y: 10, length: 80, angle: 0 }]);
});
