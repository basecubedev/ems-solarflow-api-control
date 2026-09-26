// SPDX-License-Identifier: AGPL-3.0-or-later
// Drives the real appliance app.js refresh and polling functions, so what the
// console re-reads after an operation is tested against the shipped code
// rather than against a rebuilt copy of it.
//
// Input  (stdin JSON):
//   {"mode": "poll",    "seed": [<state.data key>, ...], "ticks": [<operations payload>, ...]}
//   {"mode": "refresh", "seed": [<state.data key>, ...]}
//   {"mode": "race",    "key": "<state.data key>", "path": "<api path>"}
//     Starts a real loadInto, invalidates while it is in flight, then answers it.
// Output (stdout JSON): {"steps": [{"fetched": [<path>, ...], "held": [<key>, ...]}, ...]}
//   `held` lists the seeded keys state.data still owns after that step: a key
//   that is gone is re-read by the next render, a key set to null is not.
//   The keys are put back before each step, so every step reports what that step
//   alone dropped rather than what some earlier one did.
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
  if (start === -1) return null;
  // Walk the parameter list first: a destructured parameter opens a brace that
  // is not the body, and counting from it truncates the function silently.
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

// Module constants sit at one indentation level inside the IIFE. A literal
// array or object is balanced; a scalar ends at its semicolon.
function extractConstant(name) {
  const marker = "\n  var " + name + " = ";
  const start = source.indexOf(marker);
  if (start === -1) return null;
  const open = start + marker.length;
  if (source[open] === "[" || source[open] === "{") {
    const closer = source[open] === "[" ? "]" : "}";
    let depth = 0;
    for (let i = open; i < source.length; i++) {
      if (source[i] === source[open]) depth++;
      else if (source[i] === closer && --depth === 0) {
        return source.slice(start + 1, i + 1) + ";";
      }
    }
    throw new Error("unbalanced brackets while extracting " + name);
  }
  const end = source.indexOf(";", open);
  if (end === -1) throw new Error("unterminated declaration for " + name);
  return source.slice(start + 1, end + 1);
}

// Everything the prelude already provides, plus the language itself. A name
// that is neither defined here nor declared in app.js is a local or a global
// and is left alone.
const PROVIDED = new Set([
  "api", "render", "renderPolled", "state", "window", "Promise", "Object",
  "String", "Number", "Boolean", "Array", "JSON", "Math", "Date", "console",
  "function", "return", "if", "else", "var", "for", "while", "typeof", "true",
  "false", "null", "undefined", "new", "delete", "in", "of", "catch", "try",
  "throw", "then", "forEach", "filter", "map", "indexOf", "push", "slice",
  "all", "resolve", "reject", "length", "hasOwnProperty", "call", "keys",
]);

// One bounded fixpoint pass: whatever the entry points reference and app.js
// declares is pulled in, so a helper the fix introduces is picked up without
// this runner having to name it.
function resolve(entryPoints) {
  const defined = new Set(PROVIDED);
  const pieces = [];
  const queue = [...entryPoints];
  while (queue.length) {
    const name = queue.shift();
    if (defined.has(name)) continue;
    const body = extractFunction(name) || extractConstant(name);
    if (body === null) {
      if (entryPoints.includes(name)) {
        throw new Error("app.js declares neither a function nor a constant " + name);
      }
      defined.add(name);
      continue;
    }
    defined.add(name);
    pieces.push(body);
    for (const match of body.matchAll(/\b([A-Za-z_$][\w$]*)\b/g)) {
      if (!defined.has(match[1])) queue.push(match[1]);
    }
  }
  return pieces.join("\n\n");
}

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const scope = { steps: [], fetched: [], operations: null };

new Function(
  "scope",
  '"use strict";\n' +
    // The real state literal, not a hand-rolled stand-in: a field the fix
    // adds and this runner forgot would read as undefined here and pass
    // while the shipped console did not work.
    extractConstant("state") +
    "\nstate.authenticated = true;\n" +
    "scope.state = state;\n" +
    "function api(path) {\n" +
    "  scope.fetched.push(path);\n" +
    "  if (path === '/api/operations') return Promise.resolve(scope.operations);\n" +
    "  if (scope.hold) {\n" +
    "    return new Promise(function (settle) { scope.answer = settle; });\n" +
    "  }\n" +
    "  return Promise.resolve({});\n" +
    "}\n" +
    "function render() {}\n" +
    "function renderPolled() {}\n" +
    "var window = {\n" +
    "  setInterval: function (fn) { scope.ticker = fn; return 1; },\n" +
    "  clearInterval: function () { scope.ticker = null; }\n" +
    "};\n" +
    resolve([
      "refresh", "refreshEverything", "startPolling", "stopPolling",
      "pollOperations", "loadInto", "invalidateDerivedViews"
    ]) +
    "\nscope.refresh = refreshEverything;\nscope.startPolling = startPolling;\n" +
    "scope.loadInto = loadInto;\nscope.invalidate = invalidateDerivedViews;\n"
)(scope);

const seed = input.seed || [];

function plant() {
  seed.forEach((key) => {
    scope.state.data[key] = { seeded: true };
  });
}

// Two microtask drains: a tick's own `.then` may queue the next one.
const settle = () => new Promise((resolve) => setImmediate(() => setImmediate(resolve)));

function record() {
  scope.steps.push({
    fetched: scope.fetched.slice(),
    held: seed.filter((key) => Object.prototype.hasOwnProperty.call(scope.state.data, key)),
  });
  scope.fetched.length = 0;
}

async function main() {
  if (input.mode === "race") {
    scope.hold = true;
    const pending = scope.loadInto(input.key, input.path);
    await settle();
    // The appliance changed while that read was on its way.
    scope.invalidate();
    scope.answer({ stale: true });
    await pending;
    await settle();
    scope.steps.push({
      fetched: scope.fetched.slice(),
      held: Object.prototype.hasOwnProperty.call(scope.state.data, input.key)
        ? [input.key]
        : [],
    });
    return;
  }
  if (input.mode === "refresh") {
    plant();
    await scope.refresh();
    await settle();
    record();
    return;
  }
  scope.operations = { active: null, unacknowledged: [] };
  scope.startPolling();
  scope.fetched.length = 0;
  for (const payload of input.ticks || []) {
    plant();
    scope.operations = payload;
    scope.ticker();
    await settle();
    record();
  }
}

main().then(() => {
  process.stdout.write(JSON.stringify({ steps: scope.steps }) + "\n");
});
