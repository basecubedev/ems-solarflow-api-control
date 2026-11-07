// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { BUILTIN_BROKER, MAPPING_STEPS, emsPlace, mappedIndex, withMappedDevice, withRenamedDevice, UNITS, brokerAddress, deviceFromAnswers, emsDeviceName, freshGroup, groupValues, idFromName, isChangeOnly, likelyValue, mappedProposal, mappingStep, mapsTo, maxAgeFor, EMS_REPORT_GRACE_MS, emsReads, emsSilence, emsWaiting, savedKind, signPreset, sourceFromValue, uniqueId, preferredBroker, unitFactor, unitOf } from "../src/lib/mqtt.js";

test("each unit is the factor the catalog needs", () => {
  assert.deepEqual(UNITS, { W: 1, kW: 1000, fraction: 100, percent: 1 });
  assert.equal(unitFactor("kW"), 1000);
  assert.equal(unitFactor("nonsense"), 1);
  assert.equal(unitOf(100, ["percent", "fraction"]), "fraction");
  assert.equal(unitOf(0.5), null);
});

test("the maximum age is three measured intervals between 60 and 900 s, 600 when change-only or not measured yet", () => {
  assert.equal(maxAgeFor(10), 60);
  assert.equal(maxAgeFor(30), 90);
  assert.equal(maxAgeFor(300), 900);
  assert.equal(maxAgeFor(null), 600);
  assert.equal(maxAgeFor(undefined), 600);
  assert.equal(maxAgeFor(0), 600);
  assert.equal(maxAgeFor(10, { changeOnly: true }), 600);
  assert.equal(isChangeOnly("shellyplusplugs-x/status/switch:0", "shelly_gen2"), true);
  assert.equal(isChangeOnly("shellyplusplugs-x/online", "shelly_gen2"), false);
  assert.equal(isChangeOnly("tele/plug/SENSOR", "tasmota"), false);
});

test("a negative reading for a target that cannot be negative is inverted and clipped", () => {
  assert.deepEqual(signPreset(-612.3, "inverterPower"), { invert: true, negative: "zero" });
  assert.deepEqual(signPreset(612.3, "inverterPower"), { invert: false, negative: "keep" });
  assert.deepEqual(signPreset(-300, "batteryPower"), { invert: false, negative: "keep" });
});

test("waiting for a device offers only a group first seen after the wait began", () => {
  const group = (key, ages) => ({ key, label: "shelly_gen2", mappable: true, topics: ages.map((age, index) => ({ topic: `${key}/t${index}`, first_seen_seconds: age })) });
  const polledAt = 100_000;
  const since = 95_000;
  assert.equal(freshGroup([group("old", [10]), group("new", [2])], { since, polledAt }).key, "new");
  assert.equal(freshGroup([group("mixed", [2, 10])], { since, polledAt }), null);
  assert.equal(freshGroup([group("known", [1])], { before: ["shelly_gen2/known"], since, polledAt }), null);
  assert.equal(freshGroup([{ ...group("ems", [1]), mappable: false }], { since, polledAt }), null);
  assert.equal(freshGroup([{ key: "x", label: "unknown", mappable: true, topics: [{ topic: "x" }] }], { since, polledAt }), null);
});

test("a mapped device is adopted through the proposal its own mapping makes, never through a search's", () => {
  const external = (id, broker, mapping = broker) => ({ id: `${broker}-${id}`, mapping_broker_ref: mapping, config_fragment: { type: "external_mqtt", mqtt: { device_id: id, broker_ref: broker } } });
  const found = { id: "found-balkon", config_fragment: { type: "external_mqtt", mqtt: { device_id: "balkon", broker_ref: "local_mqtt_x" } } };
  const zendure = { id: "z", mapping_broker_ref: "ems-mqtt", config_fragment: { type: "zendure_mqtt", mqtt: { device_id: "balkon" } } };
  assert.equal(mappedProposal([found, zendure, external("balkon", "ems-mqtt")], "balkon", "ems-mqtt").id, "ems-mqtt-balkon");
  assert.equal(mappedProposal([external("balkon", "iobroker")], "balkon", "ems-mqtt"), null);
  assert.equal(mappedProposal([found], "balkon", "ems-mqtt"), null);
  assert.equal(mappedProposal([external("other", "ems-mqtt")], "balkon", "ems-mqtt"), null);
  assert.equal(mappedProposal(undefined, "balkon", "ems-mqtt"), null);
});

test("a mapped id finds its EMS device, new in the draft or saved in the config", () => {
  const added = { kind: "external_mqtt", name: "Balkon", mqtt: { device_id: "balkon" } };
  const saved = { kind: "external_mqtt", name: "Balkon", entry_ref: "r1", entry: { type: "external_mqtt", mqtt: { device_id: "balkon" } } };
  const zendure = { kind: "zendure_mqtt", name: "WR", mqtt: { device_id: "balkon" } };
  assert.equal(mapsTo("balkon")(added), true);
  assert.equal(mapsTo("balkon")(saved), true);
  assert.equal(mapsTo("balkon")(zendure), false);
  assert.equal(mapsTo("terrasse")(saved), false);
});

test("a mapped device's EMS place: missing, on its mapping's connection, or the same id read from another broker", () => {
  const saved = [{ kind: "external_mqtt", name: "Balkon", catalog_connection_id: "conn-a", entry: { type: "external_mqtt", mqtt: { device_id: "balkon" } } }];
  assert.equal(emsPlace(saved, "terrasse", "conn-a"), null);
  assert.equal(emsPlace(saved, "balkon", "conn-a"), "there");
  assert.equal(emsPlace(saved, "balkon", "conn-b"), "elsewhere");
  assert.equal(emsPlace(saved, "balkon", undefined), "there");
});

test("of EMS devices sharing a mapping's id, the one switched on is the mapping's, as EMS reads only that one", () => {
  const entry = (name, enabled, connection) => ({ kind: "external_mqtt", name, enabled, catalog_connection_id: connection, entry: { type: "external_mqtt", enabled, mqtt: { device_id: "balkon" } } });
  const devices = [entry("Balkon Alt", false, "conn-b"), entry("Balkon", true, "conn-a")];
  assert.equal(mappedIndex(devices, "balkon"), 1);
  assert.equal(mappedIndex([devices[0]], "balkon"), 0);
  assert.equal(mappedIndex(devices, "terrasse"), -1);
  assert.equal(emsPlace(devices, "balkon", "conn-a"), "there");
  assert.deepEqual(withRenamedDevice(devices, "balkon", "Balkon Nord").map((device) => device.name), ["Balkon Alt", "Balkon Nord"]);
  const toggled = [{ ...devices[1], enabled: false }, { ...devices[0], enabled: true }];
  assert.equal(mappedIndex(toggled, "balkon"), 1);
  assert.equal(mappedIndex([devices[0], { ...devices[1], enabled: undefined, entry: { ...devices[1].entry, enabled: undefined } }], "balkon"), 1);
});

test("saving a mapping adds its EMS device once, and moves one the EMS reads elsewhere", () => {
  const proposal = (connection) => ({
    id: `p-${connection}`,
    broker_ref: "local_mqtt_x",
    catalog_device_id: "catalog:v1:dev-balkon",
    catalog_connection_id: connection,
    config_fragment: { type: "external_mqtt", mqtt: { broker_ref: "local_mqtt_x", topic_family: "ems_solarflow", device_id: "balkon" } },
  });
  const inverter = { kind: "local_api", name: "INV_1" };
  const added = withMappedDevice([inverter], proposal("catalog:v1:conn-a"), "Balkon");
  assert.equal(added.length, 2);
  assert.deepEqual({ name: added[1].name, kind: added[1].kind, proposal: added[1].proposal_id, id: added[1].mqtt.device_id }, { name: "Balkon", kind: "external_mqtt", proposal: "p-catalog:v1:conn-a", id: "balkon" });
  const saved = [inverter, { kind: "external_mqtt", name: "Balkon", catalog_device_id: "catalog:v1:dev-balkon", catalog_connection_id: "catalog:v1:conn-a", entry: { mqtt: { device_id: "balkon" } } }];
  assert.equal(withMappedDevice(saved, proposal("catalog:v1:conn-a"), "Balkon"), null);
  const moved = withMappedDevice(saved, proposal("catalog:v1:conn-b"), "Balkon");
  assert.equal(moved.length, 2);
  assert.deepEqual({ name: moved[1].name, connection: moved[1].catalog_connection_id, proposal: moved[1].proposal_id }, { name: "Balkon", connection: "catalog:v1:conn-b", proposal: "p-catalog:v1:conn-b" });
});

test("a new mapping label renames its EMS device, unless another device has that name", () => {
  const devices = [{ kind: "local_api", name: "Garage" }, { kind: "external_mqtt", name: "Balkon", entry: { mqtt: { device_id: "balkon" } } }];
  assert.equal(withRenamedDevice(devices, "balkon", " Balkon Süd ")[1].name, "Balkon Süd");
  assert.equal(withRenamedDevice(devices, "balkon", "garage"), null);
  assert.equal(withRenamedDevice(devices, "balkon", "Balkon"), null);
  assert.equal(withRenamedDevice(devices, "balkon", " "), null);
  assert.equal(withRenamedDevice(devices, "terrasse", "Terrasse"), null);
});

test("an EMS device is named after its mapping's label unless another device has that name", () => {
  const devices = [{ name: "INV_1" }, { name: "Garage" }];
  assert.equal(emsDeviceName("  Balkon ", devices), "Balkon");
  assert.equal(emsDeviceName("garage", devices), "INV_3");
  assert.equal(emsDeviceName("", devices), "INV_3");
  assert.equal(emsDeviceName(undefined, []), "INV_1");
});

test("the devices that wait for a restart are the server's waiting and leaving ones, none while it cannot tell", () => {
  const devices = {
    balkon: { state: "running", change: null, name: "Balkon", ran: "Balkon" },
    garage: { state: "waiting", change: "renamed", name: "Garage Nord", ran: "Garage" },
    alt: { state: "leaving", change: "removed", name: "Alt", ran: "Alt" },
    aus: { state: "off", change: null, name: "Aus", ran: "Aus" },
  };
  assert.deepEqual(emsWaiting({ known: true, devices }).map((item) => [item.id, item.change]), [["garage", "renamed"], ["alt", "removed"]]);
  assert.deepEqual(emsWaiting({ known: false, devices }), []);
  assert.deepEqual(emsWaiting(undefined), []);
});

test("the running EMS reads a saved device once the server says it runs it, and a removed one once it has none or reads what is left under its id as saved", () => {
  const known = { known: true, devices: { balkon: { state: "running" }, aus: { state: "off" }, garage: { state: "waiting" }, alt: { state: "leaving" } } };
  assert.equal(emsReads(known, "balkon"), true);
  assert.equal(emsReads(known, "aus"), true);
  assert.equal(emsReads(known, "garage"), false);
  assert.equal(emsReads(known, "neu"), false);
  assert.equal(emsReads(known, "alt", true), false);
  assert.equal(emsReads(known, "neu", true), true);
  assert.equal(emsReads(known, "balkon", true), true);
  assert.equal(emsReads(known, "garage", true), false);
  assert.equal(emsReads({ known: false, devices: {} }, "balkon"), null);
});

test("an added device the running EMS still has from before it was removed is added again", () => {
  assert.equal(savedKind("added", { change: "changed", ran: "Balkon" }), "again");
  assert.equal(savedKind("added", { change: "added", ran: null }), "added");
  assert.equal(savedKind("added", undefined), "added");
  assert.equal(savedKind("moved", { change: "changed", ran: "Balkon" }), "moved");
  assert.equal(savedKind("removed", { change: "removed", ran: "Balkon" }), "removed");
});

test("a device the running EMS never had stays added, whatever was done to it since", () => {
  assert.equal(savedKind("renamed", { change: "added", ran: null }), "added");
  assert.equal(savedKind("moved", { change: "added", ran: null }), "added");
  assert.equal(savedKind("renamed", { change: "renamed", ran: "Garage" }), "renamed");
});

test("a name set back to the one the running EMS shows, on a device changed otherwise meanwhile, is said as a change rather than a move", () => {
  assert.equal(savedKind("renamed", { change: "changed", name: "Garage", ran: "Garage" }), "changed");
});

test("after a restart, a new EMS of another release is said at once, and no new EMS once the grace is over", () => {
  const before = "2026-11-06T06:00:00+00:00";
  const fresh = "2026-11-06T06:05:00+00:00";
  assert.equal(emsSilence({ at: 200, known: false, reason: "unnamed", startedAt: fresh }, 100, 150, before), "unnamed");
  assert.equal(emsSilence({ at: 200, known: true, reason: null, startedAt: fresh }, 100, 150, before), null);
  assert.equal(emsSilence({ at: 200, known: false, reason: "unnamed", startedAt: before }, 100, 150, before), null);
  assert.equal(emsSilence({ at: 200, known: true, reason: null, startedAt: before }, 100, 100 + EMS_REPORT_GRACE_MS, before), null);
  assert.equal(emsSilence({ at: 200, known: true, reason: null, startedAt: before }, 100, 101 + EMS_REPORT_GRACE_MS, before), "stopped");
  assert.equal(emsSilence({ at: 200, known: false, reason: "stopped", startedAt: null }, 100, 101 + EMS_REPORT_GRACE_MS, before), "stopped");
  assert.equal(emsSilence({ at: 200, known: false, reason: "unnamed", startedAt: null }, 100, 101 + EMS_REPORT_GRACE_MS, before), "unnamed");
  assert.equal(emsSilence({ at: 50, known: false, reason: "stopped", startedAt: null }, 100, 1e12, before), null);
  assert.equal(emsSilence({ at: 200, known: false, reason: "stopped", startedAt: null }, 0, 1e12, before), null);
});

test("the step band names the next step, and all four are done once every mapped device is in the EMS", () => {
  assert.deepEqual(MAPPING_STEPS, ["broker", "connect", "map", "ems"]);
  assert.equal(mappingStep(), 0);
  assert.equal(mappingStep({ readable: true }), 1);
  assert.equal(mappingStep({ readable: true, groups: 2 }), 2);
  assert.equal(mappingStep({ groups: 2, devices: 1 }), 3);
  assert.equal(mappingStep({ devices: 2, inEms: 1 }), 3);
  assert.equal(mappingStep({ devices: 2, inEms: 2 }), MAPPING_STEPS.length);
});

test("an id the mapping already uses gets a number, and stays within 64 characters", () => {
  assert.equal(uniqueId("balkon", []), "balkon");
  assert.equal(uniqueId("balkon", ["balkon"]), "balkon-2");
  assert.equal(uniqueId("balkon", ["balkon", "balkon-2"]), "balkon-3");
  assert.equal(uniqueId("x".repeat(64), ["x".repeat(64)]), `${"x".repeat(62)}-2`);
});

test("a name becomes an id the catalog accepts", () => {
  assert.equal(idFromName("Balkon Süd"), "balkon-sued");
  assert.equal(idFromName("  Größe/Ölheizung  "), "groesse-oelheizung");
  assert.equal(idFromName("Café"), "cafe");
  assert.equal(idFromName("!!!"), "device");
  assert.equal(idFromName("x".repeat(80)).length, 64);
});

const shelly = { topic: "shellyplusplugs-x/status/switch:0", path: "apower", value: -612.3, rate: 4, label: "shelly_gen2" };

test("a balcony inverter is one power value for inverter and solar power, turned round", () => {
  const device = deviceFromAnswers({ kind: "balcony", name: "Balkon", brokerRef: "ems-mqtt", power: shelly });
  const source = { topic: shelly.topic, format: "json", path: "apower", factor: 1, invert: true, negative: "zero", max_age_seconds: 600 };
  assert.deepEqual(device, {
    id: "balkon",
    label: "Balkon",
    enabled: true,
    broker_ref: "ems-mqtt",
    values: { inverterPower: { sources: [source] }, solarPower: { sources: [source] } },
  });
});

test("an AC battery maps its output, its battery power and its charge level", () => {
  const output = { topic: "akku/out", path: null, value: 0.3, rate: 6, unit: "kW" };
  const battery = { topic: "akku/battery", path: null, value: -300, rate: 6 };
  const soc = { topic: "akku/soc", path: null, value: 0.81, rate: 6, unit: "fraction" };
  const device = deviceFromAnswers({ kind: "ac_battery", name: "Keller Akku", brokerRef: "iobroker", power: output, battery, soc });
  assert.deepEqual(Object.keys(device.values), ["inverterPower", "batteryPower", "batterySoc"]);
  assert.equal(device.values.inverterPower.sources[0].factor, 1000);
  assert.equal(device.values.inverterPower.sources[0].format, "number");
  assert.equal(device.values.batteryPower.sources[0].invert, false);
  assert.equal(device.values.batterySoc.sources[0].factor, 100);
  assert.equal(device.id, "keller-akku");
});

test("a battery's negative output may be charging, so it is clipped at 0 and never turned round", () => {
  const output = { topic: "akku/out", path: null, value: -800, interval: 10 };
  const device = deviceFromAnswers({ kind: "ac_battery", name: "Akku", brokerRef: "ems-mqtt", power: output });
  assert.deepEqual(
    { invert: device.values.inverterPower.sources[0].invert, negative: device.values.inverterPower.sources[0].negative },
    { invert: false, negative: "zero" },
  );
  assert.equal(device.values.inverterPower.sources[0].max_age_seconds, 60);
});

test("real power is preselected over apparent and reactive power, the total over a phase", () => {
  const tasmota = [
    { topic: "tele/plug/SENSOR", path: "ENERGY.ApparentPower", value: 700, likely: "power" },
    { topic: "tele/plug/SENSOR", path: "ENERGY.Power", value: 612, likely: "power" },
    { topic: "tele/plug/SENSOR", path: "ENERGY.ReactivePower", value: 90, likely: "power" },
  ];
  assert.equal(likelyValue(tasmota).path, "ENERGY.Power");
  const pro3em = [
    { topic: "pro3em/status/em:0", path: "a_act_power", value: 500, likely: "power" },
    { topic: "pro3em/status/em:0", path: "total_act_power", value: 1520, likely: "power" },
  ];
  assert.equal(likelyValue(pro3em).path, "total_act_power");
  const dtu = [{ topic: "solar/1161234567/0/power", path: null, value: 410, likely: "power" }];
  assert.equal(likelyValue(dtu).topic, "solar/1161234567/0/power");
});

test("a hybrid inverter leaves out what was not chosen", () => {
  const device = deviceFromAnswers({ kind: "hybrid", name: "WR", power: { ...shelly, value: 500 } });
  assert.deepEqual(Object.keys(device.values), ["inverterPower"]);
  assert.equal(device.values.inverterPower.sources[0].invert, false);
});

test("a group's values carry the topic, its rate and the label, and the likely power is preselected", () => {
  const group = { label: "shelly_gen2", topics: [{ topic: "p/status/switch:0", messages_per_minute: 2, values: [{ path: "voltage", value: 230, likely: null }, { path: "apower", value: -5, likely: "power" }] }] };
  const values = groupValues(group);
  assert.equal(values.length, 2);
  assert.deepEqual(likelyValue(values), { topic: "p/status/switch:0", path: "apower", value: -5, likely: "power", rate: 2, interval: null, label: "shelly_gen2" });
  assert.equal(likelyValue(values, "soc"), null);
  assert.deepEqual(sourceFromValue(likelyValue(values), "inverterPower").max_age_seconds, 600);
});

test("the broker a mapping starts on is the chosen one while it can be read, else the running built-in one, else any readable one", () => {
  const builtinOff = { ref: BUILTIN_BROKER, kind: "builtin", usable: false };
  const builtinOn = { ref: BUILTIN_BROKER, kind: "builtin", usable: true };
  const found = { ref: "broker-192-168-1-7-1883", kind: "found", host: "192.168.1.7", port: 1883, usable: true };
  const locked = { ref: "broker-192-168-1-9-1883", kind: "found", usable: false };

  assert.equal(preferredBroker([builtinOn, found], found.ref), found.ref);
  assert.equal(preferredBroker([builtinOn, found], ""), BUILTIN_BROKER);
  assert.equal(preferredBroker([builtinOff, locked, found], ""), found.ref);
  assert.equal(preferredBroker([builtinOff, locked], locked.ref), BUILTIN_BROKER);
  assert.equal(preferredBroker([], ""), BUILTIN_BROKER);
  assert.equal(brokerAddress(found), "192.168.1.7:1883");
  assert.equal(brokerAddress({ ref: BUILTIN_BROKER, host: null }), "");
});

test("a consumer is one power value on its own family, a negative reading turned round", () => {
  const device = deviceFromAnswers({ kind: "consumer", name: "Washer", brokerRef: "ems-mqtt", power: { ...shelly, value: -437.2 } });
  assert.equal(device.family, "ems_consumer");
  assert.deepEqual(Object.keys(device.values), ["power"]);
  assert.equal(device.values.power.sources[0].invert, true);
  assert.equal(device.values.power.sources[0].negative, "zero");
});

test("a consumer added without a free label keeps the name it is offered under, not an INV_ name", () => {
  const offered = {
    id: "p-washer",
    broker_ref: "local_mqtt_x",
    catalog_connection_id: "conn-w",
    config_fragment: { type: "external_mqtt", name: "External consumer washer", mqtt: { broker_ref: "local_mqtt_x", topic_family: "ems_consumer", device_id: "washer" } },
  };
  const devices = [{ kind: "local_api", name: "Washer" }];
  assert.equal(withMappedDevice(devices, offered, "Washer")[1].name, "External consumer washer");
  assert.equal(withMappedDevice(devices, offered, "Dryer")[1].name, "Dryer");
});
