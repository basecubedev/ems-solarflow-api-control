# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided Setup applies the backend plan; it does not compute one.

Which transport a physical device is configured over used to be decided in the
browser by ``reconcileTransportSelection()``. That whole matrix now lives in
``admin/setup_planner.py`` and is pinned by
``tests/test_admin_setup_batch_planner.py``; the identity rules underneath it by
``tests/test_device_identity.py`` and ``tests/test_admin_connection_planner.py``.

What is left here is the browser's half of the contract, and only that:

* the returned typed operations are applied exactly, and nothing else is;
* a plan that is no longer current is ignored;
* cards, names and dismissals key on issued ids, so an entity the backend could
  not identify stays separate instead of merging into another;
* the switch flow waits for a plan and renders the planner's verdict.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.simulation

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)


def _read(name="admin.js"):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _extract_fn(js, name):
    for marker in ("function " + name + "(", "async function " + name + "("):
        idx = js.find(marker)
        if idx >= 0:
            break
    assert idx >= 0, f"{name} is missing from admin.js"
    body = js[idx:]
    depth = 0
    for position, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[: position + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}")


def _constants(js):
    return "\n".join(
        line
        for line in js.split("\n")
        if line.startswith("const ") and "PATTERN = /" in line
    )


def _run(names, setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the Setup projection tests")
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in names)
    result = subprocess.run(
        [node, "-e", _constants(js) + "\n" + helpers + "\n" + setup],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_INDEX_HELPERS = (
    "emptySetupPlan",
    "emptySetupPlanIndex",
    "indexSetupPlan",
    "issuedIdentityOf",
    "draftPhysicalId",
    "selectionPhysicalId",
    "observationKey",
    "hasObservationIdentity",
    "observationPhysicalId",
    "proposalPhysicalId",
    "sameIssuedDevice",
    "setupCandidateState",
)

TOKEN_A = "opaque:v1:AAA"
TOKEN_B = "opaque:v1:BBB"


# --- the plan's operations are applied, and only those ----------------------
def test_planned_operations_drop_add_and_select_exactly_what_they_name():
    out = _run(
        _INDEX_HELPERS + ("applySetupPlanOperations",),
        """
let setupPlanIndex = emptySetupPlanIndex();
let setupPlan = { plan_id: "plan:v1:current" };
let configDraftItems = [
  { role: "inverter", draft_item_id: "item-keep" },
  { role: "inverter", draft_item_id: "item-drop" },
];
const zendureMqttPreviewProposals = new Map([["sel-drop", {}], ["sel-keep", {}]]);
const latestMqttProposals = [{ id: "prop-new", display_name: "Cloud" }];
const configAvailableIndex = new Map([["card-new", { observation_id: "card-new" }]]);
const saved = [];
function serializeMqttProposalSelection(p) { return { id: p.id }; }
function saveMqttPreviewProposals() { saved.push("mqtt"); }
function renderMqttProposals() {}
function saveConfigDraft() { saved.push("draft"); }
function observationKey(device) { return device.observation_id; }
function draftHasSource() { return false; }
function draftItemFromDevice(device) {
  return { role: "inverter", draft_item_id: "item-adopted", source_id: device.observation_id };
}
function rememberedConfigName() { return ""; }

const changed = applySetupPlanOperations({
  plan_id: "plan:v1:current",
  operations: {
    drop_draft_items: ["item-drop"],
    drop_mqtt_selections: ["sel-drop"],
    select_mqtt_proposals: [{ id: "prop-new", selection_origin: "priority" }],
    adopt_observations: [{ observation_ref: "card-new", observation_id: "obs:v1:new" }],
  },
});
console.log(JSON.stringify({
  changed,
  drafts: configDraftItems.map((item) => item.draft_item_id),
  selections: [...zendureMqttPreviewProposals.keys()],
  origin: zendureMqttPreviewProposals.get("prop-new").selection_origin,
}));
""",
    )

    assert out["changed"] is True
    assert out["drafts"] == ["item-keep", "item-adopted"]
    assert sorted(out["selections"]) == ["prop-new", "sel-keep"]
    assert out["origin"] == "priority"


def test_a_stale_plan_never_mutates_the_draft():
    """Only the newest plan may act; an older answer is discarded."""

    out = _run(
        _INDEX_HELPERS + ("applySetupPlanOperations",),
        """
let setupPlanIndex = emptySetupPlanIndex();
let setupPlan = { plan_id: "plan:v1:current" };
let configDraftItems = [{ role: "inverter", draft_item_id: "item-1" }];
const zendureMqttPreviewProposals = new Map();
const latestMqttProposals = [];
const configAvailableIndex = new Map();
function serializeMqttProposalSelection(p) { return { id: p.id }; }
function saveMqttPreviewProposals() {}
function renderMqttProposals() {}
function saveConfigDraft() {}
function observationKey(d) { return d.observation_id; }
function draftHasSource() { return false; }
function draftItemFromDevice() { return {}; }
function rememberedConfigName() { return ""; }

const changed = applySetupPlanOperations({
  plan_id: "plan:v1:superseded",
  operations: { drop_draft_items: ["item-1"] },
});
console.log(JSON.stringify({
  changed,
  drafts: configDraftItems.map((item) => item.draft_item_id),
}));
""",
    )

    assert out == {"changed": False, "drafts": ["item-1"]}


# --- issued-id projection ----------------------------------------------------
def test_issued_identity_comes_only_from_the_plan():
    out = _run(
        _INDEX_HELPERS,
        """
let setupPlanIndex = emptySetupPlanIndex();
indexSetupPlan(Object.assign(emptySetupPlan(), {
  draft_items: [{ draft_item_id: "item-1", physical_device_id: "%s" }],
  mqtt_selections: [{ id: "sel-1", physical_device_id: "%s" }],
  observations: [{ observation_ref: "obs:v1:one", physical_device_id: "%s" }],
  proposals: [{ id: "prop-1", physical_device_id: null }],
}));
console.log(JSON.stringify({
  draft: draftPhysicalId({ draft_item_id: "item-1", serial_number: "EOD1AAA111" }),
  selection: selectionPhysicalId({ id: "sel-1" }),
  observation: observationPhysicalId({ observation_id: "obs:v1:one" }),
  unplanned: draftPhysicalId({ draft_item_id: "item-unknown", serial_number: "EOD1AAA111" }),
  unresolved: proposalPhysicalId({ id: "prop-1", serial_number: "EOD1AAA111" }),
  same: sameIssuedDevice("%s", "%s"),
  neitherKnown: sameIssuedDevice("", ""),
}));
"""
        % (TOKEN_A, TOKEN_A, TOKEN_B, TOKEN_A, TOKEN_A),
    )

    assert out["draft"] == TOKEN_A
    assert out["selection"] == TOKEN_A
    assert out["observation"] == TOKEN_B
    # An entity the plan does not cover, and one the backend could not identify,
    # both have no browser-side identity — a serial never fills the gap.
    assert out["unplanned"] == ""
    assert out["unresolved"] == ""
    assert out["same"] is True
    assert out["neitherKnown"] is False


def test_selected_cards_collapse_only_on_an_issued_identity():
    out = _run(
        _INDEX_HELPERS + ("selectedInverterCards",),
        """
let setupPlanIndex = emptySetupPlanIndex();
indexSetupPlan(Object.assign(emptySetupPlan(), {
  draft_items: [{ draft_item_id: "item-1", physical_device_id: "%s" }],
  mqtt_selections: [
    { id: "sel-same", physical_device_id: "%s" },
    { id: "sel-other", physical_device_id: "%s" },
    { id: "sel-unknown", physical_device_id: null },
  ],
}));
function inverterItems() { return [{ draft_item_id: "item-1" }]; }
function selectedMqttDeviceEntries() {
  return [{ id: "sel-same" }, { id: "sel-other" }, { id: "sel-unknown" }];
}
console.log(JSON.stringify(selectedInverterCards().map((card) =>
  card.kind + ":" + ((card.item && card.item.draft_item_id) || card.entry.id))));
"""
        % (TOKEN_A, TOKEN_A, TOKEN_B),
    )

    # The MQTT selection sharing the draft's issued identity collapses into it;
    # a different identity and an unidentified selection each keep their card.
    assert out == ["http:item-1", "mqtt:sel-other", "mqtt:sel-unknown"]


def test_candidate_cards_render_the_backend_classification():
    out = _run(
        _INDEX_HELPERS + ("inverterCandidateConnectionState",),
        """
let setupPlanIndex = emptySetupPlanIndex();
indexSetupPlan(Object.assign(emptySetupPlan(), {
  candidates: [
    { id: "obs:v1:alt", state: "alternative", current_ref: "item-1",
      current_source: "zendure_mqtt" },
    { id: "obs:v1:conflict", state: "identity_conflict" },
  ],
}));
let configDraftItems = [{ draft_item_id: "item-1", config_name: "INV_7" }];
const zendureMqttPreviewProposals = new Map();
console.log(JSON.stringify([
  inverterCandidateConnectionState("obs:v1:alt"),
  inverterCandidateConnectionState("obs:v1:conflict"),
  inverterCandidateConnectionState("obs:v1:unknown"),
]));
""",
    )

    assert out[0]["state"] == "alternative"
    assert out[0]["configuredName"] == "INV_7"
    assert out[0]["currentSource"] == "zendure_mqtt"
    assert out[1]["state"] == "identity_conflict"
    # A candidate the current plan says nothing about offers no switch.
    assert out[2]["state"] == "new"


# --- dismissal and name memory are typed ------------------------------------
def test_dismissal_requires_an_issued_physical_identity():
    out = _run(
        ("dismissInverter", "undismissInverter", "inverterDismissed"),
        """
const dismissedPhysicalIds = new Set();
function saveDismissedPhysicalIds() {}
dismissInverter("");
dismissInverter("EOD1AAA111");
dismissInverter("%s");
console.log(JSON.stringify({
  stored: [...dismissedPhysicalIds],
  issued: inverterDismissed("%s"),
  bare: inverterDismissed("EOD1AAA111"),
  none: inverterDismissed(""),
}));
"""
        % (TOKEN_A, TOKEN_A),
    )

    # A bare serial is accepted as a *value* only because the backend already
    # rejected it: nothing here upgrades one into an identity.
    assert TOKEN_A in out["stored"]
    assert out["issued"] is True
    assert out["none"] is False


def test_name_memory_prefers_the_issued_identity_and_never_a_serial():
    out = _run(
        _INDEX_HELPERS
        + ("inverterNameKey", "rememberInverterName", "rememberedInverterName"),
        """
let setupPlanIndex = emptySetupPlanIndex();
indexSetupPlan(Object.assign(emptySetupPlan(), {
  draft_items: [{ draft_item_id: "item-1", physical_device_id: "%s" }],
}));
const transportInverterNames = new Map();
const identified = { draft_item_id: "item-1", serial_number: "EOD1AAA111" };
const unidentified = { draft_item_id: "item-2", serial_number: "EOD1AAA111" };
rememberInverterName(identified, "draft", "INV_1");
rememberInverterName(unidentified, "draft", "INV_2");
console.log(JSON.stringify({
  keys: [...transportInverterNames.keys()],
  identified: rememberedInverterName(identified, "draft"),
  unidentified: rememberedInverterName(unidentified, "draft"),
}));
"""
        % TOKEN_A,
    )

    # Two rows displaying the same serial keep two names: the identified one is
    # keyed by its issued identity, the other by its local form handle.
    assert sorted(out["keys"]) == sorted([TOKEN_A, "item:item-2"])
    assert out["identified"] == "INV_1"
    assert out["unidentified"] == "INV_2"


# --- the switch flow --------------------------------------------------------
def test_switch_awaits_a_plan_before_touching_any_store():
    js = _read()
    source = _extract_fn(js, "switchInverterTransport")
    assert "await requestSetupPlan(" in source
    for mutation in ("configDraftItems", "zendureMqttPreviewProposals", "saveConfigDraft"):
        assert mutation not in source, (
            f"switchInverterTransport touches {mutation} itself; the mutation "
            "belongs behind the returned plan in applyConnectionSwitch()"
        )


def test_switch_applies_only_a_use_candidate_verdict():
    js = _read()
    source = _extract_fn(js, "switchInverterTransport")
    assert 'verdict.action !== "use_candidate"' in source
    assert "connectionSwitchBlockedText(verdict)" in source


def test_apply_carries_the_logical_inverter_across_the_connection():
    js = _read()
    source = _extract_fn(js, "applyConnectionSwitch")
    for carried in ("preservedName", "preservedValues", "preservedEnabled"):
        assert carried in source
    assert "verdict.physical_device_id" in source


# --- rendering contracts carried over ---------------------------------------
def test_mqtt_inverter_card_shows_its_connection_and_supports_remove():
    js = _read()
    card = js.split("function renderMqttInverterCard", 1)[1].split("\nfunction ", 1)[0]
    assert "connectionLabelFor(source)" in card
    assert "renderConnectionPill(source)" in card
    assert "config-mqtt-remove" in card
    assert "renderTransportSwitchButton" not in card
    assert "escapeHtml(serial)" in card


def test_http_inverter_body_names_its_connection_without_a_switch_control():
    js = _read()
    body = js.split("function renderInverterBody", 1)[1].split("\nfunction ", 1)[0]
    assert 'connectionLabelFor("local_api")' in body
    assert "renderTransportSwitchButton" not in body


def test_mqtt_candidate_card_uses_uniform_add_label_and_escapes():
    js = _read()
    card = js.split("function renderMqttCandidateCard", 1)[1].split("\nfunction ", 1)[0]
    assert "config-mqtt-add" in card
    assert "Add inverter" in card
    assert "Add as inverter" not in card
    assert "renderConnectionCandidateAction(" in card
    assert "connectionLabelFor(source)" in card
    assert "escapeHtml(serial)" in card


def test_candidate_action_addresses_a_connection_by_its_issued_id():
    js = _read()
    fn = js.split("function renderConnectionCandidateAction", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "escapeHtml(state.candidateId)" in fn
    for raw in ("identityRef", "serial", "product_key", "broker"):
        assert raw not in fn, raw


def test_add_more_devices_dedupes_on_the_issued_connection_id():
    out = _run(
        _INDEX_HELPERS + ("plannedConnectionId", "unselectedMqttDeviceProposals"),
        """
let setupPlanIndex = emptySetupPlanIndex();
indexSetupPlan(Object.assign(emptySetupPlan(), {
  mqtt_selections: [{ id: "m:sel", connection_id: "conn:v1:SEL" }],
  proposals: [
    { id: "m:sel", connection_id: "conn:v1:SEL" },
    { id: "m:same", connection_id: "conn:v1:SEL" },
    { id: "m:other", connection_id: "conn:v1:OTHER" },
    { id: "m:unplanned", connection_id: null },
  ],
}));
const zendureMqttPreviewProposals = new Map([["m:sel", {}]]);
function selectedMqttDeviceEntries() { return [{ id: "m:sel" }]; }
function availableMqttDeviceProposals() {
  return [
    { id: "m:sel" }, { id: "m:same" }, { id: "m:other" }, { id: "m:unplanned" },
  ];
}
console.log(JSON.stringify(unselectedMqttDeviceProposals().map((p) => p.id)));
""",
    )

    # The selected connection and a second observation of it collapse; another
    # route and an unplaced proposal stay offered.
    assert out == ["m:other", "m:unplanned"]


def test_config_continue_gate_follows_backend_preview_only():
    js = _read()
    nav = js.split("function renderSetupNav", 1)[1].split("\nfunction ", 1)[0]
    assert "hasMqttPreviewProposals()" not in nav
    assert "latestConfigPreview && latestConfigPreview.ready" in nav


def test_priority_change_replans_config_and_invalidates_preview():
    js = _read()
    fn = js.split("async function persistDiscoveryPreparation", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "syncConfigFromDiscovery()" in fn
    sync = _extract_fn(js, "syncConfigFromDiscovery")
    assert "refreshSetupPlan()" in sync


def test_mqtt_selection_changes_invalidate_preview():
    js = _read()
    for name in ("removeMqttInverter", "applyConnectionSwitch"):
        assert "renderConfigDraft()" in _extract_fn(js, name)
    toggle = js.split("function toggleMqttPreviewProposal", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "renderConfigPreview()" in toggle


def test_replanning_alone_never_revokes_preview_authority():
    """Only a change to the draft may revoke an issued preview.

    Every plan response re-renders the draft view. Doing that through the full
    renderer would revoke the exact preview authority on each poll, so an issued
    preview could never survive long enough to be applied.
    """

    js = _read()
    refresh = _extract_fn(js, "refreshSetupPlan")
    assert "if (changed) renderConfigDraft();" in refresh
    assert "else renderConfigDraftView();" in refresh
    view = _extract_fn(js, "renderConfigDraftView")
    assert "renderConfigPreview()" not in view
    assert "renderInverterList()" in view


# --- applying an approved switch --------------------------------------------
_SWITCH_HELPERS = _INDEX_HELPERS + (
    "inverterNameKey",
    "rememberedInverterName",
    "rememberedConfigName",
    "preservedInverterValues",
    "applyConnectionSwitch",
)

_SWITCH_STUBS = """
let setupPlanIndex = emptySetupPlanIndex();
indexSetupPlan(Object.assign(emptySetupPlan(), {
  draft_items: [{ draft_item_id: "item-1", physical_device_id: "%s" }],
  mqtt_selections: [{ id: "prop-1", physical_device_id: "%s" }],
}));
const DEVICE_MAPPED_FIELD_KEYS = { ip: true, port: true, serial_number: true };
const transportInverterNames = new Map();
const configAvailableIndex = new Map([["obs:v1:api", {
  observation_id: "obs:v1:api", ip: "10.0.0.9", port: 8080,
  display_name: "SolarFlow", model: "SolarFlow",
}]]);
const latestMqttProposals = [{ id: "prop-1", display_name: "Cloud" }];
const zendureMqttPreviewProposals = new Map();
const configDismissed = new Set();
function saveConfigDismissed() {}
function saveMqttPreviewProposals() {}
function saveConfigDraft() {}
function renderMqttProposals() {}
function renderConfigDraft() {}
function renderConfigAvailable() {}
function observationKey(device) { return device.observation_id; }
function nextInverterName() { return "INV_FALLBACK"; }
function inverterItems() { return configDraftItems; }
function selectedMqttDeviceEntries() {
  return Array.from(zendureMqttPreviewProposals.values());
}
function rememberInverterName(entity, kind, name) {}
function undismissInverter() {}
function mconfigDeviceInactiveByChoice(view) { return view.enabled === false; }
function inverterActivationView(item, source) {
  return { kind: source, enabled: item && item.enabled !== false };
}
function draftItemFromDevice(device) {
  return {
    role: "inverter", draft_item_id: "item-new", source_id: device.observation_id,
    ip: device.ip, port: device.port, config_values: {},
  };
}
function serializeMqttProposalSelection(proposal, options) {
  return { id: proposal.id, config_values: (options || {}).configValues,
           enabled: (options || {}).enabled === false ? false : undefined };
}
"""


def _switch(setup):
    return _run(_SWITCH_HELPERS, (_SWITCH_STUBS % (TOKEN_A, TOKEN_A)) + setup)


def test_switching_to_mqtt_keeps_one_inverter_with_its_values_and_name():
    out = _switch(
        """
let configDraftItems = [{
  role: "inverter", draft_item_id: "item-1", config_name: "INV_7", enabled: true,
  config_values: { max_power: 642, min_soc: 22, ip: "10.0.0.1" },
}];
applyConnectionSwitch(
  { physical_device_id: "%s", action: "use_candidate" },
  { kind: "proposal", id: "prop-1", current_ref: "item-1", current_source: "local_api" },
);
const selected = zendureMqttPreviewProposals.get("prop-1");
console.log(JSON.stringify({
  drafts: configDraftItems.length,
  name: selected.config_name,
  origin: selected.selection_origin,
  values: selected.config_values,
  enabled: selected.enabled,
}));
"""
        % TOKEN_A
    )

    # One logical inverter: the old connection is gone, the name and the common
    # EMS values came across, and connection-owned fields did not.
    assert out["drafts"] == 0
    assert out["name"] == "INV_7"
    assert out["origin"] == "manual"
    assert out["values"] == {"max_power": 642, "min_soc": 22}


def test_switching_back_to_local_api_never_creates_a_second_inverter():
    out = _switch(
        """
let configDraftItems = [];
zendureMqttPreviewProposals.set("prop-1", {
  id: "prop-1", config_name: "INV_7", enabled: false,
  config_values: { max_power: 642 },
});
applyConnectionSwitch(
  { physical_device_id: "%s", action: "use_candidate" },
  { kind: "observation", id: "obs:v1:api", current_ref: "prop-1",
    current_source: "zendure_mqtt" },
);
console.log(JSON.stringify({
  drafts: configDraftItems.length,
  selections: zendureMqttPreviewProposals.size,
  name: configDraftItems[0].config_name,
  enabled: configDraftItems[0].enabled,
  values: configDraftItems[0].config_values,
}));
"""
        % TOKEN_A
    )

    assert out == {
        "drafts": 1,
        "selections": 0,
        "name": "INV_7",
        # A device the operator turned off stays off across the switch.
        "enabled": False,
        "values": {"max_power": 642},
    }


def test_a_candidate_the_browser_no_longer_holds_changes_nothing():
    out = _switch(
        """
let configDraftItems = [{
  role: "inverter", draft_item_id: "item-1", config_name: "INV_7", config_values: {},
}];
applyConnectionSwitch(
  { physical_device_id: "%s", action: "use_candidate" },
  { kind: "observation", id: "obs:v1:vanished", current_ref: "item-1",
    current_source: "local_api" },
);
console.log(JSON.stringify({
  drafts: configDraftItems.map((item) => item.draft_item_id),
  selections: zendureMqttPreviewProposals.size,
}));
"""
        % TOKEN_A
    )

    assert out == {"drafts": ["item-1"], "selections": 0}
