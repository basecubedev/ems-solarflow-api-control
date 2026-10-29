// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * One normalized view of a device from the live snapshot.
 *
 * A field the device did not report stays null. A tile that shows it renders
 * a dash, so "not reported" can never be read as "0 W".
 */

import { finite } from "../../../shared/lib/format.js";

function deviceName(device, index) {
  return String(device?.device || device?.name || `Device ${index + 1}`);
}

export function deviceEntries(snapshot) {
  const devices = snapshot?.devices;
  if (Array.isArray(devices)) return devices.map((device, index) => [deviceName(device, index), device || {}]);
  return Object.entries(devices || {}).map(([name, device], index) => [name || deviceName(device, index), device || {}]);
}

export function normalizeDevice(name, raw) {
  const device = raw || {};
  const online = device.online === true;
  const enabled = device.enabled !== false;
  const socReported = device.soc_reported !== false && finite(device.soc ?? device.soc_percent) !== null;
  const soc = socReported ? Math.max(0, Math.min(100, finite(device.soc ?? device.soc_percent))) : null;
  const packs = finite(device.pack_num);
  const inputLimit = finite(device.input_limit_w);
  return {
    name,
    online,
    enabled,
    readOnly: device.read_only === true,
    socReported,
    soc,
    minSoc: finite(device.min_soc),
    maxSoc: finite(device.max_soc),
    pvW: finite(device.pv_input_w ?? device.pv_power_w),
    pvInputsW: Array.isArray(device.pv_inputs_w) ? device.pv_inputs_w.map(finite) : [],
    outputW: finite(device.output_w ?? device.inverter_output_w),
    batteryW: finite(device.battery_power_w),
    chargeW: finite(device.ac_charge_w),
    gridW: finite(device.grid_power_w),
    targetW: finite(device.target_w),
    limitW: finite(device.output_limit_w),
    limitApplicable: device.output_limit_applicable !== false,
    firmwareApplicable: device.firmware_status_applicable !== false,
    packs: packs !== null && packs > 0 ? packs : null,
    inputLimitW: inputLimit !== null && inputLimit > 0 ? inputLimit : null,
    capability: device.capability || null,
    settings: device.settings || null,
    fullChargeAssist: device.battery_full_charge_assist || null,
    raw: device,
  };
}

export function normalizedDevices(snapshot) {
  return deviceEntries(snapshot).map(([name, raw]) => normalizeDevice(name, raw));
}

/**
 * Firmware enums as label keys. The tables mirror Classic's label functions
 * (app.js), and a pairing test walks both; an unknown value keeps its raw
 * number, as Classic does, instead of collapsing to a bare "unknown".
 */
export const FIRMWARE_ENUMS = Object.freeze({
  soc_limit: { 0: "fw.socLimit.normal", 1: "fw.socLimit.max", 2: "fw.socLimit.min" },
  pack_state: { 0: "fw.pack.standby", 1: "fw.pack.charging", 2: "fw.pack.discharging" },
  ac_mode: { 1: "fw.acMode.input", 2: "fw.acMode.output" },
  ac_status: { 0: "fw.acStatus.standby", 1: "fw.acStatus.output", 2: "fw.acStatus.charge" },
  dc_status: { 0: "fw.dc.standby", 1: "fw.dc.input", 2: "fw.dc.output" },
  grid_state: { 0: "fw.grid.disconnected", 1: "fw.grid.connected" },
  soc_status: { 0: "fw.socStatus.none", 1: "fw.socStatus.running" },
});

function enumLabel(field, value) {
  if (value === undefined || value === null || value === "") return { key: "fw.unknownValue", vars: { value: "?" } };
  const table = FIRMWARE_ENUMS[field];
  const numeric = Number(value);
  if (Number.isFinite(numeric) && Object.prototype.hasOwnProperty.call(table, numeric)) {
    return { key: table[numeric] };
  }
  return { key: "fw.unknownValue", vars: { value: String(value) } };
}

/** acStatus decides "active"; acMode only names the standby direction, as on the Classic card. */
export function acPath(raw) {
  const acMode = Number(raw?.ac_mode ?? 0);
  const acStatus = Number(raw?.ac_status ?? 0);
  if (acStatus === 2) return { key: "fw.acPath.charging" };
  if (acStatus === 1) return { key: "fw.acPath.output" };
  if (acStatus === 0) {
    if (acMode === 1) return { key: "fw.acPath.chargeStandby" };
    if (acMode === 2) return { key: "fw.acPath.outputStandby" };
    return { key: "fw.acPath.standby" };
  }
  return enumLabel("ac_status", raw?.ac_status);
}

export function firmwareFacts(raw) {
  const facts = [
    { label: "fw.label.acPath", value: acPath(raw) },
    { label: "fw.label.socGuard", value: enumLabel("soc_limit", raw?.soc_limit) },
    { label: "fw.label.batteryState", value: enumLabel("pack_state", raw?.pack_state) },
    { label: "fw.label.dcPath", value: enumLabel("dc_status", raw?.dc_status) },
    { label: "fw.label.grid", value: enumLabel("grid_state", raw?.grid_state) },
  ];
  if (raw?.soc_status !== undefined && raw?.soc_status !== null) {
    facts.push({ label: "fw.label.socCalibration", value: enumLabel("soc_status", raw.soc_status) });
  }
  return facts;
}

/** Devices the regulation counts: a disabled device is neither online nor missing. */
export function fleetSummary(devices) {
  const enabled = devices.filter((device) => device.enabled);
  const online = enabled.filter((device) => device.online);
  return {
    total: enabled.length,
    online: online.length,
    offline: enabled.filter((device) => !device.online).map((device) => device.name),
    disabled: devices.filter((device) => !device.enabled).map((device) => device.name),
  };
}

export const ASSIST_STATUSES = Object.freeze(["active", "waiting", "unavailable", "window", "restore_pending", "overdue", "completed", "ok", "unknown"]);
const ASSIST_DESCRIPTIONS = Object.freeze({ waiting: "assist.desc.waiting", unavailable: "assist.desc.unavailable", window: "assist.desc.window", overdue: "assist.desc.overdue" });

/**
 * The full-charge assist as Classic's device card shows it
 * (renderFullChargeAssist): the facts each status names, what it means, and
 * the restore facts once a restore is pending. A restore flag while the
 * assist still charges is a plan for afterwards, not a restore problem.
 */
export function assistView(assist) {
  const status = ASSIST_STATUSES.includes(assist?.status) ? assist.status : "unknown";
  const acCharging = Number(assist?.ac_mode) === 1 && Number(assist?.ac_status) === 2;
  const restoring = Boolean(assist?.restore_pending || assist?.ac_mode_restore_pending);
  const days = (value) => (Number.isFinite(value) ? value : null);
  const facts = [];
  const firmware = { label: "assist.firmware", firmware: { socGuard: enumLabel("soc_limit", assist?.soc_limit), acPath: acPath(assist) } };
  if (status === "active") {
    facts.push({ label: "assist.started", time: assist.assist_started_at || null });
    if (acCharging) facts.push({ label: "assist.acCharge", key: "assist.running" });
    facts.push(firmware);
    if (restoring) facts.push({ label: "assist.afterCharge", key: "assist.restorePlanned" });
  } else if (status === "waiting") {
    facts.push({ label: "assist.started", time: assist.assist_started_at || null }, firmware);
  } else if (status === "unavailable") {
    facts.push({ label: "assist.last", time: assist.last_full_charge_at || null });
  } else if (status === "overdue") {
    const overdue = days(assist.days_until_due);
    facts.push({ label: "assist.overdueBy", days: overdue === null ? null : Math.abs(overdue) }, { label: "assist.next", time: assist.next_due_at || null });
  } else if (status === "window") {
    facts.push({ label: "assist.dueIn", days: days(assist.days_until_due) }, { label: "assist.windowStarts", time: assist.window_starts_at || null });
  } else {
    facts.push({ label: "assist.last", time: assist?.last_full_charge_at || null }, { label: "assist.next", time: assist?.next_due_at || null });
  }
  const description = [];
  if (status === "active") description.push(acCharging ? "assist.desc.activeAc" : "assist.desc.active");
  else if (ASSIST_DESCRIPTIONS[status]) description.push(ASSIST_DESCRIPTIONS[status]);
  else if (status === "restore_pending") {
    if (assist.restore_pending) {
      facts.push({ label: "assist.maxSocRestore", key: "assist.pending" });
      description.push("assist.desc.restoreMaxSoc");
    }
    if (assist.ac_mode_restore_pending) {
      facts.push({ label: "assist.acOutputMode", key: "assist.restorePendingValue" });
      description.push("assist.desc.restoreAcMode");
    }
    if (restoring) description.push("assist.desc.restoreWaits");
  }
  return { status, facts, description };
}
