// SPDX-License-Identifier: AGPL-3.0-or-later
// Evaluates the real admin.js decision that decides whether a config reload may
// replace the settings draft, so "a reload never discards unsaved edits" is
// tested against the shipped code.
//
// Input  (stdin JSON): {"state": <mconfigState-shaped>, "options": <any>}
//   A draft or pristine given as the string "__circular__" becomes an object
//   JSON.stringify cannot serialize, which is the unreadable-input case.
// Output (stdout JSON): {"keep": true|false}
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

const scope = {};
new Function(
  "scope",
  '"use strict";\n' +
    extractFunction("mconfigShouldKeepDraft") +
    "\nscope.mconfigShouldKeepDraft = mconfigShouldKeepDraft;"
)(scope);

function circular() {
  const value = {};
  value.self = value;
  return value;
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const state = input.state;
if (state && typeof state === "object") {
  for (const key of ["draft", "pristine"]) {
    if (state[key] === "__circular__") state[key] = circular();
  }
}

process.stdout.write(
  JSON.stringify({ keep: scope.mconfigShouldKeepDraft(state, input.options) === true }) + "\n"
);
