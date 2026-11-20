// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
// What the console must never change by itself when it adds or moves a device:
// whether the device is active, where its control comes from, and which
// identity it carries. The server decides control and identity; these pin that
// the browser only carries its answer.
import test from "node:test";
import assert from "node:assert/strict";
import {
  addFound,
  brokerProfile,
  identitiesMatch,
  meterFromProposal,
  mergeObservation,
  mqttDeviceFromProposal,
  observationKey,
  switchTransport,
} from "../src/lib/discovery.js";
import { downloadPassword, itemFromDevice } from "../src/lib/setupDraft.js";

const ID = "opaque:v1:aaaa";
const CONN = "conn:v1:aaaa";
const catalog = { default_device: { common: { max_power: 800, min_soc: 10 } }, hardware_sections: [{ id: "devices", fields: [{ path: "devices[].name" }, { path: "devices[].max_power" }] }] };

function proposal({ control }) {
  return {
    id: "p1",
    broker_ref: "broker-1",
    device_id: "SN-SERIAL",
    serial_number: "SN-SERIAL",
    physical_identity_token: ID,
    output_control_supported: control,
    config_fragment: { mqtt: { broker_ref: "broker-1", topic_family: "zendure_local", product_key: "pk" }, capabilities: { write_output_limit: control } },
  };
}

test("a device added from an MQTT proposal is active, takes the central defaults and its control from the server's fragment", () => {
  const controlled = mqttDeviceFromProposal(proposal({ control: true }), [], catalog);
  assert.equal(controlled.enabled, true);
  assert.equal(controlled.output_control, true);
  assert.equal(controlled.capabilities.write_output_limit, true);
  assert.equal(controlled.trusted_write_target, true);
  assert.equal(controlled.max_power, 800);
  assert.equal(controlled.mqtt.device_id, "", "a serial never becomes the MQTT route");
  const telemetry = mqttDeviceFromProposal(proposal({ control: false }), [], catalog);
  assert.equal(telemetry.enabled, true);
  assert.equal(telemetry.output_control, false);
  assert.equal(telemetry.capabilities.write_output_limit, false);
});

test("a device added from the search or the guided setup is active", () => {
  const added = addFound({ devices: [] }, { role: "inverter", state: "new", discovered: { observation_id: "obs:v1:one", ip: "192.168.1.20", serial_number: "SN1" } }, catalog);
  assert.equal(added.devices[0].enabled, true);
  const item = itemFromDevice({ observation_id: "obs:v1:one", ip: "192.168.1.20", serial_number: "SN1" }, "inverter", { id: "draft-1", items: [] });
  assert.equal(item.enabled, true);
});

test("a switch of connection keeps whether the device is active, both ways, and claims control only where the new connection writes", () => {
  for (const enabled of [true, false]) {
    for (const control of [true, false]) {
      const api = [{ name: "Garage", kind: "local_api", physical_device_id: ID, enabled, ip: "192.168.1.5" }];
      const toMqtt = switchTransport(api, ID, "zendure_mqtt", { proposal: proposal({ control }) }, catalog).devices[0];
      assert.equal(toMqtt.enabled, enabled, `api→mqtt enabled=${enabled} control=${control}`);
      assert.equal(toMqtt.output_control, control, `api→mqtt control=${control}`);
      assert.equal(toMqtt.capabilities.write_output_limit, control);
      const back = switchTransport([toMqtt], ID, "local_api", { discovered: { ip: "192.168.1.6", physical_device_id: ID, connection_id: CONN } }, catalog).devices[0];
      assert.equal(back.enabled, enabled, `mqtt→api enabled=${enabled}`);
      assert.equal(back.kind, "local_api");
    }
  }
});

test("a grid meter comes only from the server's fragment, and its broker profile carries no secret", () => {
  assert.equal(meterFromProposal({ broker_ref: "b", topic: "tele/meter" }), null);
  assert.equal(meterFromProposal({ grid_meter_fragment: { type: "" }, topic: "tele/meter" }), null);
  const meter = meterFromProposal({ broker_ref: "b", topic: "tele/meter", grid_meter_fragment: { type: "zendure_mqtt_d0", mqtt: { topic: "tele/meter" } } });
  assert.deepEqual(meter, { present: true, type: "zendure_mqtt_d0", mqtt: { topic: "tele/meter", broker_ref: "b" } });
  const profile = brokerProfile({ broker_ref: "b", broker_host: "10.0.0.2", password: "secret", username: "u", credentials_ref: "cred-1" }, {});
  assert.deepEqual(Object.keys(profile).sort(), ["credentials_ref", "host", "port", "ref", "source", "tls", "tls_insecure", "tls_mode"]);
  assert.ok(!JSON.stringify(profile).includes("secret"));
});

test("two observations are never one device on what the browser sees alone, and a masked serial keeps them apart", () => {
  assert.equal(identitiesMatch({ serial_number: "SN1", ip: "192.168.1.5" }, { serial_number: "SN1", ip: "192.168.1.5" }), false);
  const first = { serial_number: "SN1***", ip: "192.168.1.5" };
  const second = { serial_number: "SN1***", ip: "192.168.1.6" };
  assert.notEqual(observationKey(first), observationKey(second));
  assert.ok(!observationKey(first).includes("SN1"));
  const found = mergeObservation(mergeObservation({}, first, "scan"), second, "scan");
  assert.equal(Object.keys(found).length, 2);
});

test("a configuration download asks for its password twice", () => {
  assert.deepEqual(downloadPassword("short", "short"), { error: "downloadShort" });
  assert.deepEqual(downloadPassword("long enough password", "long enough passw0rd"), { error: "downloadDiffer" });
  assert.deepEqual(downloadPassword("long enough password", "long enough password"), { password: "long enough password" });
});
