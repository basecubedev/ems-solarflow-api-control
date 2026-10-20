// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { STRINGS } from "../src/i18n/strings.js";
import { PAGES } from "../src/app/pages.js";
import { PERIODS } from "../src/lib/energy.js";
import { RULES } from "../src/lib/status.js";
import { ANALYTICS_VIEWS, ANALYTICS_RANGES, ANALYTICS_OVERLAYS, KPIS } from "../src/lib/analytics.js";
import { CONTROL_CODES, DECISION_SENTENCES } from "../src/lib/control.js";
import { FIRMWARE_ENUMS } from "../src/lib/devices.js";
import { PRESET_ORDER, WEEKDAYS, STATUSES } from "../src/lib/schedule.js";
import { KNOWN_ERRORS } from "../src/lib/errors.js";

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

test("every literal key the components ask for exists", () => {
  const used = new Set();
  for (const text of sources()) {
    for (const match of text.matchAll(/\bt\(\s*"([a-zA-Z0-9_.\-]+)"/g)) used.add(match[1]);
  }
  const missing = [...used].filter((key) => !STRINGS[key]);
  assert.deepEqual(missing, []);
});

test("every generated key family is complete", () => {
  const required = [
    ...PAGES.flatMap((page) => [`nav.${page.id}`, `page.${page.id}.sub`]),
    ...PERIODS.map((period) => period.label),
    ...PERIODS.map((period) => `energy.periodSub.${period.kind}`),
    ...["currency", "monthsWithData", "years", "bestDay", "lifetime", "channelsSince"].map((key) => `energy.context.${key}`),
    ...RULES.map((rule) => rule.label),
    ...ANALYTICS_VIEWS.map((view) => `analytics.view.${view.id}`),
    ...ANALYTICS_RANGES.map((range) => `chart.range.${range}`),
    ...ANALYTICS_OVERLAYS.map((id) => `series.${id}`),
    ...ANALYTICS_VIEWS.flatMap((view) => view.series.map((id) => `series.${id}`)),
    ...Object.keys(KPIS).map((id) => `analytics.kpi.${id}`),
    ...CONTROL_CODES.map((code) => `code.${code}`),
    ...DECISION_SENTENCES.map((reason) => `decision.${reason}`),
    ...Object.values(FIRMWARE_ENUMS).flatMap((table) => Object.values(table)),
    ...PRESET_ORDER.map((name) => `schedule.preset.${name}`),
    ...WEEKDAYS.map((day) => `schedule.weekday.${day}`),
    ...STATUSES.map((status) => `schedule.status.${status}`),
    ...KNOWN_ERRORS.map((code) => `error.${code}`),
    ...["ok", "paused", "waiting", "stale", "meterOffline", "allOffline", "devicePowerInvalid", "devicesOffline"].map((key) => `status.${key}`),
    ...["connecting", "live", "polling", "reconnecting", "offline", "paused", "stale"].map((key) => `transport.${key}`),
    ...["charging", "discharging", "idle", "importing", "exporting", "balanced", "offline"].map((key) => `flow.${key}`),
    ...["complete", "running", "future", "unmeasured", "missing"].map((key) => `energy.state.${key}`),
    ...["send", "skip", "blocked", "warn", "none"].map((key) => `control.write.${key}`),
    ...["ok", "warning", "error", "unknown", "info"].map((key) => `diagnose.status.${key}`),
    ...["bundled", "external"].map((key) => `maint.influx.${key}`),
    ...["ok", "warning", "error", "devices"].map((key) => `diagnose.metric.${key}`),
    ...["output", "input", "mixed"].map((key) => `analytics.role.${key}`),
    ...["inputs", "weighting", "raw", "adjust", "final"].map((key) => `control.deviceStage.${key}`),
  ];
  const missing = [...new Set(required)].filter((key) => !STRINGS[key]);
  assert.deepEqual(missing, []);
});

test("no orphan keys: each key is used literally or belongs to a generated family", () => {
  const text = sources().join("\n");
  const families = [
    "nav.", "page.", "rule.", "code.", "decision.", "fw.", "channel.", "energy.state.", "energy.detail.", "chart.range.",
    "series.", "analytics.view.", "analytics.kpi.", "analytics.role.", "diagnose.profile.", "diagnose.status.", "diagnose.global.", "diagnose.metric.",
    "maint.type.", "maint.kind.", "maint.influx.", "schedule.preset.", "schedule.weekday.", "schedule.freq.", "schedule.pos.",
    "schedule.status.", "schedule.group.", "schedule.action.", "schedule.choice.", "runtime.offgrid.",
    "runtime.warn.", "setting.", "settingValue.", "control.write.", "control.stage.", "control.deviceStage.", "status.", "transport.",
    "flow.", "assist.", "error.", "appearance.density.", "schedule.monthly.", "schedule.ends.", "energy.periodSub.", "energy.context.",
  ];
  const orphans = Object.keys(STRINGS).filter((key) => !text.includes(`"${key}"`) && !families.some((prefix) => key.startsWith(prefix)));
  assert.deepEqual(orphans, []);
});
