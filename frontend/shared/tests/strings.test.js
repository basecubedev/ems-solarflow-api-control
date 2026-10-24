// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { APP_KEYS, SHARED_STRINGS } from "../i18n/strings.js";

const LANGS = ["en", "de", "nl", "fr"];
const HERE = dirname(fileURLToPath(import.meta.url));
const SHARED = join(HERE, "..");
const APPS = ["dashboard-modern", "admin-modern", "appliance-modern"].map((name) => join(HERE, "../..", name, "src")).filter(existsSync);

function sources(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "tests" || name === "node_modules" ? [] : sources(path);
    return /\.(svelte|js)$/.test(name) && name !== "strings.js" ? [readFileSync(path, "utf8")] : [];
  });
}

const placeholders = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

test("every shared key carries all four languages", () => {
  for (const [key, entry] of Object.entries(SHARED_STRINGS)) {
    for (const lang of LANGS) {
      assert.equal(typeof entry[lang], "string", `${key} has no ${lang}`);
      assert.ok(entry[lang].trim().length, `${key} is empty in ${lang}`);
    }
  }
});

test("placeholders are the same in every language", () => {
  for (const [key, entry] of Object.entries(SHARED_STRINGS)) {
    const expected = placeholders(entry.en);
    for (const lang of LANGS) assert.deepEqual(placeholders(entry[lang]), expected, `${key} (${lang})`);
  }
});

test("every literal key the shared parts ask for is shared or one each app defines", () => {
  const used = new Set();
  for (const text of sources(SHARED)) {
    for (const match of text.matchAll(/\bt\(\s*"([a-zA-Z0-9_.\-]+)"/g)) used.add(match[1]);
  }
  const missing = [...used].filter((key) => !SHARED_STRINGS[key] && !APP_KEYS.includes(key));
  assert.deepEqual(missing, []);
});

test("no orphan shared keys: each is used by a shared part or an app", () => {
  const text = [SHARED, ...APPS].flatMap(sources).join("\n");
  const families = ["appearance.density.", "appearance.nav.", "diagnose.metric.", "diagnose.status.", "diagnose.global."];
  const orphans = Object.keys(SHARED_STRINGS).filter((key) => !text.includes(`"${key}"`) && !families.some((prefix) => key.startsWith(prefix)));
  assert.deepEqual(orphans, []);
});
