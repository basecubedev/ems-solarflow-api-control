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
