// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { RUNTIME_STATES, STATUS_SOURCES, telemetryWanted, brokerView, telemetryDeviceView } from "../src/lib/telemetry.js";

const SERVER = readFileSync(new URL("../../../admin/zendure_mqtt_runtime_status.py", import.meta.url), "utf8");

test("the runtime states and status sources are the server's", () => {
  const states = new Set([...SERVER.matchAll(/"runtime_state": "([a-z]+)"|return "([a-z]+)" if status/g)].map((match) => match[1] || match[2]));
  states.add("inactive");
  assert.deepEqual([...states].filter((state) => !Object.hasOwn(RUNTIME_STATES, state)).sort(), []);
  const sources = new Set([...SERVER.matchAll(/source="([a-z_]+)"/g)].map((match) => match[1]));
  assert.deepEqual([...sources].filter((source) => !STATUS_SOURCES.includes(source)).sort(), []);
});

test("telemetry hides only behind a proven 'nothing configured'", () => {
  assert.equal(telemetryWanted(null), true);
  assert.equal(telemetryWanted({ available: false, configured_device_count: 0, invalid_device_count: 0, broker_count: 0, broker_configured: false }), true);
  assert.equal(telemetryWanted({ configured_device_count: 0, invalid_device_count: 0, broker_count: 0, broker_configured: false }), false);
  assert.equal(telemetryWanted({ broker_configured: true }), true);
  assert.equal(telemetryWanted({ invalid_device_count: 1 }), true);
});

test("a broker's state and issue, a device's status", () => {
  assert.equal(brokerView({ enabled: true, connected: true }).state, "connected");
  assert.equal(brokerView({ enabled: true, running: true }).state, "running");
  assert.equal(brokerView({ enabled: false, connected: true }).state, "disabled");
  assert.equal(brokerView({ enabled: true, issue: "broker_auth_missing" }).issue, "broker_auth_missing");
  assert.equal(brokerView({ enabled: true, issue: "who_knows" }).issue, null);
  assert.deepEqual([telemetryDeviceView({ status: "online" }).known, telemetryDeviceView({ status: "glitchy" }).known], [true, false]);
  assert.equal(telemetryDeviceView({}).status, "unseen");
});
