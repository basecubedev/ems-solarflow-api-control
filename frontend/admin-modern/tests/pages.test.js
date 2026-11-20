// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { LEGACY_ADDRESSES, MOBILE_PAGES, PAGES, legacyPage, needsConsole, pinnedPages } from "../src/app/pages.js";

const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");

test("every page in the navigation has the component that renders it, and no other", () => {
  const lazy = app.match(/const PAGES = \{([^}]*)\}/);
  const eager = app.match(/const loaded = \$state\(\{([^}]*)\}\)/);
  assert.ok(lazy && eager, "the page loaders are not found in App.svelte");
  const ids = [...eager[1].matchAll(/(\w+):/g), ...lazy[1].matchAll(/(\w+):/g)].map((match) => match[1]);
  assert.deepEqual(ids.sort(), PAGES.map((page) => page.id).sort());
});

test("the dashboard's overview is the home; the console's status, the assistant, updates and the settings lead its own group", () => {
  assert.equal(PAGES[0].id, "overview");
  const admin = PAGES.filter((page) => page.group === "admin" && !page.hidden).map((page) => page.id);
  assert.deepEqual(admin.slice(0, 4), ["status", "assistant", "updates", "settings"]);
  assert.deepEqual(MOBILE_PAGES, ["overview", "energy", "status", "updates"]);
});

test("the dashboard's pages are the dashboard's own, and only the console's need the console's sign-in", () => {
  const dashboard = readFileSync(new URL("../../dashboard-modern/src/app/pages.js", import.meta.url), "utf8");
  const own = [...dashboard.matchAll(/\{ id: "([a-z]+)", icon: "[a-z-]+", group: "(?:main|system)"/g)].map((match) => match[1]);
  const carried = PAGES.filter((page) => page.area === "ems" && page.id !== "about").map((page) => page.id);
  assert.deepEqual(carried.filter((id) => !own.includes(id)), []);
  assert.deepEqual(own.filter((id) => !carried.includes(id)).sort(), ["about", "diagnose", "maintenance"]);
  assert.ok(PAGES.every((page) => page.area === "ems" || page.area === "admin"));
  assert.ok(PAGES.filter((page) => page.area === "admin").every(needsConsole));
  assert.ok(!PAGES.filter((page) => page.area === "ems").some(needsConsole));
  assert.match(app, /import Overview from "\.\.\/\.\.\/dashboard-modern\/src\/pages\/Overview\.svelte";/);
  for (const id of carried.filter((name) => name !== "overview")) {
    assert.match(app, new RegExp(`${id}: \\(\\) => import\\("\\.\\./\\.\\./dashboard-modern/src/pages/`), id);
  }
});

test("the phone bar holds four known pages and falls back to its default", () => {
  assert.equal(MOBILE_PAGES.length, 4);
  assert.ok(MOBILE_PAGES.every((id) => PAGES.some((page) => page.id === id)));
  assert.deepEqual(pinnedPages("system,ghost,backups"), ["system", "backups", "overview", "energy"]);
  assert.deepEqual(pinnedPages(null), [...MOBILE_PAGES]);
});

test("an address of the removed Classic console opens the same page here", () => {
  assert.ok(Object.values(LEGACY_ADDRESSES).every((id) => PAGES.some((page) => page.id === id)));
  assert.equal(legacyPage("maintenance"), "status");
  assert.equal(legacyPage("diagnose"), "diagnostics");
  assert.equal(legacyPage("maintenance-upgrade-v0.10.5"), "updates");
  assert.equal(legacyPage("maintenance-settings-features"), "settings");
  assert.equal(legacyPage("maintenance-status"), "services");
  assert.equal(legacyPage("maintenance-manual"), "services");
  assert.equal(legacyPage("maintenance-backup"), "backups");
  assert.equal(legacyPage("maintenancex"), null);
  assert.equal(legacyPage("ghost"), null);
});
