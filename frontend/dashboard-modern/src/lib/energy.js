// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * View model of /api/energy-stats, with Classic's measurement semantics:
 * a ratio that is null was not measured (never 0 %), a channel with coverage
 * "none" did not measure the period, and "partial" marks a period that was
 * only partly measured.
 */

import { finite } from "../../../shared/lib/format.js";

export const PERIODS = Object.freeze([
  { key: "today", label: "energy.today", kind: "today" },
  { key: "yesterday", label: "energy.yesterday", kind: "day" },
  { key: "last_7_days", label: "energy.last7", kind: "week" },
  { key: "last_4_weeks", label: "energy.last4w", kind: "month" },
  { key: "last_12_months", label: "energy.last12m", kind: "year" },
  { key: "best_day", label: "energy.bestDay", kind: "best" },
]);

export const RATIO_CHANNELS = Object.freeze(["home_consumption", "grid_import"]);

export function energyKwh(values) {
  const kwh = finite(values?.inverter_output_kwh);
  if (kwh !== null) return kwh;
  const wh = finite(values?.inverter_output_wh);
  return wh !== null ? wh / 1000 : null;
}

export function acChargeKwh(values) {
  const kwh = finite(values?.ac_charge_kwh);
  if (kwh !== null) return kwh;
  const wh = finite(values?.ac_charge_wh);
  return wh !== null ? wh / 1000 : null;
}

export function channelKwh(values, channelId) {
  const coverage = values?.coverage?.[channelId] || "full";
  if (coverage === "none") return { kwh: null, coverage };
  const kwh = finite(values?.channels?.[channelId]?.kwh);
  return { kwh, coverage: kwh === null ? "none" : coverage };
}

export function selfSufficiency(values) {
  const ratio = values?.ratios?.self_sufficiency;
  if (typeof ratio !== "number" || !Number.isFinite(ratio)) return null;
  const coverage = values?.coverage || {};
  const partial = RATIO_CHANNELS.some((channelId) => channelId in coverage);
  return { ratio, partial };
}

export function isPartial(values) {
  return Object.values(values?.coverage || {}).some((state) => state === "partial");
}

export function periodView(values) {
  return {
    kwh: energyKwh(values),
    savings: finite(values?.savings_value),
    selfSufficiency: selfSufficiency(values),
    acChargeKwh: acChargeKwh(values),
    peakW: finite(values?.peak_output_w),
    partial: isPartial(values),
    date: values?.date || null,
    raw: values || null,
  };
}

function hasData(values) {
  const kwh = energyKwh(values);
  return kwh !== null && kwh > 0;
}

/** Classic's energyChannelsAllUnmeasured: every channel the period carries has coverage "none". */
export function channelsAllUnmeasured(values) {
  const states = Object.values(values?.coverage || {});
  const channelCount = Object.keys(values?.channels || {}).length;
  return channelCount > 0 && states.length === channelCount && states.every((coverage) => coverage === "none");
}

/**
 * The twelve months of the current year. A month after today has "future"
 * data, one before measurement began "unmeasured", and a month inside the
 * measured span without a measured day is "missing" — none of them is 0. The
 * store answers every month of the year, so "no row" is not how that shows.
 */
export function monthlyView(stats, now = new Date()) {
  const byMonth = new Map(
    (Array.isArray(stats?.monthly_current_year) ? stats.monthly_current_year : []).map((item) => [Number(item.month), item]),
  );
  const currentMonth = now.getMonth() + 1;
  const since = stats?.lifetime?.since_date ? String(stats.lifetime.since_date) : null;
  const sinceMatch = since && since.match(/^(\d{4})-(\d{2})/);
  const sinceYear = sinceMatch ? Number(sinceMatch[1]) : null;
  const sinceMonth = sinceMatch ? Number(sinceMatch[2]) : null;
  const year = now.getFullYear();
  return Array.from({ length: 12 }, (_, index) => {
    const month = index + 1;
    const record = byMonth.get(month);
    let state = "complete";
    if (month > currentMonth) state = "future";
    else if (sinceYear !== null && (sinceYear > year || (sinceYear === year && month < sinceMonth))) state = "unmeasured";
    else if (!record || (channelsAllUnmeasured(record) && !hasData(record))) state = "missing";
    else if (month === currentMonth) state = "running";
    return { month, state, ...periodView(record) };
  });
}

export function yearlyView(stats, now = new Date()) {
  const currentYear = now.getFullYear();
  return (Array.isArray(stats?.yearly) ? stats.yearly : [])
    .filter((item) => item && item.year !== undefined && item.year !== null)
    .sort((a, b) => Number(a.year) - Number(b.year))
    .slice(-8)
    .map((item) => ({ year: Number(item.year), current: Number(item.year) === currentYear, ...periodView(item) }));
}

export function energyState(stats) {
  if (!stats) return "unavailable";
  if (stats.enabled === false) return "disabled";
  const lifetime = stats.lifetime || {};
  const channelEnergy = [stats.today, stats.yesterday, lifetime].some((entry) =>
    Object.values(entry?.channels || {}).some((channel) => Number(channel?.wh) > 0),
  );
  const any = Boolean(lifetime.since_date) || channelEnergy || [
    stats.today, stats.yesterday, stats.last_7_days, stats.last_4_weeks, stats.last_12_months, stats.best_day, lifetime,
    ...(Array.isArray(stats.monthly_current_year) ? stats.monthly_current_year : []),
    ...(Array.isArray(stats.yearly) ? stats.yearly : []),
  ].some(hasData);
  return any ? "ready" : "waiting";
}

export function channelMeta(stats) {
  return (Array.isArray(stats?.channel_meta) ? stats.channel_meta : []).filter((channel) => channel && channel.id);
}

/** The latest start among the channels: before it, at least one channel was not yet measuring. */
export function channelsSince(stats) {
  const dates = channelMeta(stats).map((channel) => channel.since).filter(Boolean).sort();
  return dates.length ? dates[dates.length - 1] : null;
}

/**
 * The figures of a period after its energy, in the order the month detail
 * lists them and the month table puts them in columns: savings, the
 * self-sufficiency ratio and AC charging; Expert adds every channel and the
 * peak output.
 */
export function periodFigures(channels, expert) {
  const figures = [
    { id: "savings", label: "energy.savings", format: "money" },
    { id: "selfSufficiency", label: "energy.selfSufficiency", format: "percent" },
    { id: "acCharge", label: "energy.acCharge", format: "energy" },
  ];
  if (!expert) return figures;
  return [
    ...figures,
    ...channels.map((channel) => ({ id: `channel.${channel.id}`, label: `channel.${channel.id}`, format: "energy", channel: channel.id })),
    { id: "peak", label: "energy.peak", format: "power" },
  ];
}

/** One figure of one period: null when it was not measured, and whether it covers only part of the period. */
export function figureValue(values, figure) {
  if (figure.channel) {
    const { kwh, coverage } = channelKwh(values, figure.channel);
    return { value: kwh, partial: coverage === "partial" };
  }
  const view = periodView(values);
  if (figure.id === "selfSufficiency") return { value: view.selfSufficiency?.ratio ?? null, partial: Boolean(view.selfSufficiency?.partial) };
  const value = { savings: view.savings, acCharge: view.acChargeKwh, peak: view.peakW }[figure.id];
  return { value: value ?? null, partial: false };
}

/**
 * Whether a period lists a figure, as Classic's energy cards do: AC charging
 * and the peak only when there was some, the channels unless none of them
 * measured the period, and an unmeasured ratio only in Expert.
 */
export function figureListed(figure, cell, values, expert) {
  if (figure.channel) return !channelsAllUnmeasured(values);
  if (figure.id === "selfSufficiency") return cell.value !== null || expert;
  if (figure.id === "acCharge" || figure.id === "peak") return cell.value !== null && cell.value > 0;
  return true;
}

/** A figure as text; a partly measured one carries Classic's ◦. */
export function figureText(f, figure, cell, currency) {
  const value = cell.value;
  const text =
    figure.format === "money" ? f.money(value, currency)
    : figure.format === "percent" ? f.percent(value, { fraction: true })
    : figure.format === "power" ? f.power(value)
    : f.energy(value);
  return value !== null && cell.partial ? `${text} ◦` : text;
}
