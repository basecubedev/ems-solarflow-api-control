// SPDX-License-Identifier: AGPL-3.0-or-later
import { SHARED_STRINGS } from "./strings.js";
import { prefs } from "../state/prefs.svelte.js";
import { createFormat, localeFor } from "../lib/format.js";

const formats = new Map();
const plurals = new Map();
let table = SHARED_STRINGS;

/**
 * The app's own string table, beside the shared one. Called once while the
 * app's modules load, before anything renders; a key may live in one table
 * only, which tests/strings.test.js of each app enforces.
 */
export function useStrings(strings) {
  table = { ...SHARED_STRINGS, ...strings };
}

/**
 * The text for a key in the selected language. A missing translation falls
 * back to English for that key only; a missing key shows the key, which the
 * string-table tests turn into a failure before it can ship.
 */
export function t(key, vars) {
  const entry = table[key];
  const text = entry ? entry[prefs.lang] ?? entry.en : key;
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? String(vars[name]) : match));
}

export function fmt() {
  const lang = prefs.lang;
  if (!formats.has(lang)) formats.set(lang, createFormat(lang));
  return formats.get(lang);
}

/**
 * A text that depends on a count: the key's plural form for the reader's
 * language (key.one, key.other, and key.many where a language has one),
 * with {count} filled in as the language writes numbers.
 */
export function tn(key, count, vars) {
  const lang = prefs.lang;
  if (!plurals.has(lang)) plurals.set(lang, new Intl.PluralRules(localeFor(lang)));
  const form = `${key}.${plurals.get(lang).select(Number(count))}`;
  return t(table[form] ? form : `${key}.other`, { ...vars, count: fmt().number(count) });
}
