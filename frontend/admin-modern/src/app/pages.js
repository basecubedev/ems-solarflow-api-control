// SPDX-License-Identifier: AGPL-3.0-or-later
import { createPageSet } from "../../../shared/shell/pages.js";

export { swapPinned } from "../../../shared/shell/pages.js";

/**
 * The Admin console's pages. "classic" is the Classic console's address for
 * the same page (#maintenance-status and so on), which the switch to Classic
 * opens; tests/classic-parity.test.js requires each to be one Classic routes.
 * "shortLabel" names the page in the phone bar where its full name is long.
 * The assistant comes right after the overview, which stays the first page
 * and so the home: it is the way in for a reader who does not know where to
 * start, and the console opens on it until that reader turns it off. Updates
 * and the settings follow: they are what an owner opens most, to update and
 * to switch features such as winter mode.
 */
export const PAGES = Object.freeze([
  { id: "overview", icon: "layout-dashboard", group: "main", classic: "maintenance" },
  { id: "assistant", icon: "compass", group: "main", classic: "maintenance" },
  { id: "updates", icon: "circle-arrow-up", group: "main", classic: "maintenance-upgrade" },
  { id: "settings", icon: "sliders-horizontal", group: "main", classic: "maintenance-settings", shortLabel: "nav.settings.short" },
  { id: "services", icon: "server", group: "main", classic: "maintenance-status" },
  { id: "backups", icon: "archive", group: "main", classic: "maintenance-backup" },
  { id: "diagnostics", icon: "stethoscope", group: "system", classic: "maintenance-status" },
  { id: "system", icon: "hard-drive", group: "system", classic: "maintenance-status" },
  { id: "setup", icon: "package-plus", group: "system", classic: "setup", hidden: true },
]);

/** The four pages a phone's bar holds until the reader picks their own. */
export const MOBILE_PAGES = Object.freeze(["overview", "updates", "settings", "services"]);

export const pageSet = createPageSet({
  pages: PAGES,
  groups: [{ id: "main" }, { id: "system", label: "nav.system" }],
  mobile: MOBILE_PAGES,
});

export const { pinnedPages, pageById } = pageSet;
