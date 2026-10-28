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

/** The channels Basic lists after savings and the self-sufficiency ratio. */
export const BASIC_CHANNELS = Object.freeze(["pv_yield", "home_consumption"]);

function channelFigure(channel) {
  return { id: `channel.${channel.id}`, label: `channel.${channel.id}`, format: "energy", listed: "measured", channel: channel.id };
}

/**
 * The figures of a period after its energy, in the order the month detail
 * lists them and the month table puts them in columns: savings, the
 * self-sufficiency ratio, solar and home use; Expert puts AC charging, every
 * other channel and the peak output after them. `listed` says when a period
 * lists the figure (figureListed).
 */
export function periodFigures(channels, expert) {
  const basic = [
    { id: "savings", label: "energy.savings", format: "money", listed: "always" },
    { id: "selfSufficiency", label: "energy.selfSufficiency", format: "percent", listed: "measured" },
    ...BASIC_CHANNELS.map((id) => channels.find((channel) => channel.id === id)).filter(Boolean).map(channelFigure),
  ];
  if (!expert) return basic;
  return [
    ...basic,
    { id: "acCharge", label: "energy.acCharge", format: "energy", listed: "some" },
    ...channels.filter((channel) => !BASIC_CHANNELS.includes(channel.id)).map(channelFigure),
    { id: "peak", label: "energy.peak", format: "power", listed: "some" },
  ];
}

/**
 * The tone of a figure's mark, as the charts colour the same energy: savings
 * wear the money tone, the self-sufficiency ratio the battery's, AC charging
 * the grid's, the peak the output's, and each channel its energy's
 * (ems/energy_channels.py names them).
 */
export const FIGURE_TONES = Object.freeze({
  savings: "c-money",
  selfSufficiency: "c-bat",
  acCharge: "c-grid",
  peak: "c-out",
  grid_import: "c-grid",
  grid_export: "c-grid",
  battery_charge: "c-bat",
  battery_discharge: "c-bat",
  pv_yield: "c-pv",
  home_consumption: "c-out",
});

export function figureTone(figure) {
  return FIGURE_TONES[figure?.channel || figure?.id] || "c-idle";
}

/** One figure of one period: null when it was not measured, and whether it covers only part of the period. */
export function figureValue(values, figure) {
  if (figure.channel) {
    const { kwh, coverage } = channelKwh(values, figure.channel);
    return { value: kwh, partial: coverage === "partial" };
  }
  switch (figure.id) {
    case "selfSufficiency": {
      const ratio = selfSufficiency(values);
      return { value: ratio?.ratio ?? null, partial: Boolean(ratio?.partial) };
    }
    case "savings":
      return { value: finite(values?.savings_value), partial: false };
    case "acCharge":
      return { value: acChargeKwh(values), partial: false };
    case "peak":
      return { value: finite(values?.peak_output_w), partial: false };
    default:
      return { value: null, partial: false };
  }
}

/**
 * Whether a period lists a figure: a "some" figure only when there was some, a
 * "measured" one that was not measured only in Expert and no channel when none
 * of them measured the period, and any other figure always.
 */
export function figureListed(figure, cell, values, expert) {
  if (figure.listed === "some") return cell.value !== null && cell.value > 0;
  if (figure.listed !== "measured") return true;
  if (figure.channel && channelsAllUnmeasured(values)) return false;
  return cell.value !== null || expert;
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
