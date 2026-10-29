// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The network search for devices, as the Classic Maintenance editor runs it
 * (admin/static/admin.js, buildMaintenanceDiscoveryReview and around it):
 * what was found, how it relates to the draft, and the draft change each
 * action makes. Whether two observations are one device is always the
 * server's answer, carried as issued ids and tokens; nothing here compares
 * displayed serials, hosts or names to reach it. Only whether a device the
 * server matched still answers at its saved address is read from the
 * addresses themselves. Everything is pure.
 */

import { commonDefaults, deviceKind, fieldKey, nextInverterName, newE3dc, E3DC_DEFAULTS, IDENTITY_KEYS } from "./devices.js";

const OPAQUE = /^opaque:v1:[A-Za-z0-9_-]+$/;
const CONNECTION = /^conn:v1:[A-Za-z0-9_-]+$/;
const OBSERVATION = /^obs:v1:[A-Za-z0-9_-]+$/;
const CATALOG = /^catalog:v1:[A-Za-z0-9_-]+$/;

export const REVIEW_STATES = Object.freeze(["found", "new", "missing", "conflict", "transport", "added", "identity_conflict", "unavailable"]);

const text = (value) => String(value ?? "").trim();
const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));

export function issuedPhysicalIdentity(entry) {
  for (const value of [entry?.physical_device_id, entry?.physical_identity_token]) {
    if (OPAQUE.test(text(value))) return text(value);
  }
  return "";
}

export function issuedConnectionId(entry) {
  return CONNECTION.test(text(entry?.connection_id)) ? text(entry.connection_id) : "";
}

function catalogId(entry, field) {
  return CATALOG.test(text(entry?.[field])) ? text(entry[field]) : "";
}

function identityTokens(entry) {
  const tokens = new Set();
  const add = (value) => {
    if (OPAQUE.test(text(value))) tokens.add(text(value));
  };
  add(entry?.physical_device_id);
  add(entry?.physical_identity_token);
  if (Array.isArray(entry?.physical_identity_alias_tokens)) entry.physical_identity_alias_tokens.forEach(add);
  return tokens;
}

export function hasIdentity(entry) {
  return identityTokens(entry).size > 0;
}

/** Two confirmed, different physical identities that share an alias: one route claimed by two inverters. */
export function identityConflict(a, b) {
  if (a?.identity_status !== "confirmed" || b?.identity_status !== "confirmed") return false;
  const left = issuedPhysicalIdentity(a);
  const right = issuedPhysicalIdentity(b);
  if (!left || !right || left === right) return false;
  const tokens = identityTokens(a);
  return [...identityTokens(b)].some((token) => tokens.has(token));
}

export function identitiesMatch(a, b) {
  if (identityConflict(a, b)) return false;
  const tokens = identityTokens(a);
  return [...identityTokens(b)].some((token) => tokens.has(token));
}

function sameIssued(left, right) {
  return Boolean(left && right && left === right);
}

export function aliasTokens(value) {
  if (!Array.isArray(value)) return undefined;
  const tokens = [...new Set(value.map(text).filter((token) => OPAQUE.test(token)))];
  return tokens.length ? tokens : undefined;
}

/** The server's observation id, or a stable handle from where the device was reached when it issued none. */
export function observationKey(device) {
  if (!device || typeof device !== "object") return "unresolved:invalid";
  if (OBSERVATION.test(text(device.observation_id))) return text(device.observation_id);
  return `unresolved:${[
    String(device.device_type || device.api_family || device.role_suggestion || "device").toLowerCase(),
    String(device.id || ""),
    String(device.ip || device.host || device.hostname || "").toLowerCase(),
    String(device.port || ""),
    String(device.mqtt?.device_id || device.device_id || ""),
  ].join("|")}`;
}

export function normalizeSource(source) {
  const value = String(source || "network_scan").toLowerCase();
  if (value === "manual") return "manual_scan";
  if (value === "http_probe" || value === "network_scan") return "active_scan";
  return value;
}

export function sourcesOf(device) {
  return Array.isArray(device?.sources) && device.sources.length ? device.sources : [device?.source || "network_scan"];
}

/**
 * One observation merged into those found so far, keyed by the server's id.
 * A scan that reached the device sets its address; a port outside 1-65535 is
 * not kept.
 */
export function mergeObservation(found, device, source) {
  if (!device || typeof device !== "object") return found;
  const incoming = { ...device };
  const incomingSources = source ? [normalizeSource(source)] : sourcesOf(incoming).map(normalizeSource);
  const key = observationKey(incoming);
  const existing = found[key] || null;
  const merged = { ...(existing || {}), ...incoming, sources: [...new Set([...(existing ? sourcesOf(existing) : []).map(normalizeSource), ...incomingSources])] };
  for (const field of ["model", "device_type", "api_family", "role_suggestion", "host", "hostname"]) {
    if (!incoming[field] && existing?.[field]) merged[field] = existing[field];
  }
  merged.serial_number = incoming.serial_number || existing?.serial_number || null;
  merged.display_name = incoming.display_name || incoming.model || existing?.display_name || existing?.model || "";
  const reached = incomingSources.some((value) => value === "active_scan" || value === "manual_scan");
  if (incoming.ip && (reached || !existing?.ip)) merged.ip = incoming.ip;
  const validPort = (port) => Number(port) > 0 && Number(port) <= 65535;
  if (!validPort(incoming.port)) {
    if (existing && validPort(existing.port)) merged.port = existing.port;
    else delete merged.port;
  }
  return { ...found, [key]: merged };
}

/**
 * A typed address or range to scan: a private, link-local or loopback IPv4
 * address or a range of /24 or smaller, as the network address. ``error``
 * names why it is refused.
 */
export function scanTarget(raw) {
  const input = text(raw);
  if (!input) return { error: "empty" };
  const parts = input.split("/");
  if (parts.length > 2) return { error: "invalid" };
  const octets = parts[0].split(".");
  if (octets.length !== 4 || octets.some((part) => !/^\d{1,3}$/.test(part) || Number(part) > 255)) return { error: "invalid" };
  let prefix = 32;
  if (parts.length === 2) {
    if (!/^\d{1,2}$/.test(parts[1])) return { error: "prefix" };
    prefix = Number(parts[1]);
    if (prefix < 24 || prefix > 32) return { error: "tooLarge" };
  }
  const n = octets.map(Number);
  const allowed = n[0] === 10 || (n[0] === 172 && n[1] >= 16 && n[1] <= 31) || (n[0] === 192 && n[1] === 168) || (n[0] === 169 && n[1] === 254) || n[0] === 127;
  if (!allowed) return { error: "private" };
  if (prefix === 32) return { cidr: `${n.join(".")}/32` };
  const value = (((n[0] << 24) >>> 0) | (n[1] << 16) | (n[2] << 8) | n[3]) >>> 0;
  const network = (value & ((0xffffffff << (32 - prefix)) >>> 0)) >>> 0;
  return { cidr: `${[network >>> 24, (network >>> 16) & 255, (network >>> 8) & 255, network & 255].join(".")}/${prefix}` };
}

/** The /24 networks the draft's devices and grid meter live in. */
export function configuredNetworks(draft) {
  const addresses = (draft?.devices || []).map((device) => device?.ip);
  if (draft?.grid_meter?.present) addresses.push(draft.grid_meter.ip);
  return [...new Set(addresses.map((address) => scanTarget(`${address || ""}/24`)).filter((result) => !result.error).map((result) => result.cidr))];
}

const FAMILY_TYPES = {
  shelly_gen2: "shelly",
  shelly_3em_gen1: "shelly_3em_gen1",
  ecotracker: "ecotracker",
  zendure_grid_meter_http: "zendure_grid_meter_http",
  zendure_smartmeter_3ct_http: "zendure_grid_meter_http",
  tasmota_http: "tasmota_http",
  e3dc_modbus: "e3dc_modbus",
};
const METER_TYPES = new Set(["shelly", "shelly_3em_gen1", "ecotracker", "zendure_grid_meter_http", "zendure_smartmeter_3ct_http", "zendure_smartmeter_d0_http", "tasmota_http", "e3dc_modbus", "zendure_smartmeter_d0", "mqtt"]);

/** The meter type a found grid meter is: chosen, then by its API family, then by its description. */
export function meterTypeOf(found) {
  const explicit = text(found?.grid_meter_type).toLowerCase();
  if (METER_TYPES.has(explicit)) return explicit;
  const family = text(found?.api_family).toLowerCase();
  if (FAMILY_TYPES[family]) return FAMILY_TYPES[family];
  const description = `${found?.device_type || ""} ${found?.api_family || ""}`.toLowerCase();
  if (description.includes("ecotracker")) return "ecotracker";
  if (description.includes("3em") && description.includes("gen1")) return "shelly_3em_gen1";
  return "shelly";
}

const display = (value) => text(value).toLowerCase();
const isCandidate = (device) => ["inverter", "grid_meter"].includes(String(device?.role_suggestion || "unknown"));
const role = (device) => String(device?.role_suggestion || "unknown");
const isE3dcCandidate = (device) => String(device?.api_family || "").toLowerCase() === "e3dc_modbus";
const e3dcSetting = (value, key) => (value == null || text(value) === "" ? E3DC_DEFAULTS[key] : Number(value));
const sameE3dc = (configured, found) => display(configured?.ip) === display(found?.ip) && e3dcSetting(configured?.port, "port") === e3dcSetting(found?.port, "port");
const e3dcReachable = (configured) => !scanTarget(`${configured?.ip || ""}/24`).error && e3dcSetting(configured?.port, "port") === E3DC_DEFAULTS.port && e3dcSetting(configured?.unit_id, "unit_id") === E3DC_DEFAULTS.unit_id;

export function isGridMeterProposal(proposal) {
  return Boolean(proposal) && String(proposal.target || "device").toLowerCase() === "grid_meter" && Boolean(proposal.grid_meter_fragment);
}

export function isExternalProposal(proposal) {
  return proposal?.config_fragment?.type === "external_mqtt";
}

/** An MQTT proposal's hardware role, from the server's target and role hint only. */
export function proposalRole(proposal) {
  if (!proposal) return "unknown";
  if (isExternalProposal(proposal)) return "inverter";
  const hint = String(proposal.role_hint || "").toLowerCase();
  if (isGridMeterProposal(proposal) || String(proposal.target || "").toLowerCase() === "grid_meter" || hint === "grid_meter_candidate") return "grid_meter";
  return hint === "battery_inverter_candidate" ? "inverter" : "unknown";
}

export function proposalTopic(proposal) {
  const topic = proposal?.grid_meter_fragment?.mqtt?.topic;
  return typeof topic === "string" ? topic : "";
}

function brokerRef(proposal, mqtt) {
  return text(mqtt?.broker_ref || proposal?.broker_ref);
}

/** The broker a proposal names, without secrets, so the server can keep its profile. */
export function brokerProfile(proposal, mqtt) {
  return {
    ref: brokerRef(proposal, mqtt),
    host: proposal.broker_host || "",
    port: proposal.broker_port == null ? null : proposal.broker_port,
    tls: proposal.broker_tls === true,
    tls_insecure: proposal.broker_tls_insecure === true,
    tls_mode: proposal.broker_tls_mode || "",
    credentials_ref: proposal.credentials_ref || "",
    source: proposal.connection_source || proposal.source || "",
  };
}

/** The grid meter a proposal's server-made fragment describes, or null: a topic is never invented here. */
export function meterFromProposal(proposal) {
  const fragment = proposal?.grid_meter_fragment || null;
  const type = fragment ? text(fragment.type) : "";
  if (!type || !proposalTopic(proposal)) return null;
  const mqtt = { ...fragment.mqtt };
  const ref = brokerRef(proposal, mqtt);
  if (ref) mqtt.broker_ref = ref;
  return { present: true, type, mqtt };
}

function sameMeter(meter, mapping) {
  if (!meter || !mapping || meter.present === false) return false;
  return String(meter.type || "") === mapping.type && String(meter.mqtt?.topic || "") === String(mapping.mqtt.topic || "") && String(meter.mqtt?.broker_ref || "") === String(mapping.mqtt.broker_ref || "");
}

function identityView(proposal) {
  const fragment = proposal?.config_fragment || {};
  const aliases = (Array.isArray(proposal.physical_identity_alias_tokens) && proposal.physical_identity_alias_tokens.length && proposal.physical_identity_alias_tokens) || fragment.physical_identity_alias_tokens || [];
  return {
    physical_device_id: proposal.physical_device_id || fragment.physical_device_id,
    physical_identity_token: proposal.physical_identity_token || fragment.physical_identity_token,
    physical_identity_alias_tokens: aliases,
    connection_id: proposal.connection_id || fragment.connection_id,
    identity_status: proposal.identity_status || fragment.identity_status,
  };
}

export function proposalIdentity(proposal) {
  return issuedPhysicalIdentity(proposal) || issuedPhysicalIdentity(proposal?.config_fragment);
}

function sameConnection(device, proposal) {
  const configured = issuedConnectionId(device);
  const offered = issuedConnectionId(proposal) || issuedConnectionId(proposal?.config_fragment);
  if (configured && offered) return configured === offered;
  const configuredRef = text(device?.proposal_id);
  const offeredRef = text(proposal?.id);
  if (configuredRef && offeredRef) return configuredRef === offeredRef;
  return true;
}

function draftHasProposal(draft, proposal) {
  const offered = text(proposal?.id);
  return Boolean(offered) && (draft?.devices || []).some((device) => text(device?.proposal_id) === offered);
}

function matchingDevices(draft, view) {
  return (draft?.devices || []).filter((device) => identitiesMatch(device, view));
}

function externalState(proposal, draft, saved) {
  const issued = catalogId(proposal, "catalog_device_id");
  const connection = catalogId(proposal, "catalog_connection_id");
  const sameDevice = (device) => deviceKind(device) === "external_mqtt" && Boolean(issued) && catalogId(device, "catalog_device_id") === issued;
  const sameRoute = (device) => sameDevice(device) && Boolean(connection) && catalogId(device, "catalog_connection_id") === connection;
  const devices = draft?.devices || [];
  if (!devices.some(sameDevice)) return draftHasProposal(draft, proposal) ? "added" : "new";
  if (!devices.some(sameRoute)) return "transport";
  return (saved?.devices || []).some(sameRoute) ? "found" : "added";
}

/** What an MQTT proposal offers against the draft: new, added, found, an alternative connection, or a conflict. */
export function proposalState(proposal, draft, saved) {
  if (proposalRole(proposal) === "grid_meter") {
    const mapping = meterFromProposal(proposal);
    if (!mapping) return "unavailable";
    if (!sameMeter(draft?.grid_meter, mapping)) return "new";
    return sameMeter(saved?.grid_meter, mapping) ? "found" : "added";
  }
  if (isExternalProposal(proposal)) return externalState(proposal, draft, saved);
  const view = identityView(proposal);
  if (!hasIdentity(view)) return draftHasProposal(draft, proposal) ? "added" : "new";
  const devices = draft?.devices || [];
  if (devices.some((device) => identityConflict(device, view))) return "identity_conflict";
  const matched = matchingDevices(draft, view);
  if (matched.length > 1) return "identity_conflict";
  if (!matched.length) return "new";
  if (deviceKind(matched[0]) !== "zendure_mqtt" || !sameConnection(matched[0], proposal)) return "transport";
  const before = (saved?.devices || []).some((device) => deviceKind(device) === "zendure_mqtt" && identitiesMatch(device, view) && sameConnection(device, proposal));
  return before ? "found" : "added";
}

const IP_LITERAL = /^(\d{1,3}\.){3}\d{1,3}$|:/;

/**
 * Whether an inverter the server matched answers at an entry's address. The
 * search's finds carry no connection id, and one issued elsewhere counts the
 * port the device answered on, which a saved address never names, so the
 * addresses are compared. A host name cannot be held against the address a
 * find answered at, so an entry that names its inverter by one is never
 * called moved.
 */
function sameAddress(entry, discovered) {
  const saved = display(entry?.ip);
  if (!saved) return false;
  return !IP_LITERAL.test(saved) || saved === display(discovered?.ip);
}

function findInverter(configured, discovered, used) {
  const identity = issuedPhysicalIdentity(configured);
  if (identity) {
    const byIdentity = discovered.find((device) => !used.has(observationKey(device)) && role(device) === "inverter" && sameIssued(issuedPhysicalIdentity(device), identity));
    if (byIdentity) return { device: byIdentity, match: "identity" };
  }
  if (deviceKind(configured) === "zendure_mqtt") return null;
  const connection = issuedConnectionId(configured);
  if (!connection) return null;
  const byConnection = discovered.find((device) => !used.has(observationKey(device)) && role(device) === "inverter" && issuedConnectionId(device) === connection);
  return byConnection ? { device: byConnection, match: "connection" } : null;
}

/**
 * The review of a search against the draft, item by item, as Classic builds
 * it: each configured device found, missing, moved or reachable another way,
 * the grid meter, new devices, read-only devices, then every MQTT proposal.
 */
export function buildReview(discovered, proposals, draft, saved) {
  const supported = (discovered || []).filter(isCandidate);
  const used = new Set();
  const results = [];
  const devices = draft?.devices || [];
  devices.forEach((configured, index) => {
    const kind = deviceKind(configured);
    if (kind === "external_mqtt" || kind === "e3dc_modbus") return;
    const match = findInverter(configured, supported, used);
    if (!match) {
      if (kind !== "zendure_mqtt" && configured.original_name != null) results.push({ role: "inverter", state: "missing", configured, index });
      return;
    }
    used.add(observationKey(match.device));
    if (kind === "zendure_mqtt") {
      results.push({ role: "inverter", state: "transport", configured, discovered: match.device, index, targetSource: "local_api" });
      return;
    }
    const before = configured.original_name != null ? (saved?.devices || []).find((device) => device.name === configured.original_name && deviceKind(device) === "local_api") : null;
    const here = sameAddress(configured, match.device);
    if (!before && here) {
      results.push({ role: "inverter", state: "added", configured, discovered: match.device, index });
      return;
    }
    const moved = match.match === "identity" && (!here || !sameAddress(before, match.device));
    results.push({ role: "inverter", state: moved ? "conflict" : "found", configured, discovered: match.device, index });
  });
  const meter = draft?.grid_meter?.present ? draft.grid_meter : null;
  if (meter) {
    const found = supported.find((device) => !used.has(observationKey(device)) && role(device) === "grid_meter" && display(device.ip) === display(meter.ip));
    if (found) used.add(observationKey(found));
    results.push({ role: "grid_meter", state: found ? "found" : "missing", configured: meter, discovered: found || null });
  }
  supported.forEach((device) => {
    if (!used.has(observationKey(device))) results.push({ role: role(device), state: "new", discovered: device });
  });
  supported.filter(isE3dcCandidate).forEach((device) => {
    const configured = devices.find((item) => deviceKind(item) === "e3dc_modbus" && item.original_name != null && sameE3dc(item, device));
    results.push(configured ? { role: "read_only_device", state: "found", configured, discovered: device } : { role: "read_only_device", state: "new", discovered: device });
  });
  devices.forEach((configured, index) => {
    if (deviceKind(configured) !== "e3dc_modbus" || configured.original_name == null || !e3dcReachable(configured)) return;
    if (!supported.some((device) => isE3dcCandidate(device) && sameE3dc(configured, device))) results.push({ role: "read_only_device", state: "missing", configured, index });
  });
  (proposals || []).forEach((proposal) => {
    results.push({ role: String(proposal?.connection_source || "") === "zendure_cloud_mqtt" ? "zendure_mqtt" : "local_mqtt", state: proposalState(proposal, draft, saved), proposal });
  });
  return results;
}

/** The key a review item keeps across rebuilds, for an ignore that outlives draft changes. */
export function itemKey(item) {
  if (item.proposal) return `proposal:${item.proposal.id || observationKey(item.proposal)}`;
  const found = item.discovered;
  const base = found ? String(found.id || observationKey(found)) : `configured-${item.index != null ? item.index : item.configured?.name || item.role}`;
  return `candidate:${base}${item.role === "read_only_device" ? "-read-only" : ""}`;
}

/**
 * Whether a found device is in the draft already: by the identity the server
 * issued for the find, which an inverter added from the search carries, or,
 * for a find without one, by the address it was added with; that only keeps
 * it from being added twice.
 */
export function alreadyInDraft(item, draft) {
  const found = item?.discovered;
  if (!found || !draft) return false;
  if (item.role === "read_only_device") return (draft.devices || []).some((device) => deviceKind(device) === "e3dc_modbus" && sameE3dc(device, found));
  if ((item.role || role(found)) === "grid_meter") {
    const meter = draft.grid_meter || {};
    return Boolean(meter.present && found.ip && display(meter.ip) === display(found.ip));
  }
  const identity = issuedPhysicalIdentity(found);
  const connection = issuedConnectionId(found);
  const addedHere = (device) => device?.original_name == null && deviceKind(device) === "local_api" && Boolean(found.ip) && display(device.ip) === display(found.ip);
  return (draft.devices || []).some((device) => sameIssued(issuedPhysicalIdentity(device), identity) || (!identity && connection && issuedConnectionId(device) === connection) || addedHere(device));
}

function canControl(proposal) {
  return proposal?.output_control_supported === true || proposal?.config_fragment?.capabilities?.write_output_limit === true;
}

/** The configured inverter a review item names, when exactly one. */
export function configuredFor(item, draft) {
  if (item?.configured) return item.configured;
  if (!item?.proposal) return null;
  const matched = matchingDevices(draft, identityView(item.proposal));
  return matched.length === 1 ? matched[0] : null;
}

/** Taking a read-only connection for an inverter EMS regulates today gives up its regulation. */
export function isControlDowngrade(item, draft) {
  if (item?.state !== "transport" || !item.proposal || isGridMeterProposal(item.proposal) || canControl(item.proposal)) return false;
  const configured = configuredFor(item, draft);
  return Boolean(configured) && configured.enabled !== false;
}

/**
 * The action a review item offers: ``kind`` names what the button does,
 * ``label`` its words, and disabled items only say where they stand.
 */
export function itemAction(item, draft, ignored) {
  if (item.proposal) {
    const isMeter = proposalRole(item.proposal) === "grid_meter";
    if (isMeter) {
      if (item.state === "found") return { label: "inConfig", disabled: true };
      if (item.state === "added") return { label: "added", disabled: true };
      return { label: "useAsGridMeter", kind: "meter", disabled: item.state !== "new" };
    }
    if (item.state === "found") return { label: "inConfig", disabled: true };
    if (item.state === "added") return { label: "added", disabled: true };
    if (item.state === "identity_conflict") return { label: "identityConflict", disabled: true };
    if (item.state === "transport") {
      if (isExternalProposal(item.proposal)) return { label: "useConnection", kind: "externalConnection", disabled: false };
      return { label: isControlDowngrade(item, draft) ? "replaceControl" : "useConnection", kind: "switch", disabled: false };
    }
    return { label: "addInverter", kind: isExternalProposal(item.proposal) ? "addExternal" : "addProposal", disabled: false };
  }
  if (ignored) return { label: "ignored", disabled: true };
  if (item.state === "found") return { label: "inConfig", disabled: true };
  if (item.state === "missing") return { label: "configured", disabled: true };
  if (item.state === "transport") return { label: "useConnection", kind: "switch", disabled: false };
  if (item.state === "identity_conflict") return { label: "identityConflict", disabled: true };
  if (item.state === "conflict") {
    const current = (draft?.devices || [])[item.index];
    if (current && sameAddress(current, item.discovered)) return { label: "added", disabled: true };
    return { label: "updateDraft", kind: "updateAddress", disabled: false };
  }
  if (alreadyInDraft(item, draft)) return { label: "added", disabled: true };
  const which = item.role || role(item.discovered || {});
  return { label: which === "grid_meter" ? "addGridMeter" : which === "read_only_device" ? "addReadOnly" : "addInverter", kind: "add", disabled: false };
}

function withDefaults(device, catalog) {
  const defaults = commonDefaults(catalog);
  for (const key of Object.keys(defaults)) if (device[key] == null) device[key] = clone(defaults[key]);
  return device;
}

/** A draft entry for a trusted Zendure MQTT proposal; the same shape for adding it and for switching to it. */
export function mqttDeviceFromProposal(proposal, devices, catalog) {
  const fragment = proposal.config_fragment || {};
  const mqtt = fragment.mqtt || {};
  const caps = fragment.capabilities || {};
  const outputControl = caps.write_output_limit === true;
  const supportsControl = proposal.output_control_supported === true || outputControl;
  const routeDeviceId = mqtt.device_id || "";
  return withDefaults(
    {
      kind: "zendure_mqtt",
      original_name: null,
      proposal_id: proposal.id || "",
      proposal_broker_ref: proposal.broker_ref || mqtt.broker_ref || "",
      physical_identity_token: issuedPhysicalIdentity(proposal),
      physical_identity_alias_tokens: aliasTokens(proposal.physical_identity_alias_tokens),
      identity_status: proposal.identity_status || "",
      connection_id: issuedConnectionId(proposal) || issuedConnectionId(fragment) || "",
      name: nextInverterName(devices),
      enabled: true,
      has_enabled_key: true,
      serial_number: proposal.serial_number || fragment.serial_number || "",
      device_id: routeDeviceId || proposal.device_id || "",
      product_key: mqtt.product_key || "",
      hardware_generation: proposal.hardware_generation || "",
      hardware_model: proposal.hardware_model || fragment.hardware_profile || "",
      power_write_profile: fragment.power_write_profile || "",
      alternative_layout: Boolean(proposal.alternative_layout),
      output_control: outputControl,
      supports_output_control: supportsControl,
      control_readiness: { ready: supportsControl, reason: proposal.output_control_reason || proposal.control_block_reason || "" },
      trusted_write_target: outputControl && proposal.control_block_reason !== "write_target_missing",
      mqtt: {
        broker_ref: mqtt.broker_ref || "",
        source: mqtt.source || "",
        topic_family: mqtt.topic_family || "",
        base_topic: mqtt.base_topic == null ? null : mqtt.base_topic,
        device_id: routeDeviceId,
        product_key: mqtt.product_key || "",
        write_protocol: mqtt.write_protocol || "",
      },
      capabilities: { read_power: caps.read_power !== false, read_soc: caps.read_soc !== false, write_output_limit: outputControl },
      broker: brokerProfile(proposal, mqtt),
    },
    catalog,
  );
}

/** A draft entry for an external catalogue device a proposal offers; the server rebuilds it from the proposal. */
export function externalDeviceFromProposal(proposal, devices) {
  const mqtt = proposal.config_fragment?.mqtt || {};
  return {
    kind: "external_mqtt",
    original_name: null,
    proposal_id: proposal.id || "",
    proposal_broker_ref: proposal.broker_ref || mqtt.broker_ref || "",
    catalog_device_id: catalogId(proposal, "catalog_device_id"),
    catalog_connection_id: catalogId(proposal, "catalog_connection_id"),
    catalog_label: proposal.catalog_label || "",
    display_name: proposal.display_name || "",
    name: nextInverterName(devices),
    enabled: true,
    mqtt: { broker_ref: mqtt.broker_ref || "", topic_family: mqtt.topic_family || "", device_id: mqtt.device_id || "" },
  };
}

/** A configured external device moved to the connection a proposal offers; null when no device is that one. */
export function useExternalConnection(devices, proposal) {
  const issued = catalogId(proposal, "catalog_device_id");
  const index = (devices || []).findIndex((device) => deviceKind(device) === "external_mqtt" && Boolean(issued) && catalogId(device, "catalog_device_id") === issued);
  if (index < 0) return null;
  const mqtt = proposal.config_fragment?.mqtt || {};
  const next = [...devices];
  next[index] = {
    ...devices[index],
    proposal_id: proposal.id || "",
    proposal_broker_ref: proposal.broker_ref || mqtt.broker_ref || "",
    catalog_connection_id: catalogId(proposal, "catalog_connection_id"),
    mqtt: { broker_ref: mqtt.broker_ref || "", topic_family: mqtt.topic_family || "", device_id: mqtt.device_id || "" },
  };
  return { devices: next, index };
}

/**
 * One inverter moved to another connection in place: same name, same common
 * values, same original entry; new connection fields only. Refused (null)
 * when the identity is missing, contradicted or names more than one device.
 */
export function switchTransport(devices, identity, target, context, catalog) {
  const raw = text(identity);
  if (!raw) return null;
  const probe = { physical_device_id: raw };
  if (!hasIdentity(probe)) return null;
  if ((devices || []).some((device) => identityConflict(device, probe))) return null;
  const matched = (devices || []).map((device, index) => (identitiesMatch(device, probe) ? index : -1)).filter((index) => index >= 0);
  if (matched.length !== 1) return null;
  const index = matched[0];
  const current = devices[index];
  const kept = { original_name: current.original_name || null, has_enabled_key: true };
  if (current.name) kept.name = current.name;
  const commonFields = (catalog?.hardware_sections || []).find((section) => section?.id === "devices")?.fields || [];
  for (const field of commonFields) {
    const key = fieldKey(field.path);
    if (!IDENTITY_KEYS.includes(key) && current[key] != null) kept[key] = current[key];
  }
  let replacement;
  if (target === "local_api") {
    const found = context?.discovered || {};
    replacement = {
      kind: "local_api",
      ip: found.ip || "",
      sn: found.serial_number || current.serial_number || current.sn || "",
      physical_device_id: issuedPhysicalIdentity(found) || issuedPhysicalIdentity(current),
      identity_status: found.identity_status || current.identity_status || "",
      connection_id: issuedConnectionId(found),
      ...kept,
    };
  } else {
    if (!context?.proposal) return null;
    replacement = { ...mqttDeviceFromProposal(context.proposal, devices, catalog), ...kept };
  }
  replacement = withDefaults(replacement, catalog);
  replacement.transport_switched = true;
  replacement.enabled = current.enabled !== false;
  replacement.has_enabled_key = true;
  if (deviceKind(replacement) === "zendure_mqtt" && replacement.supports_output_control === true) {
    replacement.output_control = true;
    replacement.capabilities = { ...(replacement.capabilities || { read_power: true, read_soc: true }), write_output_limit: true };
  }
  const next = [...devices];
  next[index] = replacement;
  return { devices: next, index };
}

/** A found device added to the draft: an inverter, the grid meter or a read-only E3/DC; null when it is there already. */
export function addFound(draft, item, catalog) {
  const found = item.discovered;
  if (!found || alreadyInDraft(item, draft)) return null;
  const devices = draft.devices || [];
  if (item.role === "read_only_device") return { devices: [...devices, newE3dc(devices, { ip: found.ip, port: found.port })], index: devices.length };
  if (item.role === "grid_meter") {
    const meter = { present: true, type: meterTypeOf({ device_type: found.device_type || "", api_family: found.api_family || "" }), ip: found.ip || "" };
    if (found.port != null && found.port !== "") meter.port = found.port;
    return { grid_meter: meter };
  }
  const device = withDefaults({ original_name: null, name: nextInverterName(devices), ip: found.ip || "", sn: found.serial_number || "", enabled: true }, catalog);
  const identity = issuedPhysicalIdentity(found);
  if (identity) Object.assign(device, { physical_device_id: identity, identity_status: found.identity_status || "" });
  return { devices: [...devices, device], index: devices.length };
}
