// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { integrateKwh, hourlyKwh, gapRanges, withGapBreaks, sameTimeComparison, sampleSpacing, localDayStart, niceCeiling, zoomStep, zoomDiffers, zoomSpan, zoomCenter, escapeLeavesZoom, emptyKey, sourceKey, tickLabels, tickSpace, tickPad, atLeastAMinute } from "../src/lib/history.js";
import { createFormat } from "../../shared/lib/format.js";

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

const ticksOf = (start, span, step) => {
  if (step >= 86400) {
    const first = new Date(start * 1000);
    return Array.from({ length: Math.floor(span / 86400) }, (_, i) => new Date(first.getFullYear(), first.getMonth(), first.getDate() + 1 + i).getTime() / 1000);
  }
  const ticks = [];
  for (let at = Math.ceil(start / step) * step; at <= start + span; at += step) ticks.push(at);
  return ticks;
};

test("a tick names its seconds only when the ticks are less than a minute apart", () => {
  const f = createFormat("en");
  const at = new Date(2026, 9, 8, 12, 27, 30).getTime() / 1000;
  assert.deepEqual(tickLabels([at, at + 30], 30, f), ["12:27:30", "12:28:00"]);
  assert.deepEqual(tickLabels([at - 30, at + 570], 600, f), ["12:27", "12:37"]);
  assert.deepEqual(tickLabels([at, at + 7 * 86400], 7 * 86400, f), [f.date(at * 1000, { short: true }), f.date((at + 7 * 86400) * 1000, { short: true })]);
});

test("a whole day reads as clock times, its closing midnight with the weekday", () => {
  const f = createFormat("en");
  const day = new Date(2026, 9, 10).getTime() / 1000;
  const ticks = Array.from({ length: 13 }, (_, i) => day + i * 7200);
  const labels = tickLabels(ticks, 7200, f);
  assert.equal(labels[0], "00:00");
  assert.equal(labels[6], "12:00");
  assert.equal(labels[12], `${f.weekday((day + 86400) * 1000)} 00:00`);
  assert.ok(labels.slice(0, 12).every((label) => /^\d\d:\d\d$/.test(label)));
});

test("the ticks of a window never share a label, whatever the window and the step", () => {
  const f = createFormat("en");
  const start = new Date(2026, 9, 8, 9, 30).getTime() / 1000;
  const windows = [[180, 30], [23.9 * 3600, 3600], [25 * 3600, 2 * 3600], [26 * 3600, 2 * 3600], [2.5 * 86400, 6 * 3600], [7.3 * 86400, 12 * 3600], [9 * 86400, 12 * 3600], [30 * 86400, 86400]];
  for (const [span, step] of windows) {
    const labels = tickLabels(ticksOf(start, span, step), step, f);
    assert.equal(new Set(labels).size, labels.length, `${span}/${step}: ${labels.join(", ")}`);
  }
  const twentyFiveHours = Array.from({ length: 13 }, (_, i) => new Date(2026, 9, 8, 10 + 2 * i).getTime() / 1000);
  assert.ok(tickLabels(twentyFiveHours, 2 * 3600, f).every((label) => label.includes(" ")));
});

test("ticks a day apart over more than a year name the year", () => {
  const f = createFormat("en");
  const newYears = [2026, 2027, 2028].map((year) => new Date(year, 0, 1).getTime() / 1000);
  const labels = tickLabels(newYears, 365 * 86400, f);
  assert.equal(new Set(labels).size, 3);
  assert.ok(labels.every((label, i) => label.includes(String(2026 + i))));
});

test("where no form tells every tick apart, the one that tells the most apart wins", () => {
  const ticks = [30, 3630, 7230, 10830];
  const clock = { 30: "23:00", 3630: "01:00", 7230: "01:00", 10830: "02:00" };
  const day = { 30: "Sat", 3630: "Sat", 7230: "Sun", 10830: "Sun" };
  const f = { time: (ms) => clock[ms / 1000], weekday: (ms) => day[ms / 1000], dateTime: (ms) => `Oct ${clock[ms / 1000]}` };
  assert.deepEqual(tickLabels(ticks, 3600, f), ["Sat 23:00", "Sat 01:00", "Sun 01:00", "Sun 02:00"]);
  const sameDay = { ...f, weekday: (ms) => (ms < 3630000 ? "Sat" : "Sun") };
  assert.deepEqual(tickLabels([30, 3630, 7230], 3600, sameDay), ["23:00", "01:00", "01:00"]);
});

test("ticks a week apart or more get room for a date and time, and more for a past year", () => {
  const july = new Date(new Date().getFullYear(), 6, 1).getTime() / 1000;
  const lastYear = new Date(new Date().getFullYear() - 1, 11, 20).getTime() / 1000;
  assert.equal(tickSpace(july, july + 86400), 72);
  assert.equal(tickSpace(july, july + 7 * 86400), 100);
  assert.equal(tickSpace(july, july + 7 * 86400 - 3600), 100);
  assert.equal(tickSpace(lastYear, lastYear + 9 * 86400), 125);
  assert.equal(tickSpace(undefined, undefined), 72);
  assert.equal(tickPad(july, july + 600), 44);
  assert.equal(tickPad(july, july + 86400), 32);
  assert.equal(tickPad(july, july + 7 * 86400), 64);
  assert.equal(tickPad(july, july + 7 * 86400 - 3600), 64);
  assert.equal(tickPad(undefined, undefined), 32);
});

test("a window shorter than a minute widens to one around its middle", () => {
  assert.deepEqual(atLeastAMinute(100, 110), [75, 135]);
  assert.deepEqual(atLeastAMinute(100, 400), [100, 400]);
  assert.deepEqual(atLeastAMinute(null, null), [null, null]);
});

test("a zoom narrower than a minute widens to a minute around its middle", () => {
  assert.deepEqual(zoomStep(100, 102, 101, 1, 0, 1000), { min: 71, max: 131 });
  assert.deepEqual(zoomStep(100, 400, 250, 1, 0, 1000), { min: 100, max: 400 });
});

test("a tick a skipped hour brought closer than the step keeps a clock time, but no wider label", () => {
  const f = createFormat("en");
  const midnight = new Date(2026, 9, 10).getTime() / 1000;
  const clock = tickLabels([midnight - 7200, midnight, midnight + 3600, midnight + 3 * 3600], 7200, f);
  assert.deepEqual(clock, ["22:00", `${f.weekday(midnight * 1000)} 00:00`, "01:00", "03:00"]);
  const at = (day, hour) => new Date(2026, 9, day, hour).getTime() / 1000;
  const wide = tickLabels([at(10, 10), at(10, 12), at(10, 13), at(11, 10)], 7200, f);
  assert.equal(wide[2], null);
  assert.deepEqual(wide.filter((label) => label !== null), [`${f.weekday(at(10, 10) * 1000)} 10:00`, `${f.weekday(at(10, 12) * 1000)} 12:00`, `${f.weekday(at(11, 10) * 1000)} 10:00`]);
});

test("a day tick an hour before midnight, where the clock skips its midnight, names the day it stands for", () => {
  const f = createFormat("en");
  const midnight = new Date(2026, 9, 10).getTime() / 1000;
  const labels = tickLabels([midnight, midnight + 23 * 3600], 86400, f);
  assert.deepEqual(labels, [f.date(midnight * 1000, { short: true }), f.date((midnight + 86400) * 1000, { short: true })]);
});
