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

export const MOBILE_PAGES = Object.freeze(["overview", "devices", "energy", "control"]);

export function pageById(id) {
  return PAGES.find((page) => page.id === id) || PAGES[0];
}
