// SPDX-License-Identifier: AGPL-3.0-or-later
import { ApiError } from "../../../shared/lib/api.js";

/**
 * The Admin server's error codes that have a sentence of their own here, in
 * every language. tests/classic-parity.test.js requires every code Classic
 * has words for (ADMIN_ERROR_MESSAGES and AUTH_ERROR_MESSAGES in admin.js) to
 * be in this list.
 */
export const KNOWN_ERRORS = Object.freeze([
  "system_transition_in_progress",
  "system_alignment_incomplete",
  "setup_operation_required",
  "operation_mismatch",
  "system_build_mismatch",
  "system_build_alignment_required",
  "system_build_registry_rate_limited",
  "image_pull_rate_limited",
  "confirmation_required",
  "acknowledgement_required",
  "docker_cli_missing",
  "docker_daemon_unreachable",
  "docker_permission_denied",
  "docker_compose_unsupported",
  "workspace_permission_denied",
  "compose_port_conflict",
  "compose_container_name_conflict",
  "compose_image_unavailable",
  "refresh_failed",
  "checksum_invalid",
  "conflicts_require_policy",
  "influxdb_preview_failed",
  "not_configured",
  "target_exists",
  "invalid_legacy_config",
  "legacy_config_missing",
  "device_list_failed",
  "password_required",
  "password_mismatch",
  "auth_already_configured",
  "invalid_password",
  "login_rate_limited",
  "setup_rate_limited",
  "install_dir_unavailable",
  "not_authenticated",
  "csrf_failed",
  "auth_not_configured",
  "origin_rejected",
  "workflow_lifecycle_changed",
  "workflow_operation_in_progress",
  "workflow_switch_blocked",
  "workflow_recovery_unsafe",
  "restore_in_progress",
]);

/**
 * Some server errors are sentences in the "error" field rather than codes
 * (admin/backup_restore.py raises them that way, and admin/server.py answers
 * an unknown job so). The ones an owner can
 * reach get words in every language; tests/test_admin_modern.py holds each
 * sentence to the server's source, so a reworded one fails there.
 */
export const KNOWN_SENTENCES = Object.freeze({
  "the backup password is incorrect": "error.backup_password_incorrect",
  "unknown or expired restore plan": "error.restore_plan_expired",
  "that is not an EMS backup file name": "error.not_a_backup",
  "that file is not an EMS backup archive": "error.not_a_backup",
  "the uploaded file could not be read": "error.upload_unreadable",
  "unknown backup id": "error.backup_gone",
  "archive checksums are invalid": "error.checksum_invalid",
  "unknown backup set id": "error.backup_gone",
  "unknown job_id": "jobs.unreadable",
});

function codeOf(payload) {
  const raw = payload.error === undefined || payload.error === null ? payload.reason : payload.error;
  return raw && typeof raw === "object" ? raw.code : raw;
}

function messageOf(payload) {
  const raw = payload.error === undefined || payload.error === null ? payload.reason : payload.error;
  const nested = raw && typeof raw === "object" ? raw.message : "";
  const message = payload.message || nested;
  return typeof message === "string" ? message.trim() : "";
}

function payloadOf(error) {
  if (error instanceof ApiError) return error.payload || {};
  return error && typeof error === "object" && !(error instanceof Error) ? error : {};
}

function codeFor(error, payload) {
  return codeOf(payload) || (error?.code && !String(error.code).startsWith("http_") ? error.code : "");
}

/** The translation key this console has for an error, or null when it has none. */
export function errorKey(error) {
  const code = codeFor(error, payloadOf(error));
  if (typeof code !== "string") return null;
  if (KNOWN_ERRORS.includes(code)) return `error.${code}`;
  return Object.hasOwn(KNOWN_SENTENCES, code) ? KNOWN_SENTENCES[code] : null;
}

/** What a proxy in front of the console answers when the console itself did not. */
const GATEWAY_STATUSES = Object.freeze([502, 503, 504]);

/**
 * A request that got no answer at all. The server may have acted on it or
 * not, so it is reported as unknown and the state read again, never as a
 * failure that left everything as it was.
 */
export function noAnswer(error) {
  if (error instanceof ApiError) return GATEWAY_STATUSES.includes(error.status) && !codeOf(error.payload || {});
  return error instanceof Error && error.name !== "AbortError";
}

/**
 * The words for an error with their language: a sentence this console has
 * words for is in the reader's language; the server's own sentence stays
 * English and says so, for screen readers as much as for the eye.
 */
export function errorView(error, t, fallback = "") {
  const text = errorText(error, t, fallback);
  if (errorKey(error)) return { text, lang: undefined };
  const payload = payloadOf(error);
  const code = codeFor(error, payload);
  const servers = Boolean(messageOf(payload)) || (typeof code === "string" && code.includes(" "));
  return { text, lang: servers ? "en" : undefined };
}

/**
 * The words for an error: a code this console knows in the reader's language
 * first, then the server's own sentence, then a code that is already a
 * sentence (a third of the server's errors are), and only then a fallback.
 * Accepts an ApiError or a plain response payload.
 */
export function errorText(error, t, fallback = "") {
  const payload = payloadOf(error);
  const code = codeFor(error, payload);
  const key = errorKey(error);
  if (key) return t(key);
  const message = messageOf(payload);
  if (message) return message;
  if (typeof code === "string" && code.includes(" ")) return code;
  if (noAnswer(error)) return t("error.noResponse");
  if (fallback) return fallback;
  return t("error.generic", { code: code || error?.code || "unknown" });
}
