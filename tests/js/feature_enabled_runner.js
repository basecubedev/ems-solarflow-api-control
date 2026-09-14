// SPDX-License-Identifier: AGPL-3.0-or-later
// Evaluates the real admin.js feature-enabled resolver, so a missing value is
// tested against the shipped code rather than against a copy of it.
//
// Input  (stdin JSON): {"section": <catalog section>, "values": {<path>: <value>}}
// Output (stdout JSON): {"enabled": <true|false|null>}
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
    extractFunction("featureEnabledPath") +
    "\n" +
    extractFunction("featureEnabledFromValues") +
    "\nscope.featureEnabledFromValues = featureEnabledFromValues;"
)(scope);

const input = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(
  JSON.stringify({
    enabled: scope.featureEnabledFromValues(input.section, input.values),
  }) + "\n"
);
