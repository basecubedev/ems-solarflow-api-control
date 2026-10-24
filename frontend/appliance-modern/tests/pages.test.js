// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MOBILE_PAGES, PAGES, PAGE_IDS, pinnedPages } from "../src/app/pages.js";

const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");
const status = readFileSync(new URL("../../../appliance/status.py", import.meta.url), "utf8");

test("every page in the navigation has the component that renders it, and no other", () => {
  const table = app.match(/const PAGES = \{([^}]*)\}/);
  assert.ok(table, "the page table is not found in App.svelte");
  const ids = [...table[1].matchAll(/(\w+):/g)].map((match) => match[1]);
  assert.deepEqual(ids.sort(), [...PAGE_IDS].sort());
});

test("every section a backend finding names is a page, so its Open button goes somewhere", () => {
  const sections = [...status.matchAll(/^VIEW_[A-Z_]+ = "([a-z_]+)"/gm)].map((match) => match[1]);
  assert.ok(sections.length >= 4, "the VIEW_ constants are not found in appliance/status.py");
  assert.deepEqual(sections.filter((section) => !PAGE_IDS.includes(section)), []);
});

test("the overview comes first, and the phone bar holds four known pages", () => {
  assert.equal(PAGES[0].id, "overview");
  assert.equal(MOBILE_PAGES.length, 4);
  assert.ok(MOBILE_PAGES.every((id) => PAGE_IDS.includes(id)));
  assert.deepEqual(pinnedPages("settings,ghost,network"), ["settings", "network", "overview", "updates"]);
  assert.deepEqual(pinnedPages(null), [...MOBILE_PAGES]);
});
