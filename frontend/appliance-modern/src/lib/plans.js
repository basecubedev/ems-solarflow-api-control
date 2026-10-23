// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The plans the pages ask for, one builder each where more than one page asks
 * for the same one. The bodies are exactly what Classic sends to the same
 * endpoints (appliance/static/app.js); only names cross the wire.
 */

/** The channels an EMS Admin install can name instead of an exact release. */
export const INSTALL_CHANNELS = Object.freeze(["latest_stable", "current", "previous_known_good", "exact"]);

export function adminInstallPlan(channel, tag, reinstall, key = "install-plan") {
  const body = { channel, reinstall: reinstall === true };
  if (channel === "exact") body.tag = tag;
  return {
    key,
    endpoint: "/api/admin/plan-install",
    body,
    title: tag ? { key: "plan.title.adminInstallTag", vars: { tag } } : { key: "plan.title.adminInstall" },
    confirm: "plan.install",
  };
}

export function securityUpdatesPlan(key = "security-updates") {
  return { key, endpoint: "/api/updates/plan", body: { scope: "security" }, title: { key: "plan.title.securityUpdates" }, confirm: "plan.install" };
}

export function allUpdatesPlan() {
  return { key: "all-updates", endpoint: "/api/updates/plan", body: { scope: "all" }, title: { key: "plan.title.allUpdates" }, confirm: "plan.install" };
}

/** The package-manager repairs; an active lock is never removed. */
export const REPAIR_ACTIONS = Object.freeze(["configure_pending", "fix_broken", "refresh_index"]);

export function repairPlan(action) {
  return { key: `repair-${action}`, endpoint: "/api/updates/repair", body: { action }, title: { key: `repair.${action}` }, confirm: "plan.runRepair" };
}

export const LIFECYCLE_ACTIONS = Object.freeze(["start", "stop", "restart"]);
const LIFECYCLE_ENDPOINTS = Object.freeze({ start: "/api/admin/start", stop: "/api/admin/stop", restart: "/api/admin/restart" });

export function lifecyclePlan(action, key = `admin-${action}`) {
  return { key, endpoint: LIFECYCLE_ENDPOINTS[action], title: { key: `plan.title.admin.${action}` } };
}

export function adminRepairPlan(key = "admin-repair") {
  return { key, endpoint: "/api/admin/repair", title: { key: "plan.title.adminRepair" } };
}

export function adminRollbackPlan() {
  return { key: "admin-rollback", endpoint: "/api/admin/rollback", title: { key: "plan.title.adminRollback" }, confirm: "plan.rollBack" };
}

export function managerInstallPlan(entry, risky, key = "manager-plan-update") {
  return {
    key,
    endpoint: "/api/manager/plan-update",
    body: { release_id: entry.release_id },
    title: { key: "plan.title.managerInstall", vars: { release: entry.release_id } },
    confirm: "plan.install",
    danger: Boolean(risky),
  };
}

export function managerRevertPlan(version) {
  return { key: "manager-plan-revert", endpoint: "/api/manager/plan-revert", body: {}, title: { key: "plan.title.managerRevert", vars: { version } }, confirm: "plan.reinstall", danger: true };
}

const POWER_ENDPOINTS = Object.freeze({ reboot: "/api/system/reboot", shutdown: "/api/system/shutdown" });

export function powerPlan(action) {
  return {
    key: `power-${action}`,
    endpoint: POWER_ENDPOINTS[action],
    title: { key: `plan.title.${action}` },
    confirm: action === "reboot" ? "plan.restart" : "plan.shutDown",
    danger: true,
  };
}
