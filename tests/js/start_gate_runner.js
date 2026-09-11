// SPDX-License-Identifier: AGPL-3.0-or-later
// Evaluates the real admin.js landing verdict and draws its findings list, so
// "the landing never claims more than install-state proves" is tested against
// the shipped code rather than a copy of it.
//
// Input  (stdin JSON): {"state": <install-state payload>|null}
// Output (stdout JSON): {"view": {...}, "findings": {...}}
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

// A DOM small enough to be obvious and real enough that innerHTML would show:
// textContent escapes, so anything that reached the markup as tags is a bug.
function makeElement(tag) {
  return {
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
    replaceChildren(...nodes) {
      this.children = nodes.slice();
    },
  };
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
  const tag = node.tagName.toLowerCase();
  return "<" + tag + attrs + ">" + inner + "</" + tag + ">";
}

const section = makeElement("section");
const headline = makeElement("p");
const list = makeElement("ul");
section.append(headline, list);

const scope = {};
new Function(
  "scope",
  "document",
  '"use strict";\n' +
    extractBlock("const START_PATH_LABELS = {", "};") +
    "\n" +
    extractBlock("const START_INSTALL_VERDICTS = {", "\n};") +
    "\n" +
    extractBlock("const FINDING_SEVERITY_ORDER = [", "];") +
    "\n" +
    extractFunction("findingsStatus") +
    "\n" +
    extractFunction("maintenanceFindingsHeadline") +
    "\n" +
    extractFunction("startGateView") +
    "\n" +
    extractFunction("startFindingsView") +
    "\n" +
    extractFunction("renderFindingsPanel") +
    "\nscope.startGateView = startGateView;" +
    "\nscope.startFindingsView = startFindingsView;" +
    "\nscope.renderFindingsPanel = renderFindingsPanel;"
)(scope, { createElement: makeElement });

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const view = scope.startGateView(input.state);
scope.renderFindingsPanel(
  { section, headline, list },
  scope.startFindingsView(view.findings)
);

process.stdout.write(
  JSON.stringify({
    view,
    findings: {
      hidden: Boolean(section.hidden),
      status: section.dataset.status,
      headline: headline.textContent,
      items: list.children.map((item) => ({
        severity: item.dataset.severity,
        lines: item.children.map((line) => ({
          className: line.className,
          text: line.textContent,
        })),
      })),
      html: serialize(section),
    },
  }) + "\n"
);
