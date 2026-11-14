// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The first number input of a form that the browser could not read, or null.
 * Such an input reads as empty, so a save would take a typo for an emptied
 * field and write its default.
 */
export function unreadableInput(form) {
  return [...(form?.querySelectorAll?.('input[type="number"]') || [])].find((input) => input.validity?.badInput) || null;
}
