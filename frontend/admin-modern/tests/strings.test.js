// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { STRINGS } from "../src/i18n/strings.js";
import { APP_KEYS, SHARED_STRINGS } from "../../shared/i18n/strings.js";
import { PAGES } from "../src/app/pages.js";
import { KNOWN_ERRORS, KNOWN_SENTENCES } from "../src/lib/errors.js";
import { FINDINGS, INSTALL_STATES, CONTAINER_STATUSES } from "../src/lib/health.js";
import { CONTROL_STATUSES, CONTROL_TRANSPORTS } from "../src/lib/control.js";
import { DISPLAY_STATES, SERVICES, SYNC_ACTIONS, CONFIG_STATES, PLAN_MESSAGES } from "../src/lib/services.js";
import { BACKUP_PURPOSES, BACKUP_TYPES, CREATE_SCOPES, CREATE_STEPS, RESTORE_STEPS, FILE_ACTIONS, INFLUX_REASONS, LIST_WARNINGS, STEP_STATES } from "../src/lib/backups.js";
import { WORKFLOW_OWNERS, WORKFLOW_STATES, STALE_REASONS } from "../src/lib/workflow.js";
import { CHECK_IDS, CHECK_STATUSES, RUN_MODES, UNAVAILABLE_MESSAGES } from "../src/lib/diagnostics.js";
import { CHANNELS, UPGRADE_STATES, PAIR_STATES, RELEASE_NOTES } from "../src/lib/updates.js";
import { PHASES, OPTION_GROUPS, UPGRADE_STAGES } from "../src/lib/upgrade.js";
import { RUNTIME_STATES, STATUS_SOURCES, BROKER_ISSUES, DEVICE_STATES } from "../src/lib/telemetry.js";
import { AUTOMATIC_FEATURES, DEVICE_KINDS, EDIT_TABS, FEATURE_IDS, MQTT_SOURCES, RISKS } from "../src/lib/settings.js";

const LANGS = ["en", "de", "nl", "fr"];
const SRC = join(dirname(fileURLToPath(import.meta.url)), "../src");

function sources(dir = SRC) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(svelte|js)$/.test(name) && name !== "strings.js" ? [readFileSync(path, "utf8")] : [];
  });
}

const placeholders = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
const exists = (key) => Boolean(STRINGS[key] || SHARED_STRINGS[key]);

test("every key carries all four languages", () => {
  for (const [key, entry] of Object.entries(STRINGS)) {
    for (const lang of LANGS) {
      assert.equal(typeof entry[lang], "string", `${key} has no ${lang}`);
      assert.ok(entry[lang].trim().length, `${key} is empty in ${lang}`);
    }
  }
});

test("placeholders are the same in every language", () => {
  for (const [key, entry] of Object.entries(STRINGS)) {
    const expected = placeholders(entry.en);
    for (const lang of LANGS) assert.deepEqual(placeholders(entry[lang]), expected, `${key} (${lang})`);
  }
});

test("a key lives in one table only, and the app defines the keys the shell reads", () => {
  assert.deepEqual(Object.keys(STRINGS).filter((key) => key in SHARED_STRINGS), []);
  assert.deepEqual(APP_KEYS.filter((key) => !STRINGS[key]), []);
});

test("every literal key the components ask for exists, and every counted one has its plural forms", () => {
  const used = new Set();
  const counted = new Set();
  for (const text of sources()) {
    for (const match of text.matchAll(/\bt\(\s*"([a-zA-Z0-9_.\-:]+)"/g)) used.add(match[1]);
    for (const match of text.matchAll(/\b(?:label|key|messageKey|nameKey)\s*[:=]\s*"([a-z][a-zA-Z0-9_\-:]*\.[a-zA-Z0-9_.\-:]+)"/g)) used.add(match[1]);
    for (const match of text.matchAll(/\btn\(\s*"([a-zA-Z0-9_.\-:]+)"/g)) counted.add(match[1]);
  }
  assert.deepEqual([...used].filter((key) => !exists(key)), []);
  assert.deepEqual([...counted].filter((key) => !exists(`${key}.one`) || !exists(`${key}.other`)), []);
});

test("every generated key family is complete", () => {
  const required = [
    ...PAGES.flatMap((page) => [`nav.${page.id}`, `page.${page.id}.sub`, ...(page.shortLabel ? [page.shortLabel] : [])]),
    ...KNOWN_ERRORS.map((code) => `error.${code}`),
    ...Object.values(KNOWN_SENTENCES),
    ...Object.values(RELEASE_NOTES),
    ...Object.values(LIST_WARNINGS),
    ...Object.values(PLAN_MESSAGES),
    ...Object.values(UNAVAILABLE_MESSAGES),
    ...BACKUP_PURPOSES.map((purpose) => `backup.purpose.${purpose}`),
    ...Object.keys(STEP_STATES).map((state) => `step.state.${state}`),
    ...INFLUX_REASONS.map((reason) => `backups.scope.influxdb.${reason}`),
    ...AUTOMATIC_FEATURES.map((id) => `feature.${id}`),
    ...Object.keys(FINDINGS).filter((code) => code !== "installation_incomplete").flatMap((code) => [`finding.${code}.title`, `finding.${code}.next`]),
    ...Object.keys(FINDINGS).filter((code) => !["installation_incomplete", "installation_warning"].includes(code)).map((code) => `finding.${code}.message`),
    ...Object.values(FINDINGS).flat().map((action) => action.label),
    ...INSTALL_STATES.flatMap((state) => [`install.state.${state}`, `install.message.${state}`]),
    ...CONTAINER_STATUSES.map((status) => `container.status.${status}`),
    ...["config", "compose", "data"].map((name) => `health.path.${name}`),
    ...["loading", "ok", "info", "warning", "error", "none", "unreadable"].map((level) => `health.pill.${level}`),
    ...["loading", "ok", "none", "unreadable"].map((level) => `overview.verdict.${level}`),
    ...Object.keys(CONTROL_STATUSES).flatMap((status) => [`control.status.${status}`, `control.short.${status}`]),
    ...CONTROL_TRANSPORTS.map((gate) => `control.transport.${gate}`),
    ...["blocked", "idle"].map((state) => `control.gate.${state}`),
    ...[...DISPLAY_STATES, "unknown"].flatMap((state) => [`service.state.${state}`, `service.detail.${state}`]),
    ...SERVICES.map((service) => `service.name.${service}`),
    ...SYNC_ACTIONS.filter((id) => id !== "ems:recreate").map((id) => `services.change.${id}`),
    ...CONFIG_STATES.map((state) => `services.change.ems:recreate.${state}`),
    ...CONFIG_STATES.map((state) => `services.config.${state}`),
    ...SYNC_ACTIONS.map((id) => `services.step.${id}`),
    ...["running", "stopped", "unavailable"].map((state) => `services.desired.${state}`),
    ...["busy", "unavailable", "analytics_disabled", "influx_not_running"].map((status) => `services.status.${status}`),
    ...BACKUP_TYPES.map((type) => `backup.type.${type}`),
    ...CREATE_SCOPES.flatMap((scope) => [`backups.scope.${scope}`, `backups.scope.${scope}.desc`]),
    ...CREATE_STEPS.map((step) => `backup.step.${step}`),
    ...RESTORE_STEPS.map((step) => `backup.step.${step}`),
    ...["rollback", "apply", "postcheck"].map((step) => `backup.step.${step}.typed`),
    ...FILE_ACTIONS.map((action) => `backup.file.${action}`),
    ...WORKFLOW_OWNERS.map((owner) => `workflow.owner.${owner}`),
    ...[...WORKFLOW_STATES, "unknown"].map((state) => `workflow.state.${state}`),
    ...STALE_REASONS.map((reason) => `workflow.stale.${reason}`),
    ...CHECK_IDS.map((id) => `diagnostics.check.${id}`),
    ...Object.keys(CHECK_STATUSES).map((status) => `diagnostics.status.${status}`),
    ...RUN_MODES.map((mode) => `diagnostics.mode.${mode}`),
    ...["ok", "warning", "failed", "unavailable"].map((status) => `diagnostics.overall.${status}`),
    ...["ok", "disabled", "warning", "failed", "unavailable"].flatMap((key) => [`diagnostics.count.${key}.one`, `diagnostics.count.${key}.other`]),
    ...CHANNELS.map((channel) => `updates.channel.${channel}`),
    ...Object.keys(UPGRADE_STATES).map((state) => `updates.state.${state}`),
    ...Object.keys(PAIR_STATES).map((state) => `updates.pair.${state}`),
    ...["backup", "config", "admin", "deploy", "check"].map((step) => `updates.step.${step}`),
    ...["completed", "cancelled"].map((outcome) => `updates.last.${outcome}`),
    ...UPGRADE_STAGES.map((stage) => `upgrade.sub.${stage}`),
    ...["failed", "refused", "cancelled"].map((end) => `upgrade.end.${end}`),
    ...PHASES.flatMap((phase) => [`upgrade.phase.${phase.id}`, `upgrade.phase.${phase.id}.doing`]),
    ...Object.keys(OPTION_GROUPS).flatMap((group) => [`upgrade.choice.${group}`, `upgrade.choice.${group}.desc`]),
    ...Object.keys(RUNTIME_STATES).map((state) => `system.runtime.${state}`),
    ...STATUS_SOURCES.map((source) => `system.source.${source}`),
    ...BROKER_ISSUES.map((issue) => `system.issue.${issue}`),
    ...Object.keys(DEVICE_STATES).map((state) => `system.deviceState.${state}`),
    ...["connected", "running", "configured", "disabled"].map((state) => `system.broker.${state}`),
    ...DEVICE_KINDS.map((kind) => `settings.kind.${kind}`),
    ...MQTT_SOURCES.map((source) => `settings.source.${source}`),
    ...["missing", "invalid"].map((state) => `settings.config.${state}`),
    ...["operation", "workflow"].map((kind) => `recovery.reference.${kind}`),
    ...FEATURE_IDS.flatMap((id) => [`feature.${id}`, `feature.${id}.sub`]),
    ...[...EDIT_TABS, "devices"].map((tab) => `settings.tab.${tab}`),
    ...EDIT_TABS.map((tab) => `settings.tab.${tab}.sub`),
    ...["live", "restart"].map((when) => `settings.edit.when.${when}`),
    ...["advanced", "expert"].map((level) => `settings.edit.level.${level}`),
    ...RISKS.flatMap((risk) => [`settings.risk.${risk}`, `settings.risk.${risk}.hint`]),
    ...["control_stability", "data_loss"].map((risk) => `settings.risk.${risk}.shared`),
  ];
  assert.deepEqual([...new Set(required)].filter((key) => !exists(key)), []);
});

test("no orphan keys: each key is used literally or belongs to a generated family", () => {
  const text = sources().join("\n");
  const families = [
    "nav.", "page.", "error.", "finding.", "install.", "container.status.", "health.path.", "health.pill.", "overview.verdict.",
    "overview.attention.", "overview.notes.", "backup.purpose.", "backups.scope.influxdb.", "step.state.", "control.status.", "control.short.", "control.transport.", "control.gate.", "control.writing.",
    "service.", "services.change.", "services.config.", "services.step.", "services.desired.", "services.status.",
    "backup.type.", "backup.step.", "backup.file.", "backups.scope.", "workflow.owner.", "workflow.state.", "workflow.stale.",
    "diagnostics.check.", "diagnostics.status.", "diagnostics.mode.", "diagnostics.overall.", "diagnostics.count.",
    "updates.channel.", "updates.state.", "updates.pair.", "updates.step.", "updates.showAll.", "updates.last.", "system.runtime.", "system.source.",
    "upgrade.sub.", "upgrade.phase.", "upgrade.choice.", "upgrade.migration.", "upgrade.migrationLoses.", "upgrade.end.",
    "system.issue.", "system.deviceState.", "system.broker.", "system.brokerDevices.", "system.metrics.", "system.migrationNeeded.",
    "system.migrationLosesControl.", "settings.kind.", "settings.source.", "settings.config.", "settings.devicesSub.", "recovery.reference.",
    "settings.tab.", "settings.edit.when.", "settings.edit.level.", "settings.risk.", "settings.edit.unsaved.", "settings.edit.overrides.",
    "settings.edit.invalid.", "settings.edit.resetLine.",
    "feature.",
  ];
  const orphans = Object.keys(STRINGS).filter((key) => !APP_KEYS.includes(key) && !text.includes(`"${key}"`) && !families.some((prefix) => key.startsWith(prefix)));
  assert.deepEqual(orphans, []);
});
