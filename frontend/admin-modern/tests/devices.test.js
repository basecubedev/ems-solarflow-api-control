// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import {
  d0Serial,
  deviceFallbacks,
  editableDraft,
  modelsForGeneration,
  mqttControl,
  mqttDefaultPort,
  mqttUsesTls,
  portOrDefault,
  newLocalInverter,
  newMqttInverter,
  nextInverterName,
  reasonKey,
  removeDevice,
  sentDevices,
  setBroker,
  setD0Serial,
  setDeviceValue,
  setGridPassword,
  setGridPort,
  setMqttValue,
  switchGridType,
  maturityWords,
  operationWords,
} from "../src/lib/devices.js";

const catalog = {
  default_device: { common: { max_power: 800, min_soc: 10 } },
  zendure_mqtt_generations: [{ id: "zensdk", label: "New", default: true }, { id: "legacy", label: "Old" }],
  zendure_mqtt_hardware_models: [
    { id: "", label: "Unknown" },
    { id: "pro2", label: "800 Pro 2", generation: "zensdk", control_supported: true, power_write_profile: "zensdk_write" },
    { id: "hub", label: "Hub", generation: "legacy", control_supported: false },
  ],
  grid_meter_variants: { shelly: { id: "shelly", default_port: 80 }, ecotracker: { id: "ecotracker", default_port: 80 }, mqtt: { id: "mqtt", default_port: null } },
};

test("a new inverter is the next free INV name with the central defaults, never another device's values", () => {
  assert.equal(nextInverterName([{ name: "INV_1" }, { name: "Garage" }, { name: "inv_3" }]), "INV_4");
  assert.equal(nextInverterName([]), "INV_1");
  const device = newLocalInverter([{ name: "INV_1", max_power: 600 }], catalog);
  assert.deepEqual([device.name, device.max_power, device.min_soc, device.enabled], ["INV_2", 800, 10, true]);
  assert.equal(newMqttInverter([], catalog).hardware_generation, "zensdk");
});

test("an emptied tuning value is held empty until it is sent, an emptied identity value is written", () => {
  assert.equal(setDeviceValue({ max_power: 800 }, "max_power", " ").max_power, "");
  assert.equal(setDeviceValue({ min_soc: 20 }, "min_soc", "", { min_soc: 20 }).min_soc, "");
  assert.equal("min_soc" in setDeviceValue({ min_soc: "2" }, "min_soc", "", { name: "WR1" }), false);
  assert.equal(setDeviceValue({ ip: "1.2.3.4" }, "ip", "").ip, "");
});

test("a sent device writes each emptied value as the value its input named, an installation value included", () => {
  const tuned = {
    ...catalog,
    hardware_sections: [{ id: "devices", fields: [
      { path: "devices[].name", type: "text" },
      { path: "devices[].min_soc", type: "number", default: 15 },
      { path: "devices[].max_power", type: "number", default: 800, installation_value: true },
    ] }],
  };
  const [sent] = sentDevices([{ original_name: "WR1", name: "", min_soc: "", max_power: "", note: "" }], tuned, { min_soc: 10, max_power: 2400 });
  assert.deepEqual(sent, { original_name: "WR1", name: "", min_soc: 10, max_power: 2400, note: "" });
  assert.deepEqual(sentDevices([{ min_soc: "" }], tuned, {}), [{ min_soc: "" }]);
  const [kept] = sentDevices([{ original_name: "WR1", min_soc: "" }], tuned, { min_soc: 10 }, [{ original_name: "WR1", min_soc: "" }]);
  assert.equal(kept.min_soc, "");
});

test("a typed broker port is a number, and an untyped one the default port of its TLS", () => {
  assert.equal(portOrDefault(" 1884 ", false), 1884);
  assert.equal(portOrDefault("", true), 8883);
  assert.equal(portOrDefault(null, false), 1883);
  assert.equal(portOrDefault(0, false), 0);
});

test("MQTT settings mean TLS the way the EMS reads them, a TLS mode before the flag", () => {
  assert.equal(mqttUsesTls({ tls: true }), true);
  assert.equal(mqttUsesTls({ tls_mode: "system_ca" }), true);
  assert.equal(mqttUsesTls({ tls: true, tls_mode: "Plaintext" }), false);
  assert.equal(mqttUsesTls({ tls_mode: " " }), false);
  assert.equal(mqttUsesTls(null), false);
});

test("an emptied device ceiling falls back to the system's per-device ceiling", () => {
  const fields = new Map([["system.max_device_power", { default: 800 }]]);
  assert.equal(deviceFallbacks(catalog.default_device.common, { "system.max_device_power": 2400 }, fields).max_power, 2400);
  assert.equal(deviceFallbacks(catalog.default_device.common, { "system.max_device_power": " 2400 " }, fields).max_power, 2400);
  assert.equal(deviceFallbacks(catalog.default_device.common, { "system.max_device_power": "" }, fields).max_power, 800);
  assert.equal(deviceFallbacks(catalog.default_device.common, {}, new Map()).min_soc, 10);
  assert.equal(deviceFallbacks(catalog.default_device.common, {}, new Map()).max_power, 800);
});

test("an MQTT device controls only with a supported model and a complete write route", () => {
  const device = { kind: "zendure_mqtt", hardware_model: "pro2", mqtt: { device_id: "ID1" }, product_key: "PK" };
  assert.equal(mqttControl(device, catalog).shouldControl, true);
  assert.equal(mqttControl({ ...device, product_key: "" }, catalog).shouldControl, false);
  assert.equal(mqttControl({ ...device, mqtt: {} }, catalog).state, "routeMissing");
  assert.equal(mqttControl({ ...device, hardware_model: "hub" }, catalog).state, "telemetryOnly");
  assert.equal(mqttControl({ ...device, hardware_model: "" }, catalog).state, "modelRequired");
});

test("a generation drops a model it cannot be; a model brings its write profile; a key is mirrored into the route", () => {
  const device = { kind: "zendure_mqtt", hardware_generation: "zensdk", hardware_model: "pro2", mqtt: { device_id: "ID1" }, product_key: "PK" };
  const legacy = setMqttValue(device, "hardware_generation", "legacy", catalog);
  assert.deepEqual([legacy.hardware_model, legacy.power_write_profile, legacy.output_control], ["", null, false]);
  assert.equal(setMqttValue({ ...device, hardware_model: "" }, "hardware_model", "pro2", catalog).power_write_profile, "zensdk_write");
  const keyed = setMqttValue({ ...device, product_key: "" }, "product_key", "NEW", catalog);
  assert.deepEqual([keyed.product_key, keyed.mqtt.product_key, keyed.capabilities.write_output_limit], ["NEW", "NEW", true]);
  assert.deepEqual(modelsForGeneration(catalog, "legacy").map((model) => model.id), ["", "hub"]);
});

test("the editor starts from the draft with every MQTT device's control derived, as Classic loads it", () => {
  const draft = { devices: [{ kind: "zendure_mqtt", hardware_model: "pro2", mqtt: { device_id: "ID1" }, product_key: "PK", capabilities: { read_power: true, write_output_limit: false } }, { name: "Local" }] };
  const editable = editableDraft(draft, catalog);
  assert.deepEqual([editable.devices[0].output_control, editable.devices[0].capabilities.write_output_limit], [true, true]);
  assert.equal(editable.devices[1], draft.devices[1]);
  assert.equal(draft.devices[0].capabilities.write_output_limit, false);
});

test("the grid meter's port follows its type unless both share one, and comes back with a type", () => {
  const first = switchGridType({ type: "shelly", port: 8080 }, "ecotracker", catalog, {});
  assert.equal(first.meter.port, 8080);
  const second = switchGridType(first.meter, "mqtt", catalog, first.ports);
  assert.equal("port" in second.meter, false);
  assert.equal(switchGridType(second.meter, "ecotracker", catalog, second.ports).meter.port, 8080);
  assert.equal(switchGridType({}, "zendure_smartmeter_d0", catalog, {}).meter.mqtt.payload_format, "number");
});

test("a pairing or API key stays with its meter type", () => {
  const paired = { type: "homewizard_p1", ip: "192.168.1.40", token: "T", has_token: true, serial: "0a1b2c3d4e5f", pairing_name: "local/ems_solarflow#3f9a1c" };
  assert.deepEqual(switchGridType(paired, "powerfox_poweropti", catalog, {}).meter, { type: "powerfox_poweropti", ip: "192.168.1.40", present: true });
  assert.deepEqual(switchGridType(paired, "homewizard_p1", catalog, {}).meter, { ...paired, present: true });
  assert.equal("unit_id" in switchGridType({ type: "sunspec_modbus", ip: "192.168.1.64", unit_id: 200 }, "victron_gx_modbus", catalog, {}).meter, false);
});

test("a D0 serial makes the topic, and a stored password is kept, replaced or cleared, never shown", () => {
  const meter = setD0Serial({ type: "zendure_smartmeter_d0" }, " ABC ");
  assert.deepEqual([meter.mqtt.topic, d0Serial(meter.mqtt.topic), meter.d0_topic_mode], ["Zendure/sensor/ABC/totalPower", "ABC", "auto"]);
  assert.deepEqual(setGridPassword({ mqtt: { has_password: true } }, { clear: true }).mqtt, { has_password: true, clear_password: true, password: "" });
  assert.equal(setBroker({ host: "a" }, "security", "tls").tls_explicit, true);
  assert.equal(setBroker({ host: "a", present: true }, "host", "").present, false);
  assert.equal(setBroker({ host: "a", port: 1884 }, "port", " ", { port: 1884 }).port, "");
  assert.equal("port" in setBroker({ host: "a", port: "18" }, "port", "", { host: "a", port: null }), false);
  assert.equal(setGridPort({ type: "shelly", port: 8080 }, "", { port: 8080 }).port, "");
  assert.equal("port" in setGridPort({ type: "shelly", port: "8" }, "", { type: "shelly" }), false);
});

test("removing an installed external device records its reference, a new one leaves none", () => {
  const draft = { devices: [{ kind: "external_mqtt", entry: {}, entry_ref: "ref-1" }, { kind: "external_mqtt" }] };
  assert.deepEqual(removeDevice(draft, 0).removed_external, ["ref-1"]);
  assert.deepEqual(removeDevice(draft, 1).removed_external, []);
});

test("every reason the server gives for blocked control has words, and an unknown one says so", () => {
  assert.equal(reasonKey("broker_source_write_unverified"), "devices.reason.brokerUnverified");
  assert.equal(reasonKey("something_new"), "devices.reason.other");
});

test("a model's validation level and operations have this console's words, an unknown one keeps the catalogue's", () => {
  assert.deepEqual(maturityWords("deferred"), { key: "devices.maturity.deferred" });
  assert.deepEqual(maturityWords("lab_only"), { text: "lab_only", lang: "en" });
  assert.deepEqual(operationWords(["discharge", "teleport"]), [{ key: "devices.operation.discharge" }, { text: "teleport", lang: "en" }]);
  assert.deepEqual(operationWords(null), []);
});

test("an MQTT broker without a port listens on 8883 with TLS and on 1883 without", () => {
  assert.equal(mqttDefaultPort(true), 8883);
  assert.equal(mqttDefaultPort(false), 1883);
  assert.equal(mqttDefaultPort(undefined), 1883);
});
