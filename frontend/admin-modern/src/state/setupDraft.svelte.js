// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminDownloadFile, adminGet, adminSend } from "./auth.svelte.js";
import { setup, goTo, rejoin, setPreviewId, workflowRefused } from "./setup.svelte.js";
import { moveSource, setupDiscovery, toggleSource } from "./setupDiscovery.svelte.js";
import { saveBlob } from "../../../shared/lib/download.js";
import { observationKey } from "../lib/discovery.js";
import {
  STORE,
  applyOperations,
  adoptDismissals,
  clientHints,
  configCandidates,
  configStatus,
  conflictKind,
  deviceFieldKey,
  deviceProposals,
  discoverySignature,
  downloadPassword,
  dropSelection,
  emptyDraft,
  exportError,
  gridMeterItem,
  gridMeterType,
  hasSource,
  isBaseline,
  isGridMeterProposal,
  issuedFor,
  itemFromDevice,
  keptValues,
  manualDevice,
  manualItem,
  nameKey,
  nextName,
  opaqueIds,
  planCandidates,
  planIndex,
  planState,
  portAfterSwitch,
  previewBody,
  previewHints,
  proposalCanBeChosen,
  putSelection,
  readStore,
  reassertPriority,
  rememberedName,
  selection,
  serializeSelection,
  setDeviceField,
  storableFeatures,
  storeEnvelope,
  supportedGridMeters,
  syncGridFeatures,
  withItemIds,
  withNames,
  withValue,
  withoutValue,
} from "../lib/setupDraft.js";

const PREVIEW_DELAY_MS = 100;

function stored(key) {
  try {
    return globalThis.localStorage?.getItem(key) || null;
  } catch {
    return null;
  }
}

function json(key, fallback) {
  try {
    const value = JSON.parse(stored(key) || "null");
    return value ?? fallback;
  } catch {
    return fallback;
  }
}

function write(key, value) {
  try {
    if (value === null) globalThis.localStorage?.removeItem(key);
    else globalThis.localStorage?.setItem(key, value);
  } catch {
    return;
  }
}

const isMapping = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);

function loadDraft() {
  const manual = json(STORE.manual, []);
  const broker = json(STORE.broker, {});
  const features = json(STORE.features, {});
  return withNames({
    items: withItemIds(readStore(stored(STORE.items))),
    selections: readStore(stored(STORE.selections)).filter((entry) => isMapping(entry) && entry.id != null),
    manual: Array.isArray(manual) ? manual.filter(isMapping) : [],
    broker: isMapping(broker) ? broker : {},
    features: isMapping(features) ? features : {},
    dismissedObservations: readStore(stored(STORE.dismissedObservations)),
    dismissedPhysical: opaqueIds(readStore(stored(STORE.dismissedPhysical))),
    legacyDismissals: readStore(stored(STORE.legacyDismissals)).map((value) => String(value ?? "").trim()).filter(Boolean),
  });
}

function emptyPlan() {
  const operations = { drop_draft_items: [], drop_mqtt_selections: [], select_mqtt_proposals: [], adopt_observations: [] };
  return { identity_schema_version: 0, plan_id: "", generation: "", observations: [], proposals: [], candidates: [], draft_items: [], mqtt_selections: [], dismissals: { physical: [], observations: [], unresolved: [] }, groups: [], operations, proposed_operations: operations, confirmations: [], confirmation_required: false, unresolved_references: [], warnings: [] };
}

/**
 * Guided Setup's hardware draft and the server's answers about it: the device
 * plan that authorizes it and the preview that turns it into a config. Kept
 * in Classic's stores, so the draft survives a reload and a change of console.
 */
class SetupDraft {
  draft = $state.raw(loadDraft());
  brokerPassword = $state("");
  baseline = $state.raw(isBaseline(json(STORE.baseline, null)) ? json(STORE.baseline, null) : null);
  catalog = $state.raw(null);
  catalogError = $state.raw(null);
  template = $state.raw(null);
  plan = $state.raw(emptyPlan());
  confirmedSwitches = $state.raw([]);
  declinedSwitches = $state.raw([]);
  preview = $state.raw(null);
  previewing = $state(false);
  conflict = $state.raw(null);
  workflowMessage = $state("");
  notice = $state.raw(null);
  exportStatus = $state.raw(null);
  applyStatus = $state.raw(null);
  rollback = $state.raw(null);
  busy = $state("");
  navError = $state.raw(null);
  open = $state.raw([]);
  names = new Map();
  ports = new Map();
  ticket = 0;
  previewRequest = 0;
  previewPlanId = "";
  timer = null;
  planTimer = null;
  planRetried = false;
  answerGeneration = "";
  signature = null;
  sequence = 0;

  index = $derived(planIndex(this.plan));
  devices = $derived(setupDiscovery.devices);
  candidates = $derived(configCandidates(this.devices));
  available = $derived(new Map(this.candidates.map((device) => [observationKey(device), device])));
  meters = $derived(supportedGridMeters(this.devices));
  grid = $derived(gridMeterItem(this.draft));
  gridStale = $derived(Boolean(this.grid && !this.grid.manual && (!this.available.get(this.grid.source_id) || this.available.get(this.grid.source_id).stale)));
  hints = $derived(this.preview ? previewHints(this.preview) : clientHints(this.draft, { supportedMeters: this.meters.length, gridStale: this.gridStale }));
  status = $derived(configStatus(this.draft, this.hints));
  exportAllowed = $derived(Boolean(this.preview?.ready && setup.previewId));
}

export const setupDraft = new SetupDraft();

function save() {
  const { draft } = setupDraft;
  write(STORE.items, storeEnvelope(draft.items));
  write(STORE.selections, storeEnvelope(draft.selections));
  write(STORE.manual, JSON.stringify(draft.manual));
  write(STORE.broker, JSON.stringify({ name: String(draft.broker.name || "").trim(), host: String(draft.broker.host || "").trim(), port: String(draft.broker.port || "").trim(), security: draft.broker.security || "plain", username: String(draft.broker.username || "").trim() }));
  write(STORE.features, JSON.stringify(storableFeatures(draft.features, setupDraft.catalog)));
  write(STORE.dismissedObservations, storeEnvelope(draft.dismissedObservations));
  write(STORE.dismissedPhysical, storeEnvelope(draft.dismissedPhysical));
  write(STORE.legacyDismissals, draft.legacyDismissals.length ? storeEnvelope(draft.legacyDismissals) : null);
}

function commit(next) {
  setupDraft.draft = next;
  save();
}

function setBaseline(value) {
  setupDraft.baseline = isBaseline(value) ? value : null;
  write(STORE.baseline, setupDraft.baseline ? JSON.stringify(setupDraft.baseline) : null);
}

function newId() {
  setupDraft.sequence += 1;
  return `item-${Date.now().toString(36)}-${setupDraft.sequence}`;
}

function context() {
  return { proposals: setupDiscovery.proposals, available: setupDraft.available, names: setupDraft.names, newId };
}

function remember(entity, kind, name) {
  const value = String(name || "").trim();
  const key = nameKey(setupDraft.index, entity, kind);
  if (value && key) setupDraft.names.set(key, value);
}

function forgetName(entity, kind) {
  const key = nameKey(setupDraft.index, entity, kind);
  if (key) setupDraft.names.delete(key);
}

function dismissPhysical(draft, physicalId) {
  return { ...draft, dismissedPhysical: withValue(draft.dismissedPhysical, String(physicalId || "").trim()) };
}

function undismiss(draft, { observation, physical }) {
  return { ...draft, dismissedObservations: observation ? withoutValue(draft.dismissedObservations, observation) : draft.dismissedObservations, dismissedPhysical: physical ? withoutValue(draft.dismissedPhysical, physical) : draft.dismissedPhysical };
}

/** The setup catalogue and the template of the confirmed build. */
export async function loadCatalog() {
  try {
    setupDraft.catalog = await adminGet("/api/setup/config/catalog");
    setupDraft.catalogError = null;
  } catch (error) {
    setupDraft.catalog = null;
    setupDraft.catalogError = error;
  }
}

// --- the device plan -------------------------------------------------------------

async function requestPlan(options = {}) {
  const ticket = ++setupDraft.ticket;
  let payload = null;
  try {
    payload = await adminSend("/api/setup/device-plan", {
      body: {
        state: planState(setupDraft.draft),
        candidates: planCandidates(setupDraft.devices, setupDiscovery.proposals),
        confirmed_switches: [...setupDraft.confirmedSwitches],
        declined_switches: [...setupDraft.declinedSwitches],
        ...(options.switch ? { switch: options.switch } : {}),
      },
    });
  } catch {
    return null;
  }
  if (ticket !== setupDraft.ticket || !isMapping(payload)) return null;
  const plan = { ...emptyPlan(), ...payload };
  setupDraft.plan = plan;
  const adopted = adoptDismissals(setupDraft.draft, plan);
  if (JSON.stringify([adopted.dismissedPhysical, adopted.legacyDismissals, adopted.dismissedObservations]) !== JSON.stringify([setupDraft.draft.dismissedPhysical, setupDraft.draft.legacyDismissals, setupDraft.draft.dismissedObservations])) commit(adopted);
  const generation = String(plan.generation || "");
  if (generation !== setupDraft.answerGeneration) {
    setupDraft.answerGeneration = generation;
    setupDraft.confirmedSwitches = [];
    setupDraft.declinedSwitches = [];
  }
  return plan;
}

/**
 * One pass: ask for a plan and apply its operations; when they changed the
 * draft, ask once more so the cards show the settled answer. A preview made
 * under another plan is made again.
 */
export async function refreshPlan() {
  const plan = await requestPlan();
  if (!plan) return false;
  const first = applyOperations(setupDraft.draft, plan, setupDraft.plan, context());
  if (first.changed) {
    commit(first.draft);
    const settled = await requestPlan();
    if (settled) {
      const again = applyOperations(setupDraft.draft, settled, setupDraft.plan, context());
      if (again.changed) commit(again.draft);
    }
  }
  if (first.changed || setupDraft.previewPlanId !== setupDraft.plan.plan_id) schedulePreview();
  return first.changed;
}

/** An edit to what the plan binds (an address, a port, a serial), re-planned once the typing pauses. */
function replanSoon() {
  setupDraft.planRetried = false;
  save();
  revokePreview();
  clearTimeout(setupDraft.planTimer);
  setupDraft.planTimer = setTimeout(replan, PREVIEW_DELAY_MS * 3);
}

/** The plan first, then a preview under it: one that went out before the plan would be refused. */
async function replan() {
  revokePreview();
  const before = setupDraft.previewRequest;
  await refreshPlan();
  if (setupDraft.previewRequest === before) schedulePreview();
}

/**
 * What discovery found reached the draft: a single supported grid meter is
 * chosen when none is, and the plan decides everything about the inverters.
 */
export function syncFromDiscovery() {
  const { draft } = setupDraft;
  const meters = setupDraft.meters;
  if (!gridMeterItem(draft) && meters.length === 1 && !draft.dismissedObservations.includes(observationKey(meters[0]))) {
    const synced = syncGridFeatures(draft.features, { ...itemFromDevice(meters[0], "grid_meter", { id: newId(), items: draft.items }), auto_selected: true });
    commit({ ...draft, items: [...draft.items, synced.item], features: synced.features });
  }
  setupDraft.planRetried = false;
  return replan();
}

/** Called with what discovery holds now; re-plans only when the draft's view of it changed. */
export function discoveryChanged() {
  const signature = `${discoverySignature(setupDraft.devices, setupDiscovery.ignored)}|${JSON.stringify(deviceProposals(setupDiscovery.proposals).map((proposal) => proposal.id))}`;
  if (signature === setupDraft.signature) return;
  setupDraft.signature = signature;
  syncFromDiscovery();
}

function changed({ plan = false } = {}) {
  setupDraft.planRetried = false;
  save();
  if (plan) replan();
  else schedulePreview();
}

// --- draft changes -----------------------------------------------------------------

/** A discovered device added by hand; a grid meter replaces the one in the draft. */
export function addDevice(sourceId, role) {
  const device = setupDraft.available.get(sourceId);
  if (!device) return;
  let draft = undismiss(setupDraft.draft, { observation: sourceId, physical: issuedFor(setupDraft.index, "observations", sourceId) });
  if (role === "grid_meter") {
    setupDraft.draft = draft;
    useGridMeter(sourceId);
    return;
  }
  if (hasSource(draft, sourceId)) return;
  const name = rememberedName(draft, setupDraft.index, setupDraft.names, issuedFor(setupDraft.index, "observations", sourceId), device, "observation") || nextName(draft);
  draft = { ...draft, items: [...draft.items, { ...itemFromDevice(device, "inverter", { id: newId(), items: draft.items, configName: name }), auto_added: false }] };
  setupDraft.draft = draft;
  changed({ plan: true });
}

export function useGridMeter(sourceId) {
  const device = setupDraft.available.get(sourceId);
  if (!device) return;
  const draft = undismiss(setupDraft.draft, { observation: sourceId });
  const items = draft.items.filter((item) => item.role !== "grid_meter");
  const synced = syncGridFeatures(draft.features, { ...itemFromDevice(device, "grid_meter", { id: newId(), items }), auto_selected: false });
  setupDraft.draft = { ...draft, items: [...items, synced.item], features: synced.features };
  changed({ plan: true });
}

/** A row removed; it stays removed, by observation and by inverter, until it is added again. */
export function removeItem(sourceId) {
  const removed = setupDraft.draft.items.find((item) => item.source_id === sourceId);
  let draft = { ...setupDraft.draft, dismissedObservations: withValue(setupDraft.draft.dismissedObservations, sourceId) };
  if (removed) {
    draft = dismissPhysical(draft, issuedFor(setupDraft.index, "drafts", removed.draft_item_id));
    forgetName(removed, "draft");
  }
  setupDraft.draft = { ...draft, items: draft.items.filter((item) => item.source_id !== sourceId) };
  setupDraft.open = withoutValue(setupDraft.open, sourceId);
  changed({ plan: true });
}

export function removeSelection(id) {
  const entry = selection(setupDraft.draft, id);
  if (!entry) return;
  const draft = dismissPhysical(setupDraft.draft, issuedFor(setupDraft.index, "selections", entry.id));
  forgetName(entry, "selection");
  setupDraft.draft = { ...draft, selections: dropSelection(draft.selections, id) };
  changed({ plan: true });
}

/** An MQTT connection from Add more devices, chosen by hand. */
export function addProposal(id) {
  if (selection(setupDraft.draft, id)) return;
  const proposal = deviceProposals(setupDiscovery.proposals).find((candidate) => String(candidate.id || "") === String(id));
  if (!proposal) return;
  const physical = issuedFor(setupDraft.index, "proposals", id);
  const draft = undismiss(setupDraft.draft, { physical });
  const name = rememberedName(draft, setupDraft.index, setupDraft.names, physical, proposal, "proposal") || nextName(draft);
  const entry = { ...serializeSelection(proposal, { target: "device", configName: name }), selection_origin: "manual", display_name: proposal.display_name || proposal.hardware_model || "" };
  setupDraft.draft = { ...draft, selections: putSelection(draft.selections, entry) };
  changed({ plan: true });
}

/** Whether choosing this MQTT grid meter replaces a grid meter the draft has from the network. */
export function replacesGridMeter(proposal) {
  return isGridMeterProposal(proposal) && Boolean(setupDraft.grid && setupDraft.grid.enabled);
}

/**
 * A proposal from the Devices step added to or taken out of the draft. An
 * MQTT grid meter is the only one: it drops another chosen MQTT grid meter,
 * and replaces a network one only when ``replace`` says the reader agreed.
 */
export function toggleProposal(id, { replace = false } = {}) {
  const key = String(id);
  if (selection(setupDraft.draft, key)) {
    setupDraft.draft = { ...setupDraft.draft, selections: dropSelection(setupDraft.draft.selections, key) };
    changed();
    return;
  }
  const proposal = setupDiscovery.proposals.find((candidate) => String(candidate.id || "") === key);
  if (!proposal || !proposalCanBeChosen(proposal)) return;
  const grid = isGridMeterProposal(proposal);
  let draft = setupDraft.draft;
  if (grid) {
    const previous = draft.selections.find((entry) => String(entry.target || "").toLowerCase() === "grid_meter");
    if (previous && String(previous.id) !== key) draft = { ...draft, selections: dropSelection(draft.selections, previous.id) };
  }
  const physical = grid ? "" : issuedFor(setupDraft.index, "proposals", key);
  const name = grid ? undefined : rememberedName(draft, setupDraft.index, setupDraft.names, physical, proposal, "proposal") || nextName(draft);
  const entry = { ...serializeSelection(proposal, { target: grid ? "grid_meter" : "device", replaceGridMeter: grid && replace, configName: name }), selection_origin: "manual", display_name: proposal.display_name || proposal.hardware_model || "" };
  if (!grid) draft = undismiss(draft, { physical });
  setupDraft.draft = { ...draft, selections: putSelection(draft.selections, entry) };
  if (grid) changed();
  else {
    save();
    syncFromDiscovery();
  }
}

/**
 * Moves an inverter to another connection the plan offers, keeping its name,
 * values and enabled state. The server's verdict decides; a refusal leaves
 * the draft as it was and says why.
 */
export async function switchConnection(candidateId) {
  const ref = String(candidateId ?? "").trim();
  setupDraft.notice = null;
  const state = setupDraft.index.candidates.get(ref);
  if (!ref || state?.state !== "alternative" || !state.current_ref) return;
  const plan = await requestPlan({ switch: { current_ref: state.current_ref, candidate_id: ref } });
  const verdict = plan?.switch?.plan;
  if (!verdict) return;
  if (verdict.action !== "use_candidate") {
    setupDraft.notice = { kind: "switchBlocked", reason: String(verdict.reason || "") };
    return;
  }
  const current = setupDraft.index.candidates.get(ref) || state;
  applySwitch(verdict, current);
  await refreshPlan();
}

function applySwitch(verdict, state) {
  const physicalId = String(verdict.physical_device_id || "");
  const fromRow = state.current_source === "local_api";
  let draft = setupDraft.draft;
  const current = fromRow ? draft.items.find((item) => item.draft_item_id === state.current_ref) : selection(draft, state.current_ref);
  const name = rememberedName(draft, setupDraft.index, setupDraft.names, physicalId, current, fromRow ? "draft" : "selection") || nextName(draft);
  const values = keptValues(current);
  const enabled = current ? current.enabled !== false : true;
  const target = state.kind === "observation" ? setupDraft.available.get(String(state.id)) : setupDiscovery.proposals.find((candidate) => String(candidate.id || "") === String(state.id));
  if (!target) return;
  draft = fromRow ? { ...draft, items: draft.items.filter((item) => item.draft_item_id !== state.current_ref) } : { ...draft, selections: dropSelection(draft.selections, state.current_ref) };
  draft = undismiss(draft, { physical: physicalId });
  if (state.kind === "observation") {
    const sourceId = observationKey(target);
    draft = undismiss(draft, { observation: sourceId });
    const item = { ...itemFromDevice(target, "inverter", { id: newId(), items: draft.items, configName: name }), config_values: values, enabled, auto_added: false };
    draft = { ...draft, items: [...draft.items, item] };
    remember(item, "draft", name);
  } else {
    const entry = { ...serializeSelection(target, { target: "device", configValues: values, enabled, configName: name }), config_name: name, selection_origin: "manual", display_name: target.display_name || target.hardware_model || "" };
    draft = { ...draft, selections: putSelection(draft.selections, entry) };
    remember(entry, "selection", name);
  }
  if (physicalId) setupDraft.names.set(physicalId, name);
  commit(draft);
}

/** The operator's answer to a switch the plan asked about; it reaches the server with the next plan. */
export function answerSwitch(token, accepted) {
  const value = String(token || "");
  if (!value) return undefined;
  setupDraft.confirmedSwitches = accepted ? withValue(setupDraft.confirmedSwitches, value) : withoutValue(setupDraft.confirmedSwitches, value);
  setupDraft.declinedSwitches = accepted ? withoutValue(setupDraft.declinedSwitches, value) : withValue(setupDraft.declinedSwitches, value);
  return refreshPlan();
}

export function moveItem(sourceId, delta) {
  const items = [...setupDraft.draft.items];
  const index = items.findIndex((item) => item.source_id === sourceId);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= items.length) return;
  const [item] = items.splice(index, 1);
  items.splice(target, 0, item);
  setupDraft.draft = { ...setupDraft.draft, items };
  changed({ plan: true });
}

export function resetItemName(sourceId) {
  const item = setupDraft.draft.items.find((entry) => entry.source_id === sourceId);
  if (!item) return;
  const name = item.role === "grid_meter" ? "grid_meter" : nextName(setupDraft.draft, item);
  if (item.role !== "grid_meter") remember(item, "draft", name);
  setupDraft.draft = { ...setupDraft.draft, items: setupDraft.draft.items.map((entry) => (entry === item ? { ...entry, config_name: name } : entry)) };
  changed({ plan: true });
}

export function resetSelectionName(id) {
  const entry = selection(setupDraft.draft, id);
  if (!entry) return;
  const name = nextName(setupDraft.draft, entry);
  remember(entry, "selection", name);
  setupDraft.draft = { ...setupDraft.draft, selections: putSelection(setupDraft.draft.selections, { ...entry, config_name: name }) };
  changed();
}

function updateItem(sourceId, change) {
  setupDraft.draft = { ...setupDraft.draft, items: setupDraft.draft.items.map((item) => (item.source_id === sourceId ? change(item) : item)) };
}

export function setItemEnabled(sourceId, enabled) {
  updateItem(sourceId, (item) => ({ ...item, enabled }));
  changed();
}

export function setItemField(sourceId, field, value) {
  updateItem(sourceId, (item) => setDeviceField(item, field, value));
  if (["ip", "sn"].includes(deviceFieldKey(field.path))) replanSoon();
  else changed();
}

export function setSelectionEnabled(id, enabled) {
  const entry = selection(setupDraft.draft, id);
  if (!entry) return;
  setupDraft.draft = { ...setupDraft.draft, selections: putSelection(setupDraft.draft.selections, { ...entry, enabled }) };
  changed();
}

export function setSelectionName(id, name) {
  const entry = selection(setupDraft.draft, id);
  if (!entry) return;
  remember(entry, "selection", name);
  setupDraft.draft = { ...setupDraft.draft, selections: putSelection(setupDraft.draft.selections, { ...entry, config_name: name }) };
  changed();
}

// --- the grid meter ----------------------------------------------------------------

export function setGridEnabled(enabled) {
  const grid = setupDraft.grid;
  if (!grid) return;
  updateItem(grid.source_id, (item) => ({ ...item, enabled }));
  changed();
}

/** Another meter type: the port follows the type, unless this card had one for it before. */
export function setGridType(type) {
  const grid = setupDraft.grid;
  if (!grid) return;
  const variants = setupDraft.catalog?.grid_meter_variants || {};
  const from = gridMeterType(grid);
  const remembered = { ...(setupDraft.ports.get(grid.source_id) || {}) };
  if (grid.port != null && from) remembered[from] = grid.port;
  setupDraft.ports.set(grid.source_id, remembered);
  const port = Object.prototype.hasOwnProperty.call(remembered, type) ? remembered[type] : portAfterSwitch(grid.port, variants[from], variants[type]);
  const meter = { ...grid, grid_meter_type: type };
  if (port == null) delete meter.port;
  else meter.port = port;
  const synced = syncGridFeatures(setupDraft.draft.features, meter);
  setupDraft.draft = { ...setupDraft.draft, items: setupDraft.draft.items.map((item) => (item.source_id === grid.source_id ? synced.item : item)), features: synced.features };
  save();
  replan();
}

/** The meter's address, port or D0 serial; the serial makes the generated D0 topic. */
export function setGridEndpoint(key, value) {
  const grid = setupDraft.grid;
  if (!grid) return;
  let meter = { ...grid, [key]: value };
  let features = setupDraft.draft.features;
  if (key === "ip") features = { ...features, "grid_meter.ip": value };
  if (key === "serial_number") ({ features, item: meter } = syncGridFeatures(features, meter));
  setupDraft.draft = { ...setupDraft.draft, items: setupDraft.draft.items.map((item) => (item.source_id === grid.source_id ? meter : item)), features };
  replanSoon();
}

/** A grid meter setting; a D0 topic typed by hand is the operator's from then on. */
export function setGridFeature(path, value) {
  const grid = setupDraft.grid;
  if (grid && path === "grid_meter.mqtt.topic") updateItem(grid.source_id, (item) => ({ ...item, d0_topic_mode: "manual" }));
  setFeature(path, value);
}

export function useGeneratedTopic() {
  const grid = setupDraft.grid;
  if (!grid) return;
  const features = { ...setupDraft.draft.features };
  delete features["grid_meter.mqtt.topic"];
  const synced = syncGridFeatures(features, { ...grid, d0_topic_mode: "auto" });
  setupDraft.draft = { ...setupDraft.draft, items: setupDraft.draft.items.map((item) => (item.source_id === grid.source_id ? synced.item : item)), features: synced.features };
  changed();
}

export function setFeature(path, value) {
  setupDraft.draft = { ...setupDraft.draft, features: { ...setupDraft.draft.features, [path]: value } };
  changed();
}

// --- hand-entered devices ------------------------------------------------------------

/** A device typed by hand; answers the reason it was refused, or "". */
export function addManualItem(form) {
  const result = manualItem(setupDraft.draft, setupDraft.catalog, form, newId());
  if (result.error) return result.error;
  let draft = { ...setupDraft.draft, items: result.items, dismissedObservations: withoutValue(setupDraft.draft.dismissedObservations, result.item.source_id) };
  if (result.item.role === "grid_meter") {
    const synced = syncGridFeatures(draft.features, result.item);
    draft = { ...draft, features: synced.features, items: draft.items.map((item) => (item === result.item ? synced.item : item)) };
  }
  setupDraft.draft = draft;
  changed({ plan: true });
  return "";
}

/** A Zendure MQTT device typed by hand; answers the reason it was refused, or "". */
export function addManualDevice(form) {
  const result = manualDevice(setupDraft.catalog, setupDraft.draft.manual, form);
  if (result.error) return result.error;
  setupDraft.draft = { ...setupDraft.draft, manual: [...setupDraft.draft.manual, { ...result.device, name: result.device.name || nextName(setupDraft.draft) }] };
  changed();
  return "";
}

export function removeManualDevice(index) {
  if (index < 0 || index >= setupDraft.draft.manual.length) return;
  setupDraft.draft = { ...setupDraft.draft, manual: setupDraft.draft.manual.filter((_, position) => position !== index) };
  changed();
}

export function setBroker(key, value) {
  setupDraft.draft = { ...setupDraft.draft, broker: { ...setupDraft.draft.broker, [key]: value } };
  changed();
}

export function setBrokerPassword(value) {
  setupDraft.brokerPassword = value;
  schedulePreview();
}

/** The next free inverter name, for a form that offers one. */
export function suggestedName() {
  return nextName(setupDraft.draft);
}

// --- source priority -------------------------------------------------------------------

/** A source moved in the priority: hand-made connection choices go back to the order, then the plan follows. */
export async function reprioritize(source, delta) {
  commit(reassertPriority(setupDraft.draft));
  await moveSource(source, delta);
  await syncFromDiscovery();
}

export async function enableSource(source, enabled) {
  await toggleSource(source, enabled);
  await syncFromDiscovery();
}

// --- clearing ----------------------------------------------------------------------------

function clearMqtt(draft) {
  setupDraft.names.clear();
  setupDraft.brokerPassword = "";
  return { ...draft, selections: [], manual: [], broker: {} };
}

/**
 * Clears the draft: every discovered device and connection stays removed
 * until it is added again. Nothing on the system changes.
 */
export function clearDraft() {
  let draft = setupDraft.draft;
  for (const device of setupDraft.candidates) {
    draft = { ...draft, dismissedObservations: withValue(draft.dismissedObservations, observationKey(device)) };
    draft = dismissPhysical(draft, issuedFor(setupDraft.index, "observations", observationKey(device)));
  }
  for (const proposal of deviceProposals(setupDiscovery.proposals)) draft = dismissPhysical(draft, issuedFor(setupDraft.index, "proposals", proposal.id));
  setBaseline(null);
  setupDraft.conflict = null;
  setupDraft.draft = clearMqtt({ ...draft, items: [] });
  changed();
}

/** This tab's draft dropped, then the current workflow rejoined; the newer session's server state stays. */
export async function discardLocal() {
  setBaseline(null);
  setupDraft.draft = { ...clearMqtt(setupDraft.draft), items: [], features: {} };
  save();
  await rejoinCurrent();
}

/** Rejoins the workflow the server has; the draft is kept and previewed again under it. */
export async function rejoinCurrent() {
  setupDraft.planRetried = false;
  await rejoin();
  setupDraft.workflowMessage = "";
  setupDraft.conflict = null;
  if (!setup.stale) schedulePreview();
}

/** Everything gone, for a setup started over: the stores, the plan and the answers. */
export function resetDraft() {
  clearTimeout(setupDraft.timer);
  clearTimeout(setupDraft.planTimer);
  setupDraft.previewRequest += 1;
  setupDraft.ticket += 1;
  setupDraft.names.clear();
  setupDraft.ports.clear();
  setupDraft.draft = emptyDraft();
  setupDraft.brokerPassword = "";
  setupDraft.plan = emptyPlan();
  setupDraft.confirmedSwitches = [];
  setupDraft.declinedSwitches = [];
  setupDraft.preview = null;
  setupDraft.conflict = null;
  setupDraft.notice = null;
  setupDraft.exportStatus = null;
  setupDraft.applyStatus = null;
  setupDraft.rollback = null;
  setupDraft.navError = null;
  setupDraft.signature = null;
  setupDraft.open = [];
  setBaseline(null);
  for (const key of Object.values(STORE)) write(key, null);
}

export function toggleOpen(key) {
  setupDraft.open = setupDraft.open.includes(key) ? withoutValue(setupDraft.open, key) : [...setupDraft.open, key];
}

// --- the preview -------------------------------------------------------------------------

/** Any change takes the issued preview's authority away at once; a new one is asked for shortly. */
function revokePreview() {
  setupDraft.previewRequest += 1;
  clearTimeout(setupDraft.timer);
  setupDraft.timer = null;
  setupDraft.preview = null;
  setupDraft.previewing = true;
  setupDraft.exportStatus = null;
  setPreviewId(null);
}

export function schedulePreview() {
  revokePreview();
  setupDraft.timer = setTimeout(requestPreview, PREVIEW_DELAY_MS);
}

function body(extra = {}) {
  return { ...previewBody(setupDraft.draft, { workflowId: setup.workflowId, planId: setupDraft.plan.plan_id, supportedMeters: setupDraft.meters.length, password: setupDraft.brokerPassword }), ...extra };
}

function workflowConflict(payload) {
  revokePreview();
  setupDraft.previewing = false;
  setupDraft.workflowMessage = String(payload?.message || "");
  workflowRefused({ payload });
}

/**
 * The plan no longer authorizes the draft: say so, and plan and preview once
 * more for it. One retry per change, so a draft the server keeps refusing
 * stays refused instead of asking again and again.
 */
function planConflict(payload) {
  setPreviewId(null);
  setupDraft.preview = { ready: false, validation: { errors: [payload?.message ? { message: payload.message } : { key: "planChanged" }], warnings: [], info: [] } };
  if (setupDraft.previewPlanId !== setupDraft.plan.plan_id || setupDraft.planRetried) return;
  setupDraft.planRetried = true;
  replan();
}

async function requestPreview() {
  if (setup.stale) {
    setupDraft.previewing = false;
    return;
  }
  const request = ++setupDraft.previewRequest;
  const generation = setup.generation;
  setupDraft.previewPlanId = setupDraft.plan.plan_id;
  setupDraft.previewing = true;
  try {
    const data = await adminSend("/api/setup/config-preview", { body: body() });
    if (setup.stale) setupDraft.previewing = false;
    if (setup.stale || request !== setupDraft.previewRequest || generation !== setup.generation) return;
    setupDraft.preview = data || { ready: false, validation: { errors: [], warnings: [], info: [] } };
    setupDraft.planRetried = false;
    setPreviewId(data?.config_preview_id || null);
    setBaseline(data?.config_revision);
    setupDraft.conflict = null;
    setupDraft.previewing = false;
  } catch (error) {
    if (setup.stale || request !== setupDraft.previewRequest) return;
    setupDraft.previewing = false;
    const payload = error?.payload;
    const kind = conflictKind(payload);
    if (kind === "workflow") return workflowConflict(payload);
    if (kind === "plan") return planConflict(payload);
    setupDraft.preview = { ready: false, unavailable: true, validation: { errors: [{ message: exportError(payload) || error?.message || String(error) }], warnings: [], info: [] } };
  }
}

/** A refused preview against a moved live config: a new one, never the old id replayed. */
export async function reviewAgain() {
  setupDraft.planRetried = false;
  setupDraft.conflict = null;
  setBaseline(null);
  revokePreview();
  await requestPreview();
}

/** What a refused config write says, and the panel it opens. */
function refusal(error, fallback) {
  const payload = error?.payload;
  const kind = conflictKind(payload);
  if (kind === "workflow") {
    workflowConflict(payload);
    return { tone: "error", key: "stale" };
  }
  if (kind === "plan") {
    planConflict(payload);
    return payload?.message ? { tone: "error", raw: payload.message } : { tone: "error", key: "planChanged" };
  }
  if (kind === "config") {
    setPreviewId(null);
    setupDraft.conflict = payload;
    return { tone: "error", key: `conflict.${payload.error}` };
  }
  if (kind === "busy") return { tone: "error", key: "busy", operation: String(payload?.operation || "") };
  return { tone: "error", raw: exportError(payload) || error?.message || fallback };
}

export async function downloadConfig(password, confirmation) {
  const secret = downloadPassword(password, confirmation);
  if (secret.error) {
    setupDraft.exportStatus = { tone: "error", key: secret.error };
    return false;
  }
  setupDraft.busy = "download";
  setupDraft.exportStatus = { tone: "info", key: "preparing" };
  try {
    const { blob, name } = await adminDownloadFile("/api/setup/config/download", body({ config_preview_id: setup.previewId, overwrite: false, password: secret.password }));
    saveBlob(blob, name || "ems-config.tar.gz.enc");
    setupDraft.exportStatus = { tone: "ok", key: "downloaded" };
    return true;
  } catch (error) {
    setupDraft.exportStatus = refusal(error, "download failed");
    return false;
  } finally {
    setupDraft.busy = "";
  }
}

/** Writes the generated config to the EMS installation; an existing one is backed up first. */
export async function applyConfig() {
  setupDraft.busy = "apply";
  setupDraft.rollback = null;
  setupDraft.applyStatus = { tone: "info", key: "applying" };
  try {
    const data = await adminSend("/api/setup/config/apply", { body: body({ config_preview_id: setup.previewId, overwrite: false }) });
    setupDraft.rollback = data?.credential_rollback || null;
    if (data?.ok !== true) throw Object.assign(new Error("apply"), { payload: data || {} });
    setupDraft.applyStatus = { tone: "ok", key: data.created ? "created" : "applied", values: { path: data.path || "" }, backup: data.backup_path || "", preserved: !data.created && !data.backup_path };
  } catch (error) {
    setupDraft.rollback = error?.payload?.credential_rollback || setupDraft.rollback;
    setupDraft.applyStatus = refusal(error, "apply failed");
  } finally {
    setupDraft.busy = "";
  }
}

/**
 * Saves the generated config for the deployment and opens the next step;
 * answers whether it was saved.
 */
export async function writeConfig() {
  if (!setupDraft.preview?.ready) {
    setupDraft.navError = { key: "fixFirst" };
    return null;
  }
  setupDraft.navError = null;
  setupDraft.busy = "write";
  try {
    const data = await adminSend("/api/setup/config/write", { body: body({ config_preview_id: setup.previewId, overwrite: true }) });
    if (data?.ok === false) throw Object.assign(new Error("write"), { payload: data });
    return data || {};
  } catch (error) {
    setupDraft.navError = refusal(error, "write failed");
    return null;
  } finally {
    setupDraft.busy = "";
  }
}

