// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Analytics batteries view: what one device's batteries did over a
 * range, as InfluxDB records it (ems.history.schema BATTERY_DOWNSAMPLED).
 *
 * One measure is drawn at a time, each on its own unit: the packs'
 * temperatures beside the inverter's, the packs' states of charge, one
 * pack's cell voltages, the packs' power, their voltages beside the
 * device's, their currents, or what the battery accepts. The winter measure
 * shows what a cold morning does in one view: what charged the battery, PV
 * or AC, where the device's power went, its charge against the minimum, and
 * the packs' temperatures in a second chart under it. A pack's power and
 * current are kept positive while it charges, as the battery is, and drawn
 * below zero while it charges, as the battery is (history.shownValue). The
 * states the device and the EMS report (heating, too cold, charging role,
 * winter hold) are no lines but lanes under the chart. Basic shows what a
 * battery in the cold needs and how its packs share the work; Expert adds
 * the rest.
 */

import { MISSING } from "../../../shared/lib/format.js";
import { SERIES_STYLE } from "./analytics.js";
import { ampText } from "./batteryAxes.js";
import { cellSeriesIds, MAX_PACK_NUMBER } from "./cells.js";
import { gapLimit, sampleSpacing, zoomSpan } from "./history.js";

const WINTER_POWER = Object.freeze(["pv", "ac_charge", "battery", "output", "charge_avail"]);
const WINTER_SERIES = Object.freeze([...WINTER_POWER, "soc", "min_soc", "winter_target"]);
const LOW_HIGH = (low, high, format) => [{ label: low, reach: "min", format }, { label: high, reach: "max", format }];
const BOTH_WAYS = (format) => [{ label: "battery.strongestDischarge", reach: "discharge", format }, { label: "battery.strongestCharge", reach: "charge", format }];

/**
 * What the batteries view can draw. ``perPack`` names the series a measure
 * reads once per pack, ``device`` the device's own line beside them, and
 * ``kpis`` what the cards under the chart show over the range, a card with a
 * ``series`` reading that series of the pack shown rather than the chart's.
 * The winter measure draws the device's ``own`` series instead, which bring
 * their charge levels along and so take no overlays, a second chart of the
 * measure named ``beside`` under it, and in Basic too the ``lanes`` that say
 * what the EMS decided.
 */
export const BATTERY_MEASURES = Object.freeze([
  { id: "temperature", unit: "°C", perPack: "pack_temp", device: "device_temp", kpis: LOW_HIGH("battery.coldest", "battery.warmest", "temperature") },
  { id: "charge", unit: "%", perPack: "pack_soc", kpis: LOW_HIGH("battery.lowestCharge", "battery.highestCharge", "percent") },
  { id: "cells", unit: "V", kpis: [...LOW_HIGH("series.cellMin", "series.cellMax", "voltage"), { label: "battery.cellSpread", reach: "max", format: "millivolt", series: "cell_spread" }] },
  { id: "power", unit: "W", perPack: "pack_power", kpis: BOTH_WAYS("power") },
  { id: "winter", unit: "W", own: WINTER_SERIES, beside: "temperature", lanes: ["ac_input", "winter_hold"], overlays: false, kpis: [] },
  { id: "voltage", unit: "V", perPack: "pack_voltage", device: "device_voltage", expert: true, kpis: LOW_HIGH("battery.lowestVoltage", "battery.highestVoltage", "voltage") },
  { id: "current", unit: "A", perPack: "pack_current", expert: true, kpis: BOTH_WAYS("current") },
  { id: "limits", unit: "W", expert: true, kpis: [] },
]);

export const BATTERY_OVERLAYS = Object.freeze([
  { id: "soc" },
  { id: "min_soc", expert: true },
  { id: "winter_target", expert: true },
]);

export const STATE_LANES = Object.freeze([
  { id: "heat_state", token: "--pv", source: "device" },
  { id: "low_temp", token: "--battery", source: "device" },
  { id: "ac_input", token: "--grid", expert: true, source: "ems" },
  { id: "winter_hold", token: "--soc", expert: true, source: "ems" },
  { id: "derate", token: "--m-warn", expert: true, source: "device" },
  { id: "error_flag", token: "--m-warn", expert: true, source: "device" },
]);

const LIMIT_SERIES = Object.freeze(["charge_avail", "discharge_avail", "input_limit"]);
const PACK_TOKENS = Object.freeze(["--battery", "--pv", "--output", "--grid", "--m-accent", "--soc"]);
const PERCENT_SERIES = Object.freeze({ soc: "--soc", min_soc: "--battery", winter_target: "--m-accent" });

/** What a detail level offers: every entry in Expert, the ones not marked ``expert`` in Basic. */
function offered(entries, expert) {
  return entries.filter((entry) => expert || !entry.expert);
}

export function batteryMeasures(expert) {
  return offered(BATTERY_MEASURES, expert);
}

function measureEntry(id) {
  return BATTERY_MEASURES.find((entry) => entry.id === id);
}

/** The measure shown: the one asked for while the detail level offers it, else the temperature. */
export function shownMeasure(wanted, expert) {
  return batteryMeasures(expert).find((measure) => measure.id === wanted) || BATTERY_MEASURES[0];
}

/** The overlays a detail level offers, every measure taking all of them but one whose series bring their own. */
export function batteryOverlays(expert, measure) {
  return measureEntry(measure)?.overlays === false ? [] : offered(BATTERY_OVERLAYS, expert);
}

/** The lanes a detail level offers, and in Basic the ones a measure names besides. */
export function stateLanes(expert, measure) {
  const named = measureEntry(measure)?.lanes || [];
  return STATE_LANES.filter((lane) => expert || !lane.expert || named.includes(lane.id));
}

/**
 * The packs a measure with a line per pack draws: every pack the device
 * reports, one with a temperature or a charge but no cell voltage too, the
 * ones the history view offers for it, and as many as it counts.
 */
export function shownPacks(device, offered) {
  const numbers = new Set([...(offered?.cells || []), ...(device?.cells || [])].map((entry) => entry.pack));
  const counted = Math.min(Number(device?.packs) || 0, MAX_PACK_NUMBER);
  for (let pack = 1; pack <= counted; pack += 1) numbers.add(pack);
  return [...numbers].filter((pack) => Number.isInteger(pack) && pack > 0 && pack <= MAX_PACK_NUMBER).sort((a, b) => a - b);
}

/** The series a measure with a line per pack reads for its packs, none for another measure. */
export function packSeriesIds(measure, packs) {
  const kind = BATTERY_MEASURES.find((entry) => entry.id === measure)?.perPack;
  return kind ? packs.map((pack) => `${kind}:${pack}`) : [];
}

const PER_PACK_KINDS = Object.freeze(BATTERY_MEASURES.filter((entry) => entry.perPack).map((entry) => entry.perPack));

/** The pack a per-pack series (``pack_temp:<n>``, ``pack_soc:<n>``) reads, or null for any other series. */
export function seriesPack(id) {
  const match = /^([a-z_]+):([1-9][0-9]*)$/.exec(String(id));
  return match && PER_PACK_KINDS.includes(match[1]) ? Number(match[2]) : null;
}

/**
 * The series the chart draws: a line per pack and the device's own line
 * beside them, one pack's cell voltages, or the limits, then the overlays
 * switched on that the measure takes.
 */
export function batteryChartIds(measure, packs, pack, overlays, expert) {
  const entry = measureEntry(measure);
  const perPack = packSeriesIds(measure, packs);
  const device = entry?.device;
  const own = entry?.own ? [...entry.own] : perPack.length ? [...perPack, ...(device ? [device] : [])] : measure === "cells" ? (pack === null ? [] : cellSeriesIds(pack)) : measure === "limits" ? [...LIMIT_SERIES] : [];
  const allowed = new Set(batteryOverlays(expert, measure).map((overlay) => overlay.id));
  return own.length ? [...own, ...overlays.filter((id) => allowed.has(id))] : [];
}

/** The series of the second chart a measure draws under its own, the measure it names ``beside``; none without one. */
export function besideChartIds(measure, packs) {
  const beside = measureEntry(measure)?.beside;
  return beside ? batteryChartIds(beside, packs, null, [], false) : [];
}

/** What one request asks for: the charts' series, the cards' and the lanes the measure shows. */
export function batteryRequest(chartIds, expert, cards = [], measure = null) {
  return chartIds.length ? [...chartIds, ...cards, ...stateLanes(expert, measure).map((lane) => lane.id)] : [];
}

/** The series a measure's cards read beside its chart, for the pack shown. */
export function cardSeriesIds(measure, pack) {
  const kpis = BATTERY_MEASURES.find((entry) => entry.id === measure)?.kpis || [];
  return pack === null ? [] : kpis.filter((kpi) => kpi.series).map((kpi) => `${kpi.series}:${pack}`);
}

/**
 * How a battery series is drawn and named; ``label`` is a string key, ``args``
 * its values. In the winter measure the device's power is drawn as the other
 * views draw it, and what the battery accepts as a shaded room under zero
 * with a dotted floor (``below``) that the battery's charge, drawn below
 * zero too, can fill: none while the battery is too cold to take any.
 */
export function batteryStyle(id, measure = null) {
  const pack = seriesPack(id);
  if (pack !== null) return { label: "series.pack", args: { n: pack }, token: PACK_TOKENS[(pack - 1) % PACK_TOKENS.length], fill: false };
  if (id in PERCENT_SERIES) return { label: `series.${id}`, token: PERCENT_SERIES[id], unit: "%", ink: "--m-muted", fill: false, dash: [6, 4] };
  if (measure === "winter" && id === "charge_avail") return { label: `series.${id}`, token: "--m-muted", dash: [2, 3], below: true };
  if (measure === "winter" && WINTER_POWER.includes(id)) return { label: `series.${id}`, ...SERIES_STYLE[id] };
  if (id === "device_temp" || id === "device_voltage") return { label: `series.${id}`, token: "--inverter", fill: false };
  if (LIMIT_SERIES.includes(id)) return { label: `series.${id}`, token: { charge_avail: "--battery", discharge_avail: "--output", input_limit: "--grid" }[id], fill: false };
  return null;
}

/**
 * The stretches a lane is on, as ``{start, end}`` in epoch seconds. A point
 * is on while its value is above zero; it lasts until the next point, or one
 * typical step when it is the last one or the next one is further away than
 * the gap limit (``history.gapLimit``): a gap in the data, a null or a
 * stretch without points, ends a stretch rather than bridging it.
 */
export function laneSegments(time, values) {
  const segments = [];
  if (!Array.isArray(time) || !Array.isArray(values)) return segments;
  const spacing = sampleSpacing(time) || 0;
  const limit = gapLimit(time);
  let open = null;
  for (let index = 0; index < time.length; index += 1) {
    const value = values[index];
    const on = value !== null && value !== undefined && Number(value) > 0;
    const joined = index + 1 < time.length && time[index + 1] - time[index] <= limit;
    const end = joined ? time[index + 1] : time[index] + spacing;
    if (on && open) open.end = end;
    else if (on) open = { start: time[index], end };
    else if (open) {
      segments.push(open);
      open = null;
    }
    if (open && !joined) {
      segments.push(open);
      open = null;
    }
  }
  return segments;
}

/** How long a lane was on, in seconds. */
export function laneSeconds(segments) {
  return segments.reduce((total, segment) => total + (segment.end - segment.start), 0);
}

/** Whether a lane has any reading at all; one the device never reported is left out rather than shown off. */
export function laneReported(values) {
  return Array.isArray(values) && values.some((value) => value !== null && value !== undefined);
}

/** Each lane with its stretches, how long it was on and whether the device reported it at all. */
export function laneTotals(time, lanes) {
  return lanes.map((lane) => {
    const segments = laneSegments(time, lane.values);
    return { ...lane, reported: laneReported(lane.values), segments, seconds: laneSeconds(segments) };
  });
}

/**
 * Whether the lanes' points lie further apart than a minute. A point of a
 * long range is on when its state was on at any time of its window, so a
 * total over such points is an upper bound rather than a measurement.
 */
export function lanesCoarse(time) {
  const spacing = sampleSpacing(time);
  return spacing !== null && spacing > 60;
}

/** How long a lane was on, in the chart's span words and as an upper bound over coarse points; empty for no time. */
export function laneTime(seconds, t, f, coarse = false) {
  const span = zoomSpan(seconds);
  if (!span) return "";
  const time = t(span.key, { n: f.number(span.n, Number.isInteger(span.n) ? 0 : 1) });
  return coarse ? t("lanes.upTo", { time }) : time;
}

/**
 * What the folded States card says under its title: the states that were on
 * and for how long, that none was, or nothing while no state was reported.
 */
export function laneSummary(rows, t, f, coarse = false) {
  const on = rows.filter((row) => row.segments.length).map((row) => [row.label, laneTime(row.seconds, t, f, coarse)].filter(Boolean).join(" "));
  if (on.length) return on.join(" · ");
  return rows.some((row) => row.reported) ? t("lanes.noneOn") : "";
}

/** Where the lanes' time scale is labelled: its start, its end and the points between, in epoch seconds. */
export function laneTicks(start, end, count = 5) {
  if (!(end > start) || count < 2) return [];
  return Array.from({ length: count }, (_, index) => start + ((end - start) * index) / (count - 1));
}

/**
 * What one card under the chart shows: the lowest or highest reading over
 * the range, or the strongest charge or discharge, each as a size, from
 * values kept positive while charging; null without a reading.
 */
export function batteryKpi(data, ids, reach) {
  if (reach === "charge" || reach === "discharge") {
    const value = packExtreme(data, ids, reach === "charge" ? "max" : "min");
    return value === null ? null : Math.max(0, reach === "charge" ? value : -value);
  }
  return packExtreme(data, ids, reach);
}

/** A card's value in its measure's unit. */
export function batteryKpiText(value, format, f) {
  if (format === "current") return ampText(value, f);
  if (format === "millivolt") return Number.isFinite(value) ? `${f.number(value * 1000, 0)}\u00a0mV` : MISSING;
  return f[format](value);
}

/** The lowest or the highest value any of the series reached over the range, null without a reading. */
export function packExtreme(data, ids, kind) {
  const values = ids
    .flatMap((id) => data?.series?.[id] || [])
    .filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)))
    .map(Number);
  if (!values.length) return null;
  return kind === "min" ? Math.min(...values) : Math.max(...values);
}
