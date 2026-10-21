// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SHARED = join(HERE, "..");
const ICONS = JSON.parse(readFileSync(join(SHARED, "icons.json"), "utf8"));
const LUCIDE = join(HERE, "../../../node_modules/lucide-static/icons");

function sources(dir = SHARED) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "tests" ? [] : sources(path);
    return /\.svelte$/.test(name) ? [readFileSync(path, "utf8")] : [];
  });
}

test("every icon a shared part names is in the shared list", () => {
  const used = new Set();
  for (const text of sources()) {
    for (const m of text.matchAll(/(?:<Icon[^>]*\bname|\bicon)\s*[=:]\s*"([a-z0-9-]+)"/g)) used.add(m[1]);
    for (const m of text.matchAll(/<Icon[^>]*\bname=\{([^}]*)\}/g)) {
      for (const quoted of m[1].matchAll(/"([a-z0-9-]+)"/g)) used.add(quoted[1]);
    }
    for (const m of text.matchAll(/href="#i-([a-z0-9-]+)"/g)) used.add(m[1]);
  }
  assert.deepEqual([...used].filter((name) => !ICONS.includes(name)).sort(), []);
});

test("every listed icon exists in the pinned Lucide package", { skip: !existsSync(LUCIDE) && "node_modules not installed" }, () => {
  assert.deepEqual(ICONS.filter((name) => !existsSync(join(LUCIDE, `${name}.svg`))), []);
});
