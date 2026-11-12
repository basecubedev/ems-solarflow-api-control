// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Pairing a HomeWizard P1 Meter: the server makes one attempt per request
 * (admin/homewizard_pairing.py), the console asks again while the operator
 * presses the meter's button. The token goes into the draft only; it is saved
 * with the settings and never shown.
 */

export const HOMEWIZARD_TYPE = "homewizard_p1";
export const PAIR_PATH = "/api/grid-meter/homewizard-p1/pair";
export const PAIRING_WINDOW_MS = 60_000;
export const PAIRING_RETRY_MS = 2_000;

/** The catalogue fields pairing fills in; the pairing panel shows them instead of plain inputs. */
export const SERIAL_FIELD = "grid_meter.serial";
export const TOKEN_FIELD = "grid_meter.token";
export const PAIRING_NAME_FIELD = "grid_meter.pairing_name";
export const PAIRING_FIELDS = Object.freeze([SERIAL_FIELD, TOKEN_FIELD, PAIRING_NAME_FIELD]);

/** The request body for one attempt, with this installation's stored pairing name; null while there is no address. */
export function pairingRequest(ip, port, pairingName) {
  const address = String(ip ?? "").trim();
  if (!address) return null;
  const typed = String(port ?? "").trim();
  const name = String(pairingName ?? "").trim();
  return { ip: address, port: typed === "" ? null : Number(typed), ...(name ? { pairing_name: name } : {}) };
}

/** Failure reasons the console says in the reader's language (ems/homewizard.py REASON_*). */
export const SAID_REASONS = Object.freeze(["unreachable", "not_a_p1_meter", "no_room"]);

/** What one answer means: still waiting for the button, paired, or failed with the server's reason and words. */
export function pairingOutcome(answer) {
  if (answer?.state === "waiting") return { phase: "waiting" };
  if (answer?.state === "paired" && answer.serial && answer.token && answer.pairing_name) return { phase: "paired", serial: String(answer.serial), token: String(answer.token), pairingName: String(answer.pairing_name) };
  return { phase: "failed", reason: String(answer?.reason || ""), message: String(answer?.message || answer?.error || "") };
}
