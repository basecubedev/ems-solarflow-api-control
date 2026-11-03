// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The consumers of the live snapshot: devices that only say what they draw.
 *
 * They are part of the house load the grid meter already measured, so nothing
 * here adds to Home. Each is keyed by its history key, which a rename keeps;
 * its name is only what it is called. A consumer that is not reading has a
 * null power, never 0 W.
 */

import { finite } from "../../../shared/lib/format.js";
import { PERIODS, channelKwh } from "./energy.js";

/** How many consumers the energy flow names; the rest of the house is one node. */
export const FLOW_CONSUMERS = 2;

/** How many consumers a history chart stacks by name; the rest of the house is one band. */
export const CHART_CONSUMERS = 5;

/** The consumer colours of the design tokens, given in this order to the consumers a chart names. */
export const CONSUMER_TOKENS = Object.freeze(["--consumer-1", "--consumer-2", "--consumer-3", "--consumer-4", "--consumer-5", "--consumer-6"]);
export const OTHER_TOKEN = "--m-muted-2";

/** The two orders a list of consumers is read in: the highest first, or by name. */
export const CONSUMER_ORDERS = Object.freeze(["rank", "name"]);

/** A consumer's history series: its key percent-encoded, as a key may hold the comma that separates series. */
export function consumerSeriesId(key) {
  return `consumer:${encodeURIComponent(key)}`;
}

const NAME_SEPARATOR = "\u0000";

/**
 * A list of strings as one string, and back. A list rebuilt from every live
 * frame is a new array each time; a page that keys on the string reloads its
 * history only when the list itself changes.
 */
export function namesKey(names) {
  return names.join(NAME_SEPARATOR);
}

export function namesOf(key) {
  return key ? key.split(NAME_SEPARATOR) : [];
}

export function consumerToken(index) {
  return CONSUMER_TOKENS[index % CONSUMER_TOKENS.length];
}

const byName = (a, b) => a.name.localeCompare(b.name);

/** ``items`` in ``order``: the highest ``value`` first with the unknown ones last, or by name. */
export function ordered(items, order, value) {
  const rank = (item) => value(item) ?? -1;
  return [...items].sort(order === "name" ? byName : (a, b) => rank(b) - rank(a) || byName(a, b));
}

/** Every live consumer, the highest draw first and the silent ones last, then by name. */
export function consumerList(snapshot) {
  const items = Object.entries(snapshot?.consumers || {}).map(([key, raw]) => {
    const online = raw?.online === true;
    return { key, name: typeof raw?.name === "string" && raw.name ? raw.name : key, online, powerW: online ? finite(raw?.power_w) : null };
  });
  return ordered(items, "rank", (item) => item.powerW);
}

/** Home split for the flow: the consumers drawing most by name, and the rest of the house as one. */
export function flowConsumers(snapshot, limit = FLOW_CONSUMERS) {
  const list = consumerList(snapshot);
  const shown = list.slice(0, limit);
  const homeW = finite(snapshot?.home_load_w) ?? 0;
  return {
    shown,
    otherW: Math.max(0, homeW - shown.reduce((sum, item) => sum + (item.powerW || 0), 0)),
    totalW: finite(snapshot?.consumer_total_w) ?? list.reduce((sum, item) => sum + (item.powerW || 0), 0),
  };
}

/** One consumer's totals for a period of the energy summary, or zeros when it drew nothing there. */
export function consumerEnergy(period, key) {
  const value = period?.consumers?.[key] || {};
  return {
    kwh: finite(value.kwh) ?? 0,
    gridKwh: finite(value.grid_kwh) ?? 0,
    ownKwh: finite(value.own_kwh) ?? 0,
    cost: finite(value.cost) ?? 0,
    saved: finite(value.saved) ?? 0,
  };
}

/**
 * The consumers live now and those ``period`` recorded: ``{ key, name, live,
 * lastDate }``. A live one is called what it is called now; one that is gone
 * keeps the name it last drew under and the day it did.
 */
export function consumerRows(snapshot, period) {
  const rows = new Map(consumerList(snapshot).map((item) => [item.key, { key: item.key, name: item.name, live: true, lastDate: null }]));
  for (const [key, value] of Object.entries(period?.consumers || {})) {
    if (!rows.has(key)) rows.set(key, { key, name: typeof value?.name === "string" && value.name ? value.name : key, live: false, lastDate: value?.last_date || null });
  }
  return [...rows.values()];
}

/**
 * The live consumers a history chart names: the ones that drew most in
 * ``period``, a tie by name, never by the power of the moment, so the same
 * ones stay named from frame to frame. They come by name, so each keeps its
 * colour while the same ones are named; ``rest`` says whether others share the
 * band of the rest of the house.
 */
export function chartConsumers(snapshot, period, limit = CHART_CONSUMERS) {
  const list = consumerList(snapshot);
  const top = [...list].sort((a, b) => consumerEnergy(period, b.key).kwh - consumerEnergy(period, a.key).kwh || byName(a, b)).slice(0, limit);
  return { shown: top.sort(byName).map(({ key, name }) => ({ key, name })), rest: list.length > top.length };
}

/** The rest of the house at each point: Home less the consumers a chart names, never below zero. */
export function restOfHouse(home = [], parts = []) {
  return home.map((value, index) => {
    if (value === null || value === undefined) return null;
    return Math.max(0, value - parts.reduce((sum, list) => sum + (finite(list?.[index]) ?? 0), 0));
  });
}

/** The periods the consumers page counts: the Energy page's, with the day the consumers drew most for its best day. */
export const CONSUMER_PERIODS = Object.freeze(
  PERIODS.map((period) => (period.kind === "best" ? { key: "consumer_peak_day", label: "consumers.peakDay", kind: "best" } : period)),
);

/** What the consumers drew in a period of the energy summary together, and which of them drew most. */
export function consumerTotals(period) {
  const entries = Object.entries(period?.consumers || {});
  const sum = (field) => entries.reduce((total, [, value]) => total + (finite(value?.[field]) ?? 0), 0);
  const top = entries.reduce((best, [key, value]) => {
    const kwh = finite(value?.kwh) ?? 0;
    return kwh > (best?.kwh ?? 0) ? { key, name: typeof value?.name === "string" && value.name ? value.name : key, kwh } : best;
  }, null);
  return { kwh: sum("kwh"), gridKwh: sum("grid_kwh"), ownKwh: sum("own_kwh"), cost: sum("cost"), saved: sum("saved"), count: entries.length, top };
}

/**
 * The share of the home use ``kwh`` is, or null when there is no home use to
 * hold it: a home use measured over less of the period than the consumers
 * were would read as all of it.
 */
export function homeShare(kwh, homeKwh) {
  const home = finite(homeKwh);
  if (!home || home <= 0 || kwh > home * 1.02) return null;
  return Math.min(1, kwh / home) * 100;
}

/** A period of the consumers page: its totals and the share of the home use they are. */
export function consumerPeriod(values) {
  const totals = consumerTotals(values);
  return { kwh: totals.count ? totals.kwh : null, partial: false, date: values?.date || null, totals, share: homeShare(totals.kwh, channelKwh(values, "home_consumption").kwh) };
}

/** The months of this year as the consumers drew them: running this month, nothing yet in a past month without data. */
export function consumerMonths(stats, now = new Date()) {
  const byMonth = new Map((Array.isArray(stats?.monthly_current_year) ? stats.monthly_current_year : []).map((item) => [Number(item.month), item]));
  const current = now.getMonth() + 1;
  return Array.from({ length: 12 }, (_, index) => {
    const month = index + 1;
    const record = byMonth.get(month);
    const view = consumerPeriod(record);
    const state = month > current ? "future" : month === current ? "running" : !view.totals.count ? "missing" : "complete";
    return { month, state, ...view, consumers: record?.consumers || {} };
  });
}

/** The calendar years the consumers drew in, the newest first, as the years card lists them. */
export function consumerYears(stats, now = new Date()) {
  return (Array.isArray(stats?.yearly) ? stats.yearly : [])
    .filter((item) => Object.keys(item?.consumers || {}).length)
    .map((item) => ({ year: Number(item.year), current: Number(item.year) === now.getFullYear(), ...consumerPeriod(item) }))
    .sort((a, b) => b.year - a.year)
    .slice(0, 8);
}

/**
 * One colour per consumer for every chart: in the order of what each drew
 * over its lifetime, the most first, then those live without a value yet by
 * name. A consumer keeps its colour from chart to chart and page to page, and
 * the ones that draw most never share one.
 */
export function consumerPalette(snapshot, lifetime) {
  const ranked = consumerRows(snapshot, lifetime)
    .map((row) => ({ key: row.key, name: row.name, kwh: consumerEnergy(lifetime, row.key).kwh }))
    .sort((a, b) => b.kwh - a.kwh || a.name.localeCompare(b.name));
  return new Map(ranked.map((item, index) => [item.key, consumerToken(index)]));
}

/**
 * The colours of the consumers one chart names: each its own from
 * ``palette``. The palette repeats after six; of two in one chart that share
 * a colour, the one that drew more over its lifetime keeps it and the other
 * takes the first the chart has free, so the six that drew most keep theirs
 * on every chart.
 */
export function chartTokens(items, palette) {
  const order = [...(palette?.keys() || [])];
  const rank = (key) => (order.includes(key) ? order.indexOf(key) : order.length);
  const used = new Set();
  const tokens = new Map();
  for (const item of [...items].sort((a, b) => rank(a.key) - rank(b.key))) {
    const own = palette?.get(item.key);
    const token = own && !used.has(own) ? own : CONSUMER_TOKENS.find((candidate) => !used.has(candidate)) || own || CONSUMER_TOKENS[0];
    used.add(token);
    tokens.set(item.key, token);
  }
  return items.map((item) => ({ ...item, token: tokens.get(item.key) }));
}

/**
 * The consumers a month chart stacks by name: the ones that drew most in
 * ``period``, a tie by name, each in its colour; every other consumer is one
 * band of the rest.
 */
export function stackedConsumers(period, palette = null, limit = CHART_CONSUMERS) {
  const named = Object.entries(period?.consumers || {})
    .map(([key, value]) => ({ key, name: typeof value?.name === "string" && value.name ? value.name : key, kwh: finite(value?.kwh) ?? 0 }))
    .sort((a, b) => b.kwh - a.kwh || a.name.localeCompare(b.name))
    .slice(0, limit)
    .sort((a, b) => a.name.localeCompare(b.name));
  return chartTokens(named, palette);
}

/** One month's bands: each named consumer, then the rest of the consumers. */
export function monthSegments(month, named) {
  const segments = named.map((item) => ({ id: item.key, token: item.token, kwh: finite(month.consumers?.[item.key]?.kwh) ?? 0 }));
  const rest = Math.max(0, (month.kwh ?? 0) - segments.reduce((sum, segment) => sum + segment.kwh, 0));
  return [...segments, { id: "\u0000rest", token: OTHER_TOKEN, kwh: rest }];
}
