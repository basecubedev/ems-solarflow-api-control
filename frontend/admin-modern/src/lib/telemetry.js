// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Zendure MQTT telemetry as the EMS reports it
 * (admin/zendure_mqtt_runtime_status.py). Read-only: no command is sent from
 * here. tests/classic-parity.test.js keeps the states and the broker issue
 * codes in step with Classic's tables.
 */

export const RUNTIME_STATES = Object.freeze({ configured: "c-ok", inactive: "c-idle", unavailable: "c-warn" });
export const STATUS_SOURCES = Object.freeze(["live_runtime", "offline_config"]);
export const BROKER_ISSUES = Object.freeze(["broker_profile_disabled", "broker_profile_incomplete", "broker_auth_missing"]);
export const DEVICE_STATES = Object.freeze({ online: "c-ok", stale: "c-warn", unseen: "c-idle", offline: "c-fault", invalid: "c-fault" });

/**
 * Whether telemetry has anything to show: the card stays unless the answer is
 * a proven "nothing configured". An answer the server could not work out
 * (``available: false``, every count zero) proves nothing.
 */
export function telemetryWanted(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return true;
  if (data.available === false) return true;
  return Number(data.configured_device_count || 0) > 0 || Number(data.invalid_device_count || 0) > 0 || Number(data.broker_count || 0) > 0 || data.broker_configured === true;
}

export function brokerView(broker) {
  const state = !broker?.enabled ? "disabled" : broker.connected ? "connected" : broker.running ? "running" : "configured";
  return {
    ref: String(broker?.broker_ref || ""),
    source: String(broker?.source || ""),
    endpoint: String(broker?.endpoint || ""),
    devices: Number(broker?.device_count || 0),
    state,
    tone: state === "connected" ? "c-ok" : state === "disabled" ? "c-idle" : "c-warn",
    issue: BROKER_ISSUES.includes(broker?.issue) ? broker.issue : null,
    lastError: broker?.last_error ? String(broker.last_error) : "",
  };
}

export function telemetryDeviceView(device) {
  const status = String(device?.status || "unseen");
  return {
    name: String(device?.name || ""),
    status,
    known: Object.hasOwn(DEVICE_STATES, status),
    tone: DEVICE_STATES[status] || "c-idle",
    ageSeconds: device?.age_seconds != null ? Number(device.age_seconds) : null,
    lastSeen: device?.last_seen || null,
    metrics: Number(device?.metric_count || 0),
    broker: String(device?.broker_ref || ""),
  };
}
