// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import {
  addFound,
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
} from "../src/lib/discovery.js";

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
      { name: "A", physical_device_id: ID_A, connection_id: CONN_A },
      { name: "B", physical_device_id: ID_B, connection_id: CONN_B },
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
  assert.equal(addFound({ devices: [] }, { role: "read_only_device", discovered: { ip: "192.168.1.60", port: 502 } }, catalog).devices[0].kind, "e3dc_modbus");
  assert.equal(meterTypeOf({ device_type: "EcoTracker" }), "ecotracker");
});

test("an external device moves to the connection a proposal offers, by the catalogue id the server issued", () => {
  const devices = [{ kind: "external_mqtt", name: "Kostal", catalog_device_id: "catalog:v1:k1", mqtt: { broker_ref: "old" } }];
  const proposal = { id: "p9", catalog_device_id: "catalog:v1:k1", catalog_connection_id: "catalog:v1:c2", config_fragment: { mqtt: { broker_ref: "new", device_id: "K1" } } };
  const moved = useExternalConnection(devices, proposal).devices[0];
  assert.deepEqual([moved.name, moved.proposal_id, moved.mqtt.broker_ref, moved.catalog_connection_id], ["Kostal", "p9", "new", "catalog:v1:c2"]);
  assert.equal(useExternalConnection(devices, { ...proposal, catalog_device_id: "catalog:v1:other" }), null);
});

test("an inverter that moved offers its new address until the draft has it", () => {
  const draft = { devices: [{ name: "A", ip: "192.168.1.10", physical_device_id: ID_A, connection_id: CONN_A }] };
  const item = { role: "inverter", state: "conflict", configured: draft.devices[0], discovered: { ip: "192.168.1.11", physical_device_id: ID_A }, index: 0 };
  assert.deepEqual(itemAction(item, draft, false), { label: "updateDraft", kind: "updateAddress", disabled: false });
  const moved = { devices: [{ ...draft.devices[0], ip: "192.168.1.11" }] };
  assert.deepEqual(itemAction(item, moved, false), { label: "added", disabled: true });
});
