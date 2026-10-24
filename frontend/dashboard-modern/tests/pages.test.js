// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MOBILE_PAGES, PAGES, pinnedPages, swapPinned } from "../src/app/pages.js";

const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");

test("every page in the navigation has the component that renders it, and no other", () => {
  const block = app.match(/const COMPONENTS = \{([^}]*)\}/);
  assert.ok(block, "COMPONENTS not found in App.svelte");
  const ids = [...block[1].matchAll(/(\w+):/g)].map((match) => match[1]);
  assert.deepEqual(ids, PAGES.map((page) => page.id));
});

test("the bar's pages: four known pages, each once, the default filling the gaps", () => {
  const fallback = [...MOBILE_PAGES];
  assert.deepEqual(pinnedPages(null), fallback);
  assert.deepEqual(pinnedPages(""), fallback);
  assert.deepEqual(pinnedPages("nonsense,,"), fallback);
  assert.deepEqual(pinnedPages("logs,analytics,schedule,diagnose"), ["logs", "analytics", "schedule", "diagnose"]);
  assert.deepEqual(pinnedPages(" logs , analytics "), ["logs", "analytics", "overview", "devices"]);
  assert.deepEqual(pinnedPages("analytics,analytics,ghost,overview"), ["analytics", "overview", "devices", "energy"]);
  assert.deepEqual(pinnedPages("logs,analytics,schedule,diagnose,maintenance"), ["logs", "analytics", "schedule", "diagnose"]);
  assert.deepEqual(pinnedPages(["control", "energy"]), ["control", "energy", "overview", "devices"]);
  assert.deepEqual(pinnedPages(42), fallback);
});

test("swapping: two bar pages trade places, a page from More takes its partner's place", () => {
  const bar = ["overview", "devices", "energy", "control"];
  assert.deepEqual(swapPinned(bar, "control", "analytics"), ["overview", "devices", "energy", "analytics"]);
  assert.deepEqual(swapPinned(bar, "analytics", "devices"), ["overview", "analytics", "energy", "control"]);
  assert.deepEqual(swapPinned(bar, "overview", "control"), ["control", "devices", "energy", "overview"]);
  assert.equal(swapPinned(bar, "logs", "analytics"), null);
  assert.equal(swapPinned(bar, "energy", "energy"), null);
  assert.deepEqual(bar, ["overview", "devices", "energy", "control"]);
});
