// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Guided Setup's hardware draft as the Classic console keeps it
 * (admin/static/admin.js, the Config step): the discovered and hand-added
 * draft items, the chosen MQTT connections, the hand-entered MQTT broker and
 * devices, the feature values and what the operator removed. It is stored
 * under Classic's own keys and in Classic's shapes, so a setup moves between
 * the two consoles. Which observations are one inverter, and which connection
 * each uses, is the server's device plan; these helpers only apply its typed
 * answer and never compare serials, hosts or names to reach one. Pure.
 */

import { D0_TOPIC_PREFIX, D0_TOPIC_SUFFIX, generations, hardwareModels, modelsForGeneration } from "./devices.js";
import { aliasTokens, isExternalProposal, isGridMeterProposal, issuedPhysicalIdentity, meterTypeOf, observationKey, proposalTopic, sourcesOf } from "./discovery.js";

/** Classic's localStorage keys; the workflow identity and the step live in state/setup.svelte.js. */
export const STORE = Object.freeze({
  items: "ems-admin-config-draft",
  baseline: "ems-admin-config-baseline",
  dismissedObservations: "ems-admin-config-dismissed",
  dismissedPhysical: "ems-admin-config-dismissed-devices",
  legacyDismissals: "ems-admin-config-dismissed-serials",
  features: "ems-admin-config-features",
  selections: "ems-admin-config-mqtt-preview",
  broker: "ems-admin-config-mqtt-broker",
  manual: "ems-admin-config-mqtt-manual-devices",
});

export const IDENTITY_SCHEMA_VERSION = 1;
export const DEFAULT_INVERTER = "SolarFlow 800 Pro 2";
export const DEFAULT_GRID_METER = "Shelly Pro 3EM";
/** Catalogue device keys a draft item holds under its own name; the rest are config values. */
export const MAPPED_FIELDS = Object.freeze({ name: "config_name", ip: "ip", sn: "serial_number" });
/** The broker source a hand-entered MQTT device is judged against: the form provisions a local broker. */
export const MANUAL_BROKER_SOURCE = "local_mqtt";
export const DOWNLOAD_MIN_PASSWORD = 8;

/** Refusals and what each asks of the reader. */
export const CONFIG_CONFLICTS = Object.freeze(["stale_setup_config", "setup_preview_required", "setup_preview_mismatch"]);
export const PLAN_CONFLICTS = Object.freeze(["device_plan_required", "stale_device_plan", "device_plan_draft_mismatch", "device_plan_confirmation_required"]);
export const WORKFLOW_CONFLICTS = Object.freeze(["setup_workflow_required", "setup_workflow_not_active", "setup_intent_workflow_mismatch", "setup_transition_owner_unproven", "setup_transition_context_mismatch"]);
export const BUSY_OPERATIONS = Object.freeze(["config_write", "config_apply", "deployment_prepare", "deployment_start", "permission_repair", "container_conflict_resolution"]);
export const SWITCH_REASONS = Object.freeze(["contradictory_physical_serials", "ambiguous_mqtt_write_route", "no_shared_identity_evidence", "insufficient_identity_evidence", "candidate_cannot_write_output_limit", "control_continuity_needs_confirmation"]);
export const CONTINUITY = Object.freeze(["lost", "unknown", "preserved", "gained", "not_required"]);

const OPAQUE = /^opaque:v1:[A-Za-z0-9_-]+$/;
const CONNECTION = /^conn:v1:[A-Za-z0-9_-]+$/;
const text = (value) => String(value ?? "").trim();
const has = (object, key) => Boolean(object) && Object.prototype.hasOwnProperty.call(object, key);
const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));

/** A stored list: the versioned envelope, or the bare array an older release wrote. */
export function readStore(raw) {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed;
    return parsed && typeof parsed === "object" && Array.isArray(parsed.items) ? parsed.items : [];
  } catch {
    return [];
  }
}

export function storeEnvelope(items) {
  return JSON.stringify({ schema_version: IDENTITY_SCHEMA_VERSION, items: [...items] });
}

/** A draft written before rows had ids gets a stable local handle; it names a row, never a device. */
export function withItemIds(items) {
  return (Array.isArray(items) ? items : []).filter((item) => item && typeof item === "object").map((item, index) => (text(item.draft_item_id) ? item : { ...item, draft_item_id: `item-legacy-${index}` }));
}

export function opaqueIds(values) {
  return (Array.isArray(values) ? values : []).map(text).filter((value) => OPAQUE.test(value));
}

export function isBaseline(value) {
  return Boolean(value && typeof value === "object" && typeof value.expect_absent === "boolean" && (value.expected_revision === null || typeof value.expected_revision === "string"));
}

/** An empty draft: every list empty, nothing removed, no feature changed. */
export function emptyDraft() {
  return { items: [], selections: [], manual: [], broker: {}, features: {}, dismissedObservations: [], dismissedPhysical: [], legacyDismissals: [] };
}

export const inverters = (draft) => draft.items.filter((item) => item.role === "inverter");
export const gridMeterItem = (draft) => draft.items.find((item) => item.role === "grid_meter") || null;
export const isGridSelection = (entry) => String(entry?.target || "device").toLowerCase() === "grid_meter";
export const deviceSelections = (draft) => draft.selections.filter((entry) => !isGridSelection(entry));
export const selection = (draft, id) => draft.selections.find((entry) => String(entry.id) === String(id)) || null;
export const hasSource = (draft, sourceId) => draft.items.some((item) => item.source_id === sourceId);

/** The selections with one entry set: a known id keeps its place, a new one goes last. */
export function putSelection(selections, entry) {
  const id = String(entry.id);
  return selections.some((item) => String(item.id) === id) ? selections.map((item) => (String(item.id) === id ? entry : item)) : [...selections, entry];
}

export function dropSelection(selections, id) {
  return selections.filter((item) => String(item.id) !== String(id));
}

export const withValue = (list, value) => (value && !list.includes(value) ? [...list, value] : list);
export const withoutValue = (list, value) => list.filter((item) => item !== value);

/** The next compact inverter name: one past the highest INV_<n> and the count, never one in use. */
export function nextCompactName(names, count) {
  const list = Array.isArray(names) ? names : [];
  const used = new Set(list.map((name) => text(name).toLowerCase()));
  let highest = 0;
  for (const name of list) {
    const match = /^INV_([1-9][0-9]*)$/i.exec(text(name));
    if (match) highest = Math.max(highest, Number(match[1]));
  }
  const total = Number.isFinite(Number(count)) ? Math.max(0, Math.trunc(Number(count))) : list.length;
  let number = Math.max(1, highest + 1, total + 1);
  while (used.has(`inv_${number}`)) number += 1;
  return `INV_${number}`;
}

/** Every inverter name the draft holds: its rows, its MQTT devices and the hand-entered ones. */
export function inverterNames(draft, exclude = null) {
  return [...inverters(draft).filter((item) => item !== exclude).map((item) => item.config_name), ...deviceSelections(draft).filter((entry) => entry !== exclude).map((entry) => entry.config_name), ...draft.manual.filter((device) => device !== exclude).map((device) => device.name)];
}

export function nextName(draft, exclude = null) {
  const names = inverterNames(draft, exclude);
  return nextCompactName(names, names.length);
}

export function uniqueDisplayName(items, base, role) {
  const used = new Set(items.filter((item) => item.role === role).map((item) => item.display_name));
  if (!used.has(base)) return base;
  let suffix = 2;
  while (used.has(`${base} #${suffix}`)) suffix += 1;
  return `${base} #${suffix}`;
}

/**
 * Names a store from before every inverter carried one gets, in Classic's
 * order: the MQTT devices first, then the hand-entered ones.
 */
export function withNames(draft) {
  const names = [...inverters(draft).filter((item) => item.config_name).map((item) => item.config_name), ...deviceSelections(draft).filter((entry) => has(entry, "config_name")).map((entry) => entry.config_name), ...draft.manual.filter((device) => has(device, "name")).map((device) => device.name)];
  let count = names.length;
  const name = () => {
    const value = nextCompactName(names, count);
    names.push(value);
    count += 1;
    return value;
  };
  const selections = draft.selections.map((entry) => (isGridSelection(entry) || has(entry, "config_name") ? entry : { ...entry, config_name: name() }));
  const manual = draft.manual.map((device) => (has(device, "name") ? device : { ...device, name: name() }));
  return { ...draft, selections, manual };
}

// --- discovered devices ------------------------------------------------------

export const isConfigCandidate = (device) => ["inverter", "grid_meter"].includes(String(device?.role_suggestion || "unknown"));

/** Verified over HTTP and carrying what the config needs; a record without the flag counts as verified. */
export function isAutoConfigReady(device) {
  const ready = device?.usable_for_config !== undefined ? device.usable_for_config : device?.config_ready;
  return device?.verified !== false && Boolean(ready);
}

export const isReady = (device) => Boolean(device?.usable_for_config !== undefined ? device.usable_for_config : device?.config_ready);

/**
 * What discovery found, as one list: mDNS observations, then the scans this
 * browser ran, which a scan reached and so are not stale.
 */
export function aggregate(mdns, scanned) {
  const seen = new Map();
  const merge = (device, fresh) => {
    const key = observationKey(device);
    const existing = seen.get(key);
    if (!existing) {
      seen.set(key, { ...device, sources: sourcesOf(device), ...(fresh ? { stale: false } : {}) });
      return;
    }
    const sources = [...existing.sources];
    for (const source of sourcesOf(device)) if (!sources.includes(source)) sources.push(source);
    seen.set(key, { ...existing, ...device, sources, verified: existing.verified || device.verified, ...(fresh ? { stale: false } : {}) });
  };
  for (const device of Object.values(mdns || {})) merge(device, false);
  for (const device of Object.values(scanned || {})) merge(device, true);
  return [...seen.values()];
}

export const configCandidates = (devices) => devices.filter(isConfigCandidate);
export const supportedGridMeters = (devices) => configCandidates(devices).filter((device) => String(device.role_suggestion) === "grid_meter" && isAutoConfigReady(device));
export const deviceProposals = (proposals) => (proposals || []).filter((proposal) => !isGridMeterProposal(proposal) && proposal.config_fragment);

/** The fingerprint of what discovery found that the draft depends on; volatile fields stay out. */
export function discoverySignature(devices, ignored) {
  const entries = configCandidates(devices)
    .map((device) => ({ id: observationKey(device), role: String(device.role_suggestion || ""), ip: device.ip || "", port: device.port || "", serial_number: device.serial_number || "", device_type: device.device_type || "", api_family: device.api_family || "", verified: device.verified !== false, usable: isAutoConfigReady(device), stale: Boolean(device.stale) }))
    .sort((a, b) => a.id.localeCompare(b.id));
  const skipped = (ignored || []).map((device) => ({ id: observationKey(device), reason: device.reason || "", device_type: device.device_type || "" })).sort((a, b) => a.id.localeCompare(b.id));
  return JSON.stringify({ devices: entries, ignored: skipped });
}

// --- the server's plan ---------------------------------------------------------

/** The plan's entries by the handle each is known under. */
export function planIndex(plan) {
  const index = { drafts: new Map(), selections: new Map(), observations: new Map(), proposals: new Map(), candidates: new Map() };
  const fill = (store, entries, field) => {
    for (const entry of entries || []) {
      const ref = text(entry?.[field]);
      if (ref) index[store].set(ref, entry);
    }
  };
  fill("drafts", plan?.draft_items, "draft_item_id");
  fill("selections", plan?.mqtt_selections, "id");
  fill("observations", plan?.observations, "observation_ref");
  fill("proposals", plan?.proposals, "id");
  fill("candidates", plan?.candidates, "id");
  return index;
}

/** The physical identity the plan issued for an entity; one it could not identify has none. */
export function issuedFor(index, store, ref) {
  const token = text(index?.[store]?.get(text(ref))?.physical_device_id);
  return OPAQUE.test(token) ? token : "";
}

/** The connection the plan placed an entity on, or "" when it has not placed it. */
export function plannedConnection(index, store, ref) {
  const value = text(index?.[store]?.get(text(ref))?.connection_id);
  return CONNECTION.test(value) ? value : "";
}

const sameIssued = (left, right) => Boolean(left && right && left === right);

/** The key a name is remembered under: the issued identity, else the row, connection or observation handle. */
export function nameKey(index, entity, kind) {
  if (!entity) return "";
  if (kind === "draft") return issuedFor(index, "drafts", entity.draft_item_id) || (text(entity.draft_item_id) ? `item:${text(entity.draft_item_id)}` : "");
  if (kind === "selection" || kind === "proposal") return issuedFor(index, kind === "selection" ? "selections" : "proposals", entity.id) || (text(entity.id) ? `conn:${text(entity.id)}` : "");
  return issuedFor(index, "observations", observationKey(entity)) || (text(entity.observation_id) ? `obs:${text(entity.observation_id)}` : "");
}

/** The name a physical inverter already carries in the draft or in memory, so a connection change keeps it. */
export function rememberedName(draft, index, names, physicalId, entity, kind) {
  if (physicalId) {
    const item = inverters(draft).find((entry) => sameIssued(issuedFor(index, "drafts", entry.draft_item_id), physicalId));
    if (item && text(item.config_name)) return text(item.config_name);
    const chosen = deviceSelections(draft).find((entry) => sameIssued(issuedFor(index, "selections", entry.id), physicalId));
    if (chosen && text(chosen.config_name)) return text(chosen.config_name);
    if (names.get(physicalId)) return names.get(physicalId);
  }
  const key = nameKey(index, entity, kind);
  return (key && names.get(key)) || "";
}

/** What the browser holds, for the plan: every draft entry, so a plan authorizes exactly this draft. */
export function planState(draft) {
  return {
    identity_schema_version: IDENTITY_SCHEMA_VERSION,
    draft_items: draft.items.map((item) => ({
      draft_item_id: item.draft_item_id,
      source_id: item.source_id,
      role: item.role === "grid_meter" ? "grid_meter" : "inverter",
      serial_number: item.serial_number,
      physical_identity_token: item.physical_identity_token,
      ip: item.ip,
      port: item.port,
      api_family: item.api_family,
      device_type: item.device_type,
      grid_meter_type: item.grid_meter_type,
      auto_added: item.auto_added === true,
    })),
    mqtt_selections: deviceSelections(draft).map((entry) => ({ id: entry.id, connection_source: entry.connection_source, broker_ref: entry.broker_ref, selection_origin: entry.selection_origin, physical_identity_token: entry.physical_identity_token, physical_identity_alias_tokens: entry.physical_identity_alias_tokens })),
    physical_dismissals: [...draft.dismissedPhysical, ...draft.legacyDismissals],
    observation_dismissals: [...draft.dismissedObservations],
  };
}

/** The cards this browser renders, by handle only: the server reads what is behind each. */
export function planCandidates(devices, proposals) {
  return {
    observations: configCandidates(devices).map((device) => ({ observation_id: text(device?.observation_id), observation_ref: observationKey(device) })),
    proposals: deviceProposals(proposals).map((proposal) => ({ id: text(proposal?.id) })),
  };
}

/** The removals under the ids the plan issued; a legacy value it could not place is kept for a later plan. */
export function adoptDismissals(draft, plan) {
  const dismissals = plan?.dismissals || {};
  const unresolved = (dismissals.unresolved || []).filter(Boolean);
  const physical = (dismissals.physical || []).map((entry) => text(entry?.physical_device_id)).filter((value) => OPAQUE.test(value));
  const observations = [...(dismissals.observations || []).map((entry) => text(entry?.observation_ref)).filter(Boolean), ...unresolved.filter((entry) => entry.scope === "observation").map((entry) => text(entry.value))];
  const legacy = draft.legacyDismissals.length ? unresolved.filter((entry) => entry.scope === "physical").map((entry) => text(entry.value)) : draft.legacyDismissals;
  return { ...draft, dismissedPhysical: physical, legacyDismissals: legacy, dismissedObservations: observations };
}

/**
 * One chosen MQTT connection as Classic stores and sends it: the trusted,
 * secret-free proposal fields the server re-validates, with the inverter's
 * name, kept values and enabled state.
 */
export function serializeSelection(proposal, { target, replaceGridMeter, configValues, enabled, configName } = {}) {
  const resolved = String(target || proposal.target || "device").toLowerCase();
  const grid = resolved === "grid_meter";
  const values = configValues !== undefined ? configValues : proposal.config_values;
  const kept = !grid && values && typeof values === "object" && Object.keys(values).length ? values : undefined;
  const on = enabled !== undefined ? enabled : proposal.enabled;
  return {
    id: String(proposal.id || ""),
    target: resolved,
    config_name: grid ? undefined : has(proposal, "config_name") ? text(proposal.config_name) : configName,
    display_name: proposal.display_name || proposal.hardware_model || "",
    config_fragment: grid ? undefined : proposal.config_fragment,
    grid_meter_fragment: grid ? proposal.grid_meter_fragment : undefined,
    topic_family: proposal.topic_family,
    broker_ref: proposal.broker_ref,
    physical_identity_token: proposal.physical_identity_token,
    physical_identity_alias_tokens: aliasTokens(proposal.physical_identity_alias_tokens),
    serial_number: proposal.serial_number,
    device_id: proposal.device_id,
    seen_topics: Array.isArray(proposal.seen_topics) ? proposal.seen_topics : undefined,
    broker_host: proposal.broker_host,
    broker_port: proposal.broker_port,
    broker_tls: proposal.broker_tls,
    connection_source: proposal.connection_source,
    config_values: kept,
    enabled: on === false ? false : undefined,
    replace_grid_meter: Boolean(replaceGridMeter),
  };
}

/** The chosen MQTT connections as the preview receives them, through the one serializer. */
export function selectionsPayload(draft) {
  return draft.selections.map((entry) => serializeSelection(entry, { target: entry.target, replaceGridMeter: entry.replace_grid_meter }));
}

/** A new draft row for a discovered device; an inverter gets the name it is given. */
export function itemFromDevice(device, role, { id, items, configName }) {
  const sources = sourcesOf(device);
  const common = { source_id: observationKey(device), draft_item_id: id, role, enabled: true, ip: device.ip || "", port: device.port || "", serial_number: device.serial_number || "" };
  if (role === "grid_meter") {
    return { ...common, config_name: "grid_meter", display_name: uniqueDisplayName(items, device.display_name || device.model || DEFAULT_GRID_METER, "grid_meter"), grid_meter_type: "", device_type: device.device_type || "", api_family: device.api_family || "", discovery_source: sources[0] || "" };
  }
  return { ...common, config_name: configName, display_name: uniqueDisplayName(items, device.display_name || device.model || DEFAULT_INVERTER, "inverter"), physical_identity_token: issuedPhysicalIdentity(device), device_type: device.device_type || "", api_family: device.api_family || "", discovery_source: sources[0] || "" };
}

/**
 * The plan's typed operations applied, when the plan is still the current
 * one: rows and connections it drops, connections it selects, observations it
 * adopts. Nothing is decided here. ``newId`` mints a row handle.
 */
export function applyOperations(draft, plan, current, { proposals, available, names, newId }) {
  if (!plan || !current || plan.plan_id !== current.plan_id || plan.generation !== current.generation) return { draft, changed: false };
  const index = planIndex(plan);
  const operations = plan.operations || {};
  let next = draft;
  let changed = false;
  const drop = new Set(operations.drop_draft_items || []);
  if (drop.size) {
    const items = next.items.filter((item) => item.role !== "inverter" || !drop.has(item.draft_item_id));
    if (items.length !== next.items.length) {
      next = { ...next, items };
      changed = true;
    }
  }
  for (const id of operations.drop_mqtt_selections || []) {
    if (selection(next, id)) {
      next = { ...next, selections: dropSelection(next.selections, id) };
      changed = true;
    }
  }
  for (const chosen of operations.select_mqtt_proposals || []) {
    const proposal = (proposals || []).find((candidate) => String(candidate.id || "") === String(chosen.id));
    if (!proposal) continue;
    const fallback = rememberedName(next, index, names, issuedPhysicalIdentity(proposal), proposal, "proposal") || nextName(next);
    const entry = serializeSelection(proposal, { target: "device", configName: fallback });
    entry.config_name = rememberedName(next, index, names, chosen.physical_device_id, proposal, "proposal") || entry.config_name;
    entry.selection_origin = chosen.selection_origin || "automatic";
    entry.display_name = proposal.display_name || proposal.hardware_model || "";
    next = { ...next, selections: putSelection(next.selections, entry) };
    changed = true;
  }
  for (const adoption of operations.adopt_observations || []) {
    const device = available.get(text(adoption.observation_ref));
    if (!device || hasSource(next, observationKey(device))) continue;
    const name = rememberedName(next, index, names, issuedPhysicalIdentity(device), device, "observation") || nextName(next);
    next = { ...next, items: [...next.items, { ...itemFromDevice(device, "inverter", { id: newId(), items: next.items, configName: name }), auto_added: true }] };
    changed = true;
  }
  return { draft: next, changed };
}

/** A discovered connection's state as the plan classified it, with the configured inverter's name. */
export function candidateState(draft, index, candidateId) {
  const planned = index?.candidates?.get(text(candidateId)) || null;
  const state = { state: planned?.state || "new", configuredName: "", currentSource: planned?.current_source || null, candidateId: text(candidateId), currentRef: planned?.current_ref || null };
  if (planned?.current_ref) state.configuredName = configuredName(draft, planned.current_ref);
  return state;
}

export function configuredName(draft, ref) {
  const entry = draft.items.find((item) => item.draft_item_id === ref) || selection(draft, ref);
  return text(entry?.config_name);
}

/** The switches the plan asks the operator about and this browser has not yet confirmed. */
export function pendingConfirmations(plan, confirmed) {
  return (plan?.confirmations || []).filter((entry) => entry?.token && !confirmed.includes(entry.token));
}

/**
 * Add more devices offers connections, not devices: one entry per connection
 * the plan placed, without the ones already chosen.
 */
export function unselectedProposals(draft, proposals, index) {
  const seen = new Set(deviceSelections(draft).map((entry) => plannedConnection(index, "selections", entry.id)).filter(Boolean));
  const out = [];
  for (const proposal of deviceProposals(proposals)) {
    const id = text(proposal.id);
    if (!id || selection(draft, id)) continue;
    const connection = plannedConnection(index, "proposals", id);
    if (connection && seen.has(connection)) continue;
    if (connection) seen.add(connection);
    out.push(proposal);
  }
  return out;
}

/** The draft's inverters as cards: its rows, then chosen MQTT connections the plan does not place on a row's inverter. */
export function inverterCards(draft, index) {
  const cards = [];
  const seen = new Set();
  for (const item of inverters(draft)) {
    cards.push({ kind: "http", key: item.source_id, item });
    const issued = issuedFor(index, "drafts", item.draft_item_id);
    if (issued) seen.add(issued);
  }
  for (const entry of deviceSelections(draft)) {
    const issued = issuedFor(index, "selections", entry.id);
    if (issued && seen.has(issued)) continue;
    cards.push({ kind: "mqtt", key: `mqtt:${entry.id}`, entry });
    if (issued) seen.add(issued);
  }
  return cards;
}

export const selectionControllable = (entry) => Boolean(entry?.config_fragment?.capabilities?.write_output_limit);

/** What a removed, unused or moved card leaves of an inverter's own values: the catalogue values, never its connection. */
export function keptValues(entry) {
  const values = {};
  const source = entry?.config_values;
  if (!source || typeof source !== "object") return values;
  for (const key of Object.keys(source)) if (!has(MAPPED_FIELDS, key)) values[key] = source[key];
  return values;
}

// --- device fields -------------------------------------------------------------

export function catalogSection(catalog, id) {
  return (Array.isArray(catalog?.sections) ? catalog.sections : []).find((section) => section?.id === id) || null;
}

export const deviceFieldKey = (path) => String(path).replace(/^devices\[\]\./, "");

/** A row's value for a catalogue field: its own, else the release template's first device, else the catalogue default. */
export function deviceValue(item, field, template) {
  const key = deviceFieldKey(field.path);
  if (MAPPED_FIELDS[key]) return item[MAPPED_FIELDS[key]];
  if (has(item.config_values, key)) return item.config_values[key];
  const prototype = Array.isArray(template?.devices) ? template.devices[0] || {} : {};
  if (has(prototype, key)) return prototype[key];
  return field.default != null ? field.default : "";
}

export function setDeviceField(item, field, value) {
  const key = deviceFieldKey(field.path);
  if (MAPPED_FIELDS[key]) return { ...item, [MAPPED_FIELDS[key]]: value };
  return { ...item, config_values: { ...(item.config_values && typeof item.config_values === "object" ? item.config_values : {}), [key]: value } };
}

// --- grid meter ----------------------------------------------------------------

export const D0_TYPE = "zendure_smartmeter_d0";
const MQTT_METERS = new Set(["mqtt", D0_TYPE]);
export const meterIsMqtt = (type) => MQTT_METERS.has(type);

/** A row's meter type: the chosen one, else its discovery family, else a Shelly. */
export const gridMeterType = (item) => meterTypeOf(item);

export const d0Topic = (serial) => (text(serial) ? `${D0_TOPIC_PREFIX}${text(serial)}${D0_TOPIC_SUFFIX}` : "");

export function d0SerialFromTopic(topic) {
  const value = text(topic);
  return value.startsWith(D0_TOPIC_PREFIX) && value.endsWith(D0_TOPIC_SUFFIX) ? value.slice(D0_TOPIC_PREFIX.length, value.length - D0_TOPIC_SUFFIX.length) : "";
}

/** Who owns the D0 topic: "auto" follows the serial, "manual" stays; a topic of unknown origin stays. */
export function topicMode(item, topic) {
  if (item?.d0_topic_mode === "auto" || item?.d0_topic_mode === "manual") return item.d0_topic_mode;
  return text(topic) ? "manual" : "auto";
}

/** The feature values a grid meter row implies: its type, and its address or D0 topic. */
export function syncGridFeatures(features, item) {
  const type = gridMeterType(item);
  const meter = { ...item, grid_meter_type: type };
  const next = { ...features };
  next["grid_meter.type"] = type;
  if (type === D0_TYPE) {
    delete next["grid_meter.ip"];
    next["grid_meter.mqtt.payload_format"] = "number";
    const serial = text(meter.serial_number) || d0SerialFromTopic(next["grid_meter.mqtt.topic"]);
    const mode = topicMode(meter, text(next["grid_meter.mqtt.topic"]));
    meter.d0_topic_mode = mode;
    if (mode === "auto" && serial) next["grid_meter.mqtt.topic"] = d0Topic(serial);
  } else {
    next["grid_meter.ip"] = meter.ip || "";
  }
  return { features: next, item: meter };
}

/** A port belongs to its protocol: a meter with another default starts from that default. */
export function portAfterSwitch(port, previous, next) {
  const from = previous ? previous.default_port : null;
  const to = next ? next.default_port : null;
  if (from != null && from === to) return port;
  return to;
}

/** The grid meter section's fields for this type, without type and address, the D0 topic as an advanced read-back. */
export function gridFields(catalog, type) {
  const section = catalogSection(catalog, "grid_meter");
  const variant = catalog?.grid_meter_variants?.[type];
  const allowed = new Set(variant ? variant.fields : []);
  const out = { normal: [], advanced: [], expert: [] };
  for (const field of section?.fields || []) {
    if (field.path === "grid_meter.type" || field.path === "grid_meter.ip" || !allowed.has(field.path)) continue;
    if (["deprecated", "internal"].includes(field.level) || ["safety_gates", "safety_holds", "limits"].includes(field.group)) continue;
    const level = type === D0_TYPE && field.path === "grid_meter.mqtt.topic" ? "advanced" : ["advanced", "expert"].includes(field.level) ? field.level : "normal";
    out[level].push(field);
  }
  return out;
}

/** The value a feature field shows: the changed one, else nothing for a secret, else the catalogue default. */
export function featureValue(features, field) {
  if (has(features, field.path)) return features[field.path];
  if (field.secret) return "";
  return field.default;
}

/** Changed feature values without secrets, which stay in memory: localStorage outlives logout and setup. */
export function storableFeatures(features, catalog) {
  const secret = new Set();
  for (const section of Array.isArray(catalog?.sections) ? catalog.sections : []) for (const field of section?.fields || []) if (field?.secret && typeof field.path === "string") secret.add(field.path);
  return Object.fromEntries(Object.entries(features || {}).filter(([path]) => !secret.has(path)));
}

// --- hand-entered MQTT ---------------------------------------------------------

/** The broker form for the preview: nothing when it is empty; the password only from memory. */
export function brokerPayload(broker, password = "") {
  const host = text(broker?.host);
  const name = text(broker?.name);
  const port = text(broker?.port);
  const username = text(broker?.username);
  if (!host && !name && !username && !password) return null;
  const out = { host };
  if (name) out.name = name;
  if (port) out.port = port;
  out.security = broker?.security || "plain";
  if (username) out.username = username;
  if (password) out.password = password;
  return out;
}

export function manualPayload(manual) {
  return manual.map((device) => ({
    name: device.name || "",
    serial_number: device.serial_number || "",
    mqtt_device_id: device.mqtt_device_id || "",
    hardware_generation: device.hardware_generation || device.generation || "",
    hardware_model: device.hardware_model || device.power_hardware_profile || "",
    product_key: device.product_key || "",
    output_control: device.output_control === true,
  }));
}

/** The generation a form shows: the chosen one, else the catalogue's default, else the first. */
export function formGeneration(catalog, value) {
  const list = generations(catalog);
  return list.find((generation) => generation.id === value) || list.find((generation) => generation.default) || list[0] || null;
}

/**
 * Whether a hand-entered MQTT device would be controlled: a known model that
 * can be, a broker source its generation controls over, and a complete write
 * route. ``missing`` names the part still to type.
 */
export function manualControl(catalog, { generation, model, routeId, productKey }) {
  const chosen = formGeneration(catalog, generation);
  const exact = hardwareModels(catalog).find((entry) => entry.id === model) || null;
  if (!chosen?.id || !exact?.id) return { status: "unknown", supported: false, enabled: false, missing: "" };
  if (!exact.control_supported) return { status: "unavailable", supported: false, enabled: false, missing: "" };
  const sources = Array.isArray(chosen.control_broker_sources) ? chosen.control_broker_sources : [];
  if (!sources.includes(MANUAL_BROKER_SOURCE)) return { status: "source_unavailable", supported: false, enabled: false, missing: "", reason: "broker_source_write_unverified" };
  if (!text(productKey)) return { status: "not_ready", supported: true, enabled: false, missing: "productKey" };
  if (!text(routeId)) return { status: "not_ready", supported: true, enabled: false, missing: "mqttId" };
  return { status: "available", supported: true, enabled: true, missing: "" };
}

/** A hand-entered MQTT device, or the reason it is refused. */
export function manualDevice(catalog, manual, form) {
  const serial = text(form.serial);
  if (!serial) return { error: "serialRequired" };
  const generation = formGeneration(catalog, form.generation);
  if (!generation) return { error: "generationRequired" };
  if (manual.some((device) => device.serial_number === serial)) return { error: "serialTaken" };
  const model = hardwareModels(catalog).find((entry) => entry.id === form.model);
  return {
    device: {
      name: text(form.name),
      serial_number: serial,
      mqtt_device_id: text(form.routeId),
      hardware_generation: generation.id,
      hardware_model: model?.id || "",
      product_key: text(form.productKey),
      output_control: manualControl(catalog, form).enabled,
    },
  };
}

export { modelsForGeneration };

/** A hand-entered device row, or the reason it is refused; a grid meter replaces the one in the draft. */
export function manualItem(draft, catalog, form, id) {
  const host = text(form.host);
  if (!host) return { error: "hostRequired" };
  const role = form.role === "grid_meter" ? "grid_meter" : "inverter";
  const port = text(form.port);
  const sourceId = `manual:${host}:${port}`;
  if (hasSource(draft, sourceId)) return { error: "hostTaken" };
  const variants = Array.isArray(catalog?.hardware_variants?.[role]) ? catalog.hardware_variants[role] : [];
  const variant = variants.find((entry) => entry.id === form.type) || variants.find((entry) => entry.default) || variants[0] || null;
  if (!variant) return { error: "typeRequired" };
  const base = text(form.name) || (role === "grid_meter" ? variant.label || DEFAULT_GRID_METER : DEFAULT_INVERTER);
  const items = role === "grid_meter" ? draft.items.filter((item) => item.role !== "grid_meter") : draft.items;
  const item = {
    source_id: sourceId,
    draft_item_id: id,
    config_name: role === "grid_meter" ? "grid_meter" : nextName(draft),
    display_name: uniqueDisplayName(items, base, role),
    role,
    enabled: true,
    ip: host,
    port,
    serial_number: text(form.serial),
    device_type: "",
    api_family: "",
    discovery_source: "manual",
    manual: true,
    auto_selected: false,
    ...(role === "grid_meter" ? { grid_meter_type: text(variant.id) } : { connection_type: text(variant.id) }),
  };
  return { item, items: [...items, item] };
}

// --- checks and the preview ------------------------------------------------------

/** What the browser can say about the draft before the server has: names, roles and the grid meter. */
export function clientHints(draft, { supportedMeters, gridStale }) {
  const hints = [];
  const names = [...inverterNames(draft), ...draft.items.filter((item) => item.role === "grid_meter").map((item) => item.config_name)];
  const seen = new Set();
  const dupes = new Set();
  for (const name of names) {
    if (!name) continue;
    if (seen.has(name)) dupes.add(name);
    seen.add(name);
  }
  if (names.some((name) => !text(name))) hints.push({ tone: "error", key: "emptyName" });
  for (const name of dupes) hints.push({ tone: "error", key: "duplicateName", values: { name } });
  if (draft.items.length && !inverters(draft).length) hints.push({ tone: "warn", key: "noInverter" });
  const meters = draft.items.filter((item) => item.role === "grid_meter");
  if (!meters.length && supportedMeters >= 2) hints.push({ tone: "warn", key: "chooseGridMeter" });
  else if (draft.items.length && !meters.length) hints.push({ tone: "warn", key: "noGridMeter" });
  if (meters.length > 1) hints.push({ tone: "error", key: "twoGridMeters" });
  if (gridStale) hints.push({ tone: "warn", key: "gridMeterStale" });
  return hints;
}

/** The server's verdict on a preview as hints, in its order: errors, warnings, then notes. */
export function previewHints(preview) {
  const levels = [["errors", "error"], ["warnings", "warn"], ["info", "info"]];
  const hints = levels.flatMap(([level, tone]) => (preview?.validation?.[level] || []).map((issue) => (issue?.key ? { tone, key: issue.key } : { tone, raw: String(issue?.message || "") })));
  const inverterCount = Number(preview?.summary?.inverters || 0);
  if (inverterCount && Number(preview?.summary?.grid_meters || 0) === 1) hints.push({ tone: "info", key: "selected", values: { count: inverterCount } });
  return hints;
}

/** What a refused export says: its validation errors, else its message. */
export function exportError(payload) {
  const errors = Array.isArray(payload?.validation?.errors) ? payload.validation.errors.map((issue) => issue?.message).filter(Boolean) : [];
  return errors.length ? errors.join(" ") : String(payload?.message || "");
}

/**
 * A priority change makes the order decide again: inverters the operator
 * added by hand and connections the operator picked go back to the plan.
 */
export function reassertPriority(draft) {
  const items = draft.items.map((item) => (item.role === "inverter" && item.auto_added === false ? { ...item, auto_added: true } : item));
  const selections = draft.selections.map((entry) => (entry.selection_origin === "manual" ? { ...entry, selection_origin: "priority" } : entry));
  return { ...draft, items, selections };
}

/** Empty, a draft with warnings, one with errors, or ready, as Classic's step chip says it. */
export function configStatus(draft, hints) {
  if (!draft.items.length) return "empty";
  if (hints.some((hint) => hint.tone === "error")) return "needsAttention";
  if (hints.some((hint) => hint.tone === "warn")) return "draft";
  return "valid";
}

/** The body the preview, the download, the apply and the write each send. */
export function previewBody(draft, { workflowId, planId, supportedMeters, password = "" }) {
  return {
    setup_workflow_id: workflowId,
    device_plan_id: planId,
    devices: draft.items,
    supported_grid_meter_count: supportedMeters,
    features: draft.features,
    zendure_mqtt_proposals: selectionsPayload(draft),
    zendure_mqtt_broker: brokerPayload(draft.broker, password),
    zendure_mqtt_manual_devices: manualPayload(draft.manual),
  };
}

/** Which refusal a setup answer is, and so what the reader is asked to do. */
export function conflictKind(data) {
  const code = data?.error;
  if (WORKFLOW_CONFLICTS.includes(code)) return "workflow";
  if (PLAN_CONFLICTS.includes(code)) return "plan";
  if (CONFIG_CONFLICTS.includes(code)) return "config";
  if (code === "setup_operation_in_progress") return "busy";
  return null;
}

export function downloadPassword(password, confirmation) {
  if (String(password || "").length < DOWNLOAD_MIN_PASSWORD) return { error: "downloadShort" };
  if (password !== confirmation) return { error: "downloadDiffer" };
  return { password };
}

export function proposalCanBeChosen(proposal) {
  if (!text(proposal?.id)) return false;
  return isGridMeterProposal(proposal) ? Boolean(proposalTopic(proposal)) : Boolean(proposal.config_fragment);
}

export { isExternalProposal, isGridMeterProposal };
