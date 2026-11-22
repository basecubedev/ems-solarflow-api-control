// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import {
  addFound,
  bridgeValues,
  unitValues,
  buildReview,
  configuredNetworks,
  identitiesMatch,
  isControlDowngrade,
  itemAction,
  mergeObservation,
  meterTypeOf,
  proposalState,
  scanTarget,
  switchTransport,
  useExternalConnection,
  configuredFor,
  entryBroker,
  proposalBroker,
  readOnlyKind,
  READ_ONLY_FIND_KINDS,
  updateAddress,
} from "../src/lib/discovery.js";
import { READ_ONLY_KINDS } from "../src/lib/devices.js";

const ID_A = "opaque:v1:aaaa";
const ID_B = "opaque:v1:bbbb";
const CONN_A = "conn:v1:aaaa";
const CONN_B = "conn:v1:bbbb";

const observation = (extra) => ({ observation_id: "obs:v1:one", role_suggestion: "inverter", ip: "192.168.1.20", ...extra });
const catalog = { default_device: { common: { max_power: 800 } }, hardware_sections: [{ id: "devices", fields: [{ path: "devices[].name" }, { path: "devices[].max_power" }, { path: "devices[].min_soc" }] }] };

test("a scan target is a private range of /24 or smaller, as its network address", () => {
  assert.deepEqual(scanTarget("192.168.1.77/24"), { cidr: "192.168.1.0/24" });
  assert.deepEqual(scanTarget("10.0.0.5"), { cidr: "10.0.0.5/32" });
  assert.deepEqual(["", "1.2.3", "8.8.8.8", "192.168.1.0/16", "192.168.1.0/x"].map((raw) => scanTarget(raw).error), ["empty", "invalid", "private", "tooLarge", "prefix"]);
  assert.deepEqual(configuredNetworks({ devices: [{ ip: "192.168.1.20" }, { ip: "192.168.1.21" }, { ip: "" }], grid_meter: { present: true, ip: "10.1.2.3" } }), ["192.168.1.0/24", "10.1.2.0/24"]);
});

test("observations merge under the server's id; a scan sets the address, a bad port is dropped", () => {
  let found = mergeObservation({}, observation({ ip: "192.168.1.9", port: 80, model: "X" }), "mdns");
  found = mergeObservation(found, observation({ ip: "192.168.1.20", port: 0, model: "" }), "active_scan");
  const one = found["obs:v1:one"];
  assert.deepEqual([one.ip, one.port, one.model, one.sources], ["192.168.1.20", 80, "X", ["mdns", "active_scan"]]);
});

test("identities match on a shared issued token, never on two confirmed different devices", () => {
  assert.ok(identitiesMatch({ physical_device_id: ID_A }, { physical_identity_alias_tokens: [ID_A] }));
  assert.ok(!identitiesMatch({ physical_device_id: ID_A }, { physical_device_id: ID_B }));
  assert.ok(!identitiesMatch({ physical_device_id: ID_A, physical_identity_alias_tokens: [ID_B], identity_status: "confirmed" }, { physical_device_id: ID_B, identity_status: "confirmed" }));
});

test("the review says what each configured device and each find is", () => {
  const draft = {
    devices: [
      { name: "A", original_name: "A", physical_device_id: ID_A, connection_id: CONN_A },
      { name: "B", original_name: "B", physical_device_id: ID_B, connection_id: CONN_B },
      { name: "C", original_name: null, ip: "192.168.1.70" },
      { name: "M", kind: "zendure_mqtt", physical_device_id: "opaque:v1:mmmm" },
    ],
    grid_meter: { present: true, ip: "192.168.1.30" },
  };
  const found = [
    observation({ observation_id: "obs:v1:a", physical_device_id: ID_A, connection_id: "conn:v1:moved" }),
    observation({ observation_id: "obs:v1:m", physical_device_id: "opaque:v1:mmmm" }),
    observation({ observation_id: "obs:v1:n", ip: "192.168.1.40" }),
    observation({ observation_id: "obs:v1:g", role_suggestion: "grid_meter", ip: "192.168.1.30" }),
  ];
  const review = buildReview(found, [], draft, draft);
  assert.deepEqual(review.map((item) => [item.role === "grid_meter" ? "meter" : item.configured?.name || item.discovered?.ip, item.state]), [
    ["A", "conflict"],
    ["B", "missing"],
    ["M", "transport"],
    ["meter", "found"],
    ["192.168.1.40", "new"],
  ]);
});

test("an MQTT proposal is new, added, found, another connection or a conflict against the draft", () => {
  const proposal = { id: "p1", physical_identity_token: ID_A, connection_id: CONN_A, config_fragment: {} };
  const local = { devices: [{ name: "A", physical_device_id: ID_A }] };
  const mqtt = { devices: [{ name: "A", kind: "zendure_mqtt", physical_device_id: ID_A, connection_id: CONN_A }] };
  assert.equal(proposalState(proposal, { devices: [] }, { devices: [] }), "new");
  assert.equal(proposalState(proposal, local, local), "transport");
  assert.equal(proposalState(proposal, mqtt, { devices: [] }), "added");
  assert.equal(proposalState(proposal, mqtt, mqtt), "found");
  assert.equal(proposalState(proposal, { devices: [local.devices[0], { physical_device_id: "opaque:v1:cccc", physical_identity_alias_tokens: [ID_A] }] }, {}), "identity_conflict");
  assert.equal(proposalState({ target: "grid_meter", grid_meter_fragment: { type: "mqtt", mqtt: {} } }, {}, {}), "unavailable");
});

test("taking a read-only connection for a regulated inverter is named as giving up control", () => {
  const draft = { devices: [{ name: "A", physical_device_id: ID_A, enabled: true }] };
  const item = { state: "transport", proposal: { physical_identity_token: ID_A, config_fragment: { capabilities: { write_output_limit: false } } } };
  assert.equal(isControlDowngrade(item, draft), true);
  assert.equal(itemAction(item, draft, false).label, "replaceControl");
  assert.equal(isControlDowngrade({ ...item, proposal: { ...item.proposal, output_control_supported: true } }, draft), false);
});

test("a switch keeps the device's name, tuning and original entry, and refuses an ambiguous identity", () => {
  const devices = [{ name: "Garage", original_name: "Garage", kind: "zendure_mqtt", physical_device_id: ID_A, max_power: 600, enabled: false }];
  const result = switchTransport(devices, ID_A, "local_api", { discovered: { ip: "192.168.1.50", serial_number: "SN1", physical_device_id: ID_A, connection_id: CONN_A } }, catalog);
  assert.deepEqual(
    (({ kind, name, original_name, ip, sn, max_power, enabled, transport_switched, connection_id }) => ({ kind, name, original_name, ip, sn, max_power, enabled, transport_switched, connection_id }))(result.devices[0]),
    { kind: "local_api", name: "Garage", original_name: "Garage", ip: "192.168.1.50", sn: "SN1", max_power: 600, enabled: false, transport_switched: true, connection_id: CONN_A },
  );
  assert.equal(switchTransport([...devices, { physical_identity_alias_tokens: [ID_A] }], ID_A, "local_api", { discovered: {} }, catalog), null);
  assert.equal(switchTransport(devices, "", "local_api", {}, catalog), null);
});

test("a find is added once: an inverter with the defaults, a meter by its family, an E3/DC read only", () => {
  const item = { role: "inverter", state: "new", discovered: observation({ serial_number: "SN9", physical_device_id: ID_B }) };
  const added = addFound({ devices: [] }, item, catalog);
  assert.deepEqual([added.devices[0].ip, added.devices[0].sn, added.devices[0].max_power, added.devices[0].name], ["192.168.1.20", "SN9", 800, "INV_1"]);
  assert.equal(addFound({ devices: added.devices }, item, catalog), null);
  assert.deepEqual(addFound({}, { role: "grid_meter", discovered: { ip: "192.168.1.30", api_family: "shelly_3em_gen1", port: 80 } }, catalog).grid_meter, { present: true, type: "shelly_3em_gen1", ip: "192.168.1.30", port: 80 });
  assert.equal(addFound({ devices: [] }, { role: "read_only_device", discovered: { ip: "192.168.1.60", port: 502, api_family: "e3dc_modbus", role_suggestion: "grid_meter" } }, catalog).devices[0].kind, "e3dc_modbus");
  assert.equal(meterTypeOf({ device_type: "EcoTracker" }), "ecotracker");
});

test("a found HomeWizard P1 Meter becomes the meter of its own type", () => {
  const found = { ip: "192.168.1.40", api_family: "homewizard_p1", device_type: "homewizard_p1", port: 443 };
  assert.deepEqual(addFound({}, { role: "grid_meter", discovered: found }, catalog).grid_meter, { present: true, type: "homewizard_p1", ip: "192.168.1.40", port: 443 });
  assert.equal(meterTypeOf({ grid_meter_type: "homewizard_p1", api_family: "shelly_gen2" }), "homewizard_p1");
});

test("a found Tibber bridge becomes the meter with the password that opened it and its reading head", () => {
  const found = { ip: "192.168.1.70", api_family: "tibber_pulse_bridge", port: 80, credentials_ref: "basement", node_id: 2 };
  assert.deepEqual(addFound({}, { role: "grid_meter", discovered: found }, catalog).grid_meter, { present: true, type: "tibber_pulse_bridge", ip: "192.168.1.70", port: 80, credentials_ref: "basement", node_id: 2 });
  assert.deepEqual(bridgeValues({ credentials_ref: " ", node_id: "x" }), {});
  assert.deepEqual(bridgeValues({ node_id: "2" }), { node_id: 2 });
  assert.equal(meterTypeOf({ api_family: "tibber_pulse_bridge" }), "tibber_pulse_bridge");
});

test("an external device moves to the connection a proposal offers, by the catalogue id the server issued", () => {
  const devices = [{ kind: "external_mqtt", name: "Kostal", catalog_device_id: "catalog:v1:k1", mqtt: { broker_ref: "old" } }];
  const proposal = { id: "p9", catalog_device_id: "catalog:v1:k1", catalog_connection_id: "catalog:v1:c2", config_fragment: { mqtt: { broker_ref: "new", device_id: "K1" } } };
  const moved = useExternalConnection(devices, proposal).devices[0];
  assert.deepEqual([moved.name, moved.proposal_id, moved.mqtt.broker_ref, moved.catalog_connection_id], ["Kostal", "p9", "new", "catalog:v1:c2"]);
  assert.equal(useExternalConnection(devices, { ...proposal, catalog_device_id: "catalog:v1:other" }), null);
});

test("a found inverter is in the settings at its saved address, moved elsewhere, and never moved when saved by host name", () => {
  const review = (saved, find) => {
    const settings = { devices: [{ name: "A", original_name: "A", physical_device_id: ID_A, connection_id: CONN_A, ...saved }] };
    return buildReview([observation({ observation_id: "obs:v1:a", physical_device_id: ID_A, ...find })], [], settings, settings).map((item) => item.state);
  };
  assert.deepEqual(review({ ip: "192.168.1.50" }, { ip: "192.168.1.50", port: 80 }), ["found"]);
  assert.deepEqual(review({ ip: "192.168.1.50" }, { ip: "192.168.1.50", port: 80, connection_id: "conn:v1:with-port" }), ["found"]);
  assert.deepEqual(review({ ip: "SolarFlow.fritz.box" }, { ip: "192.168.1.50" }), ["found"]);
  assert.deepEqual(review({ ip: "192.168.1.50" }, { ip: "192.168.1.51" }), ["conflict"]);
});

test("a moved inverter stays moved once the draft takes its new address, until it is saved", () => {
  const saved = { devices: [{ name: "A", original_name: "A", ip: "192.168.1.50", physical_device_id: ID_A, connection_id: CONN_A }] };
  const find = [observation({ observation_id: "obs:v1:a", physical_device_id: ID_A, ip: "192.168.1.51" })];
  const draft = { devices: [{ ...saved.devices[0], ip: "192.168.1.51" }] };
  const [item] = buildReview(find, [], draft, saved);
  assert.equal(item.state, "conflict");
  assert.deepEqual(itemAction(item, draft, false), { label: "added", disabled: true });
  assert.equal(buildReview(find, [], draft, draft)[0].state, "found");
});

test("a draft address the device does not answer at is offered back, and a local entry not yet saved is added", () => {
  const saved = { devices: [{ name: "A", original_name: "A", ip: "192.168.1.50", physical_device_id: ID_A, connection_id: CONN_A }] };
  const find = [observation({ observation_id: "obs:v1:a", physical_device_id: ID_A, ip: "192.168.1.50" })];
  const typo = { devices: [{ ...saved.devices[0], ip: "192.168.1.99" }] };
  const [item] = buildReview(find, [], typo, saved);
  assert.equal(item.state, "conflict");
  assert.equal(itemAction(item, typo, false).kind, "updateAddress");
  const viaMqtt = { devices: [{ name: "A", original_name: "A", kind: "zendure_mqtt", physical_device_id: ID_A }] };
  const switched = { devices: [{ name: "A", original_name: "A", kind: "local_api", ip: "192.168.1.50", physical_device_id: ID_A }] };
  const [local] = buildReview(find, [], switched, viaMqtt);
  assert.equal(local.state, "added");
  assert.deepEqual(itemAction(local, switched, false), { label: "added", disabled: true });
  const added = addFound({ devices: [] }, { role: "inverter", discovered: find[0] }, catalog);
  const [fresh] = buildReview(find, [], added, { devices: [] });
  assert.equal(fresh.state, "added");
  assert.deepEqual(itemAction(fresh, added, false), { label: "added", disabled: true });
  const typed = { devices: [{ ...saved.devices[0], ip: "inverter-a.local" }] };
  const [named] = buildReview([observation({ observation_id: "obs:v1:a", physical_device_id: ID_A, ip: "192.168.1.51" })], [], typed, saved);
  assert.deepEqual([named.state, itemAction(named, typed, false).label], ["conflict", "added"]);
});

test("an address the server did not match is never taken for a device", () => {
  const saved = { devices: [{ name: "A", original_name: "A", ip: "192.168.1.50", physical_device_id: ID_A, connection_id: CONN_A }] };
  const stranger = [observation({ observation_id: "obs:v1:b", physical_device_id: ID_B, ip: "192.168.1.50" })];
  assert.deepEqual(buildReview(stranger, [], saved, saved).map((item) => item.state), ["missing", "new"]);
});

test("a read-only device in the settings is never reviewed as an inverter the search did not find", () => {
  const saved = { devices: [{ kind: "opendtu_http", name: "OpenDTU", original_name: "OpenDTU", ip: "192.168.1.70" }, { kind: "e3dc_modbus", name: "E3DC", original_name: "E3DC", ip: "192.168.1.90", port: 1502 }] };
  assert.deepEqual(buildReview([], [], saved, saved).map((item) => [item.role, item.state, item.configured.kind]), [["read_only_device", "missing", "opendtu_http"]]);
});

test("an inverter that moved offers its new address until the draft has it", () => {
  const draft = { devices: [{ name: "A", ip: "192.168.1.10", physical_device_id: ID_A, connection_id: CONN_A }] };
  const item = { role: "inverter", state: "conflict", configured: draft.devices[0], discovered: { ip: "192.168.1.11", physical_device_id: ID_A }, index: 0 };
  assert.deepEqual(itemAction(item, draft, false), { label: "updateDraft", kind: "updateAddress", disabled: false });
  const moved = { devices: [{ ...draft.devices[0], ip: "192.168.1.11" }] };
  assert.deepEqual(itemAction(item, moved, false), { label: "added", disabled: true });
});

test("a device found again on a second broker names the entry it already is and both brokers", () => {
  const draft = {
    devices: [{ kind: "external_mqtt", name: "Balkon", enabled: true, catalog_device_id: "catalog:v1:balkon", mqtt: { broker_ref: "home" } }],
    zendure_mqtt: { brokers: { home: { host: "192.168.1.5", port: 1883 } } },
  };
  const proposal = {
    id: "p1",
    catalog_device_id: "catalog:v1:balkon",
    catalog_connection_id: "catalog:v1:other",
    broker_host: "192.168.1.7",
    broker_port: 1883,
    config_fragment: { type: "external_mqtt", mqtt: { broker_ref: "other", device_id: "balkon" } },
  };
  const item = { state: "transport", proposal };

  assert.equal(configuredFor(item, draft), draft.devices[0]);
  assert.equal(entryBroker(draft.devices[0], draft), "192.168.1.5:1883");
  assert.equal(proposalBroker(proposal), "192.168.1.7:1883");
  assert.equal(isControlDowngrade(item, draft), false);
  assert.equal(configuredFor({ state: "transport", proposal: { ...proposal, catalog_device_id: "catalog:v1:other" } }, draft), null);
});

test("a found SunSpec meter brings the unit ID it answered at", () => {
  const found = { ip: "192.168.1.64", api_family: "sunspec_modbus", device_type: "sunspec_modbus", port: 502, unit_id: 200 };
  assert.deepEqual(addFound({}, { role: "grid_meter", discovered: found }, catalog).grid_meter, { present: true, type: "sunspec_modbus", ip: "192.168.1.64", port: 502, unit_id: 200 });
  assert.deepEqual(unitValues({ unit_id: "x" }), {});
  assert.equal(meterTypeOf({ api_family: "powerfox_poweropti" }), "powerfox_poweropti");
});

const opendtu = (serial, extra) => ({ observation_id: `obs:v1:${serial}`, ip: "192.168.1.70", port: 80, api_family: "opendtu_http", device_type: "opendtu_http", role_suggestion: "read_only_device", serial_number: serial, display_name: "TSUN TSOL-M800(DE)", ...extra });
const savedOpenDtu = (extra) => ({ kind: "opendtu_http", name: "OpenDTU", original_name: "OpenDTU", ip: "192.168.1.70", enabled: true, ...extra });

test("each inverter an OpenDTU reads is offered as a read-only device, never as an inverter", () => {
  const review = buildReview([opendtu("116180000001"), opendtu("114180000002")], [], { devices: [] }, { devices: [] });
  assert.deepEqual(review.map((item) => [item.role, item.state, item.discovered.serial_number, readOnlyKind(item)]), [
    ["read_only_device", "new", "116180000001", "opendtu_http"],
    ["read_only_device", "new", "114180000002", "opendtu_http"],
  ]);
  assert.deepEqual(itemAction(review[0], { devices: [] }, false), { label: "addReadOnly", kind: "add", disabled: false });
});

test("a found OpenDTU inverter is added with its address and serial, once each", () => {
  const first = { role: "read_only_device", state: "new", discovered: opendtu("116180000001") };
  const second = { role: "read_only_device", state: "new", discovered: opendtu("114180000002") };
  const added = addFound({ devices: [{ name: "INV_1", ip: "192.168.1.20" }] }, first, catalog);
  assert.deepEqual(added.devices[1], { kind: "opendtu_http", original_name: null, name: "OpenDTU", ip: "192.168.1.70", serial: "116180000001", enabled: true, has_enabled_key: true });
  assert.equal(added.index, 1);
  assert.equal(addFound(added, first, catalog), null);
  assert.deepEqual(itemAction(first, added, false), { label: "added", disabled: true });
  const both = addFound(added, second, catalog);
  assert.deepEqual(both.devices.slice(1).map((device) => [device.name, device.serial]), [["OpenDTU", "116180000001"], ["OpenDTU2", "114180000002"]]);
  assert.equal(addFound({ devices: [] }, { role: "read_only_device", discovered: opendtu("116180000001", { port: 8080 }) }, catalog).devices[0].port, 8080);
});

test("a saved OpenDTU entry is found by its serial wherever it answers, or by its address while its OpenDTU reads it alone", () => {
  const find = [opendtu("116180000001")];
  const state = (entry) => buildReview(find, [], { devices: [entry] }, { devices: [entry] }).map((item) => item.state);
  assert.deepEqual(state(savedOpenDtu({ serial: "116180000001" })), ["found"]);
  const lettered = { devices: [savedOpenDtu({ serial: "a1b2c3d4" })] };
  assert.deepEqual(buildReview([opendtu("A1B2C3D4")], [], lettered, lettered).map((item) => item.state), ["found"]);
  assert.deepEqual(state(savedOpenDtu()), ["found"]);
  assert.deepEqual(state(savedOpenDtu({ serial: "114180000002" })), ["new", "missing"]);
  assert.deepEqual(state(savedOpenDtu({ serial: "116180000001", ip: "opendtu.local" })), ["found"]);
  const named = { devices: [savedOpenDtu({ ip: "opendtu.local" })] };
  const [doubted] = buildReview(find, [], named, named);
  assert.deepEqual([doubted.state, doubted.doubts], ["new", ["OpenDTU"]]);
  assert.deepEqual(itemAction(doubted, named, false), { label: "addReadOnly", kind: "add", disabled: false });
  assert.equal("doubts" in buildReview(find, [], { devices: [] }, { devices: [] })[0], false);
  const typing = { devices: [{ ...savedOpenDtu(), original_name: null, ip: "" }] };
  assert.deepEqual(itemAction(buildReview(find, [], typing, { devices: [] })[0], typing, false).label, "addReadOnly");
});

test("a saved OpenDTU inverter whose OpenDTU moved offers the new address and is not added twice", () => {
  const find = [opendtu("116180000001", { ip: "192.168.1.71" })];
  const entry = savedOpenDtu({ serial: "116180000001" });
  const draft = { devices: [entry] };
  const review = buildReview(find, [], draft, draft);
  assert.deepEqual(review.map((item) => [item.state, item.index]), [["conflict", 0]]);
  assert.deepEqual(itemAction(review[0], draft, false), { label: "updateDraft", kind: "updateAddress", disabled: false });
  assert.equal(addFound(draft, { ...review[0], state: "new" }, catalog), null);
  const moved = { devices: [{ ...entry, ip: "192.168.1.71" }] };
  assert.deepEqual(itemAction(review[0], moved, false), { label: "added", disabled: true });
});

test("an entry without a serial names no inverter of an OpenDTU that reads several, and says it cannot be used", () => {
  const finds = [opendtu("116180000001"), opendtu("114180000002")];
  const draft = { devices: [savedOpenDtu()] };
  const review = buildReview(finds, [], draft, draft);
  assert.deepEqual(review.map((item) => [item.state, item.discovered?.serial_number, item.alone]), [["new", "116180000001", false], ["new", "114180000002", false], ["unavailable", undefined, undefined]]);
  assert.deepEqual(itemAction(review[0], draft, false), { label: "addReadOnly", kind: "add", disabled: false });
  assert.deepEqual(itemAction(review[2], draft, false), { label: "configured", disabled: true });
  assert.equal(readOnlyKind(review[2]), "opendtu_http");
  assert.deepEqual(addFound(draft, review[1], catalog).devices[1].serial, "114180000002");
  const elsewhere = buildReview([opendtu("116180000001", { ip: "192.168.1.71" }), opendtu("114180000002", { ip: "192.168.1.71" })], [], draft, draft);
  assert.deepEqual(elsewhere.map((item) => item.state), ["new", "new", "missing"]);
});

test("a taken new address stays a change until it is saved", () => {
  const find = [opendtu("116180000001", { ip: "192.168.1.71" })];
  const saved = { devices: [savedOpenDtu({ serial: "116180000001" })] };
  const [item] = buildReview(find, [], saved, saved);
  const taken = updateAddress(saved.devices, item);
  assert.equal(taken.devices[0].ip, "192.168.1.71");
  const [after] = buildReview(find, [], taken, saved);
  assert.deepEqual([after.state, itemAction(after, taken, false)], ["conflict", { label: "added", disabled: true }]);
  assert.deepEqual(buildReview(find, [], taken, taken).map((row) => row.state), ["found"]);
  assert.equal(updateAddress(saved.devices, { ...item, index: 5 }), null);
});

test("an OpenDTU found on the default port takes the entry off its own port", () => {
  const find = [opendtu("116180000001")];
  const saved = { devices: [savedOpenDtu({ serial: "116180000001", port: 8080 })] };
  const [item] = buildReview(find, [], saved, saved);
  assert.equal(item.state, "conflict");
  const taken = updateAddress(saved.devices, item);
  assert.equal("port" in taken.devices[0], false);
  assert.deepEqual(itemAction(item, taken, false), { label: "added", disabled: true });
  const other = updateAddress(saved.devices, { ...item, discovered: opendtu("116180000001", { port: 8081 }) });
  assert.equal(other.devices[0].port, 8081);
  const byName = { devices: [savedOpenDtu({ serial: "116180000001", ip: "opendtu.local", port: 8080 })] };
  const [named] = buildReview(find, [], byName, byName);
  const kept = updateAddress(byName.devices, named);
  assert.deepEqual([named.state, kept.devices[0].ip, "port" in kept.devices[0]], ["conflict", "opendtu.local", false]);
  assert.deepEqual(itemAction(named, kept, false), { label: "added", disabled: true });
});

test("an inverter two OpenDTUs list is found where its entry reads it, and not moved to the other", () => {
  const finds = [opendtu("116180000001"), opendtu("116180000001", { ip: "192.168.1.71", observation_id: "obs:v1:other" })];
  const saved = { devices: [savedOpenDtu({ serial: "116180000001" })] };
  const review = buildReview(finds, [], saved, saved);
  assert.deepEqual(review.map((item) => [item.state, item.index]), [["found", 0], ["found", 0]]);
  assert.deepEqual(review.map((item) => itemAction(item, saved, false).label), ["inConfig", "inConfig"]);
});

test("of two saved entries for one inverter the one at its address is the one found", () => {
  const find = [opendtu("116180000001", { ip: "192.168.1.71" })];
  const saved = { devices: [savedOpenDtu({ serial: "116180000001" }), savedOpenDtu({ name: "OpenDTU2", original_name: "OpenDTU2", serial: "116180000001", ip: "192.168.1.71" })] };
  assert.deepEqual(buildReview(find, [], saved, saved).map((item) => [item.state, item.index]), [["found", 1], ["missing", 0]]);
});

test("a saved OpenDTU entry the search would have reached and did not find is missing", () => {
  const saved = { devices: [savedOpenDtu({ serial: "116180000001" })] };
  assert.deepEqual(buildReview([], [], saved, saved).map((item) => [item.role, item.state, readOnlyKind(item)]), [["read_only_device", "missing", "opendtu_http"]]);
  for (const entry of [savedOpenDtu({ port: 8080 }), savedOpenDtu({ ip: "opendtu.local" }), { ...savedOpenDtu(), original_name: null }]) {
    assert.deepEqual(buildReview([], [], { devices: [entry] }, { devices: [entry] }), []);
  }
});

test("an E3/DC and an OpenDTU found together keep their own kinds", () => {
  const e3dc = { observation_id: "obs:v1:e3dc", ip: "192.168.1.60", port: 502, api_family: "e3dc_modbus", role_suggestion: "grid_meter" };
  const review = buildReview([e3dc, opendtu("116180000001")], [], { devices: [] }, { devices: [] });
  assert.deepEqual(review.map((item) => [item.role, readOnlyKind(item)]), [["grid_meter", ""], ["read_only_device", "e3dc_modbus"], ["read_only_device", "opendtu_http"]]);
  const draft = { devices: [] };
  const withE3dc = addFound(draft, review[1], catalog);
  assert.equal(withE3dc.devices[0].kind, "e3dc_modbus");
  assert.equal(addFound(withE3dc, review[2], catalog).devices[1].kind, "opendtu_http");
});

test("a read-only item of no kind the search knows adds nothing", () => {
  assert.equal(addFound({ devices: [] }, { role: "read_only_device", discovered: { ip: "192.168.1.80", api_family: "unknown_family", role_suggestion: "read_only_device" } }, catalog), null);
  assert.equal(readOnlyKind({ role: "inverter", discovered: opendtu("116180000001") }), "");
});

test("every kind the search finds read only is a read-only kind the devices page edits", () => {
  assert.deepEqual(READ_ONLY_FIND_KINDS.filter((kind) => !READ_ONLY_KINDS.includes(kind)), []);
  assert.deepEqual([...READ_ONLY_FIND_KINDS].sort(), ["e3dc_modbus", "opendtu_http"]);
});
