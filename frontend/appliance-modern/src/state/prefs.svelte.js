// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Appliance Manager's choice between Classic and Modern and its detail
 * level, beside the shared appearance. Classic's theme.js reads the choice
 * before its first paint.
 */
import { readStored, writeStored } from "../../../shared/state/prefs.svelte.js";

export * from "../../../shared/state/prefs.svelte.js";

export const UI_CHOICE_KEY = "ems-appliance-ui";
export const DETAIL_KEY = "ems-modern.detail";
export const DETAILS = Object.freeze(["basic", "expert"]);

class Detail {
  level = $state(readStored(DETAIL_KEY, DETAILS, "basic"));

  get expert() {
    return this.level === "expert";
  }
}

/** Basic and Expert render the same backend state; Expert adds host internals. Kept per browser. */
export const detail = new Detail();

export function setDetail(level) {
  if (!DETAILS.includes(level)) return;
  detail.level = level;
  writeStored(DETAIL_KEY, level);
}

/** Switches this browser to Classic; the address carries the choice and theme.js stores it on arrival. */
export function chooseClassic() {
  globalThis.location.assign("/?ui=classic");
}

export function rememberModern() {
  writeStored(UI_CHOICE_KEY, "modern");
}
