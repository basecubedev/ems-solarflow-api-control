// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { UNITS, deviceFromAnswers, freshGroup, groupValues, idFromName, isChangeOnly, likelyValue, mappedProposal, maxAgeFor, signPreset, sourceFromValue, uniqueId, unitFactor, unitOf } from "../src/lib/mqtt.js";

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

test("a mapped device is adopted only through the one proposal that carries its id", () => {
  const external = (id, broker) => ({ id: `${broker}-${id}`, config_fragment: { type: "external_mqtt", mqtt: { device_id: id, broker_ref: broker } } });
  const zendure = { id: "z", config_fragment: { type: "zendure_mqtt", mqtt: { device_id: "balkon" } } };
  assert.equal(mappedProposal([zendure, external("balkon", "ems-mqtt")], "balkon").id, "ems-mqtt-balkon");
  assert.equal(mappedProposal([external("balkon", "ems-mqtt"), external("balkon", "iobroker")], "balkon"), null);
  assert.equal(mappedProposal([external("other", "ems-mqtt")], "balkon"), null);
  assert.equal(mappedProposal(undefined, "balkon"), null);
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
