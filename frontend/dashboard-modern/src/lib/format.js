// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Number, energy, time and money formatting for one UI language.
 *
 * Every figure on screen goes through here, so "missing" is decided once: a
 * value that is not a finite number renders as an em dash, never as zero.
 */

export const LOCALES = Object.freeze({ en: "en-GB", de: "de-DE", nl: "nl-NL", fr: "fr-FR" });
export const MISSING = "—";
const MINUS = "−";

export function localeFor(lang) {
  return LOCALES[lang] || LOCALES.en;
}

export function finite(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function signOf(number, signed) {
  if (number < 0) return MINUS;
  if (signed && number > 0) return "+";
  return "";
}

export function createFormat(lang) {
  const locale = localeFor(lang);
  const decimals = (digits) =>
    new Intl.NumberFormat(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const int = decimals(0);
  const one = decimals(1);
  const two = decimals(2);
  const percentFormat = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 });
  const timeFormat = new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const timeSecondsFormat = new Intl.DateTimeFormat(locale, {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  const dateFormat = new Intl.DateTimeFormat(locale, { day: "numeric", month: "long", year: "numeric" });
  const shortDateFormat = new Intl.DateTimeFormat(locale, { day: "numeric", month: "short" });
  const dateTimeFormat = new Intl.DateTimeFormat(locale, {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const monthFormat = new Intl.DateTimeFormat(locale, { month: "short" });
  const weekdayFormat = new Intl.DateTimeFormat(locale, { weekday: "short" });
  const currencyCache = new Map();

  function power(value, { signed = false } = {}) {
    const number = finite(value);
    if (number === null) return MISSING;
    const rounded = Math.round(number);
    const magnitude = Math.abs(rounded);
    const sign = signOf(rounded, signed);
    if (magnitude >= 10000) return `${sign}${one.format(Math.abs(number) / 1000)} kW`;
    if (magnitude >= 1000) return `${sign}${two.format(Math.abs(number) / 1000)} kW`;
    return `${sign}${int.format(magnitude)} W`;
  }

  /** Under one kilowatt-hour the watt-hour is the honest unit; one decimal of a kWh is a 100 Wh step. */
  function energy(kwh, { signed = false } = {}) {
    const number = finite(kwh);
    if (number === null) return MISSING;
    const wh = Math.round(number * 1000);
    const sign = signOf(wh, signed);
    if (Math.abs(wh) < 1000) return `${sign}${int.format(Math.abs(wh))} Wh`;
    const digits = Math.abs(number) >= 1000 ? 0 : 1;
    return `${sign}${decimals(digits).format(Math.abs(number))} kWh`;
  }

  function percent(value, { fraction = false } = {}) {
    const number = finite(value);
    if (number === null) return MISSING;
    return percentFormat.format(fraction ? number : number / 100);
  }

  function money(value, currency = "EUR") {
    const number = finite(value);
    if (number === null) return MISSING;
    const code = String(currency || "EUR").toUpperCase();
    if (!currencyCache.has(code)) {
      try {
        currencyCache.set(code, new Intl.NumberFormat(locale, { style: "currency", currency: code }));
      } catch {
        currencyCache.set(code, null);
      }
    }
    const formatter = currencyCache.get(code);
    return formatter ? formatter.format(number) : `${two.format(number)} ${code}`;
  }

  function number(value, digits = 0) {
    const parsed = finite(value);
    if (parsed === null) return MISSING;
    return decimals(digits).format(parsed);
  }

  const toDate = (value) => (value instanceof Date ? value : new Date(value));
  const valid = (date) => !Number.isNaN(date.getTime());

  function time(value, { seconds = false } = {}) {
    if (value === null || value === undefined) return MISSING;
    const date = toDate(value);
    if (!valid(date)) return MISSING;
    return (seconds ? timeSecondsFormat : timeFormat).format(date);
  }

  /** A calendar date such as "2026-06-14" is a day, not an instant: it must not shift with the time zone. */
  function date(value, { short = false } = {}) {
    if (!value) return MISSING;
    const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    const parsed = match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : toDate(value);
    if (!valid(parsed)) return String(value);
    return (short ? shortDateFormat : dateFormat).format(parsed);
  }

  function dateTime(value) {
    if (!value) return MISSING;
    const parsed = toDate(value);
    return valid(parsed) ? dateTimeFormat.format(parsed) : String(value);
  }

  function month(monthNumber) {
    const index = Number(monthNumber) - 1;
    if (!Number.isInteger(index) || index < 0 || index > 11) return MISSING;
    return monthFormat.format(new Date(2026, index, 1));
  }

  function weekday(value) {
    const parsed = toDate(value);
    return valid(parsed) ? weekdayFormat.format(parsed) : MISSING;
  }

  return { lang, locale, power, energy, percent, money, number, time, date, dateTime, month, weekday };
}
