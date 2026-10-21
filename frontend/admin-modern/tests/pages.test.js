// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MOBILE_PAGES, PAGES, pinnedPages } from "../src/app/pages.js";

const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");

test("every page in the navigation has the component that renders it, and no other", () => {
  const block = app.match(/const COMPONENTS = \{([^}]*)\}/);
  assert.ok(block, "COMPONENTS not found in App.svelte");
  const ids = [...block[1].matchAll(/(\w+):/g)].map((match) => match[1]);
  assert.deepEqual(ids, PAGES.map((page) => page.id));
});

test("the phone bar holds four known pages and falls back to its default", () => {
  assert.equal(MOBILE_PAGES.length, 4);
  assert.ok(MOBILE_PAGES.every((id) => PAGES.some((page) => page.id === id)));
  assert.deepEqual(pinnedPages("system,ghost,backups"), ["system", "backups", "overview", "services"]);
  assert.deepEqual(pinnedPages(null), [...MOBILE_PAGES]);
});

test("every page names the Classic page the switch opens", () => {
  assert.ok(PAGES.every((page) => /^maintenance(-[a-z]+)*$/.test(page.classic)));
});
