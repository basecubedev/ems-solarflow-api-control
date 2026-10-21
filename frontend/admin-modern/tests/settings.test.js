// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { deviceView, featureViews, gridMeterName } from "../src/lib/settings.js";

const config = (features = {}, overrides = {}) => ({
  draft: { features },
  overrides,
  catalog: {
    feature_sections: [
      { id: "system", path: "system", kind: "system", fields: [{ path: "system.enabled", type: "boolean", default: true }] },
      { id: "winter", path: "winter", kind: "feature", title: "Winter mode", fields: [{ path: "winter.enabled", type: "boolean", default: false }] },
      { id: "influxdb", path: "influxdb", kind: "feature", title: "Analytics", fields: [{ path: "influxdb.enabled", type: "boolean", default: true }] },
      { id: "energy_savings", path: "energy_savings", kind: "feature", title: "Energy savings", fields: [{ path: "energy_savings.price", type: "number" }] },
    ],
  },
});

test("a feature is as EMS applies it: saved, else the catalogue's default", () => {
  const views = featureViews(config({ "winter.enabled": true }));
  assert.deepEqual(views.map((view) => [view.id, view.enabled]), [["winter", true], ["influxdb", true], ["energy_savings", null]]);
  assert.equal(featureViews(config({ "influxdb.enabled": false }))[1].enabled, false);
});

test("a dashboard override wins and is marked, a config-sourced value is not an override", () => {
  const overridden = featureViews(config({ "winter.enabled": false }, { "winter.enabled": { source: "dashboard_override", effective_value: true } }))[0];
  assert.deepEqual([overridden.enabled, overridden.overridden], [true, true]);
  const same = featureViews(config({ "winter.enabled": true }, { "winter.enabled": { source: "config", effective_value: false } }))[0];
  assert.deepEqual([same.enabled, same.overridden], [true, false]);
});

test("a device says how it is reached and whether EMS may write to it", () => {
  assert.deepEqual(
    [deviceView({ kind: "local_api", name: "A", ip: "1.2.3.4", max_power: 800 }).connection, deviceView({ kind: "local_api" }).readOnly],
    ["settings.kind.local_api", false],
  );
  assert.equal(deviceView({ kind: "zendure_mqtt", mqtt: { effective_source: "zendure_cloud_mqtt", broker_ref: "cloud" } }).connection, "settings.source.zendure_cloud_mqtt");
  assert.equal(deviceView({ kind: "zendure_mqtt", mqtt: { broker_ref: "cloud" } }).address, "cloud");
  assert.equal(deviceView({ kind: "e3dc_modbus" }).readOnly, true);
  assert.deepEqual([deviceView({ kind: "satellite" }).connection, deviceView({ kind: "satellite" }).connectionText], [null, "satellite"]);
  assert.equal(deviceView({ kind: "local_api", enabled: false }).enabled, false);
  assert.equal(gridMeterName("shelly", { shelly: { label: "Shelly Pro/Plus Gen2/Gen3" } }), "Shelly Pro/Plus Gen2/Gen3");
  assert.equal(gridMeterName("acme", { shelly: { label: "Shelly" } }), "acme");
  assert.equal(gridMeterName("acme"), "acme");
});

test("a device the dashboard switched off or limited at runtime shows what EMS follows, marked as such", () => {
  const item = { name: "Terrace", original_name: "Terrace", kind: "local_api", enabled: true, max_power: 800 };
  const overrides = { devices: { Terrace: { enabled: { config_value: true, effective_value: false, source: "dashboard_override" }, max_power: { config_value: 800, effective_value: 400, source: "dashboard_override" } } } };
  const view = deviceView(item, overrides);
  assert.deepEqual([view.enabled, view.maxPower, view.overridden], [false, 400, true]);
  const plain = deviceView(item, { devices: { Terrace: { enabled: { config_value: true, effective_value: true, source: "config" } } } });
  assert.deepEqual([plain.enabled, plain.maxPower, plain.overridden], [true, 800, false]);
  assert.equal(deviceView(item).overridden, false);
});
