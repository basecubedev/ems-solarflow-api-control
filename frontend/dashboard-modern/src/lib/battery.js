// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Analytics batteries view: what one device's batteries did over a
 * range, as InfluxDB records it (ems.history.schema BATTERY_DOWNSAMPLED).
 *
 * One measure is drawn at a time, each on its own unit: the packs'
 * temperatures, the packs' states of charge, one pack's cell voltages, or
 * what the battery accepts. The
 * states the device and the EMS report (heating, too cold, charging role,
 * winter hold) are no lines but lanes under the chart. Basic shows what a
 * battery in the cold needs; Expert adds the rest.
 */

import { MINUS } from "../../../shared/lib/format.js";
import { cellSeriesIds, MAX_PACK_NUMBER } from "./cells.js";
import { gapLimit, sampleSpacing, zoomSpan } from "./history.js";

export const BATTERY_MEASURES = Object.freeze([
  { id: "temperature", unit: "°C", perPack: "pack_temp" },
  { id: "charge", unit: "%", perPack: "pack_soc" },
  { id: "cells", unit: "V" },
  { id: "limits", unit: "W", expert: true },
]);

export const BATTERY_OVERLAYS = Object.freeze([
  { id: "soc" },
  { id: "min_soc", expert: true },
  { id: "winter_target", expert: true },
  { id: "device_temp", expert: true, measure: "temperature" },
]);

export const STATE_LANES = Object.freeze([
  { id: "heat_state", token: "--pv" },
  { id: "low_temp", token: "--battery" },
  { id: "ac_input", token: "--grid", expert: true },
  { id: "winter_hold", token: "--soc", expert: true },
  { id: "derate", token: "--m-warn", expert: true },
  { id: "error_flag", token: "--m-warn", expert: true },
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

/** The measure shown: the one asked for while the detail level offers it, else the temperature. */
export function shownMeasure(wanted, expert) {
  return batteryMeasures(expert).find((measure) => measure.id === wanted) || BATTERY_MEASURES[0];
}

/** The overlays a measure takes at a detail level. */
export function batteryOverlays(expert, measure) {
  return offered(BATTERY_OVERLAYS, expert).filter((overlay) => !overlay.measure || overlay.measure === measure);
}

export function stateLanes(expert) {
  return offered(STATE_LANES, expert);
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
 * The series the chart draws: every pack's temperature or charge, one pack's
 * cell voltages, or the limits, then the overlays switched on that the
 * measure takes.
 */
export function batteryChartIds(measure, packs, pack, overlays, expert) {
  const perPack = packSeriesIds(measure, packs);
  const own = perPack.length ? perPack : measure === "cells" ? (pack === null ? [] : cellSeriesIds(pack)) : measure === "limits" ? [...LIMIT_SERIES] : [];
  const allowed = new Set(batteryOverlays(expert, measure).map((overlay) => overlay.id));
  return own.length ? [...own, ...overlays.filter((id) => allowed.has(id))] : [];
}

/** What one request asks for: the chart's series and the lanes. */
export function batteryRequest(chartIds, expert) {
  return chartIds.length ? [...chartIds, ...stateLanes(expert).map((lane) => lane.id)] : [];
}

/** How a battery series is drawn and named; ``label`` is a string key, ``args`` its values. */
export function batteryStyle(id) {
  const pack = seriesPack(id);
  if (pack !== null) return { label: "series.pack", args: { n: pack }, token: PACK_TOKENS[(pack - 1) % PACK_TOKENS.length], fill: false };
  if (id in PERCENT_SERIES) return { label: `series.${id}`, token: PERCENT_SERIES[id], unit: "%", ink: "--m-muted", fill: false, dash: [6, 4] };
  if (id === "device_temp") return { label: "series.device_temp", token: "--m-muted", fill: false, dash: [2, 3] };
  if (LIMIT_SERIES.includes(id)) return { label: `series.${id}`, token: { charge_avail: "--battery", discharge_avail: "--output", input_limit: "--grid" }[id], fill: false };
  return null;
}

/** A temperature axis in steps of five degrees that always shows 0 °C, the line a battery heats from. */
export function celsiusRange(min, max) {
  const low = Number.isFinite(min) ? min : 0;
  const high = Number.isFinite(max) ? max : 10;
  return [Math.min(-5, Math.floor(low / 5) * 5), Math.max(10, Math.ceil(high / 5) * 5)];
}

/** A temperature axis label: a whole degree without a decimal, any other tick as a temperature. */
export function celsiusTick(value, f) {
  return Number.isInteger(value) ? `${value < 0 ? MINUS : ""}${f.number(Math.abs(value), 0)}\u00a0°C` : f.temperature(value);
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

/** The lowest or the highest value any of the series reached over the range, null without a reading. */
export function packExtreme(data, ids, kind) {
  const values = ids
    .flatMap((id) => data?.series?.[id] || [])
    .filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)))
    .map(Number);
  if (!values.length) return null;
  return kind === "min" ? Math.min(...values) : Math.max(...values);
}
