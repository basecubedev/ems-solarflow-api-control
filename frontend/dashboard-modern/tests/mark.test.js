// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { MARK_RING, markSvg } from "../src/brand/mark.js";

function ends(d) {
  const numbers = d.match(/-?\d+(\.\d+)?/g).map(Number);
  return [numbers.slice(0, 2), numbers.slice(-2)];
}

test("each quarter of the ring sits where the flow diagram puts its energy", () => {
  const quarter = Object.fromEntries(MARK_RING.map((arc) => {
    const [[x1, y1], [x2, y2]] = ends(arc.d);
    return [arc.token, `${Math.max(x1, x2) > 18 ? "right" : "left"} ${Math.max(y1, y2) > 18 ? "bottom" : "top"}`];
  }));
  assert.deepEqual(quarter, { "--pv": "left top", "--output": "right top", "--grid": "right bottom", "--battery": "left bottom" });
});

test("the four quarters close into one ring round the bolt, each bending outwards", () => {
  MARK_RING.forEach((arc, index) => {
    assert.match(arc.d, /^M[\d.]+ [\d.]+A11 11 0 0 1 [\d.]+ [\d.]+$/);
    const [[x1, y1], [x2, y2]] = ends(arc.d);
    for (const [x, y] of [[x1, y1], [x2, y2]]) assert.equal(Math.hypot(x - 18, y - 18), 11);
    assert.ok((x1 - 18) * (y2 - 18) - (y1 - 18) * (x2 - 18) > 0, `${arc.token} runs clockwise`);
    const [[nx, ny]] = ends(MARK_RING[(index + 1) % MARK_RING.length].d);
    assert.deepEqual([x2, y2], [nx, ny]);
  });
});

test("the tab's copy of the mark is drawn in the colours it is given", () => {
  const colors = { "--pv": "#111111", "--battery": "#222222", "--output": "#333333", "--grid": "#444444", tile: "#555555", line: "#666666", bolt: "#777777" };
  const svg = markSvg(colors);
  for (const value of Object.values(colors)) assert.ok(svg.includes(`"${value}"`), value);
  assert.equal(svg.match(/<path /g).length, MARK_RING.length + 1);
  assert.doesNotMatch(svg, /var\(/);
});
