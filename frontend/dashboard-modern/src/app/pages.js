// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The fixed navigation. "classicView" is the value Classic stores in
 * dashboard.flowView for the same page; tests/classic-parity.test.js walks Classic's
 * tab list and requires every tab to appear here.
 */
export const PAGES = Object.freeze([
  { id: "overview", icon: "layout-dashboard", group: "main", classicView: "aggregated" },
  { id: "devices", icon: "cpu", group: "main", classicView: "devices" },
  { id: "energy", icon: "chart-column", group: "main", classicView: "energy" },
  { id: "analytics", icon: "chart-spline", group: "main", classicView: "analytics" },
  { id: "control", icon: "sliders-horizontal", group: "main", classicView: "control" },
  { id: "schedule", icon: "calendar-clock", group: "main", operator: true, classicView: "schedule" },
  { id: "diagnose", icon: "stethoscope", group: "system", operator: true, classicView: "diagnose" },
  { id: "logs", icon: "scroll-text", group: "system", operator: true, classicView: "logs" },
  { id: "maintenance", icon: "wrench", group: "system", operator: true, classicView: "maintenance" },
]);

/** The four pages a phone's bar holds until the reader picks their own. */
export const MOBILE_PAGES = Object.freeze(["overview", "devices", "energy", "control"]);
export const MOBILE_SLOTS = MOBILE_PAGES.length;

/**
 * The bar's pages from what the browser stored: known ids, each once, the
 * first four kept, and the default filling any gap in its own order.
 */
export function pinnedPages(stored) {
  const ids = typeof stored === "string" ? stored.split(",") : Array.isArray(stored) ? stored : [];
  const chosen = [];
  for (const raw of ids) {
    const id = String(raw).trim();
    if (chosen.length < MOBILE_SLOTS && !chosen.includes(id) && PAGES.some((page) => page.id === id)) chosen.push(id);
  }
  for (const id of MOBILE_PAGES) {
    if (chosen.length < MOBILE_SLOTS && !chosen.includes(id)) chosen.push(id);
  }
  return chosen;
}

/**
 * Two pages in the bar trade places; a page from More takes the place of the
 * one in the bar it is swapped with. Two pages from More swap nothing: null.
 */
export function swapPinned(pinned, first, second) {
  const a = pinned.indexOf(first);
  const b = pinned.indexOf(second);
  if (first === second || (a < 0 && b < 0)) return null;
  const next = [...pinned];
  if (a >= 0) next[a] = second;
  if (b >= 0) next[b] = first;
  return next;
}

export function pageById(id) {
  return PAGES.find((page) => page.id === id) || PAGES[0];
}
