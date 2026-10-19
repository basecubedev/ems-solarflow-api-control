// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Paired registries between Classic (dashboard/static/app.js) and Modern.
 * Each test walks Classic's list and requires Modern to honour every entry,
 * because a code Classic knows and Modern does not would silently render as
 * raw words in one of the two views.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { PAGES } from "../src/app/pages.js";
import { RULES } from "../src/lib/status.js";
import { FIRMWARE_ENUMS, acPath } from "../src/lib/devices.js";
import { CONTROL_CODES, DECISION_SENTENCES } from "../src/lib/control.js";
import { PRESET_ORDER, STATUSES, WEEKDAYS } from "../src/lib/schedule.js";
import { ANALYTICS_VIEWS } from "../src/lib/analytics.js";
import { FLOW_SCALE_LADDER, FLOW_ACTIVATE_W, FLOW_DEACTIVATE_W } from "../src/lib/flow.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "../../..");
const APP = join(ROOT, "dashboard/static/app.js");
const source = readFileSync(APP, "utf8");
const classic = createRequire(import.meta.url)(APP);

function objectKeys(name) {
  const match = source.match(new RegExp(`const ${name} = \\{([\\s\\S]*?)\\n\\};`));
  assert.ok(match, `${name} not found in app.js`);
  return [...match[1].matchAll(/^\s+([a-z0-9_]+):/gm)].map((m) => m[1]);
}

function functionTable(name) {
  const match = source.match(new RegExp(`function ${name}\\(value\\) \\{[\\s\\S]*?firmwareEnumLabel\\(value, \\{([\\s\\S]*?)\\}`));
  assert.ok(match, `${name} not found in app.js`);
  return [...match[1].matchAll(/(\d+):/g)].map((m) => Number(m[1])).sort();
}

test("every Classic tab has a Modern page", () => {
  const tabs = [...readFileSync(join(ROOT, "dashboard/static/index.html"), "utf8").matchAll(/data-flow-view="([a-z_]+)"/g)].map((m) => m[1]);
  assert.ok(tabs.length >= 9);
  const covered = new Set(PAGES.map((page) => page.classicView));
  assert.deepEqual(tabs.filter((tab) => !covered.has(tab)), []);
});

test("the rule list is Classic's, in Classic's order", () => {
  const match = source.match(/function renderRules\(rules\) \{[\s\S]*?const labels = \[([\s\S]*?)\];/);
  const keys = [...match[1].matchAll(/\["([a-z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual(RULES.map((rule) => rule.key), keys);
});

test("firmware enum tables cover exactly Classic's values", () => {
  const tables = {
    soc_limit: "socLimitStatusLabel",
    pack_state: "packStateLabel",
    ac_mode: "acModeLabel",
    ac_status: "acStatusLabel",
    dc_status: "dcStatusLabel",
    grid_state: "gridStateLabel",
    soc_status: "socStatusLabel",
  };
  for (const [field, fn] of Object.entries(tables)) {
    assert.deepEqual(Object.keys(FIRMWARE_ENUMS[field]).map(Number).sort(), functionTable(fn), field);
  }
});

test("the AC path agrees with Classic on known and unknown states", () => {
  for (const acStatus of [0, 1, 2, 3]) {
    for (const acMode of [0, 1, 2]) {
      const classicLabel = classic.acPathLabel({ ac_status: acStatus, ac_mode: acMode });
      const modern = acPath({ ac_status: acStatus, ac_mode: acMode });
      assert.equal(classicLabel.startsWith("Unknown"), modern.key === "fw.unknownValue", `${acStatus}/${acMode}`);
    }
  }
});

test("control codes and decision sentences are Classic's", () => {
  assert.deepEqual([...CONTROL_CODES].sort(), Object.keys(classic.CONTROL_CODE_LABELS).sort());
  assert.deepEqual([...DECISION_SENTENCES].sort(), Object.keys(classic.CONTROL_DECISION_SENTENCES).sort());
});

test("schedule presets, statuses and weekdays are Classic's", () => {
  assert.deepEqual([...PRESET_ORDER], objectKeys("SCHEDULE_PRESET_LABELS"));
  assert.deepEqual([...STATUSES].sort(), objectKeys("SCHEDULE_STATUS_TONE").sort());
  const weekdays = [...source.match(/const SCHEDULE_WEEKDAYS = \[([^\n]*)\];/)[1].matchAll(/\["([a-z]{2})"/g)].map((m) => m[1]);
  assert.deepEqual([...WEEKDAYS], weekdays);
});

test("analytics views request the series Classic's tabs request", () => {
  const classicTabs = Object.fromEntries(classic.ANALYTICS_TABS.map((tab) => [tab.id, tab.series]));
  for (const view of ANALYTICS_VIEWS) assert.deepEqual(view.series, classicTabs[view.id], view.id);
  assert.deepEqual(ANALYTICS_VIEWS.map((v) => v.id).sort(), Object.keys(classicTabs).sort());
});

test("the flow hysteresis and the scale ladder are Classic's", () => {
  assert.equal(Number(source.match(/const FLOW_ACTIVATE_THRESHOLD_W = (\d+);/)[1]), FLOW_ACTIVATE_W);
  assert.equal(Number(source.match(/const FLOW_DEACTIVATE_THRESHOLD_W = (\d+);/)[1]), FLOW_DEACTIVATE_W);
  const ladder = source.match(/const FLOW_SCALE_LADDER = \[([^\]]*)\];/)[1].split(",").map((v) => Number(v.trim()));
  assert.deepEqual([...FLOW_SCALE_LADDER], ladder);
});
