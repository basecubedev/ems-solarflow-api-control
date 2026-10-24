// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Appliance Manager's detail level beside the shared appearance, and the
 * way to the deprecated Classic console.
 */
import { readStored, writeStored } from "../../../shared/state/prefs.svelte.js";

export * from "../../../shared/state/prefs.svelte.js";

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

/** Classic answers under /classic/ and is only ever opened on purpose: nothing is remembered, the root stays Modern. */
export function chooseClassic() {
  globalThis.location.assign("/classic/");
}
