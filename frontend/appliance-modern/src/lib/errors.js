// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The appliance's refusals by their codes. A known code is said in the
 * reader's language; any other keeps the server's sentence, marked as English,
 * so a newer server is never silenced. tests/test_appliance_modern.py holds
 * each code to the appliance source that raises it.
 */

export const KNOWN_ERRORS = Object.freeze([
  "invalid_credentials",
  "login_rate_limited",
  "login_busy",
  "password_required",
  "password_mismatch",
  "current_password_invalid",
  "previous_password_invalid",
  "password_unconfirmed",
  "password_changed",
  "agent_unavailable",
  "authentication_required",
  "csrf_token_invalid",
  "csrf_origin_rejected",
  "csrf_host_rejected",
  "operation_conflict",
  "plan_not_awaiting_confirmation",
  "confirmation_token_mismatch",
  "timezone_unchanged",
]);

/**
 * { key } for a known code, else { text, lang: "en" } with the server's own
 * message, else the fallback key. A request that never reached the appliance
 * has no code and says so.
 */
export function errorWords(error, fallbackKey = "error.failed") {
  const code = error && error.code;
  if (code && KNOWN_ERRORS.includes(code)) return { key: `error.${code}` };
  if (error && error.status === undefined && !(error.payload && error.payload.message)) return { key: "error.unreachable" };
  const message = error && error.payload && error.payload.message;
  if (message) return { text: String(message), lang: "en" };
  return { key: fallbackKey, vars: { code: code || "—" } };
}
