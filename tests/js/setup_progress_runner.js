// SPDX-License-Identifier: AGPL-3.0-or-later
// Evaluates the real admin.js Guided Setup progress sentence, so "the verdict
// line and the stepper chip cannot disagree" is tested against shipped code.
//
// Input  (stdin JSON): {"step": <id>, "statusText": <string>, "tone": <string>}
// Output (stdout JSON): {"verdict": ..., "tone": ...}
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
    extractBlock("const SETUP_STEPS = [", "];") +
    "\n" +
    extractBlock("const SETUP_STEP_TITLES = {", "\n};") +
    "\n" +
    extractFunction("setupProgressView") +
    "\nscope.setupProgressView = setupProgressView;" +
    "\nscope.SETUP_STEPS = SETUP_STEPS;"
)(scope);

const input = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(
  JSON.stringify({
    steps: scope.SETUP_STEPS,
    ...scope.setupProgressView(input.step, input.statusText, input.tone),
  }) + "\n"
);
