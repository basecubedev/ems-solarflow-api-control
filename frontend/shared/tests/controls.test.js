// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { atBound, monthRuns, monthsOf, nudged, seasonOrder, seasonText, barSpan, toggledMonth } from "../lib/controls.js";

test("a stepper moves along its step's grid and stays within its bounds", () => {
  assert.equal(nudged("50", 1, { step: 10 }), 60);
  assert.equal(nudged("52", 1, { step: 10 }), 60);
  assert.equal(nudged("52", -1, { step: 10 }), 50);
  assert.equal(nudged("50", -1, { step: 10 }), 40);
  assert.equal(nudged("0.7", 1, { step: 0.1 }), 0.8);
  assert.equal(nudged("0.3", -1, { step: 0.1 }), 0.2);
  assert.equal(nudged("0.25", 1, { step: 0.01 }), 0.26);
  assert.equal(nudged("99", 1, { step: 1, max: 100 }), 100);
  assert.equal(nudged("100", 1, { step: 1, max: 100 }), 100);
  assert.equal(nudged("3", -1, { step: 10, min: 0 }), 0);
  assert.equal(nudged("-180", -1, { step: 10, min: -200 }), -190);
});

test("an empty stepper starts from the default it names, else from its minimum, else from zero", () => {
  assert.equal(nudged("", 1, { step: 10, start: 30 }), 40);
  assert.equal(nudged("", -1, { step: 10, start: 30 }), 20);
  assert.equal(nudged(null, 1, { step: 1, min: 5 }), 6);
  assert.equal(nudged(undefined, 1, { step: 1 }), 1);
  assert.equal(nudged("", -1, { step: 1, min: 0 }), 0);
});

test("a text the browser cannot read is not stepped", () => {
  assert.equal(nudged("1e", 1, { step: 1 }), null);
  assert.equal(nudged("abc", -1, { step: 1, start: 4 }), null);
});

test("a stepper's button has nowhere to go only at the bound it moves towards", () => {
  assert.equal(atBound("100", 1, { max: 100 }), true);
  assert.equal(atBound("100", -1, { max: 100 }), false);
  assert.equal(atBound("0", -1, { min: 0 }), true);
  assert.equal(atBound("", -1, { min: 0, start: 0 }), true);
  assert.equal(atBound("", 1, { min: 0 }), false);
  assert.equal(atBound("5", 1, {}), false);
});

test("months are read from a list or a text, once each, in calendar order", () => {
  assert.deepEqual(monthsOf([10, 11, 12, 1, 2, 3]), [1, 2, 3, 10, 11, 12]);
  assert.deepEqual(monthsOf("10, 11,12 , 1"), [1, 10, 11, 12]);
  assert.deepEqual(monthsOf(["3", 3, 13, 0, "x", 2.5]), [3]);
  assert.deepEqual(monthsOf(null), []);
});

test("a season reads from the month after its longest break", () => {
  assert.deepEqual(seasonOrder([1, 2, 3, 10, 11, 12]), [10, 11, 12, 1, 2, 3]);
  assert.deepEqual(seasonOrder([12, 1]), [12, 1]);
  assert.deepEqual(seasonOrder([4, 5, 6]), [4, 5, 6]);
  assert.deepEqual(seasonOrder([3, 9]), [3, 9]);
  assert.deepEqual(seasonOrder([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  assert.deepEqual(seasonOrder([]), []);
});

test("unbroken months are one run, and December runs on into January", () => {
  assert.deepEqual(monthRuns([10, 11, 12, 1, 2, 3]), [[10, 3]]);
  assert.deepEqual(monthRuns([1, 5, 6, 7]), [[1, 1], [5, 7]]);
  assert.deepEqual(monthRuns([11, 12, 1, 5]), [[11, 1], [5, 5]]);
  assert.deepEqual(monthRuns([6]), [[6, 6]]);
  assert.deepEqual(monthRuns([]), []);
});

test("a month is added or taken away in season order, and the last one stays", () => {
  assert.deepEqual(toggledMonth([10, 11, 12, 1, 2, 3], 3), [10, 11, 12, 1, 2]);
  assert.deepEqual(toggledMonth([10, 11, 12, 1, 2], 4), [10, 11, 12, 1, 2, 4]);
  assert.deepEqual(toggledMonth([11, 12, 1, 2], 10), [10, 11, 12, 1, 2]);
  assert.deepEqual(toggledMonth([11], 11), [11]);
  assert.deepEqual(toggledMonth("", 11), [11]);
});

test("months read as a season in the reader's language", () => {
  assert.equal(seasonText([10, 11, 12, 1, 2, 3], "en-GB", "All year"), "Oct – Mar");
  assert.equal(seasonText("1, 5, 6, 7", "en-GB", "All year"), "Jan, May – Jul");
  assert.equal(seasonText([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], "en-GB", "All year"), "All year");
  assert.equal(seasonText([12, 1], "de-DE", "Das ganze Jahr", "long"), "Dezember – Januar");
  assert.equal(seasonText([], "en-GB", "All year"), "");
});

test("a bar fills from the low end, or out of zero where the range holds it", () => {
  assert.deepEqual(barSpan("40", [0, 100]), { at: 0.4, from: 0, to: 0.4 });
  assert.deepEqual(barSpan("", [0, 100], 15), { at: 0.15, from: 0, to: 0.15 });
  assert.deepEqual(barSpan("", [0, 100]), { at: 0, from: 0, to: 0 });
  assert.deepEqual(barSpan("2", [1, 3]), { at: 0.5, from: 0, to: 0.5 });
  assert.deepEqual(barSpan("100", [-200, 200]), { at: 0.75, from: 0.5, to: 0.75 });
  assert.deepEqual(barSpan("-100", [-200, 200]), { at: 0.25, from: 0.25, to: 0.5 });
  assert.deepEqual(barSpan("5000", [0, 2400]), { at: 1, from: 0, to: 1 });
  assert.deepEqual(barSpan("-5", [0, 100]), { at: 0, from: 0, to: 0 });
  assert.equal(barSpan("1e", [0, 100]), null);
  assert.equal(barSpan("40", null), null);
  assert.equal(barSpan("40", [5, 5]), null);
});
