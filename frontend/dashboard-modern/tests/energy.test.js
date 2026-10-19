// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { energyKwh, selfSufficiency, channelKwh, monthlyView, yearlyView, energyState, channelsSince, periodView } from "../src/lib/energy.js";

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
