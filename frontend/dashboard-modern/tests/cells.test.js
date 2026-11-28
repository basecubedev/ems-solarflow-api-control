// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { createFormat } from "../../shared/lib/format.js";
import { chargeText, packChargeRange, packCells, packFlows, packFlowTotal, packReadings, packTemperatures, temperatureRange, temperatureText, cellRange, cellText, cellSeriesIds, cellKind, cellStyle, cellDevices, historyCellDevices, analyticsCellDevices, packsSeen, reportedCells, shownPack, voltRange } from "../src/lib/cells.js";
import { normalizeDevice } from "../src/lib/devices.js";
import { ANALYTICS_VIEWS, requestedSeries, seriesStyle, viewById } from "../src/lib/analytics.js";

test("the live data's packs come in pack order, a missing voltage stays null and a nameless entry is dropped", () => {
  assert.deepEqual(
    packCells([
      { pack: 2, cell_min_v: 3.3, cell_max_v: null },
      { pack: 1, cell_min_v: 3.17, cell_max_v: 3.18 },
      { pack: 0, cell_min_v: 3.2 },
      { pack: "3", cell_min_v: 3.2 },
      null,
    ]),
    [{ pack: 1, low: 3.17, high: 3.18, soc: null }, { pack: 2, low: 3.3, high: null, soc: null }],
  );
  assert.deepEqual(packCells([{ pack: 17, cell_min_v: 3.2 }]), []);
  assert.deepEqual(packCells(undefined), []);
  assert.deepEqual(packCells({ pack: 1 }), []);
});

test("a pack's power is signed by the direction its state names, and a pack that names none is left out", () => {
  const flows = packFlows([
    { pack: 2, power_w: 30, state: 1 },
    { pack: 1, power_w: 4, state: 2 },
    { pack: 3, power_w: -6, state: 2 },
    { pack: 4, power_w: 0, state: 0 },
    { pack: 5, power_w: 5, state: 0 },
    { pack: 8, power_w: 7, state: null },
    { pack: 6, power_w: null, state: 2 },
    { pack: 7, cell_min_v: 3.2 },
    { pack: 17, power_w: 4, state: 2 },
    null,
  ]);
  assert.deepEqual(flows, [
    { pack: 1, watts: -4 },
    { pack: 2, watts: 30 },
    { pack: 3, watts: -6 },
    { pack: 4, watts: 0 },
  ]);
  assert.equal(packFlowTotal(flows), 20);
  assert.equal(packFlowTotal(packFlows([{ pack: 1, power_w: 4, state: 2 }])), -4);
  assert.equal(packFlowTotal([]), null);
  assert.deepEqual(packFlows(undefined), []);
});

test("the packs a card lists are those that report their cells or a power with its direction", () => {
  const device = normalizeDevice("Grill", {
    hardware: { battery: true },
    battery_packs: [
      { pack: 1, cell_min_v: 3.2, cell_max_v: 3.22, power_w: 30, state: 1 },
      { pack: 2, cell_min_v: null, cell_max_v: null, power_w: 4, state: 2 },
      { pack: 3, cell_min_v: null, cell_max_v: null, power_w: 7, state: null },
    ],
  });
  assert.deepEqual(packReadings(device), [
    { pack: 1, low: 3.2, high: 3.22, soc: null, watts: 30, celsius: null },
    { pack: 2, low: null, high: null, soc: null, watts: -4, celsius: null },
  ]);
  assert.deepEqual(packReadings(normalizeDevice("Inverter", { hardware: { battery: false }, battery_packs: [{ pack: 1, cell_min_v: 3.2 }] })), []);
});

test("each pack's temperature comes in pack order, and a pack that reports none is left out", () => {
  assert.deepEqual(
    packTemperatures([
      { pack: 2, temp_c: 25 },
      { pack: 1, temp_c: 13 },
      { pack: 3, temp_c: null },
      { pack: 4 },
      { pack: 5, temp_c: 0 },
      { pack: 17, temp_c: 20 },
      null,
    ]),
    [{ pack: 1, celsius: 13 }, { pack: 2, celsius: 25 }, { pack: 5, celsius: 0 }],
  );
  assert.deepEqual(packTemperatures(undefined), []);
});

test("a pack that reports only its temperature is still listed on the card", () => {
  const device = normalizeDevice("Shed", {
    hardware: { battery: true },
    temperature_c: 31.5,
    battery_packs: [{ pack: 1, cell_min_v: null, cell_max_v: null, temp_c: 13 }],
  });
  assert.equal(device.temperatureC, 31.5);
  assert.deepEqual(packReadings(device), [{ pack: 1, low: null, high: null, soc: null, watts: null, celsius: 13 }]);
  assert.equal(normalizeDevice("Shed", { temperature_c: null }).temperatureC, null);
  assert.deepEqual(normalizeDevice("Inverter", { hardware: { battery: false }, battery_packs: [{ pack: 1, temp_c: 13 }] }).packTemps, []);
});

test("the pack temperatures read lowest to highest, one value when they agree, a dash when none reports", () => {
  const en = createFormat("en");
  assert.deepEqual(temperatureRange([{ pack: 1, celsius: 25 }, { pack: 2, celsius: 13 }]), { low: 13, high: 25 });
  assert.deepEqual(temperatureRange([]), { low: null, high: null });
  assert.equal(temperatureText(en, 13, 25), "13.0–25.0\u00a0°C");
  assert.equal(temperatureText(en, -2, 4), "−2.0 – 4.0\u00a0°C");
  assert.equal(temperatureText(en, -6, -2), "−6.0 – −2.0\u00a0°C");
  assert.equal(temperatureText(en, 13, 13), "13.0\u00a0°C");
  assert.equal(temperatureText(en, null, null), "—");
});

test("a device without a battery has no pack power", () => {
  const packs = [{ pack: 1, power_w: 4, state: 2 }];
  assert.deepEqual(normalizeDevice("Grill", { hardware: { battery: true }, battery_packs: packs }).packFlows, [{ pack: 1, watts: -4 }]);
  assert.deepEqual(normalizeDevice("Inverter", { hardware: { battery: false }, battery_packs: packs }).packFlows, []);
});

test("the range spans every pack and says nothing where no pack reports", () => {
  assert.deepEqual(cellRange([{ pack: 1, low: 3.2, high: 3.22 }, { pack: 2, low: 3.3, high: null }]), { low: 3.2, high: 3.22 });
  assert.deepEqual(cellRange([{ pack: 1, low: null, high: null }]), { low: null, high: null });
  assert.deepEqual(cellRange([]), { low: null, high: null });
});

test("a range of cell voltages reads lowest to highest in the reader's language", () => {
  const en = createFormat("en");
  assert.equal(cellText(en, 3.17, 3.18), "3.17–3.18 V");
  assert.equal(cellText(en, 3.2, 3.2), "3.20 V");
  assert.equal(cellText(en, null, 3.18), "3.18 V");
  assert.equal(cellText(en, null, null), "—");
  assert.equal(cellText(createFormat("de"), 3.17, 3.18), "3,17–3,18 V");
});

test("a pack's two history series, and only they, are cell series", () => {
  assert.deepEqual(cellSeriesIds(2), ["cell_min:2", "cell_max:2"]);
  assert.equal(cellKind("cell_min:2"), "min");
  assert.equal(cellKind("cell_max:12"), "max");
  for (const id of ["cell_min:0", "cell_min:01", "cell_min:", "soc", "consumer:cell_min:1", undefined]) assert.equal(cellKind(id), null, String(id));
  assert.deepEqual(cellStyle("cell_min:1"), { label: "series.cellMin", token: "--battery", fill: false });
  assert.deepEqual(cellStyle("cell_max:1"), { label: "series.cellMax", token: "--m-muted", fill: false });
  assert.equal(cellStyle("battery"), null);
});

test("only a device with a battery and a reported cell voltage gets a chart", () => {
  const device = (name, hardware, packs) => normalizeDevice(name, { hardware, battery_packs: packs });
  const devices = [
    device("Grill", { battery: true }, [{ pack: 1, cell_min_v: 3.17, cell_max_v: 3.18 }]),
    device("Silent", { battery: true }, [{ pack: 1, cell_min_v: null, cell_max_v: null }]),
    device("Inverter", { battery: false }, [{ pack: 1, cell_min_v: 3.2, cell_max_v: 3.2 }]),
    device("Old", { battery: true }, undefined),
  ];
  assert.deepEqual(cellDevices(devices), [{ name: "Grill", cells: [{ pack: 1, low: 3.17, high: 3.18, soc: null }] }]);
  assert.deepEqual(reportedCells(devices[2]), []);
  assert.deepEqual(devices[3].cells, []);
});

test("the cell history offers every battery device, one that reports nothing now by the packs it counts", () => {
  const device = (name, over) => normalizeDevice(name, { hardware: { battery: true }, ...over });
  const devices = [
    device("Grill", { battery_packs: [{ pack: 1, cell_min_v: 3.17, cell_max_v: 3.18 }] }),
    device("Offline", { online: false, pack_num: 2 }),
    device("Unknown", {}),
    normalizeDevice("Inverter", { hardware: { battery: false }, pack_num: 0 }),
  ];
  assert.deepEqual(historyCellDevices(devices), [
    { name: "Grill", cells: [{ pack: 1, low: 3.17, high: 3.18, soc: null }], reporting: true },
    { name: "Offline", cells: [{ pack: 1, low: null, high: null, soc: null }, { pack: 2, low: null, high: null, soc: null }], reporting: false },
    { name: "Unknown", cells: [{ pack: 1, low: null, high: null, soc: null }], reporting: false },
  ]);
});

test("the Analytics cell view leaves out a device the EMS only reads, whose cells InfluxDB never gets", () => {
  const battery = { battery: true };
  const devices = [
    normalizeDevice("Grill", { hardware: battery, battery_packs: [{ pack: 1, cell_min_v: 3.17, cell_max_v: 3.18 }] }),
    normalizeDevice("Shed", { hardware: battery, read_only: true, battery_packs: [{ pack: 1, cell_min_v: 3.2, cell_max_v: 3.21 }] }),
  ];
  assert.deepEqual(analyticsCellDevices(devices).map((entry) => entry.name), ["Grill"]);
  assert.deepEqual(historyCellDevices(devices).map((entry) => entry.name), ["Grill", "Shed"]);
});

test("a device that went quiet keeps the packs it reported before", () => {
  const live = normalizeDevice("Grill", { hardware: { battery: true }, battery_packs: [{ pack: 1, cell_min_v: 3.2 }, { pack: 3, cell_max_v: 3.3 }] });
  const seen = packsSeen([live], {});
  assert.deepEqual(seen, { Grill: 3 });
  assert.equal(packsSeen([live], seen), seen);
  const quiet = normalizeDevice("Grill", { hardware: { battery: true }, online: false });
  assert.deepEqual(historyCellDevices([quiet], seen)[0].cells.map((pack) => pack.pack), [1, 2, 3]);
  assert.deepEqual(historyCellDevices([quiet])[0].cells.map((pack) => pack.pack), [1]);
});

test("a chart keeps the pack it was asked for while the device has it", () => {
  const packs = [{ pack: 1 }, { pack: 2 }];
  assert.equal(shownPack(packs, 2), 2);
  assert.equal(shownPack(packs, 5), 1);
  assert.equal(shownPack([], 1), null);
});

test("the volt axis hugs the data on 50 mV steps", () => {
  assert.deepEqual(voltRange(3.17, 3.18), [3.15, 3.2]);
  assert.deepEqual(voltRange(3.2, 3.2), [3.15, 3.25]);
  assert.deepEqual(voltRange(3.33, 3.65), [3.3, 3.7]);
  assert.deepEqual(voltRange(null, undefined), [3.0, 3.5]);
});

test("the batteries view leaves its series to battery.js, and a battery overlay stays out of the power views", () => {
  const view = viewById("cells");
  assert.ok(view.cells && view.modernOnly);
  assert.deepEqual(requestedSeries(view, ["target", "soc", "grid"], [], 2), ["cell_min:2", "cell_max:2"]);
  assert.deepEqual(requestedSeries(view, ["soc"], [], null), []);
  assert.deepEqual(requestedSeries(viewById("battery"), ["min_soc", "soc", "winter_target", "device_temp"]), ["battery", "soc"]);
  assert.deepEqual(seriesStyle(view, "cell_max:2"), { token: "--m-muted", fill: false });
  assert.deepEqual(seriesStyle(view, "soc").dash, [6, 4]);
  assert.ok(ANALYTICS_VIEWS.filter((item) => item.modernOnly).every((item) => item.consumers || item.cells));
});

test("a device's packs give their own charge, and the card names the emptiest and the fullest", () => {
  const packs = packCells([{ pack: 2, soc: 70, cell_min_v: 3.31 }, { pack: 1, soc: 30 }, { pack: 3 }]);
  assert.deepEqual(packs.map((pack) => [pack.pack, pack.soc]), [[1, 30], [2, 70], [3, null]]);
  assert.deepEqual(packChargeRange(packs), { low: 30, high: 70, count: 2 });
  assert.deepEqual(packChargeRange([]), { low: null, high: null, count: 0 });
  const f = { number: (value) => String(value), percent: (value) => (value === null ? "—" : `${value} %`) };
  assert.equal(chargeText(f, 30, 70), "30–70 %");
  assert.equal(chargeText(f, 55, 55), "55 %");
  assert.equal(chargeText(f, null, null), "—");
});

test("a pack that reports only its charge is still listed on the card", () => {
  const device = normalizeDevice("Shed", { battery_packs: [{ pack: 1, soc: 44 }, { pack: 2, soc: 46 }] });
  assert.deepEqual(packReadings(device).map((pack) => [pack.pack, pack.soc]), [[1, 44], [2, 46]]);
});
