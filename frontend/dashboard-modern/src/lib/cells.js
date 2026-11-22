// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Each battery pack's lowest and highest cell voltage, as the live data
 * (`battery_packs`) and the history (`cell_min:<pack>`, `cell_max:<pack>`)
 * name them. The packs are numbered from 1; a voltage the pack did not report
 * stays null.
 */

import { finite } from "../../../shared/lib/format.js";

/** The highest pack number the history serves (ems.models.MAX_PACK_NUMBER). */
export const MAX_PACK_NUMBER = 16;

/** One device's packs from the live data, in pack order. */
export function packCells(entries) {
  if (!Array.isArray(entries)) return [];
  return entries
    .filter((entry) => Number.isInteger(entry?.pack) && entry.pack > 0 && entry.pack <= MAX_PACK_NUMBER)
    .map((entry) => ({ pack: entry.pack, low: finite(entry.cell_min_v), high: finite(entry.cell_max_v) }))
    .sort((a, b) => a.pack - b.pack);
}

/** The lowest and the highest cell over every pack, null where no pack reports one. */
export function cellRange(packs) {
  const lows = packs.map((pack) => pack.low).filter((value) => value !== null);
  const highs = packs.map((pack) => pack.high).filter((value) => value !== null);
  return { low: lows.length ? Math.min(...lows) : null, high: highs.length ? Math.max(...highs) : null };
}

/** "3.17–3.18 V", one voltage when both agree or only one is known, a dash when none is. */
export function cellText(f, low, high) {
  if (low === null && high === null) return f.voltage(null);
  if (low === null || high === null || low === high) return f.voltage(low ?? high);
  return `${f.number(low, 2)}–${f.voltage(high)}`;
}

/** The two history series of one pack. */
export function cellSeriesIds(pack) {
  return [`cell_min:${pack}`, `cell_max:${pack}`];
}

/** Which of the two a history series is, or null for any other series. */
export function cellKind(id) {
  const match = /^cell_(min|max):[1-9][0-9]*$/.exec(String(id));
  return match ? match[1] : null;
}

/** How a cell series is drawn and named: the lowest cell in the battery's colour, the highest in neutral ink. */
export function cellStyle(id) {
  const kind = cellKind(id);
  if (kind === null) return null;
  return kind === "min" ? { label: "series.cellMin", token: "--battery", fill: false } : { label: "series.cellMax", token: "--m-muted", fill: false };
}

/** A pack's power signed by the direction its state names, null when it names none. */
function signedFlow(entry) {
  const watts = finite(entry.power_w);
  if (watts === null) return null;
  if (entry.state === 1) return Math.abs(watts);
  if (entry.state === 2) return -Math.abs(watts);
  return entry.state === 0 && watts === 0 ? 0 : null;
}

/**
 * Each pack's own power from the live data, signed as the device's battery
 * power is: positive while it charges, negative while it discharges. The
 * pack names the direction in its state (0 idle, 1 charging, 2 discharging);
 * a pack whose state names none, or an idle one that reports power, is left
 * out rather than shown as charging.
 */
export function packFlows(entries) {
  if (!Array.isArray(entries)) return [];
  return entries
    .filter((entry) => Number.isInteger(entry?.pack) && entry.pack > 0 && entry.pack <= MAX_PACK_NUMBER)
    .map((entry) => ({ pack: entry.pack, watts: signedFlow(entry) }))
    .filter((flow) => flow.watts !== null)
    .sort((a, b) => a.pack - b.pack);
}

/** The packs of a normalized device that report their cells or a power with its direction, each with both. */
export function packReadings(device) {
  if (!device.hasBattery) return [];
  const flows = new Map(device.packFlows.map((flow) => [flow.pack, flow.watts]));
  return device.cells
    .map((pack) => ({ ...pack, watts: flows.get(pack.pack) ?? null }))
    .filter((pack) => pack.low !== null || pack.high !== null || pack.watts !== null);
}

/** What the packs together report, null when none reports its power. */
export function packFlowTotal(flows) {
  return flows.length ? flows.reduce((sum, flow) => sum + flow.watts, 0) : null;
}

/** The packs of a normalized device that report a cell voltage; none on a device without a battery. */
export function reportedCells(device) {
  return device.hasBattery ? device.cells.filter((pack) => pack.low !== null || pack.high !== null) : [];
}

/** The devices whose packs report a cell voltage, for the charts that draw them. */
export function cellDevices(devices) {
  return devices.map((device) => ({ name: device.name, cells: reportedCells(device) })).filter((device) => device.cells.length);
}

/**
 * The devices whose cell history a chart may read: every device with a
 * battery, also one that reports no cell voltage right now -- offline today,
 * its history is still there. Its packs are the ones it reports, else as many
 * as it counts or ``seen`` remembers, else one; ``reporting`` says whether it
 * reports any now.
 */
export function historyCellDevices(devices, seen = {}) {
  return devices
    .filter((device) => device.hasBattery)
    .map((device) => {
      const cells = reportedCells(device);
      const known = Math.min(Math.max(device.packs ?? 0, seen[device.name] ?? 0) || 1, MAX_PACK_NUMBER);
      const counted = Array.from({ length: known }, (_, index) => ({ pack: index + 1, low: null, high: null }));
      return { name: device.name, cells: cells.length ? cells : counted, reporting: cells.length > 0 };
    });
}

/**
 * The highest pack number each device has reported cells for, added to
 * ``seen``; ``seen`` itself while nothing is new, so a page can keep it.
 */
export function packsSeen(devices, seen) {
  let next = seen;
  for (const device of cellDevices(devices)) {
    const highest = Math.max(...device.cells.map((pack) => pack.pack));
    if (highest > (next[device.name] ?? 0)) next = { ...next, [device.name]: highest };
  }
  return next;
}

/** The devices the Analytics cell view offers: InfluxDB records cells for the controlled devices only. */
export function analyticsCellDevices(devices, seen = {}) {
  return historyCellDevices(devices.filter((device) => !device.readOnly), seen);
}

/** The lowest or the highest value a series reached in a history answer, null without one. */
export function seriesExtreme(data, id, kind) {
  const values = (data?.series?.[id] || []).map(finite).filter((value) => value !== null);
  if (!values.length) return null;
  return kind === "min" ? Math.min(...values) : Math.max(...values);
}

/** The pack a chart shows: the one asked for while the device has it, else its first. */
export function shownPack(packs, wanted) {
  return packs.some((pack) => pack.pack === wanted) ? wanted : packs[0]?.pack ?? null;
}

const VOLT_STEP = 0.05;
const VOLT_MARGIN = 0.02;

/** The volt axis: the data with a margin, on whole steps of 50 mV; a plausible LFP band before any data. */
export function voltRange(min, max) {
  const low = finite(min);
  const high = finite(max);
  if (low === null || high === null) return [3.0, 3.5];
  const round = (value) => Math.round(value * 100) / 100;
  const steps = (value) => Math.round(value / VOLT_STEP * 1e6) / 1e6;
  return [round(Math.floor(steps(low - VOLT_MARGIN)) * VOLT_STEP), round(Math.ceil(steps(high + VOLT_MARGIN)) * VOLT_STEP)];
}
