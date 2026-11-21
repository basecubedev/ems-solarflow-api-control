// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createPageSet } from "../../../shared/shell/pages.js";

export { swapPinned } from "../../../shared/shell/pages.js";

/**
 * The product's pages in one menu: the dashboard's, whose components live in
 * frontend/dashboard-modern/ and read the EMS through this console, and the
 * console's own. ``area`` says whose server answers a page: "ems" pages are
 * readable without a sign-in, as the dashboard always was, and an ``operator``
 * one needs the EMS session; "admin" pages need the console's session.
 * "shortLabel" names the page in the phone bar where its full name is long.
 * The dashboard's overview is the home; the console's own overview is
 * "status". The assistant follows it, then updates and the settings, what an
 * owner opens most.
 */
export const PAGES = Object.freeze([
  { id: "overview", icon: "layout-dashboard", group: "dashboard", area: "ems" },
  { id: "devices", icon: "cpu", group: "dashboard", area: "ems" },
  { id: "consumers", icon: "plug-zap", group: "dashboard", area: "ems" },
  { id: "energy", icon: "chart-column", group: "dashboard", area: "ems" },
  { id: "analytics", icon: "chart-spline", group: "dashboard", area: "ems" },
  { id: "control", icon: "gauge", group: "dashboard", area: "ems" },
  { id: "schedule", icon: "calendar-clock", group: "dashboard", area: "ems", operator: true },
  { id: "logs", icon: "scroll-text", group: "dashboard", area: "ems", operator: true },
  { id: "status", icon: "activity", group: "admin", area: "admin" },
  { id: "assistant", icon: "compass", group: "admin", area: "admin" },
  { id: "updates", icon: "circle-arrow-up", group: "admin", area: "admin" },
  { id: "settings", icon: "sliders-horizontal", group: "admin", area: "admin", shortLabel: "nav.settings.short" },
  { id: "services", icon: "server", group: "admin", area: "admin" },
  { id: "backups", icon: "archive", group: "admin", area: "admin" },
  { id: "diagnostics", icon: "stethoscope", group: "admin", area: "admin" },
  { id: "system", icon: "hard-drive", group: "admin", area: "admin" },
  { id: "setup", icon: "package-plus", group: "admin", area: "admin", hidden: true },
  { id: "about", icon: "info", group: "admin", area: "ems", shortLabel: "nav.about.short", hidden: true },
]);

/** The four pages a phone's bar holds until the reader picks their own. */
export const MOBILE_PAGES = Object.freeze(["overview", "energy", "status", "updates"]);

export const pageSet = createPageSet({
  pages: PAGES,
  groups: [
    { id: "dashboard", label: "nav.group.dashboard" },
    { id: "admin", label: "nav.group.admin" },
  ],
  mobile: MOBILE_PAGES,
});

export const { pinnedPages, pageById } = pageSet;

/** Whether a page needs the console's sign-in. */
export const needsConsole = (page) => page?.area === "admin";

/**
 * The pages old addresses named, so a bookmark still opens the same page
 * here: the removed Classic console's (#maintenance-upgrade,
 * #maintenance-settings-features and the like, the longest matching prefix
 * wins; #maintenance-manual was its services panel) and the dashboard's
 * Diagnose page, which this app folds into its own Diagnostics.
 */
export const LEGACY_ADDRESSES = Object.freeze({
  maintenance: "status",
  "maintenance-upgrade": "updates",
  "maintenance-settings": "settings",
  "maintenance-status": "services",
  "maintenance-manual": "services",
  "maintenance-backup": "backups",
});
const FOLDED_PAGES = Object.freeze({ diagnose: "diagnostics" });

/** The page an old address names, or null for any other address. */
export function legacyPage(id) {
  if (Object.hasOwn(FOLDED_PAGES, id)) return FOLDED_PAGES[id];
  const match = Object.keys(LEGACY_ADDRESSES)
    .filter((prefix) => id === prefix || id.startsWith(`${prefix}-`))
    .sort((a, b) => b.length - a.length)[0];
  return match ? LEGACY_ADDRESSES[match] : null;
}
