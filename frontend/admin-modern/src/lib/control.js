// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What the saved settings allow EMS to do to the inverters: the control
 * state from the Maintenance config summary (admin/maintenance_config.py).
 * It states what the config permits, never that EMS is running, and an
 * unproven state reads as unknown, never as healthy. Where the dashboard
 * switched control on or off, or changed the total limit, at runtime (the
 * config answer's ``overrides``), the view follows the value EMS follows.
 * tests/classic-parity.test.js keeps the statuses and the transports in step
 * with Classic's MAINTENANCE_CONTROL_STATUS_TEXT and transport labels.
 */

export const CONTROL_STATUSES = Object.freeze({
  may_control: "c-ok",
  safe_mode: "c-warn",
  calculating_only: "c-inv",
  simulated: "c-inv",
  disabled: "c-inv",
  not_writing: "c-warn",
  unknown: "c-warn",
});

export const CONTROL_TRANSPORTS = Object.freeze(["api", "mqtt_local", "mqtt_zendure"]);

/**
 * The server's status ladder (_control_state), most fundamental cause first;
 * tests/control.test.js holds this order to the server source. Used only to
 * place a runtime-switched "enabled" on it.
 */
export const CONTROL_LADDER = Object.freeze(["safe_mode", "disabled", "simulated", "calculating_only", "may_control", "not_writing"]);

const finiteNumber = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);
const mapping = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});

function liveEntry(overrides, path, type) {
  const entry = mapping(mapping(overrides)[path]);
  return entry.source === "dashboard_override" && typeof entry.effective_value === type ? entry.effective_value : null;
}

function statusWith(source, enabled) {
  if (Array.isArray(source.placeholder_fields) && source.placeholder_fields.length) return "safe_mode";
  if (!enabled) return "disabled";
  if (source.simulation_mode) return "simulated";
  if (source.dry_run) return "calculating_only";
  const transports = Array.isArray(source.transports) ? source.transports : [];
  return transports.some((entry) => entry?.armed && Number(entry.device_count) > 0) ? "may_control" : "not_writing";
}

export function controlView(control, overrides) {
  const source = control && typeof control === "object" && !Array.isArray(control) ? control : null;
  let status = source && Object.hasOwn(CONTROL_STATUSES, source.status) && source.status !== "unknown" ? source.status : "unknown";
  const liveEnabled = status === "unknown" ? null : liveEntry(overrides, "system.enabled", "boolean");
  if (liveEnabled !== null) status = statusWith(source, liveEnabled);
  const view = { status, tone: CONTROL_STATUSES[status], transports: [], envelope: {}, notes: [], live: false };
  if (status === "unknown") {
    view.notes.push({ key: "control.note.provenance" });
    return view;
  }
  view.transports = (Array.isArray(source.transports) ? source.transports : [])
    .filter((entry) => entry && CONTROL_TRANSPORTS.includes(entry.control_gate))
    .map((entry) => {
      const devices = Number(entry.device_count) || 0;
      const state = !entry.armed ? "blocked" : devices ? "writing" : "idle";
      return { gate: entry.control_gate, state, devices, tone: state === "writing" ? "c-ok" : "c-idle" };
    });
  const envelope = source.envelope && typeof source.envelope === "object" ? source.envelope : {};
  const liveTotal = liveEntry(overrides, "system.max_total_power", "number");
  view.live = liveEnabled !== null || liveTotal !== null;
  view.envelope = {
    totalW: liveTotal !== null ? liveTotal : finiteNumber(envelope.total_output_w),
    deviceW: finiteNumber(envelope.device_output_w),
    socMin: finiteNumber(envelope.soc_min),
    socMax: finiteNumber(envelope.soc_max),
    socUniform: envelope.soc_uniform !== false,
  };
  const placeholders = Array.isArray(source.placeholder_fields) ? source.placeholder_fields.filter((field) => typeof field === "string" && field) : [];
  if (placeholders.length) view.notes.push({ key: "control.note.placeholders", vars: { fields: placeholders.join(", ") } });
  if (view.transports.some((entry) => entry.state === "writing")) view.notes.push({ key: "control.note.singleController" });
  if (source.state_reconciliation === true) view.notes.push({ key: "control.note.stateReconciliation" });
  const devices = Object.values(mapping(mapping(overrides).devices));
  if (devices.some((entry) => mapping(mapping(entry).enabled).source === "dashboard_override")) view.notes.push({ key: "control.note.devicesLive" });
  view.notes.push({ key: view.live ? "control.note.live" : "control.note.provenance" });
  return view;
}
