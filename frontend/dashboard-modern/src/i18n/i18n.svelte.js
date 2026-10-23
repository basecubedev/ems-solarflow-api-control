// SPDX-License-Identifier: AGPL-3.0-or-later
import { STRINGS } from "./strings.js";
import { t, useStrings } from "../../../shared/i18n/i18n.svelte.js";

useStrings(STRINGS);

export { t, fmt } from "../../../shared/i18n/i18n.svelte.js";

/** A label/value pair produced by the pure modules, e.g. a firmware enum. */
export function tv(item) {
  if (!item) return "";
  return t(item.key, item.vars);
}

/** The words this build has for a key, else the server's own (English) words. */
export function translatedOr(key, fallback) {
  const text = t(key);
  return text === key ? fallback : text;
}

/** Control codes as produced by lib/control.js: a known code is translated, an unknown one keeps its words. */
export function codesText(items) {
  return (items || []).map((item) => (item.key ? t(item.key) : item.text)).join(", ");
}
