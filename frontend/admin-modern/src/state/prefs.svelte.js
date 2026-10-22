// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Admin console's choice between Classic and Modern, beside the shared
 * appearance. Classic's admin-theme.js reads it before its first paint.
 */
import { writeStored } from "../../../shared/state/prefs.svelte.js";

export * from "../../../shared/state/prefs.svelte.js";

export const UI_CHOICE_KEY = "ems-admin-ui";

/**
 * Switches this browser to Classic and opens the same page there. The address
 * carries the choice and admin-theme.js stores it on arrival, so a reader who
 * stays (a running job asks first) keeps the choice they had.
 */
export function chooseClassic(classicPage) {
  globalThis.location.assign(`/?ui=classic${classicPage ? `#${classicPage}` : ""}`);
}

export function rememberModern() {
  writeStored(UI_CHOICE_KEY, "modern");
}
