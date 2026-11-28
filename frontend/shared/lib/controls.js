// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later

function decimals(number) {
  const text = String(number);
  const exponent = text.match(/e-(\d+)$/);
  if (exponent) return Number(exponent[1]);
  const dot = text.indexOf(".");
  return dot < 0 ? 0 : text.length - dot - 1;
}

const bound = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);

function current(value) {
  const text = String(value ?? "").trim();
  if (text === "") return null;
  const number = Number(text);
  return Number.isFinite(number) ? number : null;
}

/**
 * The number a stepper's button or arrow key moves to: one step up or down on
 * the step's grid, so 52 goes to 60 or 50 in steps of ten, held within min and
 * max. An empty field steps from ``start``, the default its placeholder names,
 * else from min, else from zero. A text the browser cannot read stays as it is.
 */
export function nudged(value, direction, { step = 1, min = null, max = null, start = null } = {}) {
  const low = bound(min);
  const high = bound(max);
  const typed = String(value ?? "").trim();
  let from = current(value);
  if (typed !== "" && from === null) return null;
  if (from === null) from = current(start) ?? low ?? 0;
  const quotient = from / step;
  const base = direction > 0 ? Math.floor(quotient + 1e-9) : Math.ceil(quotient - 1e-9);
  let next = Number(((base + direction) * step).toFixed(decimals(step)));
  if (low !== null && next < low) next = low;
  if (high !== null && next > high) next = high;
  return next;
}

/** Whether a stepper's button has nowhere to go: the value already sits at the bound it moves towards. */
export function atBound(value, direction, { min = null, max = null, start = null } = {}) {
  const typed = String(value ?? "").trim();
  const from = typed === "" ? current(start) : current(value);
  if (from === null) return false;
  const limit = bound(direction > 0 ? max : min);
  return limit !== null && (direction > 0 ? from >= limit : from <= limit);
}

/**
 * Where a stepper's bar runs for a number over its range [low, high], each as
 * a share from 0 to 1: ``at`` is the number, and the fill runs from ``from``
 * to ``to``, out of zero where the range holds it, else from the low end. An
 * empty field shows the default it steps from. The number may lie beyond
 * the range, which only fills the bar. null without a range, or for a text
 * the browser cannot read.
 */
export function barSpan(value, range, start = null) {
  const [low, high] = Array.isArray(range) ? range.map(bound) : [null, null];
  if (low === null || high === null || high <= low) return null;
  const typed = String(value ?? "").trim();
  const number = typed === "" ? current(start) : current(value);
  if (number === null && typed !== "") return null;
  const place = (point) => Math.min(1, Math.max(0, (point - low) / (high - low)));
  const origin = low < 0 && high > 0 ? place(0) : 0;
  const at = number === null ? origin : place(number);
  return { at, from: Math.min(origin, at), to: Math.max(origin, at) };
}

/** The months a value names, 1 to 12, each once and in calendar order: a list, or a text such as "10, 11, 12". */
export function monthsOf(value) {
  const items = Array.isArray(value) ? value : typeof value === "string" ? value.split(",") : [];
  const months = new Set();
  for (const item of items) {
    const text = String(item).trim();
    if (!/^\d+$/.test(text)) continue;
    const month = Number(text);
    if (month >= 1 && month <= 12) months.add(month);
  }
  return [...months].sort((a, b) => a - b);
}

/**
 * The months in the order a season reads: from the month after the longest
 * break, so October to March is 10, 11, 12, 1, 2, 3. A tie keeps calendar order.
 */
export function seasonOrder(value) {
  const months = monthsOf(value);
  const count = months.length;
  if (count < 2 || count === 12) return months;
  const gap = (index) => (months[(index + 1) % count] - months[index] + 12) % 12;
  let widest = count - 1;
  for (let index = 0; index < count - 1; index += 1) if (gap(index) > gap(widest)) widest = index;
  const first = (widest + 1) % count;
  return [...months.slice(first), ...months.slice(0, first)];
}

/** The unbroken runs of months as [first, last] pairs, in season order; December runs on into January. */
export function monthRuns(value) {
  const runs = [];
  for (const month of seasonOrder(value)) {
    const run = runs[runs.length - 1];
    if (run && month === (run[1] % 12) + 1) run[1] = month;
    else runs.push([month, month]);
  }
  return runs;
}

/** The months with one month added or taken away, in season order; the last month is never taken away. */
export function toggledMonth(value, month) {
  const months = monthsOf(value);
  if (!months.includes(month)) return seasonOrder([...months, month]);
  return months.length > 1 ? seasonOrder(months.filter((item) => item !== month)) : seasonOrder(months);
}

/**
 * The months as a season reads them in a language: "Oct – Mar", runs apart
 * joined with commas, and ``allYear`` for all twelve. ``locale`` is a BCP 47
 * tag; ``length`` is short or long.
 */
export function seasonText(value, locale, allYear, length = "short") {
  const months = monthsOf(value);
  if (months.length === 12) return allYear;
  const format = new Intl.DateTimeFormat(locale, { month: length });
  const name = (month) => format.format(new Date(2026, month - 1, 15));
  return monthRuns(months).map(([first, last]) => (first === last ? name(first) : `${name(first)} – ${name(last)}`)).join(", ");
}

const NUMBER_TYPES = new Set(["number", "integer"]);
const WATT_UNITS = new Set(["W", "W/cycle"]);
const TENTH_UNITS = new Set(["kWh", "kWp"]);
const fractional = (value) => typeof value === "number" && Number.isFinite(value) && !Number.isInteger(value);

/**
 * The range a number's stepper draws as a bar, [low, high], or null: the
 * catalogue's ``scale``, a generous range rather than a limit. A field the
 * catalogue gives none, such as a port, gets no bar.
 */
export function barFor(field) {
  const scale = field?.scale;
  if (!NUMBER_TYPES.has(field?.type) || !Array.isArray(scale) || scale.length !== 2) return null;
  const [low, high] = scale.map(Number);
  return Number.isFinite(low) && Number.isFinite(high) && high > low ? [low, high] : null;
}

function niceStep(size) {
  const power = 10 ** Math.floor(Math.log10(size));
  return [1, 2, 5, 10].map((factor) => factor * power).find((step) => step >= size);
}

/**
 * How far one press of a stepper moves a number. A settings catalogue names no
 * step, so it follows the unit, the values and the bar: watts by one over a
 * bar up to 200 W wide (a deadband, a margin) and by ten beyond, tenths of a
 * kWh, cents of a price, tenths where a default or a bound is a fraction, the
 * range is one wide or a factor's bar at most five, else ones; a bar more
 * than 500 such steps wide moves in round hundredths of it instead, so a
 * timeout of hours is not stepped by seconds. Typing stays free of it.
 */
export function stepFor(field) {
  const step = unitStep(field);
  const bar = barFor(field);
  if (!bar || (bar[1] - bar[0]) / step <= 500) return step;
  return Math.max(step, niceStep((bar[1] - bar[0]) / 100));
}

function unitStep(field) {
  if (field?.type === "integer") return 1;
  const unit = String(field?.unit || "");
  const bar = barFor(field);
  const span = bar ? bar[1] - bar[0] : null;
  if (unit.startsWith("currency/")) return 0.01;
  if (TENTH_UNITS.has(unit)) return 0.1;
  if ([field?.default, field?.min, field?.max].some(fractional)) return 0.1;
  if (Number.isFinite(field?.min) && Number.isFinite(field?.max) && field.max - field.min <= 1) return 0.1;
  if (!unit && span !== null && span <= 5) return 0.1;
  if (WATT_UNITS.has(unit)) return span !== null && span <= 200 ? 1 : 10;
  return 1;
}
