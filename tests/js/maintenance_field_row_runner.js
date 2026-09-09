// SPDX-License-Identifier: AGPL-3.0-or-later
// Renders one real admin.js settings row for a catalog field and prints what it
// carried, so the risk/level markers are tested against the shipped renderer.
//
// Input  (stdin JSON): {"field": {...}, "value": <any>}
// Output (stdout JSON): {className, dataset, badges: [{className, text, title}]}
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

class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.className = "";
    this._text = "";
  }
  set textContent(value) {
    this._text = value == null ? "" : String(value);
    this.children = [];
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  append(...nodes) {
    nodes.forEach((node) => this.appendChild(node));
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }
  addEventListener() {}
  querySelector() {
    return null;
  }
  descendants() {
    return this.children.flatMap((child) => [child, ...child.descendants()]);
  }
}

const document = { createElement: (tag) => new Node(tag) };

const HELPERS = [
  "mconfigTextControl",
  "mconfigCheckboxControl",
  "mconfigSelectControl",
  "mconfigCatalogControl",
  "mconfigFieldRiskBadge",
  "mconfigLabelRow",
  "mconfigCatalogRow",
];

const input = JSON.parse(fs.readFileSync(0, "utf8"));

const scope = {};
const factory = new Function(
  "document",
  "scope",
  '"use strict";\n' +
    extractConst("MCONFIG_FIELD_RISK_LABELS") +
    "\n" +
    HELPERS.map(extractFunction).join("\n") +
    "\nscope.mconfigCatalogRow = mconfigCatalogRow;"
);
factory(document, scope);

const row = scope.mconfigCatalogRow(input.field, input.value, () => {});
const has = (node, name) => (node.className || "").split(/\s+/).includes(name);
process.stdout.write(
  JSON.stringify({
    className: row.className,
    dataset: row.dataset,
    badges: row
      .descendants()
      .filter((node) => has(node, "mconfig-risk-badge"))
      .map((node) => ({
        className: node.className,
        text: node.textContent,
        title: node.attributes.title || null,
      })),
  }) + "\n"
);
