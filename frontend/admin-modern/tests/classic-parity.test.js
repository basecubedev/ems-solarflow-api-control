// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Classic console (admin/static/admin.js) and this one read the same
 * server, so the vocabulary both put words to has to be the same: every error
 * code Classic explains, every control and workflow state it names, and every
 * Classic page this console hands over to. Walked out of admin.js itself, the
 * paired-registry rule applied to a second front end.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { KNOWN_ERRORS } from "../src/lib/errors.js";
import { CONTROL_STATUSES, CONTROL_TRANSPORTS } from "../src/lib/control.js";
import { WORKFLOW_OWNERS, WORKFLOW_STATES, STALE_REASONS } from "../src/lib/workflow.js";
import { RUNTIME_STATES, STATUS_SOURCES, BROKER_ISSUES } from "../src/lib/telemetry.js";
import { CHECK_STATUSES, RUN_MODES } from "../src/lib/diagnostics.js";
import { SETTINGS_TABS } from "../src/lib/settings.js";
import { PAGES } from "../src/app/pages.js";
import { FINDINGS } from "../src/lib/health.js";

const CLASSIC = readFileSync(new URL("../../../admin/static/admin.js", import.meta.url), "utf8");

function tableKeys(name) {
  const start = CLASSIC.indexOf(`const ${name} = {`);
  assert.ok(start >= 0, `${name} not found in admin.js`);
  const body = CLASSIC.slice(start, CLASSIC.indexOf("\n};", start));
  return [...body.matchAll(/^ {2}([a-z_]+):/gm)].map((match) => match[1]);
}

function arrayItems(name) {
  const match = CLASSIC.match(new RegExp(`const ${name} = \\[([^\\]]*)\\]`));
  assert.ok(match, `${name} not found in admin.js`);
  return [...match[1].matchAll(/"([a-z_-]+)"/g)].map((item) => item[1]);
}

test("every error code Classic explains has words here", () => {
  const codes = [...tableKeys("ADMIN_ERROR_MESSAGES"), ...tableKeys("AUTH_ERROR_MESSAGES")];
  assert.ok(codes.length >= 25);
  assert.deepEqual(codes.filter((code) => !KNOWN_ERRORS.includes(code)), []);
});

test("control states and transports are Classic's", () => {
  assert.deepEqual(tableKeys("MAINTENANCE_CONTROL_STATUS_TEXT").sort(), Object.keys(CONTROL_STATUSES).sort());
  assert.deepEqual(tableKeys("MAINTENANCE_CONTROL_TRANSPORT_LABELS"), [...CONTROL_TRANSPORTS]);
});

test("workflow owners, states and stale reasons are Classic's", () => {
  assert.deepEqual(tableKeys("WORKFLOW_OWNER_LABELS").sort(), [...WORKFLOW_OWNERS].sort());
  assert.deepEqual(tableKeys("WORKFLOW_STATE_LABELS").sort(), [...WORKFLOW_STATES].sort());
  assert.deepEqual(tableKeys("WORKFLOW_STALE_REASON_LABELS").sort(), [...STALE_REASONS].sort());
});

test("telemetry and diagnostics vocabularies are Classic's", () => {
  assert.deepEqual(tableKeys("ZENDURE_MQTT_STATUS_LABELS").sort(), Object.keys(RUNTIME_STATES).sort());
  assert.deepEqual(tableKeys("ZENDURE_MQTT_SOURCE_LABELS").sort(), [...STATUS_SOURCES].sort());
  const issues = CLASSIC.slice(CLASSIC.indexOf("function zmqttBrokerIssueCopy"), CLASSIC.indexOf("function renderZendureMqttRuntimeStatus"));
  assert.deepEqual([...issues.matchAll(/case "([a-z_]+)"/g)].map((match) => match[1]).sort(), [...BROKER_ISSUES].sort());
  assert.deepEqual(tableKeys("DIAGNOSTICS_STATUS_TONE").sort(), Object.keys(CHECK_STATUSES).sort());
  assert.deepEqual(tableKeys("DIAGNOSTICS_MODE_LABELS").sort(), [...RUN_MODES].sort());
});

/**
 * "setup" is the one hand-off Classic does not route: it never opens the
 * guided setup from an address, so the hash only keeps the browser in Classic,
 * on the start page whose first action is that setup. The action that sends
 * there says "in the Classic view" for that reason.
 */
test("every Classic page this console hands over to is one Classic routes", () => {
  const panels = tableKeys("MAINTENANCE_PANEL_IDS").filter((path) => path !== "hub");
  const aliases = Object.keys(JSON.parse(CLASSIC.match(/const MAINTENANCE_PATH_ALIASES = (\{[^}]*\})/)[1].replace(/(\w+):/g, '"$1":')));
  const tabs = arrayItems("MAINTENANCE_SETTINGS_TABS");
  assert.deepEqual(tabs, [...SETTINGS_TABS]);
  const routable = new Set(["maintenance", ...[...panels, ...aliases].map((path) => `maintenance-${path}`), ...tabs.map((tab) => `maintenance-settings-${tab}`)]);
  const handed = [
    ...PAGES.map((page) => page.classic),
    ...Object.values(FINDINGS).flat().filter((action) => action.kind === "classic").map((action) => action.target),
    "maintenance-upgrade",
    "maintenance-settings",
    "maintenance-status",
  ];
  assert.deepEqual([...new Set(handed.filter((hash) => !routable.has(hash)))], ["setup"]);
  assert.ok(!CLASSIC.slice(CLASSIC.indexOf("function applyHashRoute"), CLASSIC.indexOf("// --- setup wizard")).includes('"setup"'));
});
