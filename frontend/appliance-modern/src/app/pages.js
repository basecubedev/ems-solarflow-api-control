// SPDX-License-Identifier: AGPL-3.0-or-later
import { createPageSet } from "../../../shared/shell/pages.js";

/**
 * The Appliance Manager's pages. Their ids are the sections the backend names
 * in a finding (appliance/status.py VIEW_*), so "Open Updates" on a finding is
 * the page of the same id. The assistant follows the overview, which stays the
 * first page and so the home: the console opens on it until the reader turns
 * that off. The host settings come last: they are read-only here and opened
 * least.
 */
export const PAGES = Object.freeze([
  { id: "overview", icon: "layout-dashboard", group: "main" },
  { id: "assistant", icon: "compass", group: "main" },
  { id: "updates", icon: "circle-arrow-up", group: "main" },
  { id: "admin", icon: "server", group: "main" },
  { id: "network", icon: "wifi", group: "main" },
  { id: "access", icon: "key-round", group: "main", shortLabel: "nav.access.short" },
  { id: "diagnostics", icon: "stethoscope", group: "system" },
  { id: "settings", icon: "sliders-horizontal", group: "system" },
]);

export const PAGE_IDS = Object.freeze(PAGES.map((page) => page.id));

/** The four pages a phone's bar holds until the reader picks their own: what a recovery visit needs first. */
export const MOBILE_PAGES = Object.freeze(["overview", "updates", "admin", "diagnostics"]);

export const pageSet = createPageSet({
  pages: PAGES,
  groups: [{ id: "main" }, { id: "system", label: "nav.system" }],
  mobile: MOBILE_PAGES,
});

export const { pinnedPages, pageById } = pageSet;
