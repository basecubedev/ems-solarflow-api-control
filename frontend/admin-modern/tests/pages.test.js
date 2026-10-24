// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MOBILE_PAGES, PAGES, pinnedPages } from "../src/app/pages.js";

const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");

test("every page in the navigation has the component that renders it, and no other", () => {
  const lazy = app.match(/const PAGES = \{([^}]*)\}/);
  const eager = app.match(/const loaded = \$state\(\{([^}]*)\}\)/);
  assert.ok(lazy && eager, "the page loaders are not found in App.svelte");
  const ids = [...eager[1].matchAll(/(\w+):/g), ...lazy[1].matchAll(/(\w+):/g)].map((match) => match[1]);
  assert.deepEqual(ids.sort(), PAGES.map((page) => page.id).sort());
});

test("the overview stays the home; the assistant, updates and the settings follow it in the menu, updates and the settings on the phone bar", () => {
  assert.deepEqual(PAGES.slice(0, 4).map((page) => page.id), ["overview", "assistant", "updates", "settings"]);
  assert.ok(PAGES.slice(0, 4).every((page) => page.group === "main" && !page.hidden));
  assert.deepEqual(MOBILE_PAGES.slice(0, 3), ["overview", "updates", "settings"]);
});

test("the phone bar holds four known pages and falls back to its default", () => {
  assert.equal(MOBILE_PAGES.length, 4);
  assert.ok(MOBILE_PAGES.every((id) => PAGES.some((page) => page.id === id)));
  assert.deepEqual(pinnedPages("system,ghost,backups"), ["system", "backups", "overview", "updates"]);
  assert.deepEqual(pinnedPages(null), [...MOBILE_PAGES]);
});

test("every page names the Classic page the switch opens", () => {
  assert.ok(PAGES.every((page) => /^maintenance(-[a-z]+)*$/.test(page.classic) || (page.hidden && page.classic === "setup")));
});
