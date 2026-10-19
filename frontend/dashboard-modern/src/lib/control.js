// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Reading the EMS control explanation. The EMS decides; this only reads what
 * it reported. Codes the EMS writes are paired with Classic's
 * CONTROL_CODE_LABELS and CONTROL_DECISION_SENTENCES, and an unknown code
 * still falls back to its words, so a newer EMS stays legible.
 */

export const CONTROL_CODES = Object.freeze([
  "pv_first", "battery_discharge", "night_min_soc_idle", "ac_charge", "no_devices", "max_total_power", "zero_floor",
  "pv_surplus_available", "full_soc_pv_priority", "pv_only_target_limit", "battery_topup_after_pv_first",
  "battery_discharge_required", "discharge_capacity", "pv_only_final_limits", "device_output_limits",
  "undistributed_target", "min_output_limit", "cannot_export", "no_pv_only_available", "cannot_discharge",
  "no_battery_available", "no_usable_battery", "pv_only_limit", "max_output_limit", "no_allocation", "no_battery",
  "below_min_soc", "soc_protection", "offline", "device_disabled", "control_disabled", "not_controllable",
  "runtime_role_ac_input", "runtime_role_ac_output", "runtime_role_blocked", "ac_discharge_disabled",
  "winter_solar_charge", "already_parked", "already_at_min_output_limit", "park_at_min_output_limit", "deadband",
  "output_limit_update", "normal", "charge_inhibit", "discharge_inhibit", "dc_inactive", "ac_inactive",
  "pack_standby", "fault_observed", "pv_evidence", "output_evidence", "output_limit_evidence", "ac_evidence",
]);

export const DECISION_SENTENCES = Object.freeze([
  "pv_first_allocation", "full_soc_pv_priority", "pv_first_with_battery_topup", "solar_plus_battery_discharge",
  "night_min_soc_idle", "ac_charge_allocation", "ac_charge_share_too_small", "ac_charge_no_ceiling",
  "ac_charge_not_permitted", "ac_charge_held_by_full_charge_assist",
]);

export function has(value) {
  return value !== undefined && value !== null && value !== "";
}

export function first(...values) {
  return values.find(has);
}

function numeric(value) {
  const number = Number(value);
  return has(value) && Number.isFinite(number) ? number : null;
}

/** One label item per comma-separated code: a known code by key, an unknown one by its words. */
export function codeItems(value) {
  if (!has(value)) return [];
  return String(value)
    .split(",")
    .map((code) => code.trim())
    .filter(Boolean)
    .map((code) => {
      const normalized = code.toLowerCase();
      return CONTROL_CODES.includes(normalized) ? { key: `code.${normalized}` } : { text: code.replaceAll("_", " ") };
    });
}

export function decisionItem(reason) {
  if (!has(reason)) return null;
  const normalized = String(reason).trim().toLowerCase();
  if (DECISION_SENTENCES.includes(normalized)) return { key: `decision.${normalized}` };
  return { codes: codeItems(reason) };
}

export function deviceEntries(explain) {
  const devices = explain?.devices;
  if (Array.isArray(devices)) return devices.map((device, index) => [String(device?.device || device?.name || `Device ${index + 1}`), device || {}]);
  return Object.entries(devices || {}).map(([name, device]) => [name, device || {}]);
}

export function finalTarget(device) {
  return first(device.final_target_w, device.effective_target_w, device.adjusted_target_w, device.allocated_target_w, device.raw_target_w);
}

export function finalTotal(explain) {
  return first(explain.final_target_total_w, explain.effective_target_total_w, explain.allocated_target_total_w, explain.commanded_total_w, explain.requested_total_w);
}

export function commandableTotal(explain) {
  return first(
    explain.commandable_total_w, explain.adjusted_commandable_total_w, explain.effective_command_total_w,
    explain.commanded_total_w, explain.allocated_target_total_w, explain.effective_target_total_w,
  );
}

/** Classic's controlDeviceWriteDecision: the EMS's own decision when it reports one, else the deadband rule. */
export function writeDecision(device, explain) {
  const explicit = first(device.write_decision, device.write_state, device.command_decision);
  const target = first(device.command_target_w, finalTarget(device));
  if (has(explicit)) {
    const normalized = String(explicit).toLowerCase();
    const tone = normalized.includes("send")
      ? "send"
      : normalized.includes("block") || normalized.includes("error")
        ? "blocked"
        : normalized.includes("skip")
          ? "skip"
          : "warn";
    return { tone, reason: first(device.write_reason, device.deadband_reason, device.decision_reason), target };
  }
  if (device.online === false) return { tone: "blocked", reason: "offline", target };
  const reference = numeric(first(device.deadband_reference_w, device.output_limit_w, device.output_w));
  const value = numeric(target);
  const deadband = numeric(first(device.deadband_w, explain.deadband_w));
  if (value === null) return { tone: "warn", reason: null, target: null, unavailable: true };
  if (reference !== null && deadband !== null && Math.abs(value - reference) < deadband) return { tone: "skip", reason: "deadband", target: value };
  if (reference !== null && Math.abs(value - reference) < 0.5) return { tone: "skip", reason: null, target: value, atTarget: true };
  return { tone: "send", reason: null, target: value };
}

export function globalWrite(devices, explain) {
  if (!devices.length) return "none";
  const decisions = devices.map(([, device]) => writeDecision(device, explain));
  if (decisions.some((d) => d.tone === "blocked")) return "blocked";
  if (decisions.some((d) => d.tone === "send")) return "send";
  if (decisions.every((d) => d.tone === "skip")) return "skip";
  return "warn";
}

export function activeLimits(explain) {
  return (Array.isArray(explain?.limits) ? explain.limits : []).filter((limit) => limit && limit.active);
}
