// SPDX-License-Identifier: AGPL-3.0-or-later
// Evaluates the real admin.js error resolver, so the precedence is tested
// against the shipped code rather than against a copy of it.
//
// Input  (stdin JSON): {"payload": <any>, "fallback": "<sentence>"}
// Output (stdout JSON): {"text": "<resolved>"}
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
    extractBlock("const ADMIN_ERROR_MESSAGES = {", "\n};") +
    "\n" +
    extractFunction("humanErrorText") +
    "\nscope.humanErrorText = humanErrorText;"
)(scope);

const input = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(
  JSON.stringify({ text: scope.humanErrorText(input.payload, input.fallback) }) + "\n"
);
