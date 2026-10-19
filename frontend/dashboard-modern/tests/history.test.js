// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { integrateKwh, hourlyKwh, gapRanges, withGapBreaks, sameTimeComparison, sampleSpacing, localDayStart } from "../src/lib/history.js";

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
