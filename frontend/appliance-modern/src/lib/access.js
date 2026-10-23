// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * SSH, the console rescue account and the read-only backup export. The console
 * reports which state the appliance is in; it never demands a change, and it
 * never renders a state it could not read as one it could.
 */

/** The rescue account's state, judged in the order that matters: a network login first. */
export function rescueState(rescue) {
  const value = rescue || {};
  if (!value.present) return { key: "rescue.absent", tone: "c-warn" };
  if ((value.ssh || {}).state === "accepted") return { key: "rescue.network", tone: "c-fault" };
  if (value.unreadable) return { key: "rescue.unknown", tone: "c-idle" };
  if (value.password_set === false) return { key: "rescue.noPassword", tone: "c-warn" };
  if (value.password_is_default === null || value.password_is_default === undefined) return { key: "rescue.unknown", tone: "c-idle" };
  if (value.password_is_default) return { key: "rescue.shipped", tone: "c-warn" };
  return { key: "rescue.changed", tone: "c-ok" };
}

/** What the running daemon answered about the rescue account's password -- not what the package wrote. */
export const RESCUE_SSH_STATES = Object.freeze(["refused", "accepted", "unknown", "absent"]);

export function rescueSsh(ssh) {
  const state = (ssh || {}).state;
  return RESCUE_SSH_STATES.includes(state) ? state : "unknown";
}

/** What the export setup reports as a whole (appliance/backup_access.py STATUS_*). */
export const EXPORT_ACCESS_STATUSES = Object.freeze(["configured", "pending", "degraded", "unknown"]);

/** A mount is a read-only export only when the kernel says it publishes the configured EMS directory. */
export const EXPORT_STATES = Object.freeze(["mounted", "foreign", "writable", "not_mounted", "missing"]);

export function exportPathTone(item) {
  if (item.state === "mounted") return "c-ok";
  return item.exists ? "c-fault" : "c-warn";
}

export function exportTone(status) {
  if (status === "configured") return "c-ok";
  if (status === "failed" || status === "unavailable" || status === "degraded") return "c-fault";
  return "c-warn";
}

/** Which promised restrictions sshd does not apply, named one by one, or that it could not be asked. */
export function confinementGaps(backup) {
  const confinement = (backup || {}).confinement || {};
  if (confinement.available === false) return { unverified: true, violations: [] };
  return { unverified: false, violations: confinement.violations || [] };
}

/** What the export setup last reported, when that is not "configured": a refused source is only visible here. */
export function exportSetupReport(backup) {
  const reported = (((backup || {}).export_access || {}).reported) || {};
  if (!reported.status || reported.status === "configured") return null;
  return { status: reported.status, detail: reported.detail || "" };
}

/**
 * The root-capable shell account: two gates, and the owner has to see both --
 * an enabled account without a key admits nobody, and a key on a disabled one
 * opens nothing.
 */
export function shellAccess(ssh) {
  const state = (ssh || {}).shell_access || {};
  const enabled = state.enabled === true;
  return {
    account: state.account || "",
    enabled,
    confirmedOff: !enabled && state.effectively_disabled === true,
    daemonAccepts: state.daemon === "accepted",
    keysAllowed: state.key_deployment_allowed !== false,
    homeBroken: state.home_writable === false,
  };
}
