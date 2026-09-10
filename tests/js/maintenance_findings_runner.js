// SPDX-License-Identifier: AGPL-3.0-or-later
// Renders the real admin.js findings list against a minimal DOM, so the status
// page's leading answer is tested against the shipped code rather than a copy.
//
// Input  (stdin JSON): {"health": {...}}
// Output (stdout JSON): {"hidden", "headline", "items": [...], "html"}
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

// A DOM small enough to be obvious and real enough that innerHTML would show:
// textContent escapes, so anything that reached the markup as tags is a bug.
function makeElement(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    className: "",
    hidden: false,
    _text: "",
    get textContent() {
      return this.children.length
        ? this.children.map((child) => child.textContent).join("")
        : this._text;
    },
    set textContent(value) {
      this.children = [];
      this._text = String(value);
    },
    append(...nodes) {
      nodes.forEach((child) => this.children.push(child));
    },
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    replaceChildren(...nodes) {
      this.children = nodes.slice();
    },
    setAttribute(name, value) {
      this.dataset["attr:" + name] = String(value);
    },
  };
  return node;
}

function escapeText(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeAttr(value) {
  return escapeText(value).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function serialize(node) {
  const attrs = Object.entries(node.dataset)
    .map(([key, value]) => " " + key + '="' + escapeAttr(value) + '"')
    .join("");
  const inner = node.children.length
    ? node.children.map(serialize).join("")
    : escapeText(node._text);
  return (
    "<" + node.tagName.toLowerCase() + attrs + ">" + inner +
    "</" + node.tagName.toLowerCase() + ">"
  );
}

const section = makeElement("section");
const headline = makeElement("p");
const list = makeElement("ul");
// The real markup nests both inside the section, so serializing the section
// shows everything the renderer wrote.
section.append(headline, list);

const maintenanceEls = {
  findings: section,
  findingsHeadline: headline,
  findingsList: list,
};

const scope = {};
new Function(
  "scope",
  "maintenanceEls",
  "document",
  '"use strict";\n' +
    extractFunction("maintenanceFindingsHeadline") +
    "\n" +
    extractFunction("renderFindingsPanel") +
    "\n" +
    extractFunction("renderMaintenanceFindings") +
    "\nscope.renderMaintenanceFindings = renderMaintenanceFindings;"
)(scope, maintenanceEls, { createElement: makeElement });

const input = JSON.parse(fs.readFileSync(0, "utf8"));
scope.renderMaintenanceFindings(input.health);

const items = list.children.map((item) => ({
  code: item.dataset.code,
  severity: item.dataset.severity,
  title: (item.children[0] || { textContent: "" }).textContent,
  message: (item.children[1] || { textContent: "" }).textContent,
  nextStep: (item.children[2] || { textContent: "" }).textContent,
}));

process.stdout.write(
  JSON.stringify({
    hidden: Boolean(section.hidden),
    headline: headline.textContent,
    items,
    html: serialize(section),
  }) + "\n"
);
