// SPDX-License-Identifier: AGPL-3.0-or-later
import { STRINGS } from "./strings.js";
import { CATALOG_NAMES, CATALOG_OPTIONS, CATALOG_WORDS } from "./catalog.js";
import { t, useStrings } from "../../../shared/i18n/i18n.svelte.js";
import { prefs } from "../../../shared/state/prefs.svelte.js";

useStrings(STRINGS);

export { t, tn, fmt } from "../../../shared/i18n/i18n.svelte.js";

/** A text that is either a key with its facts or the server's own words. */
export function say(item) {
  if (!item) return "";
  if (item.key) return t(item.key, item.vars);
  return String(item.text || "");
}

/**
 * A text the settings catalogue sends, in the reader's language where this
 * build has it; otherwise the catalogue's own English, marked as such.
 */
export function catalogText(text) {
  const value = String(text ?? "");
  if (!value || prefs.lang === "en" || CATALOG_NAMES.includes(value)) return { text: value };
  const entry = CATALOG_WORDS[value];
  return entry?.[prefs.lang] ? { text: entry[prefs.lang] } : { text: value, lang: "en" };
}

/** An option the catalogue offers, by the value the config stores. */
export function optionText(value) {
  const entry = CATALOG_OPTIONS[String(value)];
  return entry ? entry[prefs.lang] ?? entry.en : String(value);
}
