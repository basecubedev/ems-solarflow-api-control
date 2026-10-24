// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What needs the owner's attention, ranked, and the one sentence above it.
 * The findings are the backend's (status.health.warnings) beside the two this
 * console adds about its own session; the ranking, the verdict and the marks
 * in the menu are the rules Classic applies (appliance/static/app.js), so the
 * two views never disagree about the same appliance.
 *
 * A finding arrives as a code beside English sentences. A known code is said
 * in the reader's language with the facts read from the same status payload;
 * an unknown one keeps the server's words, marked as English.
 */

export const SEVERITY_ORDER = Object.freeze(["error", "warning", "info"]);
export const SEVERITY_TONES = Object.freeze({ error: "c-fault", warning: "c-warn", info: "c-acc" });
export const SEVERITY_ICONS = Object.freeze({ error: "circle-x", warning: "triangle-alert", info: "info" });

/** The status sections whose unreadable probe is a finding of its own (status.SECTION_LABELS). */
export const STATUS_SECTIONS = Object.freeze(["system", "docker", "admin", "updates", "network", "ssh", "operations"]);

import { CONTAINER_STATES, DAEMON_STATES, word } from "./words.js";

/** The rescue account the image ships (appliance/rescue_account.py ACCOUNT). */
export const RESCUE_ACCOUNT = "ems-rescue";

const words = (key, vars, count) => ({ key, vars, count });

function updates(status) {
  return (status && status.updates) || {};
}

/** The daemon's state as the Docker tile says it; a state nobody anticipated keeps the host's word. */
function daemonWords(status) {
  const state = (((status && status.docker) || {}).daemon || {}).state;
  return word("docker", DAEMON_STATES, state) || { key: "common.unknown" };
}

function emsContainer(status) {
  const docker = (status && status.docker) || {};
  return (docker.containers || []).find((item) => item.name === docker.ems_container) || {};
}

/**
 * Every code the backend raises (appliance/status.py), and the words for it.
 * tests/test_appliance_modern.py walks the backend's finding() calls against
 * this table in both directions.
 */
export const FINDINGS = Object.freeze({
  docker_not_running: (status) => ({
    title: words("finding.docker_not_running.title"),
    message: words("finding.docker_not_running.message", { state: daemonWords(status) }),
    next: words("finding.docker_not_running.next"),
  }),
  ems_not_running: (status) => ({
    title: words("finding.ems_not_running.title"),
    message: words("finding.ems_not_running.message", { name: emsContainer(status).name || "—", state: word("container", CONTAINER_STATES, emsContainer(status).state) || { key: "common.unknown" } }),
    next: words("finding.ems_not_running.next"),
  }),
  admin_not_installed: (status) => ({
    title: words("finding.admin_not_installed.title"),
    message: words("finding.admin_not_installed.message"),
    next: words(((status && status.admin) || {}).bootstrap_required ? "finding.admin_not_installed.nextInstall" : "finding.admin_not_installed.nextRepair"),
  }),
  admin_unhealthy: () => ({
    title: words("finding.admin_unhealthy.title"),
    message: words("finding.admin_unhealthy.message"),
    next: words("finding.admin_unhealthy.next"),
  }),
  security_updates_pending: (status) => ({
    title: words("finding.security_updates_pending.title"),
    message: words("finding.security_updates_pending.message", {}, Number(updates(status).security_count) || 0),
    next: words("finding.security_updates_pending.next"),
  }),
  reboot_required: () => ({
    title: words("finding.reboot_required.title"),
    message: words("finding.reboot_required.message"),
    next: words("finding.reboot_required.next"),
  }),
  package_index_stale: (status) => ({
    title: words("finding.package_index_stale.title"),
    message: words("finding.package_index_stale.message", {}, Math.floor((Number(updates(status).index_age_seconds) || 0) / 86400)),
    next: words("finding.package_index_stale.next"),
  }),
  update_check_failed: () => ({
    title: words("finding.update_check_failed.title"),
    message: words("finding.update_check_failed.message"),
    next: words("finding.update_check_failed.next"),
  }),
  package_manager_unhealthy: () => ({
    title: words("finding.package_manager_unhealthy.title"),
    message: words("finding.package_manager_unhealthy.message"),
    next: words("finding.package_manager_unhealthy.next"),
  }),
  storage_low: () => ({
    title: words("finding.storage_low.title"),
    message: words("finding.storage_low.message"),
    next: words("finding.storage.next"),
  }),
  persistent_storage_low: () => ({
    title: words("finding.persistent_storage_low.title"),
    message: words("finding.persistent_storage_low.message"),
    next: words("finding.storage.next"),
  }),
  rescue_password_accepted_over_ssh: () => ({
    title: words("finding.rescue_password_accepted_over_ssh.title"),
    message: words("finding.rescue_password_accepted_over_ssh.message", { account: RESCUE_ACCOUNT }),
    next: words("finding.rescue_password_accepted_over_ssh.next", { account: RESCUE_ACCOUNT }),
  }),
  status_unavailable: (status) => ({
    title: words("finding.status_unavailable.title"),
    message: words("finding.status_unavailable.message", { code: (status && status.error) || "—" }),
    next: words("finding.status_unavailable.next"),
  }),
  security_audit_degraded: (status, finding) => ({
    title: words("finding.security_audit_degraded.title"),
    message: words("finding.security_audit_degraded.message", { error: finding.facts?.error || "—" }, Number(finding.facts?.unrecorded) || 0),
    next: words("finding.security_audit_degraded.next"),
  }),
  password_unconfirmed: () => ({
    title: words("finding.password_unconfirmed.title"),
    message: words("finding.password_unconfirmed.message"),
    next: words("finding.password_unconfirmed.next"),
  }),
  ...Object.fromEntries(
    STATUS_SECTIONS.map((section) => [
      `${section}_unavailable`,
      () => ({
        title: words(`finding.${section}_unavailable.title`),
        message: words("finding.section_unavailable.message"),
        next: words("finding.section_unavailable.next"),
      }),
    ]),
  ),
});

export function rankSeverity(severity) {
  const index = SEVERITY_ORDER.indexOf(severity);
  return index === -1 ? SEVERITY_ORDER.length : index;
}

/**
 * The audit log is where a sign-in is recorded; when it cannot be written that
 * is a finding about this appliance like any other.
 */
export function auditFinding(audit) {
  if (!audit || !audit.degraded) return null;
  return {
    code: "security_audit_degraded",
    severity: "error",
    section: "settings",
    title: "Some sign-ins were not recorded",
    message: audit.message || "Authentication events could not be written to the audit log.",
    next_step: "Open Settings for the audit state.",
    facts: { unrecorded: audit.unrecorded_events, error: audit.last_error || "" },
  };
}

/** Only an explicit "no" is a finding: an agent that could not be asked has not said the password is unconfirmed. */
export function confirmationFinding(confirmed) {
  if (confirmed !== false) return null;
  return {
    code: "password_unconfirmed",
    severity: "warning",
    section: "settings",
    title: "SSH and Manager changes wait for the previous password",
    message: "",
    next_step: "",
  };
}

export function sessionFindings(audit, confirmed) {
  return [auditFinding(audit), confirmationFinding(confirmed)].filter(Boolean);
}

/**
 * A status read that failed is itself the finding: tiles full of em dashes
 * read as a healthy appliance with nothing installed, not as one nobody could
 * ask. Errors first, then warnings, then the rest, each in the server's order.
 */
export function rankedFindings(status, extra = []) {
  if (status && status.error) {
    return [{ code: "status_unavailable", severity: "error", section: "diagnostics", title: "The appliance status could not be read", message: "", next_step: "" }];
  }
  const own = ((status && status.health) || {}).warnings || [];
  return [...own, ...(extra || [])]
    .map((item, index) => ({ item, index }))
    .sort((left, right) => rankSeverity(left.item.severity) - rankSeverity(right.item.severity) || left.index - right.index)
    .map(({ item }) => item);
}

/**
 * The verdict line. The browser's own findings can raise it, never lower it,
 * and info moves nothing -- the mapping status.health_level() applies.
 */
export function overviewVerdict(status, extra = []) {
  if (status === undefined || status === null) return { key: "verdict.loading", tone: "idle" };
  if (status.error) return { key: "verdict.unread", tone: "fault" };
  const level = (status.health || {}).level;
  const worst = (extra || []).reduce((carried, item) => (rankSeverity(item?.severity) < rankSeverity(carried) ? item.severity : carried), "");
  if (level === "degraded" || worst === "error") return { key: "verdict.degraded", tone: "fault" };
  if (level === "attention" || worst === "warning") return { key: "verdict.attention", tone: "warn" };
  if (level === "healthy") return { key: "verdict.healthy", tone: "ok" };
  return { key: "verdict.unknown", tone: "warn" };
}

/** The worst severity per page, for the marks beside the menu entries. */
export function attentionBySection(status, extra = []) {
  const marks = {};
  rankedFindings(status, extra).forEach((item) => {
    if (!item.section) return;
    if (!marks[item.section] || rankSeverity(item.severity) < rankSeverity(marks[item.section])) marks[item.section] = item.severity;
  });
  return marks;
}

/** The page a finding sends the reader to, when going there changes the page. */
export function findingTarget(finding, currentPage, pageIds) {
  const section = finding && finding.section;
  if (!section || section === currentPage || !pageIds.includes(section)) return null;
  return section;
}

/**
 * The words for one finding: a known code in the reader's language, else the
 * server's sentences marked as English.
 */
export function findingWords(finding, status) {
  const known = FINDINGS[finding.code];
  if (known) return { known: true, ...known(status, finding) };
  const english = (text) => (text ? { text: String(text), lang: "en" } : null);
  return { known: false, title: english(finding.title || finding.code), message: english(finding.message), next: english(finding.next_step) };
}
