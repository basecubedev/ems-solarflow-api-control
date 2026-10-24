// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The runtime editor's contract, ported from Classic: only changed fields are
 * sent, numbers are sent as numbers, and the confirmations Classic asks before
 * a change that stops or redirects control are asked here too. The backend
 * validates every value; nothing here decides whether a write is allowed.
 */

export const SYSTEM_ENDPOINT = "/api/runtime/system";
export const WINTER_ENDPOINT = "/api/runtime/winter";
export const AC_CHARGE_ENDPOINT = "/api/runtime/ac_charge_control";
export const DEVICE_PREFIX = "/api/runtime/device/";
const AC_INPUT_ROLES = new Set(["ac_input", "ac_input_charge", "reserved"]);

export function deviceEndpoint(name) {
  return `${DEVICE_PREFIX}${encodeURIComponent(name)}`;
}

export function endpointDevice(endpoint) {
  return endpoint.startsWith(DEVICE_PREFIX) ? decodeURIComponent(endpoint.slice(DEVICE_PREFIX.length)) : "";
}

export function acRole(device) {
  return AC_INPUT_ROLES.has(String(device?.runtime_role || "")) ? "ac_input" : "ac_output";
}

export function settingWord(value) {
  return value === undefined || value === null ? "unmanaged" : String(value).trim().toLowerCase();
}

/** The settings a device form shows: the ones the device offers, plus any it no longer offers but still has a value for. */
export function deviceSettings(device, catalogue, offered) {
  const has = new Set(offered || []);
  return (catalogue || [])
    .filter((setting) => has.has(setting.key) || (device?.[setting.key] ?? null) !== null)
    .map((setting) => ({ ...setting, offered: has.has(setting.key) }));
}

export function normalizeValue(field, value) {
  if (field.type === "toggle") return Boolean(value);
  if (field.type === "number") {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }
  return value === null || value === undefined ? "" : String(value);
}

/**
 * The values a card shows after /api/runtime answered. A field the operator
 * has edited keeps the edit and the value it was edited from; every other
 * field takes the stored value. ``fresh`` re-seeds every field, after a save.
 */
export function reseed(fields, { values = {}, baseline = {}, initial = {}, fresh = false }) {
  const next = { ...values };
  const base = { ...baseline };
  fields.forEach((field) => {
    const edited = !fresh && normalizeValue(field, values[field.name]) !== normalizeValue(field, baseline[field.name]);
    if (edited) return;
    next[field.name] = field.type === "number" ? initial[field.name] ?? "" : initial[field.name];
    base[field.name] = initial[field.name];
  });
  return { values: next, baseline: base };
}

/** Classic's changedRuntimePayload: a field that equals its initial value is not sent; an empty number is not sent. */
export function changedPayload(fields, values, initial) {
  const payload = {};
  fields.forEach((field) => {
    if (field.type === "readonly") return;
    const current = normalizeValue(field, values[field.name]);
    const before = normalizeValue(field, initial[field.name]);
    if (current === before) return;
    if (field.type === "number" && current === null) return;
    payload[field.name] = current;
  });
  return payload;
}

function settingChange(payload, runtime) {
  const settings = (runtime?._limits?.device_settings || []).filter((setting) => payload[setting.key] !== undefined);
  if (!settings.length) return null;
  return {
    kept: settings.filter((setting) => payload[setting.key] !== "unmanaged").map((setting) => setting.key),
    handed: settings.filter((setting) => payload[setting.key] === "unmanaged").map((setting) => setting.key),
  };
}

function settingsKept(device, payload, runtime) {
  const stored = runtime?.devices?.[device] || {};
  const offered = new Set(runtime?._limits?.device_settings_offered?.[device] || []);
  return (runtime?._limits?.device_settings || [])
    .filter((setting) => payload[setting.key] === undefined
      && offered.has(setting.key)
      && setting.values.some((value) => value.value === settingWord(stored[setting.key])))
    .map((setting) => setting.key);
}

function offNote(device, payload, runtime) {
  const emsOff = runtime?.system?.enabled === false;
  const deviceOff = runtime?.devices?.[device]?.enabled === false && payload.enabled !== true;
  if (emsOff && deviceOff) return { key: "runtime.warn.bothOff", vars: { device } };
  if (emsOff) return { key: "runtime.warn.emsOff" };
  if (deviceOff) return { key: "runtime.warn.deviceOutside", vars: { device } };
  return null;
}

/**
 * The confirmation a change needs, as message items, or null. Setting keys are
 * passed as a list so the caller can name them in the reader's language.
 */
export function changeWarning(endpoint, payload, runtime) {
  if (endpoint === SYSTEM_ENDPOINT && payload.enabled === false) {
    const kept = Object.entries(runtime?.devices || {}).some(
      ([name, entry]) => entry?.enabled !== false && settingsKept(name, {}, runtime).length,
    );
    return [{ key: "runtime.warn.systemOff" }, kept ? { key: "runtime.warn.systemOffKept" } : null].filter(Boolean);
  }
  const device = endpointDevice(endpoint);
  if (!device) return null;
  const change = settingChange(payload, runtime);
  const notes = payload.auto_heat === "off" ? [{ key: "runtime.warn.autoHeatOff" }] : [];
  if (payload.enabled === false) {
    const left = settingsKept(device, payload, runtime);
    return [
      { key: "runtime.warn.deviceOff", vars: { device } },
      change?.kept.length ? { key: "runtime.warn.settingsSavedForLater", settings: change.kept, vars: { device } } : null,
      left.length ? { key: "runtime.warn.settingsLeft", settings: left, vars: { device } } : null,
      ...notes,
      change?.handed.length ? { key: "runtime.warn.settingsHanded", settings: change.handed } : null,
    ].filter(Boolean);
  }
  const off = offNote(device, payload, runtime);
  let opening = null;
  if (payload.runtime_role === "ac_input") opening = { key: "runtime.warn.acInput", vars: { device } };
  else if (payload.runtime_role === "ac_output" && off) opening = { key: "runtime.warn.acOutput", vars: { device } };
  else if (payload.offgrid_socket_mode !== undefined) opening = { key: "runtime.warn.offgrid", vars: { device } };
  if (!opening && !change) return null;
  const kept = change?.kept || [];
  const handed = change?.handed || [];
  return [
    opening || { key: "runtime.warn.settingsQuestion", settings: [...kept, ...handed], vars: { device } },
    kept.length ? { key: "runtime.warn.settingsKept", settings: kept } : null,
    ...notes,
    opening || kept.length ? off : null,
    payload.runtime_role === "ac_output" && off ? { key: "runtime.warn.stopChargeInApp" } : null,
    handed.length ? { key: "runtime.warn.settingsHanded", settings: handed } : null,
  ].filter(Boolean);
}
