// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { PAIRING_FIELDS, pairingOutcome, pairingRequest } from "../src/lib/homewizard.js";
import { gridFields, keepPairing, syncGridFeatures } from "../src/lib/setupDraft.js";

test("one pairing attempt asks for the typed address and port, or for nothing without an address", () => {
  assert.deepEqual(pairingRequest(" 192.168.1.40 ", "443"), { ip: "192.168.1.40", port: 443 });
  assert.deepEqual(pairingRequest("192.168.1.40", "", " local/ems_solarflow#3f9a1c "), { ip: "192.168.1.40", port: null, pairing_name: "local/ems_solarflow#3f9a1c" });
  assert.deepEqual(pairingRequest("192.168.1.40", ""), { ip: "192.168.1.40", port: null });
  assert.deepEqual(pairingRequest("192.168.1.40", undefined), { ip: "192.168.1.40", port: null });
  assert.equal(pairingRequest("  ", 443), null);
});

test("an answer is waiting, paired with serial and token, or failed with the server's reason", () => {
  assert.deepEqual(pairingOutcome({ state: "waiting" }), { phase: "waiting" });
  assert.deepEqual(pairingOutcome({ state: "paired", serial: "0a1b2c3d4e5f", token: "T", pairing_name: "local/ems_solarflow#3f9a1c" }), { phase: "paired", serial: "0a1b2c3d4e5f", token: "T", pairingName: "local/ems_solarflow#3f9a1c" });
  assert.deepEqual(pairingOutcome({ state: "paired", serial: "0a1b2c3d4e5f", token: "T" }), { phase: "failed", reason: "", message: "" });
  assert.deepEqual(pairingOutcome({ state: "failed", reason: "unreachable", message: "cannot reach" }), { phase: "failed", reason: "unreachable", message: "cannot reach" });
  assert.deepEqual(pairingOutcome({ error: "Enter the P1 Meter's IP address." }), { phase: "failed", reason: "", message: "Enter the P1 Meter's IP address." });
});

test("the setup shows the pairing panel instead of plain serial and token inputs", () => {
  const catalog = {
    sections: [{ id: "grid_meter", fields: [{ path: "grid_meter.ip" }, { path: "grid_meter.serial", level: "advanced" }, { path: "grid_meter.token" }, { path: "grid_meter.pairing_name", level: "advanced" }] }],
    grid_meter_variants: { homewizard_p1: { fields: ["grid_meter.ip", ...PAIRING_FIELDS] } },
  };
  assert.deepEqual(gridFields(catalog, "homewizard_p1"), { normal: [], advanced: [], expert: [] });
});

test("a setup pairing belongs to the meter it was made with: another address or type leaves it behind", () => {
  const paired = { "grid_meter.type": "homewizard_p1", "grid_meter.ip": "192.168.1.40", "grid_meter.serial": "0a1b2c3d4e5f", "grid_meter.token": "T", "grid_meter.pairing_name": "local/ems_solarflow#3f9a1c" };
  assert.deepEqual(keepPairing(paired, { ...paired }), paired);
  assert.deepEqual(keepPairing(paired, { ...paired, "grid_meter.ip": "192.168.1.42" }), { "grid_meter.type": "homewizard_p1", "grid_meter.ip": "192.168.1.42" });
  const other = syncGridFeatures(paired, { role: "grid_meter", ip: "192.168.1.42", api_family: "homewizard_p1", grid_meter_type: "homewizard_p1" }).features;
  assert.equal(other["grid_meter.ip"], "192.168.1.42");
  assert.ok(!("grid_meter.token" in other) && !("grid_meter.serial" in other));
  const same = syncGridFeatures(paired, { role: "grid_meter", ip: "192.168.1.40", api_family: "homewizard_p1", grid_meter_type: "homewizard_p1" }).features;
  assert.equal(same["grid_meter.token"], "T");
  const shelly = syncGridFeatures(paired, { role: "grid_meter", ip: "192.168.1.40", api_family: "shelly_gen2", grid_meter_type: "shelly" }).features;
  assert.ok(!("grid_meter.token" in shelly));
});

test("an API key stays with its meter type and shows as a field, a pairing token does not", () => {
  const catalog = {
    sections: [{ id: "grid_meter", fields: [{ path: "grid_meter.ip" }, { path: "grid_meter.token", type: "password", secret: true }] }],
    grid_meter_variants: { powerfox_poweropti: { fields: ["grid_meter.ip", "grid_meter.token"] } },
  };
  assert.deepEqual(gridFields(catalog, "powerfox_poweropti").normal.map((field) => field.path), ["grid_meter.token"]);
  const keyed = { "grid_meter.type": "powerfox_poweropti", "grid_meter.ip": "192.168.1.50", "grid_meter.token": "K" };
  assert.deepEqual(keepPairing(keyed, { ...keyed, "grid_meter.ip": "192.168.1.51" }), { ...keyed, "grid_meter.ip": "192.168.1.51" });
  assert.deepEqual(keepPairing(keyed, { ...keyed, "grid_meter.type": "shelly" }), { "grid_meter.type": "shelly", "grid_meter.ip": "192.168.1.50" });
  const paired = { "grid_meter.type": "homewizard_p1", "grid_meter.ip": "192.168.1.50", "grid_meter.token": "T", "grid_meter.serial": "0a1b2c3d4e5f" };
  assert.deepEqual(keepPairing(paired, { ...paired, "grid_meter.type": "powerfox_poweropti" }), { "grid_meter.type": "powerfox_poweropti", "grid_meter.ip": "192.168.1.50" });
  assert.deepEqual(keepPairing(keyed, { ...keyed, "grid_meter.type": "homewizard_p1" }), { "grid_meter.type": "homewizard_p1", "grid_meter.ip": "192.168.1.50" });
});
