// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The one place the overall system status is decided.
 *
 * The order matters: missing or stale data outranks everything, because a
 * status derived from old figures would claim "all normal" about a system
 * nobody is currently looking at.
 */

import { fleetSummary, normalizedDevices } from "./devices.js";

export const STALE_FLOOR_SECONDS = 30;

/** Classic's rule, timed by the browser clock so a skewed EMS clock cannot keep a dead feed live. */
export function staleAfterMs(loopIntervalSeconds) {
  const loop = Number(loopIntervalSeconds) || 5;
  return Math.max(STALE_FLOOR_SECONDS, loop * 3) * 1000;
}

export function deriveSystemStatus({ snapshot, changedAt, now, staleAfter }) {
  if (!snapshot) return { level: "waiting", key: "status.waiting", fleet: null };

  const devices = normalizedDevices(snapshot);
  const fleet = fleetSummary(devices);
  const age = changedAt ? now - changedAt : 0;

  if (changedAt && age > staleAfter) {
    return { level: "stale", key: "status.stale", ageSeconds: Math.round(age / 1000), fleet };
  }
  if (snapshot.grid_power_valid === false) {
    return { level: "fault", key: "status.meterOffline", fleet };
  }
  if (fleet.total > 0 && fleet.online === 0) {
    return { level: "fault", key: "status.allOffline", fleet };
  }
  if (snapshot.device_power_valid === false) {
    return { level: "attention", key: "status.devicePowerInvalid", fleet };
  }
  if (fleet.offline.length) {
    return { level: "attention", key: "status.devicesOffline", vars: { n: fleet.offline.length }, fleet };
  }
  if (snapshot.controller && snapshot.controller.enabled === false) {
    return { level: "paused", key: "status.paused", fleet };
  }
  return { level: "ok", key: "status.ok", fleet };
}

export const RULES = Object.freeze([
  { key: "ems_enabled", label: "rule.ems_enabled", icon: "circle-check" },
  { key: "soc_limit_active", label: "rule.soc_limit_active", icon: "gauge" },
  { key: "output_limit_active", label: "rule.output_limit_active", icon: "sliders-horizontal" },
  { key: "winter_soc_mode", label: "rule.winter_soc_mode", icon: "snowflake" },
  { key: "full_charge_assist_active", label: "rule.full_charge_assist_active", icon: "battery-full" },
  { key: "pv_priority_balancing", label: "rule.pv_priority_balancing", icon: "sun" },
  { key: "battery_balancing", label: "rule.battery_balancing", icon: "battery-medium" },
  { key: "night_min_soc_idle", label: "rule.night_min_soc_idle", icon: "moon" },
  { key: "offline_devices", label: "rule.offline_devices", icon: "wifi-off" },
]);

/**
 * A rule's reason in the dashboard's own words (dashboard/telemetry.py
 * RULE_REASONS), which has a translation here; a list of device names or any
 * other reason is shown as the dashboard gives it.
 */
export const RULE_REASONS = Object.freeze({
  soc_limit_active: "one or more devices report a SOC limit",
  output_limit_active: "device outputLimit is present",
  winter_soc_mode: "configured winter month and runtime winter toggle",
  full_charge_assist_active: "no device currently in full-charge assist",
  pv_priority_balancing: "per-device PV priority factor differs from 1.0",
  battery_balancing: "effective device target differs from allocated target",
  night_min_soc_idle: "all controllable devices are parked at the minimum output floor",
  offline_devices: "all devices online",
});

export function ruleStates(snapshot) {
  const rules = snapshot?.rules || {};
  return RULES.map((rule) => {
    const state = rules[rule.key] || {};
    const reason = typeof state.reason === "string" ? state.reason : "";
    return { ...rule, active: state.active === true, reason, reasonKey: reason && reason === RULE_REASONS[rule.key] ? `rule.reason.${rule.key}` : null };
  });
}
