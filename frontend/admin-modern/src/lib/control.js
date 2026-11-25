// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What the settings allow EMS to do to the inverters: the control state from
 * the Maintenance config summary (admin/maintenance_config.py), which takes
 * control on or off as the EMS runs it. It states what the settings permit,
 * never that EMS is running, and an unproven state reads as unknown, never as
 * healthy. The total output is the operating value the EMS runs below its
 * limit (the config answer's ``operating``). tests/control.test.js keeps the
 * statuses and the transports in step with the server's.
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

const finiteNumber = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);
const mapping = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});

export function controlView(control, operating = null) {
  const source = control && typeof control === "object" && !Array.isArray(control) ? control : null;
  const status = source && Object.hasOwn(CONTROL_STATUSES, source.status) ? source.status : "unknown";
  const view = { status, tone: CONTROL_STATUSES[status], transports: [], envelope: {}, notes: [] };
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
  const limit = finiteNumber(envelope.total_output_w);
  const running = finiteNumber(mapping(operating)["system.max_total_power"]);
  view.envelope = {
    totalW: running !== null && limit !== null ? Math.min(running, limit) : running ?? limit,
    totalLimitW: limit,
    deviceW: finiteNumber(envelope.device_output_w),
    socMin: finiteNumber(envelope.soc_min),
    socMax: finiteNumber(envelope.soc_max),
    socUniform: envelope.soc_uniform !== false,
  };
  const placeholders = Array.isArray(source.placeholder_fields) ? source.placeholder_fields.filter((field) => typeof field === "string" && field) : [];
  if (placeholders.length) view.notes.push({ key: "control.note.placeholders", vars: { fields: placeholders.join(", ") } });
  if (view.transports.some((entry) => entry.state === "writing")) view.notes.push({ key: "control.note.singleController" });
  if (source.state_reconciliation === true) view.notes.push({ key: "control.note.stateReconciliation" });
  if (Object.values(mapping(mapping(operating).devices)).some((entry) => mapping(entry).control === false)) view.notes.push({ key: "control.note.devicesOff" });
  view.notes.push({ key: "control.note.provenance" });
  return view;
}
