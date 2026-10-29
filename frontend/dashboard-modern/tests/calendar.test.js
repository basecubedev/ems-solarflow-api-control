// SPDX-License-Identifier: AGPL-3.0-or-later
// The day the clocks go back in Germany, 2026-10-25, has 25 hours. Every test
// in this file runs in that zone, so the calendar is the one an owner sees.
process.env.TZ = "Europe/Berlin";

import test from "node:test";
import assert from "node:assert/strict";
import { hourlyKwh, localDayStart, sameTimeComparison } from "../src/lib/history.js";

const at = (...parts) => Math.floor(new Date(...parts).getTime() / 1000);

function constant(from, to, watts = 1000, step = 300) {
  const time = [];
  for (let t = from; t <= to; t += step) time.push(t);
  return { time, values: time.map(() => watts) };
}

test("a calendar day is as long as the calendar says", () => {
  const day = new Date(2026, 9, 25, 12);
  assert.equal(localDayStart(day, 1) - localDayStart(day), 25 * 3600);
  assert.equal(localDayStart(new Date(2026, 2, 29, 12), 1) - localDayStart(new Date(2026, 2, 29, 12)), 23 * 3600);
  assert.equal(localDayStart(new Date(2026, 9, 26, 9), -1), at(2026, 9, 25));
});

test("the day the clocks go back keeps all of its energy, its last hour included", () => {
  const start = localDayStart(new Date(2026, 9, 25, 12));
  const end = localDayStart(new Date(2026, 9, 25, 12), 1);
  const { time, values } = constant(start, end);
  const hours = hourlyKwh(time, values, start);
  assert.equal(hours.length, 24);
  assert.ok(Math.abs(hours[2] - 2) < 1e-9);
  assert.ok(Math.abs(hours[22] - 1) < 1e-9);
  assert.ok(Math.abs(hours[23] - 1) < 1e-9);
  assert.ok(Math.abs(hours.reduce((sum, kwh) => sum + kwh, 0) - 25) < 1e-9);
});

test("the hour the clocks skip has no energy rather than an hour of someone else's", () => {
  const start = localDayStart(new Date(2026, 2, 29, 12));
  const { time, values } = constant(start, localDayStart(new Date(2026, 2, 29, 12), 1));
  const hours = hourlyKwh(time, values, start);
  assert.equal(hours[2], null);
  assert.ok(Math.abs(hours.reduce((sum, kwh) => sum + (kwh || 0), 0) - 23) < 1e-9);
});

test("the day after the clocks go back compares by the clock, from yesterday's midnight", () => {
  const now = at(2026, 9, 26, 10);
  const today = localDayStart(new Date(now * 1000));
  const history = { time: [], series: {} };
  const { time, values } = constant(localDayStart(new Date(now * 1000), -1), now);
  history.time = time;
  history.series.pv = values;
  const comparison = sameTimeComparison(history, "pv", now, today);
  assert.ok(Math.abs(comparison.today - 10) < 1e-9);
  assert.ok(Math.abs(comparison.yesterday - 11) < 1e-9);
});
