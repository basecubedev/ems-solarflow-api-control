// SPDX-License-Identifier: AGPL-3.0-or-later
// Runs the real admin.js control-and-safety view against a control summary and
// prints what it resolved to, so the Maintenance safety statement is tested
// against the shipped renderer instead of a hand-rebuilt copy.
//
// Input  (stdin JSON): {"control": {...} | null}
// Output (stdout JSON): {status, tone, verdict, transports, envelope, notes}
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

function extractConst(name) {
  const marker = "const " + name + " = ";
  const start = source.indexOf(marker);
  if (start === -1) throw new Error("const not found in admin.js: " + name);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) {
      return source.slice(start, i + 1) + ";";
    }
  }
  throw new Error("unbalanced braces while extracting " + name);
}

const HELPERS = [
  "maintenanceControlTransportRow",
  "maintenanceControlEnvelopeRows",
  "maintenanceControlNotes",
  "maintenanceControlView",
];

const input = JSON.parse(fs.readFileSync(0, "utf8"));

const scope = {};
const factory = new Function(
  "scope",
  '"use strict";\n' +
    extractConst("MAINTENANCE_CONTROL_STATUS_TEXT") +
    "\n" +
    extractConst("MAINTENANCE_CONTROL_TRANSPORT_LABELS") +
    "\n" +
    extractConst("MAINTENANCE_CONTROL_NOTES") +
    "\n" +
    HELPERS.map(extractFunction).join("\n") +
    "\nscope.maintenanceControlView = maintenanceControlView;"
);
factory(scope);

process.stdout.write(
  JSON.stringify(scope.maintenanceControlView(input.control)) + "\n"
);
