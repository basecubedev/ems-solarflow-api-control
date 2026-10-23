// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The logs the console may open are the backend's list (/api/settings
 * log_sources); a copy here once stopped at nine while sixteen were declared.
 * Basic shows the three a first look needs, Expert every declared one.
 */

export const BASIC_LOG_SOURCES = Object.freeze(["admin_container", "operations", "audit"]);

/**
 * The sources this build has a name for (appliance/validation.py
 * LOG_SOURCES); a source a newer backend declares is still offered, spelled
 * out.
 */
export const KNOWN_LOG_SOURCES = Object.freeze([
  "appliance_web",
  "appliance_agent",
  "operations",
  "audit",
  "admin_container",
  "ems_container",
  "docker_daemon",
  "boot",
  "packages",
  "manager_install",
  "manager_verify",
  "export",
  "config_seed",
  "grow_root",
  "sshd_keys",
  "backup_access",
  "admin_restart_policy",
]);
export const LOG_LINES = 200;

export function logSources(settings, expert) {
  const declared = (settings && settings.log_sources) || [];
  if (expert) return [...declared];
  return BASIC_LOG_SOURCES.filter((item) => declared.includes(item));
}

/** An empty log and a log nobody could read are two statements. */
export function logSummary(log) {
  if (!log) return null;
  if (log.unreadable) return { unreadable: String(log.unreadable), lines: 0, truncated: false, text: "" };
  return { unreadable: "", lines: Number(log.lines) || 0, truncated: Boolean(log.truncated), text: String(log.text || "") };
}

export function logPath(source) {
  return `/api/logs/${encodeURIComponent(source)}?lines=${LOG_LINES}`;
}
