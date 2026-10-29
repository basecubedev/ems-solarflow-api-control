// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import {
  adoptDismissals,
  applyOperations,
  brokerPayload,
  clientHints,
  conflictKind,
  emptyDraft,
  inverterCards,
  manualControl,
  manualItem,
  nextCompactName,
  planCandidates,
  planIndex,
  planState,
  readStore,
  reassertPriority,
  serializeSelection,
  storeEnvelope,
  syncGridFeatures,
  unselectedProposals,
  withItemIds,
  withNames,
} from "../src/lib/setupDraft.js";

const ID_A = "opaque:v1:aaaa";
const ID_B = "opaque:v1:bbbb";
const OBS_A = "obs:v1:a1";
const CONN_1 = "conn:v1:c1";

const draftWith = (change) => ({ ...emptyDraft(), ...change });

const catalog = {
  zendure_mqtt_generations: [{ id: "zensdk", label: "New", default: true, control_broker_sources: ["local_mqtt", "zendure_mqtt"] }, { id: "legacy", label: "Old", control_broker_sources: ["zendure_mqtt"] }],
  zendure_mqtt_hardware_models: [{ id: "", label: "Unknown" }, { id: "pro2", generation: "zensdk", compatible_generations: ["zensdk", "legacy"], control_supported: true }, { id: "hub", generation: "legacy", control_supported: false }],
  hardware_variants: { inverter: [{ id: "zendure_local_http", label: "Zendure local API", default: true, default_port: 80 }], grid_meter: [{ id: "shelly", label: "Shelly Pro 3EM", default: true, default_port: 80 }, { id: "tasmota_http", label: "Tasmota", default_port: 80 }] },
};

test("a stored list reads as the envelope or the bare array an older release wrote, and is written as the envelope", () => {
  assert.deepEqual(readStore('{"schema_version":1,"items":["a"]}'), ["a"]);
  assert.deepEqual(readStore('["b"]'), ["b"]);
  assert.deepEqual(readStore("not json"), []);
  assert.deepEqual(JSON.parse(storeEnvelope(new Set(["x"]))), { schema_version: 1, items: ["x"] });
  assert.deepEqual(withItemIds([{ source_id: "s" }, { draft_item_id: "item-1" }]).map((item) => item.draft_item_id), ["item-legacy-0", "item-1"]);
});

test("a new inverter name is one past the highest and the count, never a name in use", () => {
  assert.equal(nextCompactName([], 0), "INV_1");
  assert.equal(nextCompactName(["INV_1", "Garage"], 2), "INV_3");
  assert.equal(nextCompactName(["inv_4"], 1), "INV_5");
  const named = withNames(draftWith({ items: [{ role: "inverter", config_name: "INV_1" }], selections: [{ id: "p1", target: "device" }, { id: "g", target: "grid_meter" }], manual: [{ serial_number: "S" }] }));
  assert.deepEqual([named.selections[0].config_name, "config_name" in named.selections[1], named.manual[0].name], ["INV_2", false, "INV_3"]);
});

test("the plan is asked about every draft entry and only the cards on show, by handle", () => {
  const draft = draftWith({
    items: [{ draft_item_id: "item-1", source_id: OBS_A, role: "inverter", ip: "1.2.3.4", auto_added: true, config_values: { max_power: 600 } }, { draft_item_id: "item-2", source_id: "manual:h:", role: "grid_meter" }],
    selections: [{ id: "p1", target: "device", connection_source: "local_mqtt", selection_origin: "manual" }, { id: "g", target: "grid_meter" }],
    dismissedPhysical: [ID_B],
    legacyDismissals: ["SERIAL"],
    dismissedObservations: ["obs:v1:gone"],
  });
  const state = planState(draft);
  assert.deepEqual(state.draft_items.map((item) => [item.role, item.auto_added]), [["inverter", true], ["grid_meter", false]]);
  assert.equal("config_values" in state.draft_items[0], false);
  assert.deepEqual(state.mqtt_selections.map((entry) => entry.id), ["p1"]);
  assert.deepEqual(state.physical_dismissals, [ID_B, "SERIAL"]);
  const candidates = planCandidates([{ observation_id: OBS_A, role_suggestion: "inverter" }, { observation_id: "obs:v1:x", role_suggestion: "unknown" }], [{ id: "p1", config_fragment: {} }, { id: "g", target: "grid_meter", grid_meter_fragment: {} }]);
  assert.deepEqual(candidates, { observations: [{ observation_id: OBS_A, observation_ref: OBS_A }], proposals: [{ id: "p1" }] });
});

test("a plan's operations apply only while it is the current plan, and keep a known inverter's name", () => {
  const device = { observation_id: OBS_A, role_suggestion: "inverter", ip: "10.0.0.2", display_name: "SolarFlow", physical_device_id: ID_A };
  const plan = { plan_id: "p", generation: "g", draft_items: [], observations: [{ observation_ref: OBS_A, physical_device_id: ID_A }], operations: { adopt_observations: [{ observation_ref: OBS_A }], select_mqtt_proposals: [{ id: "m1", physical_device_id: ID_B, selection_origin: "priority" }] } };
  const names = new Map([[ID_A, "Garage"]]);
  let sequence = 0;
  const context = { proposals: [{ id: "m1", config_fragment: { capabilities: {} }, display_name: "Hub" }], available: new Map([[OBS_A, device]]), names, newId: () => `item-${++sequence}` };
  assert.equal(applyOperations(emptyDraft(), plan, { plan_id: "other", generation: "g" }, context).changed, false);
  const { draft, changed } = applyOperations(emptyDraft(), plan, plan, context);
  assert.equal(changed, true);
  assert.deepEqual([draft.items[0].config_name, draft.items[0].auto_added, draft.items[0].draft_item_id, draft.items[0].physical_identity_token], ["Garage", true, "item-1", ID_A]);
  assert.deepEqual([draft.selections[0].id, draft.selections[0].selection_origin, draft.selections[0].config_name], ["m1", "priority", "INV_1"]);
  const dropped = applyOperations(draft, { ...plan, operations: { drop_draft_items: ["item-1"], drop_mqtt_selections: ["m1"] } }, { plan_id: "p", generation: "g" }, context);
  assert.deepEqual([dropped.draft.items.length, dropped.draft.selections.length], [0, 0]);
});

test("removals are kept under the ids the plan issued; a legacy serial it could not place stays for a later plan", () => {
  const draft = draftWith({ dismissedPhysical: [], legacyDismissals: ["OLD1", "OLD2"], dismissedObservations: ["x"] });
  const plan = { dismissals: { physical: [{ physical_device_id: ID_A }, { physical_device_id: "not-issued" }], observations: [{ observation_ref: OBS_A }], unresolved: [{ scope: "physical", value: "OLD2" }, { scope: "observation", value: "obs:v1:later" }] } };
  const adopted = adoptDismissals(draft, plan);
  assert.deepEqual(adopted.dismissedPhysical, [ID_A]);
  assert.deepEqual(adopted.legacyDismissals, ["OLD2"]);
  assert.deepEqual(adopted.dismissedObservations, [OBS_A, "obs:v1:later"]);
});

test("a chosen MQTT connection keeps only trusted, secret-free fields, its name, kept values and an off switch", () => {
  const proposal = { id: "m1", config_fragment: { mqtt: {} }, broker_ref: "local_mqtt_a", physical_identity_alias_tokens: [ID_A, "raw", ID_A], config_values: {}, enabled: true, password: "secret" };
  const entry = serializeSelection(proposal, { target: "device", configName: "INV_1", configValues: { max_power: 500 }, enabled: false });
  assert.deepEqual([entry.config_name, entry.config_values, entry.enabled, entry.physical_identity_alias_tokens, entry.replace_grid_meter], ["INV_1", { max_power: 500 }, false, [ID_A], false]);
  assert.equal("password" in entry, false);
  assert.equal(serializeSelection(proposal, { target: "device", configName: "X" }).enabled, undefined);
  const grid = serializeSelection({ id: "g", grid_meter_fragment: { type: "mqtt" }, config_fragment: {} }, { target: "grid_meter", replaceGridMeter: true });
  assert.deepEqual([grid.config_name, grid.config_fragment, grid.replace_grid_meter], [undefined, undefined, true]);
});

test("a grid meter row sets its type and address; a D0 topic follows its serial until it is typed by hand", () => {
  const shelly = syncGridFeatures({ "grid_meter.mqtt.topic": "x" }, { api_family: "shelly_gen2", ip: "10.0.0.9" });
  assert.deepEqual([shelly.features["grid_meter.type"], shelly.features["grid_meter.ip"], shelly.item.grid_meter_type], ["shelly", "10.0.0.9", "shelly"]);
  const d0 = syncGridFeatures({ "grid_meter.ip": "10.0.0.9" }, { grid_meter_type: "zendure_smartmeter_d0", serial_number: "ABC" });
  assert.deepEqual([d0.features["grid_meter.mqtt.topic"], d0.features["grid_meter.mqtt.payload_format"], "grid_meter.ip" in d0.features, d0.item.d0_topic_mode], ["Zendure/sensor/ABC/totalPower", "number", false, "auto"]);
  const typed = syncGridFeatures({ "grid_meter.mqtt.topic": "custom/topic" }, { grid_meter_type: "zendure_smartmeter_d0", serial_number: "ABC" });
  assert.deepEqual([typed.features["grid_meter.mqtt.topic"], typed.item.d0_topic_mode], ["custom/topic", "manual"]);
});

test("the browser's own checks name empty and doubled names and the grid meter's state", () => {
  const hints = (draft, extra = {}) => clientHints(draft, { supportedMeters: 0, gridStale: false, ...extra }).map((hint) => hint.key);
  assert.deepEqual(hints(draftWith({ items: [{ role: "inverter", config_name: "INV_1" }, { role: "inverter", config_name: "INV_1" }] })), ["duplicateName", "noGridMeter"]);
  assert.deepEqual(hints(draftWith({ items: [{ role: "grid_meter", config_name: "grid_meter" }] }), { gridStale: true }), ["noInverter", "gridMeterStale"]);
  assert.deepEqual(hints(emptyDraft(), { supportedMeters: 2 }), ["chooseGridMeter"]);
  assert.deepEqual(hints(draftWith({ selections: [{ id: "p", target: "device", config_name: " " }] })), ["emptyName"]);
});

test("a hand-entered MQTT device is controlled only with a controllable model, its source and a complete route", () => {
  const form = { generation: "zensdk", model: "pro2", routeId: "R1", productKey: "PK" };
  assert.equal(manualControl(catalog, form).status, "available");
  assert.deepEqual(manualControl(catalog, { ...form, productKey: "" }), { status: "not_ready", supported: true, enabled: false, missing: "productKey" });
  assert.equal(manualControl(catalog, { ...form, routeId: "" }).missing, "mqttId");
  assert.equal(manualControl(catalog, { ...form, model: "" }).status, "unknown");
  assert.equal(manualControl(catalog, { ...form, model: "hub" }).status, "unavailable");
  assert.equal(manualControl(catalog, { ...form, generation: "legacy" }).status, "source_unavailable");
});

test("a device typed by hand needs a host and is keyed by it; a grid meter replaces the one in the draft", () => {
  const draft = draftWith({ items: [{ role: "grid_meter", source_id: "obs", display_name: "Shelly Pro 3EM" }] });
  assert.equal(manualItem(draft, catalog, { host: " " }, "id").error, "hostRequired");
  const result = manualItem(draft, catalog, { role: "grid_meter", type: "tasmota_http", host: "10.0.0.5", port: "80" }, "item-9");
  assert.deepEqual(result.items.map((item) => item.source_id), ["manual:10.0.0.5:80"]);
  assert.deepEqual([result.item.grid_meter_type, result.item.manual, result.item.config_name, result.item.display_name], ["tasmota_http", true, "grid_meter", "Tasmota"]);
  assert.equal(manualItem({ ...draft, items: result.items }, catalog, { role: "inverter", host: "10.0.0.5", port: "80" }, "x").error, "hostTaken");
  const inverter = manualItem(emptyDraft(), catalog, { role: "inverter", host: "10.0.0.6" }, "y").item;
  assert.deepEqual([inverter.connection_type, inverter.config_name, inverter.display_name], ["zendure_local_http", "INV_1", "SolarFlow 800 Pro 2"]);
});

test("an empty broker form sends nothing, and its password only from memory", () => {
  assert.equal(brokerPayload({}, ""), null);
  assert.deepEqual(brokerPayload({ host: " 10.0.0.1 ", port: "1883", security: "tls" }, "pw"), { host: "10.0.0.1", port: "1883", security: "tls", password: "pw" });
});

test("each refusal is named by what it asks of the reader", () => {
  assert.equal(conflictKind({ error: "setup_workflow_not_active" }), "workflow");
  assert.equal(conflictKind({ error: "stale_device_plan" }), "plan");
  assert.equal(conflictKind({ error: "setup_preview_mismatch" }), "config");
  assert.equal(conflictKind({ error: "setup_operation_in_progress" }), "busy");
  assert.equal(conflictKind({ error: "other" }), null);
});

test("Add more devices offers each placed connection once, and one inverter shows as one card", () => {
  const draft = draftWith({ items: [{ role: "inverter", draft_item_id: "item-1", source_id: OBS_A }], selections: [{ id: "chosen", target: "device" }, { id: "same", target: "device" }] });
  const plan = { draft_items: [{ draft_item_id: "item-1", physical_device_id: ID_A }], mqtt_selections: [{ id: "chosen", connection_id: CONN_1, physical_device_id: ID_B }, { id: "same", physical_device_id: ID_A }], proposals: [{ id: "dup", connection_id: CONN_1 }, { id: "new", connection_id: "conn:v1:c2" }] };
  const index = planIndex(plan);
  const offered = unselectedProposals(draft, [{ id: "chosen", config_fragment: {} }, { id: "dup", config_fragment: {} }, { id: "new", config_fragment: {} }], index);
  assert.deepEqual(offered.map((proposal) => proposal.id), ["new"]);
  assert.deepEqual(inverterCards(draft, index).map((card) => card.key), [OBS_A, "mqtt:chosen"]);
});

test("a source moved in the order hands the hand-made connection choices back to it", () => {
  const draft = reassertPriority(draftWith({ items: [{ role: "inverter", auto_added: false }, { role: "grid_meter", auto_added: false }], selections: [{ id: "a", selection_origin: "manual" }, { id: "b", selection_origin: "automatic" }] }));
  assert.deepEqual(draft.items.map((item) => item.auto_added), [true, false]);
  assert.deepEqual(draft.selections.map((entry) => entry.selection_origin), ["priority", "automatic"]);
});
