// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import {
  changeCount,
  changeGroups,
  choices,
  defaultHint,
  fieldIndex,
  fieldMatches,
  levelled,
  liveEdits,
  overrideTargets,
  restartNeeded,
  sameAsSaved,
  safetyBlocks,
  sectionTab,
  sharedRisk,
  shownValue,
  storable,
  switchedOn,
  tabSections,
} from "../src/lib/settings.js";

const winter = {
  id: "winter",
  path: "winter",
  enabled_path: "winter.enabled",
  setup_group: "features",
  fields: [
    { path: "winter.enabled", type: "boolean", default: false },
    { path: "winter.months", type: "month_list", level: "normal", label: "Months", default: [11, 12, 1] },
    { path: "winter.ramp", type: "number", level: "advanced", label: "Ramp step", description: "Watts per step" },
    { path: "winter.old", type: "number", level: "deprecated" },
  ],
};
const system = {
  id: "system",
  path: "system",
  setup_group: "advanced",
  groups: [
    { id: "limits", title: "Limits", order: 3 },
    { id: "safety_gates", title: "Write gates", order: 1 },
  ],
  fields: [
    { path: "system.allow_hardware_writes", type: "boolean", default: true, group: "safety_gates", level: "advanced" },
    { path: "system.max_total_power", type: "number", group: "limits", level: "normal" },
    { path: "system.loop_interval", type: "number", level: "expert", risk: "control_stability" },
  ],
};
const editor = { catalog: { feature_sections: [system, winter] } };

test("a switch the values do not hold is the catalogue's default, not off", () => {
  assert.equal(switchedOn(winter, {}), false);
  assert.equal(switchedOn(winter, { "winter.enabled": true }), true);
  assert.equal(switchedOn({ ...winter, fields: [{ path: "winter.enabled", default: true }] }, {}), true);
  assert.equal(switchedOn(system, {}), null);
});

test("each section lands on one tab, its fields by level without the switch, safety fields and hidden levels", () => {
  assert.deepEqual([sectionTab(winter), sectionTab(system)], ["features", "expert"]);
  assert.deepEqual(tabSections(editor, "features").map((section) => section.id), ["winter"]);
  const levels = levelled(winter);
  assert.deepEqual([levels.normal, levels.advanced, levels.expert].map((list) => list.map((field) => field.path)), [["winter.months"], ["winter.ramp"], []]);
  assert.deepEqual(levelled(system).expert.map((field) => field.path), ["system.loop_interval"]);
  assert.deepEqual(levelled(system).normal, []);
});

test("the safety tab is one flat block per safety group, in the catalogue's order", () => {
  const blocks = safetyBlocks(editor);
  assert.deepEqual(blocks.map((block) => [block.id, block.fields.map((field) => field.path)]), [
    ["safety_gates", ["system.allow_hardware_writes"]],
    ["limits", ["system.max_total_power"]],
  ]);
});

test("a risk is said once for a block only when every field shares it", () => {
  const risky = { risk: "control_stability" };
  assert.equal(sharedRisk([risky, risky]), "control_stability");
  assert.equal(sharedRisk([risky, {}]), null);
  assert.equal(sharedRisk([risky]), null);
  assert.equal(sharedRisk([{ risk: "secret" }, { risk: "secret" }]), null);
});

test("an unset switch shows its default, a list shows as text, an empty input names the default", () => {
  const gate = system.fields[0];
  assert.equal(shownValue(gate, undefined), true);
  assert.equal(shownValue(gate, false), false);
  assert.equal(shownValue(winter.fields[1], [11, 12]), "11, 12");
  assert.equal(shownValue(winter.fields[2], null), "");
  assert.equal(defaultHint(winter.fields[1], undefined), "11, 12, 1");
  assert.equal(defaultHint(winter.fields[1], [1]), "");
  assert.equal(defaultHint(gate, undefined), "");
  assert.deepEqual(choices({ options: ["a", "b"] }, "c"), ["c", "a", "b"]);
  assert.deepEqual(choices({ options: ["a", "b"] }, "a"), ["a", "b"]);
});

test("changes are counted per setting, device and block, the way Classic counts them", () => {
  const saved = { features: { "a.x": 1 }, devices: [{ name: "A" }], grid_meter: { type: "shelly" } };
  assert.equal(changeCount(saved, saved), 0);
  assert.equal(changeCount({ ...saved, features: { "a.x": 2, "a.y": "" } }, saved), 2);
  assert.equal(changeCount({ ...saved, devices: [{ name: "B" }, { name: "C" }], grid_meter: { type: "mqtt" } }, saved), 3);
});

test("every previewed change is shown, grouped by whether it needs a restart", () => {
  const groups = changeGroups({
    changes: [{ path: "a", applies_live: true }],
    added: [{ path: "b" }],
    removed: [{ path: "c", applies_live: false }],
  });
  assert.deepEqual(groups.map((group) => [group.when, group.rows.map((row) => `${row.kind}:${row.path}`)]), [
    ["live", ["changed:a"]],
    ["restart", ["added:b", "removed:c"]],
  ]);
  assert.deepEqual(changeGroups(null), []);
});

test("only the dashboard's overrides are reset, a device by its opaque identity", () => {
  const targets = overrideTargets({
    "system.max_total_power": { source: "dashboard_override" },
    "winter.enabled": { source: "dashboard_override" },
    "system.enabled": { source: "config" },
    devices: {
      INV_1: { enabled: { source: "dashboard_override", physical_identity_token: "opaque:v1:abc" }, max_power: { source: "config" } },
      INV_2: { enabled: { source: "dashboard_override", physical_identity_token: "raw-serial" } },
    },
  });
  assert.deepEqual(targets, [
    { scope: "system", key: "max_total_power" },
    { scope: "section", section: "winter", key: "enabled" },
    { scope: "device", name: "INV_1", key: "enabled", physical_identity_token: "opaque:v1:abc" },
    { scope: "device", name: "INV_2", key: "enabled" },
  ]);
});

test("the search finds a field by label, description or path, ignoring case", () => {
  const field = winter.fields[2];
  assert.ok(fieldMatches(field, "RAMP"));
  assert.ok(fieldMatches(field, "per step"));
  assert.ok(fieldMatches(field, "winter.ramp"));
  assert.ok(fieldMatches(field, "  "));
  assert.ok(!fieldMatches(field, "battery"));
});

test("the search finds a field by the words the page shows for it, too", () => {
  const field = winter.fields[2];
  const shown = (text) => (text === field.label ? "Schrittweite der Reserve" : text);
  assert.ok(fieldMatches(field, "schrittweite", shown));
  assert.ok(fieldMatches(field, "RAMP", shown));
  assert.ok(!fieldMatches(field, "schrittweite"));
});

test("a value is no change when it is what the config says, as saved, as shown, or as the default of an absent switch", () => {
  const number = { path: "system.max_total_power", type: "number" };
  const gate = { path: "system.allow_hardware_writes", type: "boolean", default: true };
  assert.ok(sameAsSaved(number, 800, { "system.max_total_power": 800 }));
  assert.ok(sameAsSaved(number, "800", { "system.max_total_power": 800 }));
  assert.ok(!sameAsSaved(number, "", { "system.max_total_power": 800 }));
  assert.ok(sameAsSaved(number, "", {}));
  assert.ok(!sameAsSaved(number, "900", {}));
  assert.ok(sameAsSaved(gate, true, {}));
  assert.ok(!sameAsSaved(gate, false, {}));
});

test("edits that no longer change anything, or name a field the catalogue dropped, fall away", () => {
  const fields = fieldIndex(editor);
  assert.ok(fields.has("winter.enabled") && fields.has("system.loop_interval"));
  const edits = { "winter.ramp": "", "system.loop_interval": "7", "gone.field": 1, "winter.enabled": true };
  assert.deepEqual(liveEdits(edits, { "system.loop_interval": 5 }, fields), { "system.loop_interval": "7", "winter.enabled": true });
  assert.deepEqual(liveEdits(edits, { "system.loop_interval": 7, "winter.enabled": true }, fields), {});
});

test("a restart is offered unless every change reached the running EMS", () => {
  const live = { changes: [{ path: "a", applies_live: true }] };
  assert.equal(restartNeeded(live, { runtime_sync: { applied: ["a"], skipped: [], warnings: [] } }), false);
  assert.equal(restartNeeded({ changes: [{ path: "a", applies_live: false }] }, {}), true);
  assert.equal(restartNeeded({ removed: [{ path: "a", applies_live: true }] }, {}), true);
  assert.equal(restartNeeded(live, { runtime_sync: { skipped: ["a"] } }), true);
  assert.equal(restartNeeded(live, { runtime_sync: { warnings: ["runtime sync skipped"] } }), true);
});

test("a typed broker or grid meter password never reaches the tab's storage", () => {
  const stored = storable({ base: "b", values: { zendure_mqtt: { host: "h", password: "secret" }, grid_meter: { type: "mqtt", mqtt: { topic: "t", password: "secret2" } }, devices: [] } });
  assert.ok(!JSON.stringify(stored).includes("secret"));
  assert.deepEqual([stored.values.zendure_mqtt.host, stored.values.grid_meter.mqtt.topic, stored.base], ["h", "t", "b"]);
});
