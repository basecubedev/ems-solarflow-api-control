// SPDX-License-Identifier: AGPL-3.0-or-later
// Renders the real admin.js Maintenance MQTT device card against a minimal DOM
// shim and reports the device object before/after, so renderer purity is proven
// against the shipped renderer instead of a hand-rebuilt copy.
//
// Input  (stdin JSON): {"device": {...}, "catalog": {...}, "renders": 2}
// Output (stdout JSON): {before, after, mutated, outputControl, note,
//                        controlReadiness, writeProtocol}
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

class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.className = "";
    this.hidden = false;
    this.value = "";
    this.checked = false;
    this._text = "";
  }
  set textContent(value) {
    this._text = value == null ? "" : String(value);
    this.children = [];
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join(" ");
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  append(...nodes) {
    nodes.forEach((node) => this.appendChild(node));
  }
  replaceWith() {}
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  addEventListener() {}
  descendants() {
    return this.children.flatMap((child) => [child, ...child.descendants()]);
  }
}

const document = { createElement: (tag) => new Node(tag), getElementById: () => null };

const input = JSON.parse(fs.readFileSync(0, "utf8"));

// Card chrome, common tuning fields and draft bookkeeping have their own suites;
// here they only have to yield nodes so the MQTT editor can render.
const STUBS = `
const mconfigState = {
  openHardware: new Set(),
  catalog: ${JSON.stringify(input.catalog || {})},
  draft: { devices: [] },
};
const discoverySessions = { maintenance: { mqttProposals: [] } };
function renderCommonInverterFields() { return document.createElement("div"); }
function renderMaintenanceInverters() {}
function mconfigRerenderDiscoveryReview() {}
function mconfigMarkDraftChanged() {}
function connectionLabelFor(source) { return String(source || ""); }
function mconfigGenerationLabel(id) { return String(id || ""); }
function mconfigHardwareCard(options) {
  const element = document.createElement("article");
  element.dataset.sourceId = options.id;
  element.appendChild(options.body);
  return {
    element,
    meta: document.createElement("span"),
    status: document.createElement("span"),
  };
}
`;

const HELPERS = [
  "mconfigLabelRow",
  "mconfigTextControl",
  "mconfigSelectControl",
  "mconfigCheckboxControl",
  "mconfigGenerations",
  "mconfigHardwareModels",
  "mconfigHardwareModel",
  "mconfigHardwareModelLabel",
  "mconfigModelsForGeneration",
  "mconfigIsMqttDevice",
  "mconfigDeviceMqttSource",
  "mconfigDeviceConnectionSource",
  "mqttSourceOfConnection",
  "connectionBrokerScope",
  "maintenanceMqttProposals",
  "mqttControlReasonLabel",
  "mconfigMqttControlSupported",
  "mconfigMqttControlProjection",
  "mconfigNormalizeMqttControl",
  "mconfigDeviceControlBlockReason",
  "mconfigMqttDeviceSummary",
  "renderMaintenanceZendureMqttDevice",
];

const scope = {};
const factory = new Function(
  "document",
  "scope",
  '"use strict";\n' +
    STUBS +
    HELPERS.map(extractFunction).join("\n") +
    "\nscope.renderMaintenanceZendureMqttDevice = renderMaintenanceZendureMqttDevice;" +
    "\nscope.mconfigNormalizeMqttControl = mconfigNormalizeMqttControl;"
);
factory(document, scope);

const device = input.device;
const before = JSON.parse(JSON.stringify(device));
const renders = Number(input.renders || 2);
let element = null;
for (let i = 0; i < renders; i++) {
  element = scope.renderMaintenanceZendureMqttDevice(device, 0);
}
const after = JSON.parse(JSON.stringify(device));

const values = element
  .descendants()
  .filter((node) => (node.className || "").includes("feature-readonly-value"))
  .map((node) => node.textContent);
const note = element
  .descendants()
  .filter((node) => (node.className || "").includes("mconfig-mqtt-note"))
  .map((node) => node.textContent)[0];

process.stdout.write(
  JSON.stringify({
    before,
    after,
    mutated: JSON.stringify(before) !== JSON.stringify(after),
    readonlyValues: values,
    note: note || "",
  })
);
