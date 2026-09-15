// SPDX-License-Identifier: AGPL-3.0-or-later
// Evaluates the real admin.js loadUpgradePlanning, so the race between a
// hash-route load and a resume that pins the transition tag is tested against
// the shipped code rather than against a copy of it.
//
// Input  (stdin JSON): {"calls": [<pinnedTag|null>, ...], "default": "<tag>"}
//   All calls start in the same tick, in order, and are awaited together.
// Output (stdout JSON): {"selected": "<tag>", "pins": [...], "runs": <n>}
"use strict";

const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(
  path.join(__dirname, "..", "..", "admin", "static", "admin.js"),
  "utf8"
);

function extractFunction(name) {
  const marker = "function " + name + "(";
  const start = source.indexOf(marker);
  if (start === -1) throw new Error("function not found in admin.js: " + name);
  let depth = 0;
  let cursor = source.indexOf("(", start);
  for (let i = cursor; i < source.length; i++) {
    if (source[i] === "(") depth++;
    else if (source[i] === ")" && --depth === 0) {
      cursor = i;
      break;
    }
  }
  depth = 0;
  for (let i = source.indexOf("{", cursor); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("unbalanced braces while extracting " + name);
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const scope = { pins: [], runs: 0, selected: null };
const tick = () => new Promise((resolve) => setImmediate(resolve));
new Function(
  "scope",
  "tick",
  '"use strict";\n' +
    "const upgradeState = { loading: false, loadingPromise: null, selected: null };\n" +
    "async function loadUpgradeCurrentVersion() { await tick(); }\n" +
    "async function loadUpgradeReleases(pinnedTag) {\n" +
    "  scope.runs += 1; scope.pins.push(pinnedTag === undefined ? null : pinnedTag);\n" +
    "  await tick();\n" +
    "  upgradeState.selected = pinnedTag || scope.defaultTag;\n" +
    "}\n" +
    "async function loadUpgradeMigrationReview() { await tick(); }\n" +
    "async function loadSystemAlignmentStatus() { await tick(); }\n" +
    "function renderUpgradeAdminAlignment() {}\n" +
    "function renderUpgradePlan() {}\n" +
    "function updateUpgradeActionButtons() {}\n" +
    extractFunction("loadUpgradePlanning") +
    "\nscope.run = async (calls) => {\n" +
    "  await Promise.all(calls.map((pin) => loadUpgradePlanning(pin || undefined)));\n" +
    "  scope.selected = upgradeState.selected;\n" +
    "};"
)(scope, tick);
scope.defaultTag = input.default;
scope.run(input.calls).then(() => {
  process.stdout.write(
    JSON.stringify({ selected: scope.selected, pins: scope.pins, runs: scope.runs }) + "\n"
  );
});
