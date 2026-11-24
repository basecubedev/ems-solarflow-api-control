// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * A grid target is stored as one signed number of watts, import positive and
 * export negative. It is entered as a direction and a whole number of watts,
 * so nobody has to know which sign means what.
 */

function empty(value) {
  return value === "" || value === null || value === undefined;
}

/**
 * The direction and watts an input shows for a stored ``value``. A value of 0
 * or none has no direction of its own: it keeps the one last chosen.
 */
export function gridTargetParts(value, direction) {
  if (empty(value)) return { direction, watts: "" };
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return { direction, watts: Number.isFinite(number) ? 0 : value };
  return { direction: number > 0 ? "import" : "export", watts: Math.abs(number) };
}

/** The signed value a direction and an entered number of watts stand for; an empty entry stays empty. */
export function gridTargetValue(direction, watts) {
  if (empty(watts)) return "";
  const number = Number(watts);
  if (!Number.isFinite(number)) return watts;
  const magnitude = Math.abs(number);
  return direction === "export" ? -magnitude : magnitude;
}

/**
 * What a target means for the meter, as a string key and its watts.
 * ``phase`` is "discharge" or "charge"; an empty value reads as ``fallback``.
 */
export function gridTargetMeaning(phase, value, fallback = 0) {
  const number = Number(empty(value) ? fallback : value);
  if (!Number.isFinite(number) || number === 0) return { key: "gridTarget.meaning.zero", watts: 0 };
  return { key: `gridTarget.meaning.${phase}.${number > 0 ? "import" : "export"}`, watts: Math.abs(number) };
}

/** An active target in words, "10 W import" or "10 W export", in the reader's language. */
export function gridTargetText(t, f, value) {
  const number = Number(value);
  return t(number > 0 ? "gridTarget.importValue" : "gridTarget.exportValue", { w: f.power(Math.abs(number)) });
}
