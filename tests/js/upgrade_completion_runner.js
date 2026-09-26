// SPDX-License-Identifier: AGPL-3.0-or-later
// Drives the real admin.js pollUpgradeJob to its terminal status, so what the
// Guided Upgrade panel re-reads once the job is over is tested against the
// shipped code. loadUpgradeCurrentVersion is the real one too: the point of the
// test is whether the finished job reaches it.
//
// Input  (stdin JSON): {"status": "succeeded"|"failed", "overview": {...}}
// Output (stdout JSON): {"fetched": [<url>, ...], "current": {...}, "rendered": [...]}
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
  const async = source.slice(Math.max(0, start - 6), start) === "async ";
  // Walk the parameter list first: admin.js already declares destructured
  // parameters with defaults, and their brace is not the body.
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
    else if (source[i] === "}" && --depth === 0) {
      return (async ? "async " : "") + source.slice(start, i + 1);
    }
  }
  throw new Error("unbalanced braces while extracting " + name);
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const scope = { fetched: [], rendered: [] };

new Function(
  "scope",
  "input",
  '"use strict";\n' +
    "const upgradeState = { current: { tag: 'v0.8.5', image: 'repo:v0.8.5', state: 'installed' },\n" +
    "  runningAdmin: { tag: null, image: null }, completed: false };\n" +
    "scope.upgradeState = upgradeState;\n" +
    "const UPGRADE_POLL_INTERVAL_MS = 1200;\n" +
    "let upgradePollTimer = null;\n" +
    "async function fetch(url) {\n" +
    "  scope.fetched.push(url);\n" +
    "  if (url.indexOf('/api/admin/maintenance/overview') === 0) {\n" +
    "    return { ok: true, json: async () => input.overview };\n" +
    "  }\n" +
    "  return { ok: true, json: async () => ({ ok: true, status: input.status,\n" +
    "    steps: [], result: { ok: input.status === 'succeeded' } }) };\n" +
    "}\n" +
    "function stopUpgradePolling() { upgradePollTimer = null; }\n" +
    "function renderSystemAlignmentStatus() {}\n" +
    "function renderUpgradeSteps() {}\n" +
    "function renderUpgradeValidation(items) { scope.rendered.push('validation'); }\n" +
    "function renderUpgradeResult(data) {\n" +
    "  scope.rendered.push('result');\n" +
    "  if (data && data.ok) upgradeState.completed = true;\n" +
    "}\n" +
    "function renderUpgradeCurrent() { scope.rendered.push('current'); }\n" +
    "function renderUpgradeAdminAlignment() { scope.rendered.push('admin-alignment'); }\n" +
    "function renderUpgradePlan() { scope.rendered.push('plan'); }\n" +
    "function setUpgradeRunning() {}\n" +
    "function updateUpgradeActionButtons() {}\n" +
    extractFunction("loadUpgradeCurrentVersion") +
    "\n" +
    extractFunction("pollUpgradeJob") +
    "\nscope.run = () => pollUpgradeJob('job-1');\n"
)(scope, input);

scope.run().then(() => {
  process.stdout.write(
    JSON.stringify({
      fetched: scope.fetched,
      current: scope.upgradeState.current,
      rendered: scope.rendered,
    }) + "\n"
  );
});
