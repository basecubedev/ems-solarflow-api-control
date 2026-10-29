// SPDX-License-Identifier: AGPL-3.0-or-later
/** Server error codes that have their own sentence; everything else names its code. */
export const KNOWN_ERRORS = Object.freeze([
  "invalid_password",
  "login_rate_limited",
  "not_authenticated",
  "csrf_failed",
  "auth_not_configured",
  "maintenance_busy",
  "diagnose_busy",
  "request_too_large",
]);

export function errorText(error, t) {
  const payload = error?.payload || {};
  const message = typeof payload.message === "string" ? payload.message.trim() : "";
  const code = error?.code || payload.error || "";
  if (KNOWN_ERRORS.includes(code)) return t(`error.${code}`);
  if (message) return message;
  return t("error.generic", { code: code || "unknown" });
}
