// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createPageSet } from "../../../shared/shell/pages.js";

export { swapPinned } from "../../../shared/shell/pages.js";

/** The fixed navigation. */
export const PAGES = Object.freeze([
  { id: "overview", icon: "layout-dashboard", group: "main" },
  { id: "devices", icon: "cpu", group: "main" },
  { id: "consumers", icon: "plug-zap", group: "main" },
  { id: "energy", icon: "chart-column", group: "main" },
  { id: "analytics", icon: "chart-spline", group: "main" },
  { id: "control", icon: "sliders-horizontal", group: "main" },
  { id: "schedule", icon: "calendar-clock", group: "main", operator: true },
  { id: "diagnose", icon: "stethoscope", group: "system", operator: true },
  { id: "logs", icon: "scroll-text", group: "system", operator: true },
  { id: "maintenance", icon: "wrench", group: "system", operator: true },
  { id: "about", icon: "info", group: "system", shortLabel: "nav.about.short", hidden: true },
]);

/** The four pages a phone's bar holds until the reader picks their own. */
export const MOBILE_PAGES = Object.freeze(["overview", "devices", "energy", "control"]);

export const pageSet = createPageSet({
  pages: PAGES,
  groups: [{ id: "main" }, { id: "system", label: "nav.system" }],
  mobile: MOBILE_PAGES,
});

export const { pinnedPages, pageById } = pageSet;
