// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { PAGES } from "../src/app/pages.js";

const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");

test("every page in the navigation has the component that renders it, and no other", () => {
  const block = app.match(/const COMPONENTS = \{([^}]*)\}/);
  assert.ok(block, "COMPONENTS not found in App.svelte");
  const ids = [...block[1].matchAll(/(\w+):/g)].map((match) => match[1]);
  assert.deepEqual(ids, PAGES.map((page) => page.id));
});
