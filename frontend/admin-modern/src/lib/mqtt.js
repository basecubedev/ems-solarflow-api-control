// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The codes the built-in broker, the MQTT mapping and the live topics answer
 * with. Each list is paired with the server's own (tests/test_admin_modern_mqtt.py),
 * and every entry has a translation (tests/strings.test.js).
 */

export const BUILTIN_BROKER = "ems-mqtt";

export const BROKER_ISSUES = Object.freeze([
  "mqtt_broker_settings_invalid",
  "mqtt_broker_port_invalid",
  "mqtt_broker_port_in_use",
  "mqtt_broker_no_access",
  "mqtt_broker_account_name_invalid",
  "mqtt_broker_account_duplicate",
  "mqtt_broker_account_reserved",
  "mqtt_broker_account_unknown",
  "mqtt_broker_account_password_missing",
  "mqtt_broker_account_password_invalid",
  "mqtt_broker_topic_prefix_invalid",
  "mqtt_broker_grid_meter_topic_unprotectable",
  "mqtt_broker_profile_name_reserved",
]);

export const MAPPING_ISSUES = Object.freeze([
  "mqtt_mapping_document_invalid",
  "mqtt_mapping_schema_unsupported",
  "mqtt_mapping_limit_exceeded",
  "mqtt_mapping_broker_invalid",
  "mqtt_mapping_device_id_invalid",
  "mqtt_mapping_device_id_duplicate",
  "mqtt_mapping_target_unknown",
  "mqtt_mapping_target_required_missing",
  "mqtt_mapping_battery_shape_invalid",
  "mqtt_mapping_topic_invalid",
  "mqtt_mapping_topic_native",
  "mqtt_mapping_path_invalid",
  "mqtt_mapping_option_invalid",
  "mqtt_mapping_source_shared",
  "mqtt_mapping_brokers_unreadable",
]);

export const LIVE_LABELS = Object.freeze([
  "shelly_gen1",
  "shelly_gen2",
  "tasmota",
  "opendtu",
  "ahoydtu",
  "zendure_native",
  "zendure_meter_native",
  "ems_output",
  "unknown",
]);

export const SILENCE_REASONS = Object.freeze(["disabled", "source_missing", "stale", "source_unreadable", "catalog_refused"]);

export const VALUE_REASONS = Object.freeze(["format_invalid", "not_json", "path_invalid", "path_missing", "not_a_number", "out_of_range"]);

export const TARGETS = Object.freeze(["inverterPower", "solarPower", "batteryPower", "batterySoc"]);

export const BROKER_STATES = Object.freeze(["off", "running", "stopped", "unknown"]);

/** off, running, stopped (switched on, container not running) or unknown, from the server's view. */
export function brokerState(view) {
  if (!view || !view.settings) return "unknown";
  if (!view.settings.enabled) return "off";
  return view.running ? "running" : "stopped";
}

/** A source as the mapping file stores it, with every default the server would fill in. */
export function newSource(topic, path) {
  return path == null || path === ""
    ? { topic, format: "number", factor: 1, invert: false, negative: "keep", max_age_seconds: 60 }
    : { topic, format: "json", path, factor: 1, invert: false, negative: "keep", max_age_seconds: 60 };
}

/** A new mapped device on ``brokerRef`` with an empty inverter power target. */
export function newDevice(brokerRef) {
  return { id: "", label: "", enabled: true, broker_ref: brokerRef || BUILTIN_BROKER, values: { inverterPower: { sources: [] } } };
}

/** The sources of one target, whichever shape it has, as [side, index, source]. */
export function targetSources(target) {
  if (!target) return [];
  if (Array.isArray(target.sources)) return target.sources.map((source, index) => ["sources", index, source]);
  return [
    ...(target.charge || []).map((source, index) => ["charge", index, source]),
    ...(target.discharge || []).map((source, index) => ["discharge", index, source]),
  ];
}

/** A device id proposed from a live-topic group key: what the catalog accepts, at most 64 characters. */
export function proposedId(key) {
  return String(key || "").replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
}
