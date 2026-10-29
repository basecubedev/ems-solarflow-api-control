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

/** Classic's per-device decision helpers, in Classic's fallback order. */
export function rawTarget(device) {
  return first(device.raw_target_w, device.raw_allocation_w);
}

export function deviceWeight(device) {
  return first(device.effective_weight, device.pv_weight, device.capacity_weight);
}

export function baseWeight(device) {
  const explicit = first(device.base_weight, device.base_pv_weight);
  if (has(explicit)) return explicit;
  const pvWeight = numeric(device.pv_weight);
  if (pvWeight !== null) {
    const denominator = (numeric(device.pv_priority_factor) || 1) * (numeric(device.charge_balance_multiplier) || 1);
    if (denominator > 0) return pvWeight / denominator;
  }
  return first(device.capacity_weight, device.pv_only_limit_w);
}

export function totalWeight(entries) {
  const total = entries.reduce((sum, [, device]) => sum + (numeric(deviceWeight(device)) || 0), 0);
  return total > 0 ? total : null;
}

export function deviceShare(device, explain, weights) {
  const explicit = numeric(first(device.share, device.share_percent));
  if (explicit !== null) return explicit > 1 ? explicit / 100 : explicit;
  const raw = numeric(rawTarget(device));
  const requested = numeric(explain?.requested_total_w);
  if (raw !== null && requested && requested > 0) return raw / requested;
  const weight = numeric(deviceWeight(device));
  return weight !== null && weights ? weight / weights : null;
}

export function adjustedTarget(device) {
  return first(device.adjusted_target_w, device.effective_target_w, device.allocated_target_w, finalTarget(device));
}

export function adjustmentDelta(device) {
  const explicit = first(device.adjustment_delta_w, device.adjustment_delta);
  if (has(explicit)) return explicit;
  const raw = numeric(rawTarget(device));
  const final = numeric(finalTarget(device));
  return raw === null || final === null ? null : final - raw;
}

export function outputDelta(device) {
  const final = numeric(finalTarget(device));
  const output = numeric(device.output_w);
  return final === null || output === null ? null : final - output;
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

/**
 * The reasons the EMS gives its limits (ems/target_control.py add_limit,
 * ems/controller.py ControlLimitExplanation), keyed by their English, as
 * RULE_REASONS keys the dashboard's own; tests/test_dashboard_modern.py walks
 * both sources against this table.
 */
export const LIMIT_REASONS = Object.freeze({
  "requested total was clamped to the configured maximum": "maxTotalClamped",
  "configured maximum total output": "maxTotal",
  "requested total was clamped to zero": "zeroClamped",
  "target calculation never requests negative output": "zeroFloor",
  "total PV can cover the requested output": "pvCovers",
  "battery discharge is needed beyond PV output": "dischargeBeyondPv",
  "devices that cannot store their own PV export first": "fullSocFirst",
  "PV-only contribution could not cover the target": "pvOnlyShort",
  "PV-only allocation covered the target": "pvOnlyCovered",
  "battery discharge topped up PV-first allocation": "topupUsed",
  "battery top-up was not needed or not possible": "topupNotNeeded",
  "no PV-only contribution was available": "noPvOnly",
  "battery discharge topped up zero PV-only allocation": "topupFromZero",
  "battery top-up was not possible": "topupImpossible",
  "requested target exceeds exportable PV": "exceedsPv",
  "exportable PV covers the requested target": "pvCoversTarget",
  "no usable discharge capacity was available": "noDischarge",
  "final constraints preserve PV-only target limits": "pvOnlyFinal",
  "one or more device targets were clamped": "deviceClamped",
  "device targets fit within capability limits": "deviceFits",
  "some target could not be distributed to devices": "undistributed",
  "requested target was distributed within device limits": "distributed",
  "no device states were available": "noDevices",
  "battery/SOC protection idle mode is active": "nightIdle",
  "devices are parked at the configured minimum output floor": "parkedAtFloor",
});

/** The notes the EMS adds to its explanation, keyed by their English; the one with a number is matched by UNDISTRIBUTED_NOTE. */
export const EXPLAIN_NOTES = Object.freeze({
  "no devices available for target calculation": "noDevices",
  "battery top-up was applied after PV-first allocation": "topupApplied",
  "Battery/SOC protection idle mode is active; normal output allocation is intentionally bypassed.": "nightIdle",
});

export const UNDISTRIBUTED_NOTE = /^(-?\d+(?:\.\d+)?) W could not be distributed$/;

/** A note in the reader's words, with the lang of what stays the server's own. */
export function noteText(note, t, f) {
  const known = EXPLAIN_NOTES[note];
  if (known) return { text: t(`note.${known}`) };
  const match = UNDISTRIBUTED_NOTE.exec(String(note));
  if (match) return { text: t("note.undistributed", { w: f.power(Number(match[1])) }) };
  return { text: String(note), lang: "en" };
}

/**
 * A limit's value in words, or null when it says nothing: watts as power, a
 * per-device list against the devices' names in the controller's order
 * (``device_order``; the devices mapping arrives sorted by name).
 */
export function limitValueText(limit, explain, f) {
  const value = limit?.value;
  const names = Array.isArray(explain?.device_order) ? explain.device_order : [];
  if (typeof value === "number") return Number.isFinite(value) && value !== 0 ? f.power(value) : null;
  if (typeof value !== "string" || !value.trim()) return null;
  let list;
  try {
    list = JSON.parse(value);
  } catch {
    return value;
  }
  if (!Array.isArray(list)) return value;
  if (!list.length) return null;
  if (limit.name === "full_soc_pv_priority") return list.map((index) => names[index] ?? `#${index + 1}`).join(", ");
  return list.map((watts, index) => `${names[index] ?? `#${index + 1}`} ${f.power(watts)}`).join(" / ");
}

export function activeLimits(explain) {
  return (Array.isArray(explain?.limits) ? explain.limits : []).filter((limit) => limit && limit.active);
}

/** Classic's "Inactive gates": the limits the EMS checked and did not apply this cycle. */
export function inactiveLimits(explain) {
  return (Array.isArray(explain?.limits) ? explain.limits : []).filter((limit) => limit && !limit.active && has(limit.name));
}
