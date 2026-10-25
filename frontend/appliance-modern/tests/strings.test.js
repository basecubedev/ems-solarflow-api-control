// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { STRINGS } from "../src/i18n/strings.js";
import { APP_KEYS, SHARED_STRINGS } from "../../shared/i18n/strings.js";
import { PAGES } from "../src/app/pages.js";
import { KNOWN_ERRORS } from "../src/lib/errors.js";
import { FINDINGS, STATUS_SECTIONS } from "../src/lib/findings.js";
import { OPERATION_STATES, OPERATION_TYPES, VERIFICATION_FAILURES } from "../src/lib/operations.js";
import { PLAN_FIELD_ORDER, PLAN_SCOPES } from "../src/lib/plan.js";
import { RELEASE_TRACKS } from "../src/lib/host.js";
import { MANAGER_DIRECTIONS, MANAGER_OUTCOMES, MANAGER_VERDICTS } from "../src/lib/manager.js";
import { EXPORT_ACCESS_STATUSES, EXPORT_STATES, RESCUE_SSH_STATES } from "../src/lib/access.js";
import { KNOWN_LOG_SOURCES } from "../src/lib/logs.js";
import { CONNECTIVITY, CONTAINER_STATES, DAEMON_STATES, DEVICE_STATES, UNIT_STATES } from "../src/lib/words.js";
import { INSTALL_CHANNELS, LIFECYCLE_ACTIONS, REPAIR_ACTIONS } from "../src/lib/plans.js";
import { APPLIANCE_KINDS, STEP_IDS, TASKS } from "../src/lib/assistant.js";

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
const plural = (key) => [`${key}.one`, `${key}.other`];

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
    for (const match of text.matchAll(/\b(?:key|confirm)\s*:\s*"([a-z][a-zA-Z0-9_\-]*\.[a-zA-Z0-9_.\-]+)"/g)) used.add(match[1]);
    for (const match of text.matchAll(/\bwords\(\s*"([a-zA-Z0-9_.\-]+)"/g)) used.add(match[1]);
    for (const match of text.matchAll(/\btn\(\s*"([a-zA-Z0-9_.\-:]+)"/g)) counted.add(match[1]);
  }
  const countedWords = ["finding.security_updates_pending.message", "finding.package_index_stale.message", "finding.security_audit_degraded.message"];
  countedWords.forEach((key) => {
    used.delete(key);
    counted.add(key);
  });
  assert.deepEqual([...used].filter((key) => !exists(key)), []);
  assert.deepEqual([...counted].filter((key) => !plural(key).every(exists)), []);
});

/** Every key a component builds from a vocabulary list rather than spells out. */
const GENERATED = [
  ...PAGES.flatMap((page) => [`nav.${page.id}`, `page.${page.id}.sub`, ...(page.shortLabel ? [page.shortLabel] : [])]),
  ...KNOWN_ERRORS.map((code) => `error.${code}`),
  ...STATUS_SECTIONS.map((section) => `finding.${section}_unavailable.title`),
  ...OPERATION_TYPES.map((type) => `op.${type}`),
  ...Object.keys(OPERATION_STATES).map((state) => `opstate.${state}`),
  ...VERIFICATION_FAILURES.map((code) => `verify.${code}`),
  ...PLAN_FIELD_ORDER.map((field) => `plan.field.${field}`),
  ...PLAN_SCOPES.map((scope) => `plan.scope.${scope}`),
  ...RELEASE_TRACKS.flatMap((track) => [`track.${track}`, `track.${track}.help`]),
  ...MANAGER_DIRECTIONS.map((direction) => `manager.direction.${direction}`),
  ...MANAGER_OUTCOMES.map((outcome) => `manager.outcome.${outcome}`),
  ...Object.keys(MANAGER_VERDICTS).flatMap((verdict) => [`manager.verdict.${verdict}`, `manager.verdict.${verdict}.short`]),
  ...EXPORT_ACCESS_STATUSES.map((status) => `exportAccess.${status}`),
  ...EXPORT_STATES.map((state) => `export.${state}`),
  ...RESCUE_SSH_STATES.map((state) => `rescue.ssh.${state}`),
  ...["absent", "network", "unknown", "noPassword", "shipped", "changed"].map((state) => `rescue.${state}.hint`),
  ...KNOWN_LOG_SOURCES.map((source) => `log.${source}`),
  ...CONTAINER_STATES.map((state) => `container.${state}`),
  ...CONNECTIVITY.map((state) => `connectivity.${state}`),
  ...UNIT_STATES.map((state) => `systemd.${state}`),
  ...DEVICE_STATES.map((state) => `device.${state}`),
  ...DAEMON_STATES.map((state) => `docker.${state}`),
  ...LIFECYCLE_ACTIONS.flatMap((action) => [`admin.${action}`, `plan.title.admin.${action}`]),
  ...REPAIR_ACTIONS.map((action) => `repair.${action}`),
  ...INSTALL_CHANNELS.map((channel) => `install.channel.${channel}`),
  ...["basic", "expert"].map((level) => `detail.${level}`),
  ...["loading", "unread", "degraded", "attention", "healthy", "unknown"].map((verdict) => `verdict.${verdict}.short`),
  ...["healthy", "attention", "degraded"].map((level) => `level.${level}`),
  ...["reboot", "shutdown"].map((action) => `plan.title.${action}`),
  ...["day", "hour", "minute", "second", "dayOld", "hourOld"].flatMap((unit) => plural(`unit.${unit}`)),
  ...APPLIANCE_KINDS.map((kind) => `assistant.lead.${kind}`),
  ...STEP_IDS.map((id) => `assistant.step.${id}.title`),
  ...["bootstrap", "missing", "unhealthy", "stopped", "starting", "docker", "live"].map((kind) => `assistant.step.admin.${kind}`),
  ...["setup", "stopped"].map((kind) => `assistant.step.ems.${kind}`),
  ...TASKS.flatMap((task) => [`assistant.task.${task.id}.title`, `assistant.task.${task.id}.text`]),
];

test("every generated key family is complete", () => {
  assert.deepEqual(GENERATED.filter((key) => !exists(key)), []);
});

test("every finding the console knows has its words", () => {
  for (const [code, words] of Object.entries(FINDINGS)) {
    const said = words({ updates: {}, docker: {}, admin: {} }, { facts: {} });
    for (const part of ["title", "message", "next"]) {
      const entry = said[part];
      assert.ok(entry && entry.key, `${code} has no ${part}`);
      const keys = entry.count === undefined ? [entry.key] : plural(entry.key);
      assert.deepEqual(keys.filter((key) => !exists(key)), [], `${code}.${part}`);
    }
  }
});

test("no key is defined and never asked for", () => {
  const text = sources().join("\n");
  const generated = new Set(GENERATED);
  const orphans = Object.keys(STRINGS).filter((key) => {
    if (APP_KEYS.includes(key) || generated.has(key)) return false;
    const base = key.replace(/\.(one|other)$/, "");
    return !text.includes(`"${key}"`) && !text.includes(`"${base}"`);
  });
  assert.deepEqual(orphans, []);
});
