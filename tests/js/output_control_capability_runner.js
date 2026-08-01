// SPDX-License-Identifier: AGPL-3.0-or-later
// Runs the real admin.js output-control capability chain so the write-eligibility
// contract is exercised against the shipped predicates instead of a rebuilt copy.
//
// Input  (stdin JSON): {"scenario": "capability"|"switch"|"proposal_add",
//                       "device": {...}, "generation": {...}, "model": {...},
//                       "proposal": {...}, "current": {...}, "catalog": {...}}
// Output (stdout JSON): scenario-specific, see the dispatch at the bottom.
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

const input = JSON.parse(fs.readFileSync(0, "utf8"));

const HELPERS = [
  "mconfigMqttControlSupported",
  "mconfigIsMqttDevice",
  "mconfigDeviceIsActive",
  "mconfigDeviceInactiveByChoice",
  "mconfigApplyTransportSwitchActivation",
  "mconfigZendureMqttDraftFromProposal",
  "mconfigDeviceCommonDefaults",
  "mconfigApplyCommonDefaults",
  "mconfigNextInverterName",
  "mqttProposalBrokerRef",
  "mqttProposalBrokerProfile",
];

const STUBS = `
function nextCompactInverterName(names, count) { return "INV_" + (count + 1); }
const mconfigState = {
  catalog: ${JSON.stringify(input.catalog || {})},
  draft: ${JSON.stringify(input.draft || { devices: [] })},
};
`;

const scope = {};
const factory = new Function(
  "scope",
  '"use strict";\n' +
    STUBS +
    HELPERS.map(extractFunction).join("\n") +
    "\n" +
    HELPERS.map((name) => "scope." + name + " = " + name + ";").join("\n")
);
factory(scope);

function capability(device, model) {
  return scope.mconfigMqttControlSupported(device, model);
}

let result;
if (input.scenario === "capability") {
  result = {
    supported: capability(input.device || {}, input.model),
  };
} else if (input.scenario === "proposal_add") {
  const device = scope.mconfigZendureMqttDraftFromProposal(input.proposal || {});
  result = {
    output_control: device.output_control,
    supports_output_control: device.supports_output_control,
    write_output_limit: (device.capabilities || {}).write_output_limit,
    trusted_write_target: device.trusted_write_target,
    route_device_id: (device.mqtt || {}).device_id,
    supported: capability(device, input.model),
  };
} else if (input.scenario === "switch") {
  const replacement = scope.mconfigZendureMqttDraftFromProposal(input.proposal || {});
  scope.mconfigApplyTransportSwitchActivation(replacement, input.current || {});
  result = {
    output_control: replacement.output_control,
    supports_output_control: replacement.supports_output_control,
    write_output_limit: (replacement.capabilities || {}).write_output_limit,
    trusted_write_target: replacement.trusted_write_target,
    route_device_id: (replacement.mqtt || {}).device_id,
    enabled: replacement.enabled,
    supported: capability(replacement, input.model),
    active: scope.mconfigDeviceIsActive(replacement),
  };
} else {
  throw new Error("unknown scenario: " + input.scenario);
}
process.stdout.write(JSON.stringify(result));
