// SPDX-License-Identifier: AGPL-3.0-or-later
// Evaluates the real admin.js hash-routing helpers for the Maintenance view, so
// the permanent #maintenance-manual alias and the settings tab deep link are
// tested against the shipped code rather than against a copy of it.
//
// Input  (stdin JSON): {"hash": "<hash without #>"}
// Output (stdout JSON): {"path": "<path>", "tab": "<tab>"|null}
"use strict";

const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(
  path.join(__dirname, "..", "..", "admin", "static", "admin.js"),
  "utf8"
);

function extractBlock(marker, terminator) {
  const start = source.indexOf(marker);
  if (start === -1) throw new Error("not found in admin.js: " + marker);
  const end = source.indexOf(terminator, start);
  if (end === -1) throw new Error("unterminated block: " + marker);
  return source.slice(start, end + terminator.length);
}

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

const scope = {};
new Function(
  "scope",
  '"use strict";\n' +
    extractBlock("const MAINTENANCE_PATHS = [", "];") +
    "\n" +
    extractBlock("const MAINTENANCE_PATH_ALIASES = {", "};") +
    "\n" +
    extractBlock("const MAINTENANCE_SETTINGS_TABS = [", "];") +
    "\n" +
    extractFunction("maintenancePathForHash") +
    "\n" +
    extractFunction("maintenanceSettingsTabForHash") +
    "\nscope.maintenancePathForHash = maintenancePathForHash;" +
    "\nscope.maintenanceSettingsTabForHash = maintenanceSettingsTabForHash;" +
    "\nscope.MAINTENANCE_PATHS = MAINTENANCE_PATHS;"
)(scope);

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const resolved = scope.maintenancePathForHash(input.hash);
process.stdout.write(
  JSON.stringify({
    path: resolved,
    known: scope.MAINTENANCE_PATHS.includes(resolved),
    tab: scope.maintenanceSettingsTabForHash(input.hash),
  }) + "\n"
);
