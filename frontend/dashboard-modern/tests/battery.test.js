// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { test } from "node:test";
import assert from "node:assert/strict";

import { MINUS, MISSING } from "../../shared/lib/format.js";
import { STRINGS } from "../src/i18n/strings.js";
import {
  batteryChartIds,
  batteryKpi,
  batteryKpiText,
  batteryMeasures,
  batteryOverlays,
  batteryRequest,
  batteryStyle,
  besideChartIds,
  BATTERY_MEASURES,
  cardSeriesIds,
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
import { ampRange, ampText, celsiusRange, celsiusTick, currentReading } from "../src/lib/batteryAxes.js";

test("Basic offers temperature, charge, cell voltage, pack power and winter; Expert adds voltage, current and the limits", () => {
  assert.deepEqual(batteryMeasures(false).map((m) => m.id), ["temperature", "charge", "cells", "power", "winter"]);
  assert.deepEqual(batteryMeasures(true).map((m) => m.id), ["temperature", "charge", "cells", "power", "winter", "voltage", "current", "limits"]);
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

test("the temperature draws every pack and the inverter, the cell voltage one pack", () => {
  assert.deepEqual(batteryChartIds("temperature", [1, 2, 3], 2, [], false), ["pack_temp:1", "pack_temp:2", "pack_temp:3", "device_temp"]);
  assert.deepEqual(batteryChartIds("cells", [1, 2, 3], 2, [], false), ["cell_min:2", "cell_max:2"]);
  assert.deepEqual(batteryChartIds("limits", [1], 1, [], true), ["charge_avail", "discharge_avail", "input_limit"]);
});

test("an overlay is drawn only where the detail level offers it; the inverter is no overlay but a line of the temperature", () => {
  const all = ["soc", "min_soc", "winter_target", "device_temp"];
  assert.deepEqual(batteryChartIds("temperature", [1], 1, all, false), ["pack_temp:1", "device_temp", "soc"]);
  assert.deepEqual(batteryChartIds("temperature", [1], 1, all, true), ["pack_temp:1", "device_temp", "soc", "min_soc", "winter_target"]);
  assert.deepEqual(batteryChartIds("cells", [1], 1, all, true), ["cell_min:1", "cell_max:1", "soc", "min_soc", "winter_target"]);
  assert.deepEqual(batteryOverlays(false).map((o) => o.id), ["soc"]);
  assert.deepEqual(batteryOverlays(true).map((o) => o.id), ["soc", "min_soc", "winter_target"]);
});

test("pack power, voltage and current draw every pack; the voltage the device's own beside them", () => {
  assert.deepEqual(batteryChartIds("power", [1, 2], 1, ["soc"], false), ["pack_power:1", "pack_power:2", "soc"]);
  assert.deepEqual(batteryChartIds("voltage", [1, 2], 1, [], true), ["pack_voltage:1", "pack_voltage:2", "device_voltage"]);
  assert.deepEqual(batteryChartIds("current", [2], 2, [], true), ["pack_current:2"]);
  assert.deepEqual(batteryChartIds("voltage", [], null, [], true), []);
  assert.deepEqual(batteryMeasures(true).map((m) => m.unit), ["°C", "%", "V", "W", "W", "V", "A", "W"]);
  for (const id of ["device_temp", "device_voltage"]) {
    assert.deepEqual(batteryStyle(id), { label: `series.${id}`, token: "--inverter", fill: false });
  }
  assert.deepEqual(batteryStyle("pack_power:2"), { label: "series.pack", args: { n: 2 }, token: "--pv", fill: false });
});

test("a current axis is in whole amps and always shows 0 A", () => {
  assert.deepEqual(ampRange(-2.5, 0.1), [-3, 1]);
  assert.deepEqual(ampRange(0.2, 6.3), [0, 7]);
  assert.deepEqual(ampRange(2.2, 6.3), [0, 7]);
  assert.deepEqual(ampRange(-6.3, -2.2), [-7, 1]);
  assert.deepEqual(ampRange(null, undefined), [0, 1]);
});

const fakeFormat = {
  number: (value, digits) => value.toFixed(digits),
  power: (value) => (value === null ? MISSING : `${value} W`),
  voltage: (value) => (value === null ? MISSING : `${value.toFixed(2)} V`),
};

test("a pack's current reads as Charge or Discharge only once it rounds to a tenth of an amp", () => {
  assert.deepEqual(currentReading(-6.2), { key: "chart.charge", amps: 6.2 });
  assert.deepEqual(currentReading(2.1), { key: "chart.discharge", amps: 2.1 });
  assert.deepEqual(currentReading(-0.03), { key: null, amps: 0 });
  assert.deepEqual(currentReading(0.04), { key: null, amps: 0 });
  assert.deepEqual(currentReading(0.05), { key: "chart.discharge", amps: 0.1 });
});

test("a current reads to a tenth of an amp with a true minus", () => {
  assert.equal(ampText(-2.5, fakeFormat), `${MINUS}2.5\u00a0A`);
  assert.equal(ampText(6.25, fakeFormat), "6.3\u00a0A");
  assert.equal(ampText(null, fakeFormat), MISSING);
  assert.equal(ampText(0.6, fakeFormat, { signed: true }), "+0.6\u00a0A");
  assert.equal(ampText(-0.04, fakeFormat), "0.0\u00a0A");
  assert.equal(ampText(0.04, fakeFormat, { signed: true }), "0.0\u00a0A");
});

test("the cell voltage's card reads the pack's own cell spread, which the request carries beside the chart", () => {
  assert.deepEqual(cardSeriesIds("cells", 2), ["cell_spread:2"]);
  assert.deepEqual(cardSeriesIds("cells", null), []);
  assert.deepEqual(cardSeriesIds("temperature", 1), []);
  assert.deepEqual(batteryRequest(["cell_min:2", "cell_max:2"], false, ["cell_spread:2"]), ["cell_min:2", "cell_max:2", "cell_spread:2", "heat_state", "low_temp"]);
  assert.deepEqual(batteryRequest([], false, ["cell_spread:2"]), []);
  assert.equal(batteryKpi({ series: { "cell_spread:2": [0.01, null, 0.012] } }, ["cell_spread:2"], "max"), 0.012);
});

test("a card names the strongest charge and discharge as sizes, from values kept positive while charging", () => {
  const data = { series: { "pack_power:1": [300, -120, null], "pack_power:2": [-80, 40] } };
  const ids = ["pack_power:1", "pack_power:2"];
  assert.equal(batteryKpi(data, ids, "charge"), 300);
  assert.equal(batteryKpi(data, ids, "discharge"), 120);
  assert.equal(batteryKpi({ series: { "pack_power:1": [5, 9] } }, ["pack_power:1"], "discharge"), 0);
  assert.equal(batteryKpi({ series: {} }, ids, "charge"), null);
  assert.equal(batteryKpi(data, ids, "min"), -120);
  assert.equal(batteryKpiText(batteryKpi(data, ids, "charge"), "power", fakeFormat), "300 W");
  assert.equal(batteryKpiText(0.012, "millivolt", fakeFormat), "12\u00a0mV");
  assert.equal(batteryKpiText(null, "millivolt", fakeFormat), MISSING);
  assert.equal(batteryKpiText(-1.25, "current", fakeFormat), `${MINUS}1.3\u00a0A`);
});

test("every measure names each card once, and every measure, card and line has its words in every language", () => {
  for (const measure of BATTERY_MEASURES) {
    const labels = measure.kpis.map((kpi) => kpi.label);
    assert.equal(new Set(labels).size, labels.length, measure.id);
    const keys = [`battery.measure.${measure.id}`, `analytics.sourceBattery.${measure.id}`, ...labels, ...(measure.device ? [`series.${measure.device}`] : [])];
    for (const key of keys) assert.ok(key in STRINGS, key);
  }
});

test("a device without packs asks for nothing, not for its lanes alone", () => {
  assert.deepEqual(batteryChartIds("temperature", [], null, ["soc"], true), []);
  assert.deepEqual(batteryRequest([], true), []);
});

test("one request carries the chart and the lanes of the detail level", () => {
  assert.deepEqual(batteryRequest(["pack_temp:1"], false), ["pack_temp:1", "heat_state", "low_temp"]);
  assert.deepEqual(stateLanes(true).map((lane) => lane.id), ["heat_state", "low_temp", "ac_input", "winter_hold", "derate", "error_flag"]);
});

test("winter draws the device's power and charge with the packs' temperatures beside them, and takes no overlays", () => {
  const winter = ["pv", "ac_charge", "battery", "output", "charge_avail", "soc", "min_soc", "winter_target"];
  assert.deepEqual(batteryChartIds("winter", [1, 2], null, ["soc", "min_soc", "winter_target"], false), winter);
  assert.deepEqual(batteryChartIds("winter", [], null, [], true), winter);
  assert.deepEqual(besideChartIds("winter", [1, 2]), ["pack_temp:1", "pack_temp:2", "device_temp"]);
  assert.deepEqual(besideChartIds("temperature", [1, 2]), []);
  assert.deepEqual(batteryOverlays(true, "winter"), []);
  assert.deepEqual(batteryOverlays(false, "temperature").map((o) => o.id), ["soc"]);
  assert.equal(shownMeasure("winter", false).id, "winter");
  for (const id of winter) assert.ok(`series.${id}` in STRINGS, id);
});

test("winter shows in Basic the lanes that say what the EMS decided, and one request carries both charts", () => {
  assert.deepEqual(stateLanes(false, "winter").map((lane) => lane.id), ["heat_state", "low_temp", "ac_input", "winter_hold"]);
  assert.deepEqual(stateLanes(false, "temperature").map((lane) => lane.id), ["heat_state", "low_temp"]);
  assert.deepEqual(stateLanes(true, "winter").map((lane) => lane.id), ["heat_state", "low_temp", "ac_input", "winter_hold", "derate", "error_flag"]);
  assert.deepEqual(batteryRequest(["pv", "pack_temp:1"], false, [], "winter"), ["pv", "pack_temp:1", "heat_state", "low_temp", "ac_input", "winter_hold"]);
});

test("winter draws the device's power as the other views do, and what the battery accepts as a dotted floor under the charge", () => {
  assert.deepEqual(batteryStyle("pv", "winter"), { label: "series.pv", token: "--pv" });
  assert.deepEqual(batteryStyle("battery", "winter"), { label: "series.battery", token: "--battery" });
  assert.deepEqual(batteryStyle("charge_avail", "winter"), { label: "series.charge_avail", token: "--m-muted", dash: [2, 3], below: true });
  assert.deepEqual(batteryStyle("charge_avail", "limits"), { label: "series.charge_avail", token: "--battery", fill: false });
  assert.equal(batteryStyle("soc", "winter").unit, "%");
  assert.equal(batteryStyle("output", "temperature"), null);
});

test("every lane names where it comes from: the device reports its battery's states, the EMS decides the charging role and the winter hold", () => {
  const sources = Object.fromEntries(stateLanes(true).map((lane) => [lane.id, lane.source]));
  assert.deepEqual(sources, { heat_state: "device", low_temp: "device", ac_input: "ems", winter_hold: "ems", derate: "device", error_flag: "device" });
  for (const source of new Set(Object.values(sources))) {
    assert.ok(`lanes.source.${source}` in STRINGS, source);
    assert.ok(`lanes.sourceTitle.${source}` in STRINGS, source);
  }
  assert.equal(STRINGS["series.winter_hold"].de, "Akku zuerst");
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
