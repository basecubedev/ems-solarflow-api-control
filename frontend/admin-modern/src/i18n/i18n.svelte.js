// SPDX-License-Identifier: AGPL-3.0-or-later
import { STRINGS } from "./strings.js";
import { t, useStrings } from "../../../shared/i18n/i18n.svelte.js";

useStrings(STRINGS);

export { t, tn, fmt } from "../../../shared/i18n/i18n.svelte.js";

/** A text that is either a key with its facts or the server's own words. */
export function say(item) {
  if (!item) return "";
  if (item.key) return t(item.key, item.vars);
  return String(item.text || "");
}
