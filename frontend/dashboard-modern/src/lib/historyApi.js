// SPDX-License-Identifier: AGPL-3.0-or-later
import { getJson, query } from "../../../shared/lib/api.js";

/** The local SQLite history (Overview, Devices) or the InfluxDB analytics series. */
export async function fetchSeries({ source = "history", range, start, end, series, devices, signal }) {
  const path = source === "analytics" ? "/api/analytics/series" : "/api/history/series";
  const params = { series, devices };
  if (start !== undefined && end !== undefined) {
    params.start = Math.floor(start);
    params.end = Math.ceil(end);
  } else {
    params.range = range || "24h";
  }
  const payload = await getJson(`${path}${query(params)}`, { signal });
  return {
    available: payload?.available !== false,
    reason: payload?.reason || null,
    hint: payload?.hint || null,
    time: Array.isArray(payload?.time) ? payload.time.map(Number) : [],
    series: payload?.series || {},
    meta: payload?.meta || {},
    devices: payload?.devices || [],
    source: payload?.source || source,
    raw: payload,
  };
}
