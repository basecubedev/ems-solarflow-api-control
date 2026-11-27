// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  batteryChartIds,
  batteryMeasures,
  batteryOverlays,
  batteryRequest,
  batteryStyle,
  celsiusRange,
  celsiusTick,
  laneReported,
  laneSeconds,
  laneSegments,
  laneSummary,
  lanesCoarse,
  laneTicks,
  laneTime,
  laneTotals,
  packExtreme,
  packSeriesIds,
  seriesPack,
  shownMeasure,
  stateLanes,
  shownPacks,
} from "../src/lib/battery.js";

test("Basic offers temperature, charge and cell voltage; Expert adds the limits", () => {
  assert.deepEqual(batteryMeasures(false).map((m) => m.id), ["temperature", "charge", "cells"]);
  assert.deepEqual(batteryMeasures(true).map((m) => m.id), ["temperature", "charge", "cells", "limits"]);
});

test("the charge draws every pack's own state of charge on a percent axis, with the percent overlays", () => {
  assert.equal(batteryMeasures(false).find((m) => m.id === "charge").unit, "%");
  assert.deepEqual(packSeriesIds("charge", [1, 2]), ["pack_soc:1", "pack_soc:2"]);
  assert.deepEqual(packSeriesIds("cells", [1, 2]), []);
  const all = ["soc", "min_soc", "winter_target", "device_temp"];
  assert.deepEqual(batteryChartIds("charge", [1, 2], 1, all, true), ["pack_soc:1", "pack_soc:2", "soc", "min_soc", "winter_target"]);
  assert.deepEqual(batteryChartIds("charge", [], null, all, true), []);
  assert.deepEqual(batteryStyle("pack_soc:3"), { label: "series.pack", args: { n: 3 }, token: "--output", fill: false });
});

test("a measure Basic does not offer falls back to the temperature", () => {
  assert.equal(shownMeasure("limits", false).id, "temperature");
  assert.equal(shownMeasure("limits", true).id, "limits");
  assert.equal(shownMeasure("nonsense", true).id, "temperature");
});

test("the temperature draws every pack, the cell voltage one pack", () => {
  assert.deepEqual(batteryChartIds("temperature", [1, 2, 3], 2, [], false), ["pack_temp:1", "pack_temp:2", "pack_temp:3"]);
  assert.deepEqual(batteryChartIds("cells", [1, 2, 3], 2, [], false), ["cell_min:2", "cell_max:2"]);
  assert.deepEqual(batteryChartIds("limits", [1], 1, [], true), ["charge_avail", "discharge_avail", "input_limit"]);
});

test("an overlay is drawn only where the detail level and the measure take it", () => {
  const all = ["soc", "min_soc", "winter_target", "device_temp"];
  assert.deepEqual(batteryChartIds("temperature", [1], 1, all, false), ["pack_temp:1", "soc"]);
  assert.deepEqual(batteryChartIds("temperature", [1], 1, all, true), ["pack_temp:1", "soc", "min_soc", "winter_target", "device_temp"]);
  assert.deepEqual(batteryChartIds("cells", [1], 1, all, true), ["cell_min:1", "cell_max:1", "soc", "min_soc", "winter_target"]);
  assert.deepEqual(batteryOverlays(false, "temperature").map((o) => o.id), ["soc"]);
});

test("a device without packs asks for nothing, not for its lanes alone", () => {
  assert.deepEqual(batteryChartIds("temperature", [], null, ["soc"], true), []);
  assert.deepEqual(batteryRequest([], true), []);
});

test("one request carries the chart and the lanes of the detail level", () => {
  assert.deepEqual(batteryRequest(["pack_temp:1"], false), ["pack_temp:1", "heat_state", "low_temp"]);
  assert.deepEqual(stateLanes(true).map((lane) => lane.id), ["heat_state", "low_temp", "ac_input", "winter_hold", "derate", "error_flag"]);
});

test("a pack series names its pack", () => {
  assert.equal(seriesPack("pack_temp:12"), 12);
  assert.equal(seriesPack("pack_soc:2"), 2);
  assert.equal(seriesPack("pack_temp:0"), null);
  assert.equal(seriesPack("cell_min:1"), null);
  assert.equal(seriesPack("pack_volt:1"), null);
  assert.deepEqual(batteryStyle("pack_temp:2"), { label: "series.pack", args: { n: 2 }, token: "--pv", fill: false });
  assert.equal(batteryStyle("min_soc").unit, "%");
  assert.equal(batteryStyle("pv"), null);
});

test("the temperature axis always shows 0 °C, in steps of five", () => {
  assert.deepEqual(celsiusRange(1, 7), [-5, 10]);
  assert.deepEqual(celsiusRange(-12.5, 22), [-15, 25]);
  assert.deepEqual(celsiusRange(undefined, undefined), [-5, 10]);
});

test("a lane is on from a point above zero to the next point, and a gap ends it", () => {
  const time = [0, 60, 120, 180, 240, 300];
  assert.deepEqual(laneSegments(time, [0, 1, 1, 0, 1, 1]), [{ start: 60, end: 180 }, { start: 240, end: 360 }]);
  assert.deepEqual(laneSegments(time, [1, null, 1, 0, 0, 0]), [{ start: 0, end: 60 }, { start: 120, end: 180 }]);
  assert.equal(laneSeconds(laneSegments(time, [0, 1, 1, 0, 1, 1])), 240);
  assert.deepEqual(laneSegments(time, [0, 0, 0, 0, 0, 0]), []);
});

test("a stretch without points, an outage of the EMS, ends a lane after one typical step", () => {
  const time = [0, 60, 120, 28920, 28980];
  assert.deepEqual(laneSegments(time, [0, 0, 1, 1, 0]), [{ start: 120, end: 180 }, { start: 28920, end: 28980 }]);
  assert.equal(laneSeconds(laneSegments(time, [0, 0, 1, 0, 0])), 60);
  assert.deepEqual(laneSegments([0, 60, 120, 180], [0, 0, 0, 1]), [{ start: 180, end: 240 }]);
});

test("a lane the device never reported is told apart from one that stayed off", () => {
  assert.equal(laneReported([null, null]), false);
  assert.equal(laneReported([0, null]), true);
  assert.equal(laneReported(undefined), false);
});

test("the coldest and the warmest pack over the range", () => {
  const data = { series: { "pack_temp:1": [1.5, null, 2], "pack_temp:2": [-0.5, 3] } };
  assert.equal(packExtreme(data, ["pack_temp:1", "pack_temp:2"], "min"), -0.5);
  assert.equal(packExtreme(data, ["pack_temp:1", "pack_temp:2"], "max"), 3);
  assert.equal(packExtreme({ series: {} }, ["pack_temp:1"], "min"), null);
});

test("the folded States card names the states that were on and for how long", () => {
  const t = (key, args = {}) => ({ "chart.span.m": `${args.n} min`, "lanes.noneOn": "No state was on" })[key];
  const f = { number: (value) => String(value) };
  const time = [0, 60, 120];
  const rows = laneTotals(time, [
    { id: "heat_state", label: "Heating", values: [0, 1, 0] },
    { id: "low_temp", label: "Too cold", values: [1, 1, 1] },
    { id: "derate", label: "Derating", values: [null, null, null] },
  ]);
  assert.equal(laneSummary(rows, t, f), "Heating 1 min · Too cold 3 min");
  assert.equal(laneSummary(laneTotals(time, [{ id: "heat_state", label: "Heating", values: [0, 0, null] }]), t, f), "No state was on");
  assert.equal(laneSummary(laneTotals(time, [{ id: "heat_state", label: "Heating", values: [null, null, null] }]), t, f), "");
  assert.equal(laneTime(0, t, f), "");
});

test("over points further apart than a minute a lane's time is an upper bound, and a single point is on without a time", () => {
  const t = (key, args = {}) => ({ "chart.span.h": `${args.n} h`, "lanes.upTo": `up to ${args.time}`, "lanes.noneOn": "No state was on" })[key];
  const f = { number: (value) => String(value) };
  const hourly = [0, 3600, 7200, 10800];
  assert.equal(lanesCoarse(hourly), true);
  assert.equal(lanesCoarse([0, 60, 120]), false);
  assert.equal(lanesCoarse([0]), false);
  const rows = laneTotals(hourly, [{ id: "heat_state", label: "Heating", values: [0, 1, 0, 0] }]);
  assert.equal(laneSummary(rows, t, f, lanesCoarse(hourly)), "Heating up to 1 h");
  const single = laneTotals([600], [{ id: "heat_state", label: "Heating", values: [1] }]);
  assert.equal(single[0].segments.length, 1);
  assert.equal(laneSummary(single, t, f), "Heating");
});

test("a whole degree on the temperature axis drops the decimal", () => {
  const f = { number: (value, digits) => value.toFixed(digits), temperature: (value) => `${value.toFixed(1)} °C` };
  assert.equal(celsiusTick(10, f), "10\u00a0°C");
  assert.equal(celsiusTick(-4, f), "\u22124\u00a0°C");
  assert.equal(celsiusTick(0, f), "0\u00a0°C");
  assert.equal(celsiusTick(2.5, f), "2.5 °C");
});

test("the lanes' time scale is labelled at its ends and evenly between", () => {
  assert.deepEqual(laneTicks(0, 400), [0, 100, 200, 300, 400]);
  assert.deepEqual(laneTicks(10, 10), []);
  assert.deepEqual(laneTicks(0, 60, 2), [0, 60]);
});

test("the temperature draws a pack that reports no cell voltage, and every pack the device counts", () => {
  const device = { packs: 3, cells: [{ pack: 1, low: 3.2, high: 3.21 }, { pack: 2, low: null, high: null }] };
  const offered = { cells: [{ pack: 1, low: 3.2, high: 3.21 }] };
  assert.deepEqual(shownPacks(device, offered), [1, 2, 3]);
  assert.deepEqual(shownPacks({ packs: 0, cells: [] }, { cells: [{ pack: 1, low: null, high: null }] }), [1]);
  assert.deepEqual(shownPacks(undefined, null), []);
  const uncounted = { packs: null, cells: [{ pack: 1, low: 3.2, high: 3.21 }, { pack: 2, low: null, high: null }] };
  assert.deepEqual(shownPacks(uncounted, offered), [1, 2]);
  assert.deepEqual(shownPacks({ packs: 40, cells: [] }, null).length, 16);
});
