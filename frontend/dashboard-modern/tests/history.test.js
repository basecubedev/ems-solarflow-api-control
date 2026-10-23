// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { integrateKwh, hourlyKwh, gapRanges, withGapBreaks, sameTimeComparison, sampleSpacing, localDayStart, niceCeiling, zoomStep, zoomDiffers, zoomSpan, zoomCenter, escapeLeavesZoom, emptyKey, sourceKey } from "../src/lib/history.js";

const MIDNIGHT = localDayStart(new Date(1_800_000_000 * 1000));

const step = 300;
const series = (count, value, start = 0) => ({
  time: Array.from({ length: count }, (_, i) => start + i * step),
  values: Array.from({ length: count }, () => value),
});

test("a constant 1 kW for one hour integrates to 1 kWh", () => {
  const { time, values } = series(13, 1000);
  assert.ok(Math.abs(integrateKwh(time, values) - 1) < 1e-9);
});

test("missing samples add nothing and are not counted as zero", () => {
  const { time, values } = series(13, 1000);
  values[6] = null;
  assert.ok(Math.abs(integrateKwh(time, values) - (10 / 12)) < 1e-9);
  assert.equal(integrateKwh(time, values.map(() => null)), null);
});

test("a step longer than the gap limit is a gap, not a straight line", () => {
  const time = [0, 300, 600, 7200, 7500];
  const values = [1000, 1000, 1000, 1000, 1000];
  assert.equal(sampleSpacing(time), 300);
  assert.ok(Math.abs(integrateKwh(time, values) - (900 / 3600)) < 1e-9);
  assert.deepEqual(gapRanges(time, [values]), [{ from: 600, to: 7200 }]);
  const broken = withGapBreaks(time, [values]);
  assert.equal(broken.time.length, 6);
  assert.equal(broken.series[0][3], null);
});

test("the gap limit is three typical steps, as in Classic, without a floor", () => {
  const time = [];
  for (let t = 0; t <= 3600; t += 5) if (t < 1800 || t > 1905) time.push(t);
  const values = time.map(() => 1000);
  assert.equal(sampleSpacing(time), 5);
  assert.deepEqual(gapRanges(time, [values]), [{ from: 1795, to: 1910 }]);
  assert.ok(Math.abs(integrateKwh(time, values) - (3600 - 115) / 3600) < 1e-9);
});

test("a transform splits a signed series into its two directions", () => {
  const { time } = series(13, 0);
  const values = time.map((_, i) => (i < 6 ? 600 : -600));
  const imported = integrateKwh(time, values, { transform: (v) => Math.max(0, v) });
  const exported = integrateKwh(time, values, { transform: (v) => Math.max(0, -v) });
  assert.ok(imported > 0 && exported > 0);
});

test("hourly energy leaves an hour without samples empty", () => {
  const day = MIDNIGHT;
  const time = Array.from({ length: 25 }, (_, i) => day + i * step);
  const values = time.map(() => 600);
  const hours = hourlyKwh(time, values, day);
  assert.ok(Math.abs(hours[0] - 0.6) < 1e-9);
  assert.equal(hours[5], null);
});

test("today is compared with yesterday up to the same time of day", () => {
  const dayStart = MIDNIGHT;
  const time = [];
  const pv = [];
  for (let t = dayStart - 86400; t <= dayStart + 3 * 3600; t += step) {
    time.push(t);
    pv.push(t < dayStart ? 500 : 1000);
  }
  const result = sameTimeComparison({ time, series: { pv } }, "pv", dayStart + 3 * 3600, dayStart);
  assert.ok(Math.abs(result.today - 3) < 1e-9);
  assert.ok(Math.abs(result.yesterday - 1.5) < 1e-9);
  assert.ok(Math.abs(result.delta - 1.5) < 1e-9);
});

test("no comparison when the history does not reach back to yesterday morning", () => {
  const dayStart = MIDNIGHT;
  const time = [dayStart - 3600, dayStart, dayStart + 3600];
  assert.equal(sameTimeComparison({ time, series: { pv: [1, 1, 1] } }, "pv", dayStart + 3600, dayStart), null);
});

test("the hourly bars are scaled to the next round figure above the largest", () => {
  assert.equal(niceCeiling(0.52), 0.6);
  assert.equal(niceCeiling(0.6), 0.6);
  assert.equal(niceCeiling(0.61), 0.8);
  assert.equal(niceCeiling(1.2), 1.5);
  assert.equal(niceCeiling(-0.07), 0.08);
  assert.equal(niceCeiling(9.1), 10);
  assert.equal(niceCeiling(0.3), 0.3);
  assert.equal(niceCeiling(0), null);
  assert.equal(niceCeiling(null), null);
  assert.equal(niceCeiling(Number.NaN), null);
});

test("a zoom step keeps the point under the pointer, stays inside the bounds and never narrows below a minute", () => {
  assert.deepEqual(zoomStep(0, 1000, 500, 0.5, 0, 1000), { min: 250, max: 750 });
  assert.deepEqual(zoomStep(0, 1000, 0, 0.5, 0, 1000), { min: 0, max: 500 });
  assert.deepEqual(zoomStep(400, 600, 500, 4, 0, 1000), { min: 100, max: 900 });
  assert.deepEqual(zoomStep(900, 1000, 950, 4, 0, 1000), { min: 600, max: 1000 });
  assert.equal(zoomStep(0, 100, 50, 0.01, 0, 1000).max - zoomStep(0, 100, 50, 0.01, 0, 1000).min, 60);
  assert.equal(zoomStep(0, 0, 0, 0.5, 0, 1000), null);
});

test("a window equal to its extent is no zoom; one that differs either way is", () => {
  assert.equal(zoomDiffers(0, 1000, 0, 1000), false);
  assert.equal(zoomDiffers(2, 999, 0, 1000), false);
  assert.equal(zoomDiffers(100, 1000, 0, 1000), true);
  assert.equal(zoomDiffers(-100, 1000, 0, 1000), true);
});

test("an empty chart says whether it loads, has no answer or no samples; the badge names known sources only", () => {
  assert.equal(emptyKey(null, false), "chart.loading");
  assert.equal(emptyKey(null, true), "chart.unavailable");
  assert.equal(emptyKey({ meta: { unavailable: true } }, false), "chart.unavailable");
  assert.equal(emptyKey({ meta: {} }, false), "chart.empty");
  assert.deepEqual([sourceKey("sqlite"), sourceKey("influxdb"), sourceKey("other")], ["source.sqlite", "source.influxdb", null]);
});

test("a zoomed window is named by its length as Classic names the KPI period", () => {
  assert.deepEqual(zoomSpan(20), { key: "chart.span.m", n: 1 });
  assert.deepEqual(zoomSpan(45 * 60), { key: "chart.span.m", n: 45 });
  assert.deepEqual(zoomSpan(3600 * 9.46), { key: "chart.span.h", n: 9.5 });
  assert.deepEqual(zoomSpan(86400 * 3.04), { key: "chart.span.d", n: 3 });
  assert.equal(zoomSpan(0), null);
  assert.equal(zoomSpan(Number.NaN), null);
});

test("the buttons zoom around the data inside the window, not into an empty future", () => {
  assert.equal(zoomCenter(0, 86400, -86400, 6.5 * 3600), 3.25 * 3600);
  assert.equal(zoomCenter(100, 200, 0, 1000), 150);
  assert.equal(zoomCenter(100, 200, 300, 400), 150);
  assert.equal(zoomCenter(100, 200, undefined, undefined), 150);
});

test("Escape leaves a zoom only when no dialog or popover takes it", () => {
  const none = { querySelector: () => null };
  const open = { querySelector: (selector) => (selector.split(",").map((part) => part.trim()).includes("dialog[open]") ? {} : null) };
  const oldEngine = { querySelector: (selector) => { if (selector.includes(":popover-open")) throw new SyntaxError(selector); return null; } };
  assert.equal(escapeLeavesZoom({ key: "Escape" }, none), true);
  assert.equal(escapeLeavesZoom({ key: "Escape" }, open), false);
  assert.equal(escapeLeavesZoom({ key: "Escape", defaultPrevented: true }, none), false);
  assert.equal(escapeLeavesZoom({ key: "Enter" }, none), false);
  assert.equal(escapeLeavesZoom({ key: "Escape" }, oldEngine), true);
});
