// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { STRINGS } from "../src/i18n/strings.js";
import { APP_KEYS, SHARED_STRINGS } from "../../shared/i18n/strings.js";
import { PAGES } from "../src/app/pages.js";
import { PERIODS } from "../src/lib/energy.js";
import { RULES } from "../src/lib/status.js";
import { ANALYTICS_VIEWS, ANALYTICS_RANGES, ANALYTICS_OVERLAYS, KPIS } from "../src/lib/analytics.js";
import { CONTROL_CODES, DECISION_SENTENCES, LIMIT_REASONS, EXPLAIN_NOTES } from "../src/lib/control.js";
import { FIRMWARE_ENUMS } from "../src/lib/devices.js";
import { PRESET_ORDER, WEEKDAYS, STATUSES } from "../src/lib/schedule.js";
import { KNOWN_ERRORS } from "../src/lib/errors.js";
import { batteryReading } from "../src/lib/history.js";

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

test("a key lives in one table only, and the app defines the keys the shell reads", () => {
  assert.deepEqual(Object.keys(STRINGS).filter((key) => key in SHARED_STRINGS), []);
  assert.deepEqual(APP_KEYS.filter((key) => !STRINGS[key]), []);
});

test("every literal key the components ask for exists", () => {
  const used = new Set();
  for (const text of sources()) {
    for (const match of text.matchAll(/\bt\(\s*"([a-zA-Z0-9_.\-]+)"/g)) used.add(match[1]);
  }
  const missing = [...used].filter((key) => !STRINGS[key] && !SHARED_STRINGS[key]);
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
    ...Object.values(LIMIT_REASONS).map((key) => `limit.reason.${key}`),
    ...Object.values(EXPLAIN_NOTES).map((key) => `note.${key}`),
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
    batteryReading(-1).key,
    batteryReading(1).key,
  ];
  const missing = [...new Set(required)].filter((key) => !STRINGS[key] && !SHARED_STRINGS[key]);
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
    "flow.", "assist.", "error.", "schedule.monthly.", "schedule.ends.", "energy.periodSub.", "energy.context.", "limit.reason.", "note.",
  ];
  const orphans = Object.keys(STRINGS).filter((key) => !APP_KEYS.includes(key) && !text.includes(`"${key}"`) && !families.some((prefix) => key.startsWith(prefix)));
  assert.deepEqual(orphans, []);
});

/**
 * One quantity, one name: the keys that label the same measured quantity on
 * different pages (the energy flow, the device cards, the charts, Analytics
 * and the Energy page) carry the same words in every language.
 */
const SAME_NAME = {
  solar: ["flow.solar", "series.pv", "device.solar", "analytics.view.pv", "analytics.kpi.pv", "channel.pv_yield"],
  battery: ["flow.battery", "series.battery", "device.battery", "analytics.view.battery"],
  grid: ["flow.grid", "series.grid", "device.grid", "analytics.view.grid"],
  homeUse: ["flow.home", "series.home", "analytics.kpi.home", "channel.home_consumption"],
  output: ["flow.inverter", "series.output", "device.output", "analytics.kpi.output", "energy.energy"],
  gridImport: ["channel.grid_import", "analytics.kpi.gridImport"],
  gridExport: ["channel.grid_export", "analytics.kpi.gridExport"],
  batteryCharged: ["channel.battery_charge", "analytics.kpi.charge"],
  batteryDischarged: ["channel.battery_discharge", "analytics.kpi.discharge"],
  acCharging: ["series.ac_charge", "energy.acCharge", "analytics.kpi.acCharge", "assist.acCharge", "code.ac_charge", "runtime.acChargeTitle", "schedule.group.ac_charging"],
  stateOfCharge: ["flow.soc", "series.soc"],
  gridImportToday: ["today.gridImport", "flow.importedToday"],
  energyFlowsSince: ["energy.measuredSince", "energy.context.channelsSince"],
};

/** A label that counts a quantity over a day begins with that quantity's name, and the meter's offline status with the meter's. */
const NAMED_AFTER = {
  "today.solar": "flow.solar",
  "today.home": "flow.home",
  "today.batteryNet": "flow.battery",
  "today.gridImport": "channel.grid_import",
  "flow.importedToday": "channel.grid_import",
  "flow.exportedToday": "channel.grid_export",
  "status.meterOffline": "device.meter",
};

test("the same quantity carries the same name on every page", () => {
  for (const [quantity, keys] of Object.entries(SAME_NAME)) {
    for (const lang of LANGS) {
      const names = keys.map((key) => STRINGS[key][lang]);
      assert.deepEqual(new Set(names).size, 1, `${quantity} in ${lang}: ${keys.map((key, i) => `${key}=${names[i]}`).join(", ")}`);
    }
  }
  for (const [key, named] of Object.entries(NAMED_AFTER)) {
    for (const lang of LANGS) {
      const name = STRINGS[named][lang].toLocaleLowerCase(lang);
      assert.ok(STRINGS[key][lang].toLocaleLowerCase(lang).startsWith(name), `${key} in ${lang} does not begin with ${named} (${STRINGS[named][lang]})`);
    }
  }
});
