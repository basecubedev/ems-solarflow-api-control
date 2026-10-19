// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Analytics views. Each view requests exactly the series it shows and
 * integrates only those: a KPI that read a series its view never asked for
 * would silently show nothing (agent-rules, paired registries).
 */

import { integrateKwh } from "./history.js";

export const ANALYTICS_RANGES = Object.freeze(["1h", "6h", "24h", "7d", "30d", "365d"]);

export const ANALYTICS_VIEWS = Object.freeze([
  { id: "overview", series: ["pv", "output", "battery"], kpis: ["pv", "output", "charge", "discharge"] },
  { id: "devices", series: ["pv", "output", "battery"], kpis: ["pv", "output", "charge", "discharge"], perDevice: true },
  { id: "grid", series: ["grid", "home", "ac_charge"], kpis: ["gridImport", "gridExport", "home", "acCharge"] },
  { id: "battery", series: ["battery"], kpis: ["charge", "discharge"] },
  { id: "pv", series: ["pv", "output"], kpis: ["pv", "pvPeak", "output"] },
]);

export const ANALYTICS_OVERLAYS = Object.freeze(["target", "grid", "ac_charge", "soc"]);

export const SERIES_STYLE = Object.freeze({
  pv: { token: "--pv" },
  output: { token: "--output" },
  battery: { token: "--battery", signed: true },
  grid: { token: "--grid", signed: true },
  home: { token: "--output" },
  ac_charge: { token: "--grid" },
  target: { token: "--m-accent", fill: false },
  soc: { token: "--battery", unit: "%" },
});

const pos = (v) => Math.max(0, v);
const neg = (v) => Math.max(0, -v);

export const KPIS = Object.freeze({
  pv: { series: "pv", kind: "energy", transform: pos, token: "--pv" },
  output: { series: "output", kind: "energy", transform: pos, token: "--output" },
  charge: { series: "battery", kind: "energy", transform: pos, token: "--battery" },
  discharge: { series: "battery", kind: "energy", transform: neg, token: "--battery" },
  gridImport: { series: "grid", kind: "energy", transform: pos, token: "--grid" },
  gridExport: { series: "grid", kind: "energy", transform: neg, token: "--grid" },
  home: { series: "home", kind: "energy", transform: pos, token: "--output" },
  acCharge: { series: "ac_charge", kind: "energy", transform: pos, token: "--grid" },
  pvPeak: { series: "pv", kind: "peak", token: "--pv" },
});

export function viewById(id) {
  return ANALYTICS_VIEWS.find((view) => view.id === id) || ANALYTICS_VIEWS[0];
}

/** The series one request asks for: the view's own plus the power overlays the reader switched on. */
export function requestedSeries(view, overlays) {
  const ids = [...view.series];
  overlays.forEach((id) => {
    if (!ids.includes(id)) ids.push(id);
  });
  return ids;
}

export function computeKpi(id, data) {
  const kpi = KPIS[id];
  if (!kpi || !data) return null;
  const values = data.series?.[kpi.series];
  if (!values) return null;
  if (kpi.kind === "peak") {
    const numbers = values.filter((v) => v !== null && v !== undefined && Number.isFinite(Number(v))).map(Number);
    return numbers.length ? Math.max(...numbers) : null;
  }
  return integrateKwh(data.time, values, { transform: kpi.transform });
}
