// SPDX-License-Identifier: AGPL-3.0-or-later
// Renders real admin.js Maintenance hardware cards against a minimal DOM shim
// and prints what the card resolved to, so the hardware-role/transport contract
// is tested against the shipped renderers instead of a hand-rebuilt copy.
//
// Input  (stdin JSON): {"card": "mqtt_proposal"|"hardware"|"configured_devices",
//                       "payload": {...}, "draft": {...}, "pristine": {...}}
// Output (stdout JSON): {className, dataset, transportPill, text, action}, or
//                       for "configured_devices" one {classes, dataset, text,
//                       controls, buttons} per card the draft's devices render.
//
// A proposal payload without an explicit "state" resolves it through the real
// mconfigMqttProposalReviewState against the supplied draft/pristine.
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

// --- minimal DOM ---------------------------------------------------------

class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.className = "";
    this._text = "";
  }
  get classList() {
    return {
      add: (...names) => {
        this.className = [this.className, ...names].filter(Boolean).join(" ");
      },
      contains: (name) => this.className.split(/\s+/).includes(name),
    };
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

const document = {
  createElement: (tag) => new Node(tag),
  getElementById: () => null,
};

// --- real admin.js helpers ------------------------------------------------

const HELPERS = [
  "hardwareCardKindForRole",
  "hardwareCardClass",
  "isMqttGridMeterProposal",
  "isExternalMqttProposal",
  "mqttProposalHardwareRole",
  "mqttGridMeterProposalTopic",
  "mqttProposalBrokerRef",
  "mqttProposalBrokerProfile",
  "mqttGridMeterConfigFromProposal",
  "mconfigGridMeterIsMapping",
  "mconfigMqttGridMeterState",
  "mconfigMqttProposalReviewState",
  "mqttSourceOfConnection",
  "connectionLabelFor",
  "mqttTransportLabel",
  "mqttGenerationLabel",
  "mqttWriteProtocolLabel",
  "mqttControlReasonLabel",
  "mqttProposalControlReason",
  "mqttProposalWriteProtocol",
  "mconfigProposalCanControl",
  "mconfigConnectionIsControlDowngrade",
  "mconfigConfiguredDeviceForCandidate",
  "mconfigDeviceIsActive",
  "mconfigIsMqttDevice",
  "mconfigAppendDeviceFact",
  "mconfigAppendSourceBadge",
  "mconfigSetExpanded",
  "mconfigHardwareCard",
  "renderMaintenanceMqttProposalCard",
  "mconfigLabelRow",
  "mconfigCheckboxControl",
  "mconfigDeviceConnectionSource",
  "mconfigInverterSummary",
  "mconfigIsExternalDevice",
  "mconfigTextControl",
  "mconfigExternalDeviceSummary",
  "renderMaintenanceExternalDevice",
  "renderMaintenanceInverter",
  "renderMaintenanceInverters",
];

const input = JSON.parse(fs.readFileSync(0, "utf8"));

// The inverter state machine has its own suite (connection switch-back); here it
// only has to yield a state, so the card can be rendered.
const STUBS = `
function generationLabel(id) { return id || ""; }
function mconfigConnectionRelationshipNote() { return null; }
function mconfigMqttProposalState() { return "new"; }
function editorStub(name) {
  const node = document.createElement("input");
  node.dataset.editor = name;
  return node;
}
function renderLocalApiConnectionFields() { return editorStub("local_api_connection"); }
function renderCommonInverterFields() { return editorStub("common_inverter_fields"); }
function renderMaintenanceZendureMqttDevice() { return editorStub("zendure_mqtt"); }
function mconfigAttachOverrideBadge(row) { return row; }
function mconfigDeviceOverrideEntry() { return null; }
function mconfigRerenderDiscoveryReview() {}
const mconfigEls = { inverters: document.createElement("div") };
const mconfigState = {
  openHardware: new Set(),
  draft: ${JSON.stringify(input.draft || {})},
  pristine: ${JSON.stringify(input.pristine || {})},
};
`;

const scope = {};
const factory = new Function(
  "document",
  "scope",
  '"use strict";\n' +
    STUBS +
    extractConst("MCONFIG_MQTT_PROPOSAL_ACTIONS") +
    "\n" +
    extractConst("MCONFIG_MQTT_GRID_METER_ACTIONS") +
    "\n" +
    extractConst("MCONFIG_DISCOVERY_STATUS_TEXT") +
    "\n" +
    HELPERS.map(extractFunction).join("\n") +
    "\nscope.renderMaintenanceMqttProposalCard = renderMaintenanceMqttProposalCard;" +
    "\nscope.mconfigMqttProposalReviewState = mconfigMqttProposalReviewState;" +
    "\nscope.mconfigHardwareCard = mconfigHardwareCard;" +
    "\nscope.renderMaintenanceInverters = renderMaintenanceInverters;" +
    "\nscope.mconfigEls = mconfigEls;"
);
factory(document, scope);

// --- render ---------------------------------------------------------------

function describe(element) {
  const nodes = element.descendants();
  const has = (node, name) => (node.className || "").split(/\s+/).includes(name);
  const pill = nodes.find((node) => has(node, "connection-pill"));
  const button = nodes.find((node) => has(node, "mconfig-discovery-add-button"));
  return {
    className: element.className,
    classes: element.className.split(/\s+/).filter(Boolean),
    dataset: element.dataset,
    transportPill: pill
      ? { text: pill.textContent, connection: pill.dataset.connection || "" }
      : null,
    action: button
      ? {
          text: button.textContent,
          disabled: !!button.disabled,
          classes: button.className.split(/\s+/).filter(Boolean),
        }
      : null,
    text: element.textContent,
  };
}

function describeConfiguredCard(element) {
  const nodes = element.descendants();
  return {
    classes: (element.className || "").split(/\s+/).filter(Boolean),
    dataset: element.dataset,
    text: element.textContent,
    controls: nodes
      .filter((node) => ["INPUT", "SELECT", "TEXTAREA"].includes(node.tagName))
      .map((node) => ({
        tag: node.tagName,
        type: node.type || "",
        editor: node.dataset.editor || "",
      })),
    buttons: nodes
      .filter((node) => node.tagName === "BUTTON")
      .map((node) => node.className),
  };
}

function rendered() {
  if (input.card === "configured_devices") {
    scope.renderMaintenanceInverters();
    return scope.mconfigEls.inverters.children.map(describeConfiguredCard);
  }
  let element;
  if (input.card === "mqtt_proposal") {
    const item = Object.assign({}, input.payload);
    if (item.state === undefined) {
      item.state = scope.mconfigMqttProposalReviewState(item.mqttProposal);
    }
    element = scope.renderMaintenanceMqttProposalCard(item);
  } else {
    const options = Object.assign({}, input.payload);
    options.body = new Node("div");
    element = scope.mconfigHardwareCard(options).element;
  }
  return describe(element);
}

process.stdout.write(JSON.stringify(rendered()));
