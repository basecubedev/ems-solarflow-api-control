// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { energyKwh, selfSufficiency, channelKwh, monthlyView, yearlyView, energyState, channelsSince, periodView, periodFigures, BASIC_CHANNELS, figureValue, figureListed, figureText, figureTone, FIGURE_TONES } from "../src/lib/energy.js";
import { createFormat } from "../../shared/lib/format.js";

test("a ratio that was not measured is null, never 0 %", () => {
  assert.equal(selfSufficiency({ ratios: { self_sufficiency: null } }), null);
  assert.deepEqual(selfSufficiency({ ratios: { self_sufficiency: 0.54 } }), { ratio: 0.54, partial: false });
  assert.equal(selfSufficiency({ ratios: { self_sufficiency: 0.5 }, coverage: { grid_import: "partial" } }).partial, true);
});

test("a channel with no coverage has no value", () => {
  assert.deepEqual(channelKwh({ coverage: { pv_yield: "none" }, channels: { pv_yield: { kwh: 3 } } }, "pv_yield"), { kwh: null, coverage: "none" });
  assert.deepEqual(channelKwh({ channels: { pv_yield: { kwh: 3 } } }, "pv_yield"), { kwh: 3, coverage: "full" });
  assert.deepEqual(channelKwh({}, "pv_yield"), { kwh: null, coverage: "none" });
});

test("energy falls back from kWh to Wh and is null when absent", () => {
  assert.equal(energyKwh({ inverter_output_kwh: 3.2 }), 3.2);
  assert.equal(energyKwh({ inverter_output_wh: 1500 }), 1.5);
  assert.equal(energyKwh({}), null);
});

test("months after today, before measurement and without a row are not zero", () => {
  const stats = {
    lifetime: { since_date: "2026-03-10" },
    monthly_current_year: [{ month: 3, inverter_output_kwh: 40 }, { month: 4, inverter_output_kwh: 58 }, { month: 10, inverter_output_kwh: 28 }],
  };
  const months = monthlyView(stats, new Date(2026, 9, 19));
  assert.equal(months[0].state, "unmeasured");
  assert.equal(months[2].state, "complete");
  assert.equal(months[4].state, "missing");
  assert.equal(months[9].state, "running");
  assert.equal(months[10].state, "future");
  assert.equal(months[4].kwh, null);
});

test("a month the store answers with nothing measured is missing, not a zero bar", () => {
  const unmeasured = { inverter_output_kwh: 0, channels: { pv_yield: { wh: 0 }, grid_import: { wh: 0 } }, coverage: { pv_yield: "none", grid_import: "none" } };
  const stats = {
    lifetime: { since_date: "2026-03-10" },
    monthly_current_year: Array.from({ length: 12 }, (_, i) => ({ month: i + 1, ...(i === 3 ? { inverter_output_kwh: 58 } : unmeasured) })),
  };
  const months = monthlyView(stats, new Date(2026, 9, 19));
  assert.equal(months[3].state, "complete");
  assert.equal(months[4].state, "missing");
});

test("years are sorted, capped at eight and the current one is marked", () => {
  const years = yearlyView({ yearly: [{ year: 2026, inverter_output_kwh: 1 }, { year: 2025, inverter_output_kwh: 2 }] }, new Date(2026, 0, 1));
  assert.deepEqual(years.map((year) => year.year), [2025, 2026]);
  assert.equal(years[1].current, true);
});

test("the energy page states: unavailable, disabled, waiting, ready", () => {
  assert.equal(energyState(null), "unavailable");
  assert.equal(energyState({ enabled: false }), "disabled");
  assert.equal(energyState({ today: { inverter_output_kwh: 0 }, lifetime: {} }), "waiting");
  assert.equal(energyState({ today: { inverter_output_kwh: 0.2 } }), "ready");
});

test("channels are measured since the latest channel start", () => {
  assert.equal(channelsSince({ channel_meta: [{ id: "a", since: "2026-01-02" }, { id: "b", since: "2026-03-01" }] }), "2026-03-01");
});

test("a period with partial coverage is marked", () => {
  assert.equal(periodView({ coverage: { pv_yield: "partial" } }).partial, true);
});

test("Basic lists savings, self-sufficiency, solar and home use, and Expert goes on from there", () => {
  const channels = ["grid_import", "grid_export", "battery_charge", "battery_discharge", "pv_yield", "home_consumption"].map((id) => ({ id }));
  const basic = periodFigures(channels, false).map((figure) => figure.id);
  const expert = periodFigures(channels, true).map((figure) => figure.id);
  assert.deepEqual(basic, ["savings", "selfSufficiency", "channel.pv_yield", "channel.home_consumption"]);
  assert.deepEqual(expert, [
    ...basic,
    "acCharge", "channel.grid_import", "channel.grid_export", "channel.battery_charge", "channel.battery_discharge", "peak",
  ]);
});

test("Basic lists solar before home use whatever order the EMS sends them in", () => {
  assert.deepEqual(periodFigures([{ id: "home_consumption" }, { id: "pv_yield" }], false).map((figure) => figure.id), ["savings", "selfSufficiency", "channel.pv_yield", "channel.home_consumption"]);
});

test("Basic lists no channel but solar and home use, and of those only one the EMS records", () => {
  assert.deepEqual(periodFigures([{ id: "grid_import" }], false).map((figure) => figure.id), ["savings", "selfSufficiency"]);
  assert.deepEqual(periodFigures([{ id: "home_consumption" }], false).map((figure) => figure.id), ["savings", "selfSufficiency", "channel.home_consumption"]);
});

test("a figure is listed for some AC charging or peak, a measured channel, and an unmeasured channel or ratio only in Expert", () => {
  const values = {
    savings_value: 4.2,
    ratios: { self_sufficiency: null },
    ac_charge_kwh: 0,
    peak_output_w: 900,
    channels: { grid_import: { kwh: 28.9 }, pv_yield: { kwh: 0 } },
    coverage: { grid_import: "partial" },
  };
  const figure = Object.fromEntries(periodFigures([{ id: "grid_import" }, { id: "pv_yield" }], true).map((item) => [item.id, item]));
  const { savings, selfSufficiency: ratio, acCharge, "channel.grid_import": gridImport, peak } = figure;
  const listed = (figure, expert) => figureListed(figure, figureValue(values, figure), values, expert);
  assert.deepEqual(figureValue(values, savings), { value: 4.2, partial: false });
  assert.deepEqual(figureValue(values, gridImport), { value: 28.9, partial: true });
  assert.equal(listed(ratio, false), false);
  assert.equal(listed(ratio, true), true);
  assert.equal(listed(acCharge, true), false);
  assert.equal(listed(peak, true), true);
  assert.equal(listed(gridImport, true), true);
  const unmeasured = { channels: { grid_import: { kwh: 0 } }, coverage: { grid_import: "none" } };
  assert.equal(figureListed(gridImport, figureValue(unmeasured, gridImport), unmeasured, true), false);
  const oneUnmeasured = { channels: { grid_import: { kwh: 1.2 }, pv_yield: { kwh: 0 } }, coverage: { pv_yield: "none" } };
  const solar = periodFigures([{ id: "grid_import" }, { id: "pv_yield" }], false).find((item) => item.id === "channel.pv_yield");
  assert.equal(figureListed(solar, figureValue(oneUnmeasured, solar), oneUnmeasured, false), false);
  assert.equal(figureListed(solar, figureValue(oneUnmeasured, solar), oneUnmeasured, true), true);
});

test("a figure reads as text with Classic's partial mark, and a missing one as a dash without it", () => {
  const f = createFormat("en");
  const figure = Object.fromEntries(periodFigures([{ id: "grid_import" }], true).map((item) => [item.id, item]));
  const { selfSufficiency: ratio, "channel.grid_import": gridImport } = figure;
  assert.equal(figureText(f, gridImport, { value: 28.9, partial: true }, "EUR"), `${f.energy(28.9)} ◦`);
  assert.equal(figureText(f, ratio, { value: null, partial: true }, "EUR"), "—");
});

test("every figure and every channel the EMS records wears a tone, the one its energy wears in the charts, and says when it is listed", () => {
  const server = readFileSync(new URL("../../../ems/energy_channels.py", import.meta.url), "utf8");
  const channels = [...server.matchAll(/EnergyChannel\(\s*"([a-z_]+)"/g)].map((match) => match[1]);
  assert.ok(channels.length >= 6);
  for (const id of channels) assert.ok(FIGURE_TONES[id], id);
  for (const id of BASIC_CHANNELS) assert.ok(channels.includes(id), id);
  for (const figure of periodFigures(channels.map((id) => ({ id })), true)) {
    assert.match(figureTone(figure), /^c-[a-z]+$/);
    assert.ok(["always", "some", "measured"].includes(figure.listed), figure.id);
  }
  assert.equal(figureTone({ id: "savings" }), "c-money");
  assert.equal(figureTone({ id: "channel.pv_yield", channel: "pv_yield" }), "c-pv");
  assert.equal(figureTone({ id: "unknown" }), "c-idle");
});
