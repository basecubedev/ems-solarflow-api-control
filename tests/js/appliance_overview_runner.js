// SPDX-License-Identifier: AGPL-3.0-or-later
// Runs the real appliance app.js overview view functions and prints what they
// resolved to, so the verdict, the ranked findings and the nav attention marks
// are tested against the shipped code rather than a rebuilt copy.
//
// Input  (stdin JSON): {"status": {...}, "view": "overview"}
// Output (stdout JSON): {verdict, findings, actions, attention}
//
// The functions under test are pure: they map the status payload onto view
// data and touch no DOM, so this runner needs no element shim.
"use strict";

const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(
  path.join(__dirname, "..", "..", "appliance", "static", "app.js"),
  "utf8"
);

function extractFunction(name) {
  const marker = "function " + name + "(";
  const start = source.indexOf(marker);
  if (start === -1) throw new Error("function not found in app.js: " + name);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error("unbalanced braces while extracting " + name);
}

function extractVar(name) {
  const marker = "var " + name + " = ";
  const start = source.indexOf(marker);
  if (start === -1) throw new Error("var not found in app.js: " + name);
  const open = source.slice(start).search(/[[{]/) + start;
  const closer = source[open] === "[" ? "]" : "}";
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === source[open]) depth++;
    else if (source[i] === closer && --depth === 0) return source.slice(start, i + 1) + ";";
  }
  throw new Error("unbalanced brackets while extracting " + name);
}

// VIEWS is the one table of view ids and labels, and its entries also point at
// the render functions. Those are stubbed rather than extracted: nothing here
// calls them, and reading the names out of the table keeps this runner working
// when a view is added.
function viewRendererStubs(table) {
  const names = [...new Set([...table.matchAll(/render:\s*([A-Za-z_$][\w$]*)/g)].map((m) => m[1]))];
  if (!names.length) throw new Error("VIEWS declares no render functions; the table changed shape");
  return "var " + names.join(", ") + ";\n";
}

const viewsTable = extractVar("VIEWS");

const scope = {};
new Function(
  "scope",
  '"use strict";\n' +
    viewRendererStubs(viewsTable) +
    viewsTable +
    "\n" +
    extractVar("FINDING_SEVERITY_ORDER") +
    "\n" +
    extractFunction("format") +
    "\n" +
    extractFunction("viewLabel") +
    "\n" +
    extractFunction("rankSeverity") +
    "\n" +
    extractFunction("rankedFindings") +
    "\n" +
    extractFunction("findingsHeadline") +
    "\n" +
    extractFunction("findingsView") +
    "\n" +
    extractFunction("overviewVerdict") +
    "\n" +
    extractFunction("verdictAnnouncement") +
    "\n" +
    extractFunction("findingAction") +
    "\n" +
    extractFunction("attentionBySection") +
    "\nscope.findingsView = findingsView;" +
    "\nscope.overviewVerdict = overviewVerdict;" +
    "\nscope.verdictAnnouncement = verdictAnnouncement;" +
    "\nscope.findingAction = findingAction;" +
    "\nscope.attentionBySection = attentionBySection;"
)(scope);

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const status = input.status || {};
const view = input.view || "overview";
const findings = scope.findingsView(status);

process.stdout.write(
  JSON.stringify({
    verdict: scope.overviewVerdict(status),
    announcement: {
      first: scope.verdictAnnouncement(undefined, "This appliance is healthy."),
      unchanged: scope.verdictAnnouncement("This appliance is healthy.", "This appliance is healthy."),
      changed: scope.verdictAnnouncement("This appliance is healthy.", "Something on this appliance is not working.")
    },
    findings: findings,
    actions: findings.findings.map(function (item) {
      return scope.findingAction(item, view);
    }),
    attention: scope.attentionBySection(status)
  }) + "\n"
);
