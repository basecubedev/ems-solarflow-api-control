// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const compiler = await import("svelte/compiler").catch(() => null);

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "../..");
const ROOTS = ["admin-modern/src", "appliance-modern/src", "dashboard-modern/src", "shared"];
const GLOBAL_STYLES = ["shared/styles/base.css", "appliance-modern/src/styles.css"];
const SHARED_CLASSES = new Set(GLOBAL_STYLES.flatMap((path) => [...readFileSync(join(FRONTEND, path), "utf8").matchAll(/\.([a-z][\w-]*)/g)].map((match) => match[1])));

function sources(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (name === "node_modules") return [];
    if (statSync(path).isDirectory()) return sources(path);
    return name.endsWith(".svelte") ? [path] : [];
  });
}

/** The class names a page hands a Card, where they are written as text. */
function cardClasses(text) {
  const found = [];
  const visit = (node) => {
    if (node?.type === "Component" && node.name === "Card") {
      for (const attribute of node.attributes || []) {
        if (attribute.name !== "className" || !Array.isArray(attribute.value)) continue;
        for (const part of attribute.value) if (part.type === "Text") found.push(...part.data.split(/\s+/).filter(Boolean));
      }
    }
    for (const value of Object.values(node || {})) {
      if (value?.type === "Fragment") value.nodes.forEach(visit);
    }
  };
  visit({ fragment: compiler.parse(text, { modern: true }).fragment });
  return found;
}

test("a card's own class names no class the shared styles give a look of their own", { skip: !compiler && "node_modules not installed" }, () => {
  assert.ok(SHARED_CLASSES.has("check") && SHARED_CLASSES.has("card"));
  assert.deepEqual(cardClasses('<Card className="check wide" />'), ["check", "wide"]);
  const clashes = ROOTS.flatMap((root) => sources(join(FRONTEND, root))).flatMap((path) => cardClasses(readFileSync(path, "utf8")).filter((name) => SHARED_CLASSES.has(name)).map((name) => `${relative(FRONTEND, path)}: ${name}`));
  assert.deepEqual(clashes, []);
});
