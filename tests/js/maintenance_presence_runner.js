// SPDX-License-Identifier: AGPL-3.0-or-later
// Evaluates the real admin.js card-presence predicates for the manual panel, so
// "a card disappears only on a proven negative answer" is tested against the
// shipped code.
//
// Input  (stdin JSON): {"predicate": "<function name>", "payload": <any>}
// Output (stdout JSON): {"present": true|false}
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

const PREDICATES = [
  "maintenanceTelemetryCardPresent",
  "maintenanceMigrationCardPresent",
  "maintenanceRecoveryCardPresent",
];

const input = JSON.parse(fs.readFileSync(0, "utf8"));
if (!PREDICATES.includes(input.predicate)) {
  throw new Error("unknown predicate: " + input.predicate);
}

const scope = {};
const factory = new Function(
  "scope",
  '"use strict";\n' +
    PREDICATES.map(extractFunction).join("\n") +
    "\n" +
    PREDICATES.map((name) => "scope." + name + " = " + name + ";").join("\n")
);
factory(scope);

process.stdout.write(
  JSON.stringify({ present: scope[input.predicate](input.payload) === true }) + "\n"
);
