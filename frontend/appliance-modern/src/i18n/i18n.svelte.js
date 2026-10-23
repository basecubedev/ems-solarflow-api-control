// SPDX-License-Identifier: AGPL-3.0-or-later
import { STRINGS } from "./strings.js";
import { t, tn, useStrings } from "../../../shared/i18n/i18n.svelte.js";

useStrings(STRINGS);

export { t, tn, fmt } from "../../../shared/i18n/i18n.svelte.js";

/**
 * Words that are either a key with its facts -- a fact may itself be a key,
 * and a counted text carries its count -- or the server's own sentence.
 */
export function say(words) {
  if (!words) return "";
  if (!words.key) return String(words.text || "");
  const vars = Object.fromEntries(Object.entries(words.vars || {}).map(([name, value]) => [name, value && typeof value === "object" ? say(value) : value]));
  return words.count === undefined ? t(words.key, vars) : tn(words.key, words.count, vars);
}

/** The lang attribute for words that kept the server's English. */
export function langOf(words) {
  return words && !words.key ? words.lang : undefined;
}
