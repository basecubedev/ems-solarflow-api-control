// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * How a chart reads a battery's temperatures and currents: the axes and the
 * labels. Kept apart from the batteries view (battery.js), which only the
 * Analytics page loads, as every chart reads its axes through these.
 */

import { MINUS, MISSING } from "../../../shared/lib/format.js";

/** A temperature axis in steps of five degrees that always shows 0 °C, the line a battery heats from. */
export function celsiusRange(min, max) {
  const low = Number.isFinite(min) ? min : 0;
  const high = Number.isFinite(max) ? max : 10;
  return [Math.min(-5, Math.floor(low / 5) * 5), Math.max(10, Math.ceil(high / 5) * 5)];
}

/** A temperature axis label: a whole degree without a decimal, any other tick as a temperature. */
export function celsiusTick(value, f) {
  return Number.isInteger(value) ? `${value < 0 ? MINUS : ""}${f.number(Math.abs(value), 0)}\u00a0°C` : f.temperature(value);
}

/** A current axis in whole amps that always shows 0 A, the line between charging and discharging. */
export function ampRange(min, max) {
  const low = Number.isFinite(min) ? min : 0;
  const high = Number.isFinite(max) ? max : 1;
  return [Math.min(0, Math.floor(low)), Math.max(1, Math.ceil(high))];
}

/**
 * A pack's current as a legend reads it: Charge or Discharge by the sign it
 * is drawn with (below zero while charging), once it rounds to the tenth of
 * an amp it is shown in, and neither for one that rounds to 0 A.
 */
export function currentReading(shown) {
  const tenth = Math.round(Number(shown) * 10) / 10;
  if (tenth < 0) return { key: "chart.charge", amps: -tenth };
  if (tenth > 0) return { key: "chart.discharge", amps: tenth };
  return { key: null, amps: 0 };
}

/** A current in amps to the tenth the packs report it in. */
export function ampText(value, f) {
  return Number.isFinite(value) ? `${value < 0 ? MINUS : ""}${f.number(Math.abs(value), 1)}\u00a0A` : MISSING;
}
