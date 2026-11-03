// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import {
  chartConsumers,
  chartTokens,
  consumerEnergy,
  consumerList,
  consumerMonths,
  consumerPalette,
  consumerPeriod,
  consumerRows,
  consumerSeriesId,
  consumerToken,
  consumerTotals,
  consumerYears,
  flowConsumers,
  homeShare,
  monthSegments,
  ordered,
  restOfHouse,
  stackedConsumers,
  CONSUMER_PERIODS,
  CONSUMER_TOKENS,
  OTHER_TOKEN,
} from "../src/lib/consumers.js";
import { PERIODS } from "../src/lib/energy.js";
import { stackLists } from "../src/lib/history.js";

const SNAPSHOT = {
  home_load_w: 1000,
  home_other_w: 290,
  consumer_total_w: 650,
  consumers: {
    office: { name: "Office", online: true, power_w: 60 },
    washer: { name: "Washer", online: true, power_w: 420 },
    dryer: { name: "Dryer", online: false, power_w: 900 },
    fridge: { name: "Fridge", online: true, power_w: 85 },
    lamp: { name: "Lamp", online: true, power_w: 85 },
  },
};

test("consumers come by their key with their name, highest draw first, a silent one last and without a power", () => {
  const list = consumerList(SNAPSHOT);
  assert.deepEqual(list.map((item) => item.name), ["Washer", "Fridge", "Lamp", "Office", "Dryer"]);
  assert.deepEqual([list[0].key, list.at(-1).powerW], ["washer", null]);
  assert.equal(consumerList({ consumers: { plug: { online: true, power_w: 1 } } })[0].name, "plug");
});

test("the flow names the two drawing most and gives the rest of the house to Other, so they add up to Home", () => {
  const split = flowConsumers(SNAPSHOT);
  assert.deepEqual(split.shown.map((item) => item.name), ["Washer", "Fridge"]);
  assert.equal(split.otherW, 1000 - 420 - 85);
  assert.equal(split.totalW, 650);
  assert.equal(flowConsumers({ home_load_w: 100, consumers: { a: { online: true, power_w: 300 } } }).otherW, 0);
  assert.equal(flowConsumers({}).shown.length, 0);
});

test("a list reads by rank or by name, the unknown last in the ranking", () => {
  const items = [{ name: "b", v: 1 }, { name: "a", v: null }, { name: "c", v: 5 }];
  assert.deepEqual(ordered(items, "rank", (item) => item.v).map((item) => item.name), ["c", "b", "a"]);
  assert.deepEqual(ordered(items, "name", (item) => item.v).map((item) => item.name), ["a", "b", "c"]);
});

test("a period a consumer did not draw in reads zero, never missing", () => {
  const period = { consumers: { washer: { name: "Washer", kwh: 1.2, grid_kwh: 0.4, own_kwh: 0.8, cost: 0.12, saved: 0.24 } } };
  assert.deepEqual(consumerEnergy(period, "washer"), { kwh: 1.2, gridKwh: 0.4, ownKwh: 0.8, cost: 0.12, saved: 0.24 });
  assert.deepEqual(consumerEnergy(period, "fridge"), { kwh: 0, gridKwh: 0, ownKwh: 0, cost: 0, saved: 0 });
});

test("a renamed consumer is called by its name now, one that is gone by its last name and its last day", () => {
  const snapshot = { consumers: { washer: { name: "Waschmaschine", online: true, power_w: 1 } } };
  const period = { consumers: { washer: { name: "Washer", last_date: "2026-11-04" }, freezer: { name: "Freezer", last_date: "2026-04-12" } } };
  assert.deepEqual(consumerRows(snapshot, period), [
    { key: "washer", name: "Waschmaschine", live: true, lastDate: null },
    { key: "freezer", name: "Freezer", live: false, lastDate: "2026-04-12" },
  ]);
});

test("a chart names the five that drew most, a tie by name and never by the power of the moment, and says when others share the rest", () => {
  const consumers = Object.fromEntries(Array.from({ length: 30 }, (_, index) => [`c${index}`, { name: `Consumer ${String(index).padStart(2, "0")}`, online: true, power_w: index }]));
  const period = { consumers: { c3: { kwh: 9 }, c1: { kwh: 8 }, c29: { kwh: 7 } } };
  const chosen = chartConsumers({ consumers }, period);
  assert.deepEqual(chosen.shown.map((item) => item.name), ["Consumer 00", "Consumer 01", "Consumer 02", "Consumer 03", "Consumer 29"]);
  const later = { consumers: Object.fromEntries(Object.entries(consumers).map(([key, value]) => [key, { ...value, power_w: 100 - value.power_w }])) };
  assert.deepEqual(chartConsumers(later, period).shown, chosen.shown);
  assert.equal(chosen.rest, true);
  assert.equal(chartConsumers(SNAPSHOT, {}).rest, false);
});

test("the rest of the house is Home less the named consumers, a gap stays a gap and it is never below zero", () => {
  assert.deepEqual(restOfHouse([1000, null, 100, 500], [[400, 10, 300, null], [100, 5, 0, null]]), [500, null, 0, 500]);
});

test("a consumer's series is its key percent-encoded, and the colours come in a fixed order", () => {
  assert.equal(consumerSeriesId("plug@Dryer, cellar"), "consumer:plug%40Dryer%2C%20cellar");
  assert.equal(consumerToken(0), CONSUMER_TOKENS[0]);
  assert.equal(consumerToken(CONSUMER_TOKENS.length), CONSUMER_TOKENS[0]);
});

test("a stacked chart draws running sums, a missing value adds nothing and a gap stays a gap", () => {
  assert.deepEqual(
    stackLists([
      [100, null, 50, null],
      [20, 30, -5, null],
      [1, 1, 1, null],
    ]),
    [
      [100, null, 50, null],
      [120, 30, 50, null],
      [121, 31, 51, null],
    ],
  );
});

const drew = (entries) => Object.fromEntries(entries.map(([key, kwh, cost = 0]) => [key, { name: key[0].toUpperCase() + key.slice(1), kwh, cost, saved: kwh / 10, grid_kwh: kwh / 4, own_kwh: (kwh * 3) / 4 }]));

test("the consumers page counts the Energy page's periods, the day the consumers drew most for its best day", () => {
  assert.deepEqual(CONSUMER_PERIODS.map((period) => period.kind), PERIODS.map((period) => period.kind));
  assert.equal(CONSUMER_PERIODS.at(-1).key, "consumer_peak_day");
});

test("a period adds up what the consumers drew and names the one that drew most", () => {
  const totals = consumerTotals({ consumers: drew([["washer", 3, 0.4], ["dryer", 5, 0.6]]) });
  assert.deepEqual([totals.kwh, totals.cost, totals.count, totals.top.name], [8, 1, 2, "Dryer"]);
  assert.deepEqual(consumerTotals({}), { kwh: 0, gridKwh: 0, ownKwh: 0, cost: 0, saved: 0, count: 0, top: null });
  assert.equal(consumerPeriod({}).kwh, null);
});

test("a share of the home use is never made up from a home use that does not hold the consumers", () => {
  assert.equal(homeShare(2, 8), 25);
  assert.equal(homeShare(8, 2), null);
  assert.equal(homeShare(1, 0), null);
  assert.equal(homeShare(1, null), null);
});

test("this year's months run in the current month, have nothing yet in a past month without data and none ahead", () => {
  const stats = { monthly_current_year: [{ month: 1, consumers: drew([["washer", 4]]) }, { month: 3, consumers: drew([["washer", 1]]) }] };
  const months = consumerMonths(stats, new Date(2026, 2, 15));
  assert.deepEqual(months.slice(0, 4).map((month) => [month.state, month.kwh]), [["complete", 4], ["missing", null], ["running", 1], ["future", null]]);
  assert.equal(consumerMonths(stats, new Date(2026, 1, 1))[1].state, "running");
});

test("the years list the newest first and only years the consumers drew in", () => {
  const stats = { yearly: [{ year: 2025, consumers: drew([["washer", 4]]) }, { year: 2026, consumers: drew([["washer", 2]]) }, { year: 2024, consumers: {} }] };
  assert.deepEqual(consumerYears(stats, new Date(2026, 5, 1)).map((year) => [year.year, year.current, year.kwh]), [[2026, true, 2], [2025, false, 4]]);
});

test("a month stacks the five that drew most this year by name, every other consumer in one band of the rest", () => {
  const year = { consumers: drew([["a", 9], ["b", 8], ["c", 7], ["d", 6], ["e", 5], ["f", 1]]) };
  const named = stackedConsumers(year);
  assert.deepEqual(named.map((item) => item.key), ["a", "b", "c", "d", "e"]);
  const month = { kwh: 10, consumers: drew([["a", 2], ["f", 3]]) };
  const segments = monthSegments(month, named);
  assert.deepEqual(segments.map((segment) => segment.kwh), [2, 0, 0, 0, 0, 8]);
  assert.equal(segments.at(-1).token, OTHER_TOKEN);
});

test("a consumer keeps one colour on every chart, the most drawn first, and no chart names two in one colour", () => {
  const snapshot = { consumers: { new: { name: "New", online: true, power_w: 1 } } };
  const lifetime = { consumers: drew([["small", 1], ["big", 9]]) };
  const palette = consumerPalette(snapshot, lifetime);
  assert.deepEqual([...palette.entries()], [["big", CONSUMER_TOKENS[0]], ["small", CONSUMER_TOKENS[1]], ["new", CONSUMER_TOKENS[2]]]);
  const seven = new Map(["a", "b", "c", "d", "e", "f", "g"].map((key, index) => [key, consumerToken(index)]));
  assert.equal(seven.get("g"), seven.get("a"));
  const withA = chartTokens([{ key: "g" }, { key: "a" }, { key: "c" }], seven);
  const withoutA = chartTokens([{ key: "g" }, { key: "e" }], seven);
  assert.deepEqual(withA.map((item) => item.key), ["g", "a", "c"]);
  assert.equal(withA[1].token, seven.get("a"));
  assert.equal(new Set(withA.map((item) => item.token)).size, 3);
  assert.equal(withoutA[1].token, seven.get("e"));
});
