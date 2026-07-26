// SPDX-License-Identifier: AGPL-3.0-or-later
// Executes the real admin.js `serializeMqttProposalSelection` against a proposal
// read as JSON from stdin, and prints the serialized selection JSON to stdout.
//
// The function's exact source is extracted from admin.js (brace-matched) and
// evaluated, so the browser payload contract is tested from the real code, not a
// hand-rebuilt copy. Input:  {"proposal": {...}, "options": {...}}
// Output: the payload dict the backend preview receives for one selection.
"use strict";

const fs = require("fs");
const path = require("path");

const adminJsPath = path.join(__dirname, "..", "..", "admin", "static", "admin.js");
const source = fs.readFileSync(adminJsPath, "utf8");

function extractFunction(name) {
  const marker = "function " + name;
  const start = source.indexOf(marker);
  if (start === -1) {
    throw new Error("serializer function not found in admin.js: " + name);
  }
  // Skip the parameter list first (it contains destructuring braces) by
  // matching parentheses, then take the body brace after the closing ")".
  const parenOpen = source.indexOf("(", start);
  let parenDepth = 0;
  let j = parenOpen;
  for (; j < source.length; j++) {
    if (source[j] === "(") parenDepth++;
    else if (source[j] === ")") {
      parenDepth--;
      if (parenDepth === 0) break;
    }
  }
  let i = source.indexOf("{", j);
  if (i === -1) {
    throw new Error("serializer function body not found: " + name);
  }
  let depth = 0;
  for (; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) {
        return source.slice(start, i + 1);
      }
    }
  }
  throw new Error("unbalanced braces while extracting: " + name);
}

// eslint-disable-next-line no-new-func
const factory = new Function(
  extractFunction("normalizeInverterAliasTokens") +
    "\n" +
    extractFunction("serializeMqttProposalSelection") +
    "\nreturn serializeMqttProposalSelection;"
);
const serializeMqttProposalSelection = factory();

const raw = fs.readFileSync(0, "utf8");
const input = JSON.parse(raw || "{}");
const result = serializeMqttProposalSelection(input.proposal || {}, input.options || {});
process.stdout.write(JSON.stringify(result));
