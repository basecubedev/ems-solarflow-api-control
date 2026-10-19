// SPDX-License-Identifier: AGPL-3.0-or-later
import { STRINGS } from "./strings.js";
import { prefs } from "../state/prefs.svelte.js";
import { createFormat } from "../lib/format.js";

const formats = new Map();

/**
 * The text for a key in the selected language. A missing translation falls
 * back to English for that key only; a missing key shows the key, which the
 * string-table test turns into a failure before it can ship.
 */
export function t(key, vars) {
  const entry = STRINGS[key];
  const text = entry ? entry[prefs.lang] ?? entry.en : key;
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? String(vars[name]) : match));
}

export function fmt() {
  const lang = prefs.lang;
  if (!formats.has(lang)) formats.set(lang, createFormat(lang));
  return formats.get(lang);
}

/** A label/value pair produced by the pure modules, e.g. a firmware enum. */
export function tv(item) {
  if (!item) return "";
  return t(item.key, item.vars);
}

/** Control codes as produced by lib/control.js: a known code is translated, an unknown one keeps its words. */
export function codesText(items) {
  return (items || []).map((item) => (item.key ? t(item.key) : item.text)).join(", ");
}
