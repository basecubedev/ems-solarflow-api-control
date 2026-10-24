// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Devices, the grid meter and the top-level Zendure MQTT broker in the
 * settings draft, edited the way the Classic editor edits them
 * (admin/static/admin.js, mconfig*): the same draft keys, the same defaults
 * and the same derived control capability, so the server's preview and apply
 * see what they would see from Classic. Everything here is pure: it returns a
 * new value and never changes the one it was given.
 */

export const E3DC_DEFAULTS = Object.freeze({ port: 502, unit_id: 1 });
export const D0_TOPIC_PREFIX = "Zendure/sensor/";
export const D0_TOPIC_SUFFIX = "/totalPower";
const GRID_MQTT_PREFIX = "grid_meter.mqtt.";
/** Connection values a named broker profile owns; never edited beside the reference. */
const GRID_MQTT_CONNECTION = new Set(["grid_meter.mqtt.host", "grid_meter.mqtt.port", "grid_meter.mqtt.username", "grid_meter.mqtt.password"]);
/** name, ip and sn always write, a cleared IP included; other device values drop back to unset when emptied. */
export const IDENTITY_KEYS = Object.freeze(["name", "ip", "sn"]);

const mapping = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});
const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));

export function deviceKind(device) {
  if (device?.kind === "external_mqtt") return "external_mqtt";
  if (device?.kind === "e3dc_modbus") return "e3dc_modbus";
  if (device?.kind === "zendure_mqtt" || device?.type === "zendure_mqtt") return "zendure_mqtt";
  return "local_api";
}

/** The draft key a catalogue device path names: devices[].max_power is max_power. */
export function fieldKey(path) {
  return String(path).replace(/^devices\[\]\./, "");
}

function hardwareSection(catalog, id) {
  return (mapping(catalog).hardware_sections || []).find((section) => section?.id === id) || null;
}

/** The catalogue's device fields, split into the identity a Local API device shows and the tuning every inverter shares. */
export function deviceFields(catalog) {
  const fields = hardwareSection(catalog, "devices")?.fields || [];
  return {
    identity: fields.filter((field) => IDENTITY_KEYS.includes(fieldKey(field.path))),
    common: fields.filter((field) => !IDENTITY_KEYS.includes(fieldKey(field.path))),
  };
}

export function commonDefaults(catalog) {
  return mapping(mapping(mapping(catalog).default_device).common);
}

/** One device value changed: an emptied value other than the identity is unset, so the default applies. */
export function setDeviceValue(device, key, value) {
  const next = clone(device);
  if (!IDENTITY_KEYS.includes(key) && String(value ?? "").trim() === "") delete next[key];
  else next[key] = value;
  return next;
}

/** The next free INV_<n> name, as Guided Setup and Classic name a new inverter. */
export function nextInverterName(devices) {
  const names = (Array.isArray(devices) ? devices : []).map((device) => String(device?.name ?? "").trim());
  const used = new Set(names.map((name) => name.toLowerCase()));
  let highest = 0;
  for (const name of names) {
    const match = /^INV_([1-9][0-9]*)$/i.exec(name);
    if (match) highest = Math.max(highest, Number(match[1]));
  }
  let number = Math.max(1, highest + 1, names.length + 1);
  while (used.has(`inv_${number}`)) number += 1;
  return `INV_${number}`;
}

function withDefaults(device, catalog) {
  const defaults = commonDefaults(catalog);
  for (const key of Object.keys(defaults)) if (device[key] == null) device[key] = clone(defaults[key]);
  return device;
}

/** A new Local API inverter: the central defaults, never another device's values. */
export function newLocalInverter(devices, catalog) {
  return withDefaults({ original_name: null, name: nextInverterName(devices), ip: "", sn: "", enabled: true }, catalog);
}

/** A new read-only E3/DC, named E3DC, E3DC2 and so on. */
export function newE3dc(devices, endpoint = {}) {
  const taken = new Set((devices || []).map((device) => String(device?.name || "")));
  let name = "E3DC";
  for (let suffix = 2; taken.has(name); suffix += 1) name = `E3DC${suffix}`;
  return { kind: "e3dc_modbus", original_name: null, name, ip: endpoint.ip || "", port: endpoint.port || E3DC_DEFAULTS.port, unit_id: E3DC_DEFAULTS.unit_id, enabled: true, has_enabled_key: true };
}

export function generations(catalog) {
  const list = mapping(catalog).zendure_mqtt_generations;
  return Array.isArray(list) ? list : [];
}

export function hardwareModels(catalog) {
  const list = mapping(catalog).zendure_mqtt_hardware_models;
  return Array.isArray(list) ? list : [];
}

export function hardwareModel(catalog, id) {
  return hardwareModels(catalog).find((model) => model?.id === id) || null;
}

/** The models a generation can be: those that name it as compatible, and the unknown one. */
export function modelsForGeneration(catalog, generation) {
  return hardwareModels(catalog).filter((model) => {
    if (!model?.id) return true;
    const compatible = Array.isArray(model.compatible_generations) ? model.compatible_generations : [model.generation];
    return compatible.includes(generation);
  });
}

/** A new Zendure MQTT inverter, telemetry only until its model and write route are known. */
export function newMqttInverter(devices, catalog) {
  const list = generations(catalog);
  const preferred = list.find((generation) => generation?.default) || list[0];
  return withDefaults(
    {
      kind: "zendure_mqtt",
      original_name: null,
      name: nextInverterName(devices),
      enabled: true,
      has_enabled_key: true,
      serial_number: "",
      device_id: "",
      product_key: "",
      hardware_generation: preferred ? preferred.id : "",
      hardware_model: "",
      power_write_profile: null,
      alternative_layout: false,
      output_control: false,
      supports_output_control: false,
      control_readiness: { ready: false, reason: "hardware_profile_missing" },
      capabilities: { read_power: true, read_soc: true, write_output_limit: false },
    },
    catalog,
  );
}

/**
 * Whether output control can be offered: the device's own backend verdict for
 * its pinned model wins, otherwise the model decides. A generation never
 * authorizes a write.
 */
export function mqttControlSupported(device, model) {
  if (device?.mqtt?.write_protocol === "custom_properties_write" && device.control_readiness?.ready) return true;
  if (!model?.id || !model.control_supported) return false;
  if (device.control_readiness && device.hardware_model === model.id) {
    if (typeof device.supports_output_control === "boolean") return device.supports_output_control;
    return device.control_readiness.ready === true;
  }
  return true;
}

/** What the card says about an MQTT device's control; reads only. */
export function mqttControl(device, catalog) {
  const model = hardwareModel(catalog, device?.hardware_model);
  const supported = mqttControlSupported(device, model);
  const explicitWriteTopic = Boolean(device?.mqtt?.write_topic);
  const trustedWriteTarget = device?.trusted_write_target === true;
  const routeDeviceId = typeof device?.mqtt?.device_id === "string" ? device.mqtt.device_id.trim() : "";
  const proposalRouted = typeof device?.proposal_id === "string" && device.proposal_id.trim() !== "";
  const hasWriteTarget = Boolean(device?.product_key) || explicitWriteTopic || trustedWriteTarget;
  const routeComplete = (Boolean(routeDeviceId) || proposalRouted) && hasWriteTarget;
  const readiness = mapping(device?.control_readiness);
  let state;
  if (!supported) state = model?.id ? "telemetryOnly" : "modelRequired";
  else if (!routeDeviceId && !proposalRouted) state = "routeMissing";
  else if (readiness.reason === "write_target_missing" && !hasWriteTarget) state = "writeTargetMissing";
  else if (readiness.ready && device.hardware_model === model.id) state = "ready";
  else state = "readyAfterCheck";
  return {
    model,
    supported,
    shouldControl: supported && routeComplete,
    showProductKey: Boolean(device?.product_key) || (supported && !explicitWriteTopic && !trustedWriteTarget),
    state,
    blockReason: supported ? "write_target_missing" : String(device?.control_block_reason || (readiness.ready === true ? "" : readiness.reason) || "hardware_profile_missing"),
  };
}

/** Writes the derived capability onto the device, after an explicit change only. */
export function normalizeMqttControl(device, catalog) {
  const next = clone(device);
  const shouldControl = mqttControl(next, catalog).shouldControl;
  next.capabilities = { ...(next.capabilities || { read_power: true, read_soc: true }), write_output_limit: shouldControl };
  next.output_control = shouldControl;
  return next;
}

/** An MQTT device field changed, with what Classic changes along with it. */
export function setMqttValue(device, key, value, catalog) {
  let next = clone(device);
  if (key === "device_id") {
    next.mqtt = { ...(next.mqtt || {}), device_id: String(value).trim() };
  } else if (key === "product_key") {
    next.product_key = value;
    next.mqtt = { ...(next.mqtt || {}), product_key: value };
  } else if (key === "hardware_generation") {
    next.hardware_generation = value;
    next.alternative_layout = false;
    if (!modelsForGeneration(catalog, value).some((model) => model.id === next.hardware_model)) {
      next.hardware_model = "";
      next.power_write_profile = null;
    }
  } else if (key === "hardware_model") {
    next.hardware_model = value;
    next.power_write_profile = hardwareModel(catalog, value)?.power_write_profile ?? null;
  } else {
    next[key] = value;
    return next;
  }
  next = normalizeMqttControl(next, catalog);
  return next;
}

/** Where a configured MQTT device's broker is: its own source, else the one its broker profile resolves to. */
export function connectionSource(device) {
  if (deviceKind(device) === "e3dc_modbus") return "modbus_tcp";
  if (deviceKind(device) !== "zendure_mqtt") return deviceKind(device) === "local_api" ? "local_api" : "";
  const source = String(device?.mqtt?.source || device?.broker?.source || device?.mqtt?.effective_source || "").trim();
  if (!source) return "";
  return source === "zendure_cloud_mqtt" ? "zendure_mqtt" : "local_mqtt";
}

/** Removing a device: an installed external entry leaves only by the reference the server issued for it. */
export function removeDevice(draft, index) {
  const devices = [...(draft.devices || [])];
  const [gone] = devices.splice(index, 1);
  const removed = [...(draft.removed_external || [])];
  if (deviceKind(gone) === "external_mqtt" && gone?.entry && gone.entry_ref) removed.push(gone.entry_ref);
  return { devices, removed_external: removed };
}

export function gridVariant(catalog, type) {
  return mapping(mapping(catalog).grid_meter_variants)[type] || null;
}

export function gridIsMqtt(catalog, type) {
  return (gridVariant(catalog, type)?.fields || []).some((path) => path.startsWith(GRID_MQTT_PREFIX));
}

/** The grid meter's fields for its type, without what a named broker profile owns; a D0 topic is advanced. */
export function gridFields(catalog, meter) {
  const type = String(meter?.type || "");
  const variant = gridVariant(catalog, type);
  const section = hardwareSection(catalog, "grid_meter");
  if (!variant || !section) return [];
  const allowed = new Set(variant.fields || []);
  let fields = (section.fields || []).filter((field) => allowed.has(field.path));
  if (gridIsMqtt(catalog, type) && meter?.mqtt?.broker_ref) fields = fields.filter((field) => !GRID_MQTT_CONNECTION.has(field.path));
  if (type === "zendure_smartmeter_d0") fields = fields.map((field) => (field.path === "grid_meter.mqtt.topic" ? { ...field, level: "advanced" } : field));
  return fields;
}

export function gridValue(meter, path) {
  if (path.startsWith(GRID_MQTT_PREFIX)) return meter?.mqtt?.[path.slice(GRID_MQTT_PREFIX.length)];
  const key = path.replace("grid_meter.", "");
  if (key === "channels" && Array.isArray(meter?.channels)) return meter.channels.join(", ");
  return meter?.[key];
}

/** The operator's answer travels as given; what an emptied field means is the server's to decide. */
export function setGridValue(meter, path, value) {
  const next = clone(meter) || {};
  if (path.startsWith(GRID_MQTT_PREFIX)) next.mqtt = { ...(next.mqtt || {}), [path.slice(GRID_MQTT_PREFIX.length)]: value };
  else next[path.replace("grid_meter.", "")] = value;
  if (next.type === "zendure_smartmeter_d0" && path === "grid_meter.mqtt.topic") next.d0_topic_mode = "manual";
  return next;
}

export function setGridPort(meter, value) {
  const next = clone(meter) || {};
  if (String(value ?? "").trim() === "") delete next.port;
  else next.port = value;
  return next;
}

/**
 * Another meter type. The port follows the type unless both share a default
 * port; a port the reader set for a type comes back with it (``ports``, held
 * by the page). A D0 meter reads plain numbers unless told otherwise.
 */
export function switchGridType(meter, type, catalog, ports = {}) {
  const next = clone(meter) || {};
  const from = String(next.type || "");
  const remembered = { ...ports };
  if (next.port != null && from) remembered[from] = next.port;
  let port;
  if (Object.hasOwn(remembered, type)) port = remembered[type];
  else {
    const before = gridVariant(catalog, from)?.default_port ?? null;
    const after = gridVariant(catalog, type)?.default_port ?? null;
    port = before != null && before === after ? next.port : after;
  }
  if (port == null) delete next.port;
  else next.port = port;
  next.type = type;
  next.present = Boolean(type);
  if (type === "zendure_smartmeter_d0") next.mqtt = { payload_format: "number", ...(next.mqtt || {}) };
  return { meter: next, ports: remembered };
}

export function d0Serial(topic) {
  const text = String(topic || "").trim();
  return text.startsWith(D0_TOPIC_PREFIX) && text.endsWith(D0_TOPIC_SUFFIX) ? text.slice(D0_TOPIC_PREFIX.length, text.length - D0_TOPIC_SUFFIX.length) : "";
}

/** The D0 topic follows the serial typed for it. */
export function setD0Serial(meter, serial) {
  const next = clone(meter) || {};
  const value = String(serial || "").trim();
  const mqtt = { ...(next.mqtt || {}) };
  if (value) mqtt.topic = `${D0_TOPIC_PREFIX}${value}${D0_TOPIC_SUFFIX}`;
  else delete mqtt.topic;
  next.mqtt = mqtt;
  next.d0_topic_mode = "auto";
  return next;
}

/** A stored MQTT password is never shown: an empty field keeps it, the clear box removes it. */
export function setGridPassword(meter, { password, clear }) {
  const next = clone(meter) || {};
  const mqtt = { ...(next.mqtt || {}) };
  if (clear !== undefined) {
    mqtt.clear_password = clear;
    if (clear) mqtt.password = "";
  }
  if (password !== undefined) mqtt.password = password;
  next.mqtt = mqtt;
  return next;
}

export const NO_GRID_METER = Object.freeze({ present: false, type: "", ip: "" });

/** The legacy top-level broker form; a named-profile setup is read-only here. */
export function setBroker(broker, key, value) {
  const next = clone(broker) || {};
  if (key === "host") next.host = String(value).trim();
  else if (key === "port") {
    const port = String(value ?? "").trim();
    if (port === "") delete next.port;
    else next.port = port;
  } else if (key === "username") next.username = String(value).trim();
  else if (key === "security") {
    next.tls = value === "tls";
    next.tls_explicit = true;
  } else if (key === "password") next.password = value;
  else if (key === "clear") {
    next.clear_password = value;
    if (value) next.password = "";
  }
  if (["host", "port", "username"].includes(key)) next.present = Boolean(next.host);
  return next;
}

/**
 * The saved draft as the editor starts from it: every MQTT device's output
 * control derived once from its model and write route, as Classic derives it
 * when it loads the draft, so an apply sends what the cards say.
 */
export function editableDraft(draft, catalog) {
  if (!draft || !Array.isArray(draft.devices)) return draft;
  return { ...draft, devices: draft.devices.map((device) => (deviceKind(device) === "zendure_mqtt" ? normalizeMqttControl(device, catalog) : device)) };
}

const REASONS = Object.freeze({
  hardware_profile_missing: "model",
  write_method_missing: "model",
  hardware_profile_ambiguous: "model",
  hardware_profile_conflict: "model",
  hardware_generation_unknown: "generation",
  hardware_profile_unknown: "unsupported",
  hardware_profile_deferred: "unvalidated",
  transport_write_not_implemented: "unsupported",
  write_target_missing: "route",
  missing_product_key: "productKey",
  missing_device_id: "deviceId",
  mqtt_device_id_missing: "deviceId",
  missing_write_topic: "route",
  invalid_write_topic: "route",
  identity_route_product_conflict: "identity",
  identity_route_serial_conflict: "identity",
  identity_conflict: "identity",
  broker_source_write_unverified: "brokerUnverified",
  broker_source_unknown: "brokerUnknown",
  output_control_not_observed: "notObserved",
  external_device_read_only: "readOnlyDevice",
});

/** The words for why output control is not available, by the server's reason code. */
export const REASON_WORDS = Object.freeze([...new Set([...Object.values(REASONS), "other"])]);

export function reasonKey(code) {
  return `devices.reason.${REASONS[String(code || "")] || "other"}`;
}

/** The broker reference a typed broker name becomes: local_mqtt, or local_mqtt_<name>. */
export function brokerRef(name) {
  const slug = String(name ?? "").trim().toLowerCase().replace(/[^a-z0-9_-]/g, "").replace(/^[_-]+/, "").replace(/[_-]+$/, "");
  if (!slug || slug === "local_mqtt") return "local_mqtt";
  const suffix = (slug.startsWith("local_mqtt_") ? slug.slice("local_mqtt_".length) : slug).replace(/^[_-]+/, "").replace(/[_-]+$/, "");
  return suffix ? `local_mqtt_${suffix}` : "local_mqtt";
}

/**
 * The local broker typed beside a new MQTT inverter, or null when no host was
 * typed. A username without a password, or the other way round, is refused.
 */
export function manualBroker({ name = "", host = "", port = "", security = "plain", username = "", password = "" } = {}) {
  const address = String(host).trim();
  if (!address) return null;
  const tls = security === "tls";
  let number = parseInt(String(port).trim(), 10);
  if (!Number.isFinite(number) || number <= 0) number = tls ? 8883 : 1883;
  const user = String(username).trim();
  return { ref: brokerRef(name), host: address, port: number, tls, username: user, password, hasAuth: Boolean(user && password), authPartial: Boolean(user) !== Boolean(password) };
}

/** A new MQTT inverter bound to a typed local broker; its password travels only as the saved credential's reference. */
export function onLocalBroker(device, broker, credentialsRef = "") {
  const next = clone(device);
  next.mqtt = { broker_ref: broker.ref, topic_family: "", base_topic: null, device_id: "" };
  next.broker = { ref: broker.ref, host: broker.host, port: broker.port, tls: broker.tls, tls_insecure: false, tls_mode: "", source: "local_mqtt" };
  if (credentialsRef) next.broker.credentials_ref = credentialsRef;
  return next;
}

/** A model's validation levels, as the hardware catalogue names them (ems/mqtt_control/zendure_profiles.py VALIDATION_*). */
export const VALIDATION_MATURITIES = Object.freeze(["existing_support", "community_hardware_validation_required", "deferred", "telemetry_only"]);

/** The per-device operations (ems/power_direction.py OPERATION_*). */
export const DEVICE_OPERATIONS = Object.freeze(["discharge", "idle", "charge"]);

/** A model's validation level in words this console has, else the catalogue's own word. */
export function maturityWords(value) {
  return VALIDATION_MATURITIES.includes(value) ? { key: `devices.maturity.${value}` } : { text: String(value || ""), lang: "en" };
}

/** The operations a model supports, each in words this console has where it has them. */
export function operationWords(values) {
  return (Array.isArray(values) ? values : []).map((value) => (DEVICE_OPERATIONS.includes(value) ? { key: `devices.operation.${value}` } : { text: String(value), lang: "en" }));
}
