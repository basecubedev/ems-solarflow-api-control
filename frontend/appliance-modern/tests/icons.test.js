// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { PAGES } from "../src/app/pages.js";
import { SEVERITY_ICONS } from "../src/lib/findings.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "../src");
const OWN = JSON.parse(readFileSync(join(SRC, "icons.json"), "utf8"));
const SHARED = JSON.parse(readFileSync(join(HERE, "../../shared/icons.json"), "utf8"));
const ICONS = [...SHARED, ...OWN];
const LUCIDE = join(HERE, "../../../node_modules/lucide-static/icons");

function sources(dir = SRC) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(svelte|js)$/.test(name) ? [readFileSync(path, "utf8")] : [];
  });
}

test("every icon the components name is in the sprite lists", () => {
  const used = new Set([...PAGES.map((page) => page.icon), ...Object.values(SEVERITY_ICONS)]);
  for (const text of sources()) {
    for (const m of text.matchAll(/(?:<Icon[^>]*\bname|\bicon)\s*[=:]\s*"([a-z0-9-]+)"/g)) used.add(m[1]);
    for (const m of text.matchAll(/<(?:Icon|PlanButton)[^>]*\b(?:name|icon)=\{([^}]*)\}/g)) {
      for (const quoted of m[1].matchAll(/[?:] "([a-z0-9-]+)"/g)) used.add(quoted[1]);
    }
    for (const m of text.matchAll(/\b[A-Z_]*ICONS?\s*=\s*(?:Object\.freeze\()?\{([^}]*)\}/g)) {
      for (const quoted of m[1].matchAll(/:\s*"([a-z0-9-]+)"/g)) used.add(quoted[1]);
    }
    for (const m of text.matchAll(/\bicon\s*=\s*\$derived\(([\s\S]*?)\);/g)) {
      for (const quoted of m[1].matchAll(/[?:] "([a-z0-9-]+)"/g)) used.add(quoted[1]);
    }
  }
  assert.ok(used.size > 30, `only ${used.size} icons found; the scan no longer reads the components`);
  assert.deepEqual([...used].filter((name) => !ICONS.includes(name)).sort(), []);
});

test("the app lists no icon the shared list already has", () => {
  assert.deepEqual(OWN.filter((name) => SHARED.includes(name)), []);
});

test("every listed icon exists in the pinned Lucide package", { skip: !existsSync(LUCIDE) && "node_modules not installed" }, () => {
  assert.deepEqual(OWN.filter((name) => !existsSync(join(LUCIDE, `${name}.svg`))), []);
});
