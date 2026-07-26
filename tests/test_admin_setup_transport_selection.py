# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guided Setup unified transport-selection behavior.

These tests drive the real admin.js pure helpers that decide, per physical
device, which discovery transport is configured — the resolver
(``resolveSelectedDeviceSource``) and the reconciler
(``reconcileTransportSelection``). The reconciler is the single source of truth
that keeps the Local-API draft (configDraftItems) and the selected Zendure MQTT
proposals (zendureMqttPreviewProposals) consistent so exactly one transport is
configured per physical serial.

The reproduction at the top encodes the reported bug: initial discovery
auto-adds two Local-API inverters; the user later adds Zendure cloud
credentials, moves Zendure MQTT to priority 1 and rescans; MQTT proposals arrive
for the SAME two serials. The device must then be configured over MQTT, not
left as Local API.
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

# Pure helpers extracted from admin.js and evaluated together in node. Order
# matters: dependencies first.
_PURE_HELPERS = (
    "normalizeSerial",
    "usableSerialValue",
    "physicalInverterIdentity",
    "inverterVisibleSerial",
    "inverterIdentityTokens",
    "inverterIdentitySet",
    "inverterHasIdentity",
    "inverterIdentityConflict",
    "inverterIdentitiesMatch",
    "inverterIdentitySetOf",
    "dismissalStorageKey",
    "mqttSourceOfConnection",
    "resolveSelectedDeviceSource",
    "reconcileTransportSelection",
)


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _extract_fn(js, name):
    marker = "function " + name
    assert marker in js, (
        f"{name} is missing from admin.js — the unified transport-selection "
        "logic that replaces a Local-API draft with the prioritized MQTT "
        "selection does not exist yet"
    )
    body = js.split(marker, 1)[1].split("\nfunction ", 1)[0]
    return marker + body


def _run(setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the transport-selection behavior tests")
    js = _read("admin.js")
    helpers = "\n".join(_extract_fn(js, name) for name in _PURE_HELPERS)
    script = helpers + "\n" + setup
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _reconcile(state):
    return _run(
        "const state = " + json.dumps(state) + ";\n"
        "console.log(JSON.stringify(reconcileTransportSelection(state)));"
    )


# The reported flow: two Local-API inverters were auto-added before Zendure
# credentials existed; Zendure MQTT is now priority 1 and offers proposals for
# the same two serials.
LATE_MQTT_STATE = {
    "httpInverters": [
        {"source_id": "zendure:EOD1AAA", "serial_number": "EOD1AAA", "auto_added": True},
        {"source_id": "zendure:EOD1BBB", "serial_number": "EOD1BBB", "auto_added": True},
    ],
    "mqttSelections": [],
    "httpCandidateSerials": ["EOD1AAA", "EOD1BBB"],
    "mqttProposals": [
        {"id": "zendure-mqtt:EOD1AAA", "serial_number": "EOD1AAA", "connection_source": "zendure_cloud_mqtt"},
        {"id": "zendure-mqtt:EOD1BBB", "serial_number": "EOD1BBB", "connection_source": "zendure_cloud_mqtt"},
    ],
    "priority": ["zendure_mqtt", "local_api", "local_mqtt"],
    "enabledSources": {"local_api": True, "zendure_mqtt": True, "local_mqtt": False},
    "dismissedSerials": [],
}


def test_reconcile_late_mqtt_priority_replaces_local_api_draft():
    """Reproduction: after a late Zendure MQTT rescan with MQTT at priority 1,
    both auto-added Local-API inverters are dropped and the matching MQTT
    proposals become the selected transport for the same serials."""
    plan = _reconcile(LATE_MQTT_STATE)

    assert set(plan["dropHttpSourceIds"]) == {"zendure:EOD1AAA", "zendure:EOD1BBB"}
    selected = {p["id"]: p for p in plan["selectMqttProposalIds"]}
    assert set(selected) == {"zendure-mqtt:EOD1AAA", "zendure-mqtt:EOD1BBB"}

    by_serial = {d["serial"]: d for d in plan["physicalDevices"]}
    for serial in ("eod1aaa", "eod1bbb"):
        assert by_serial[serial]["selectedSource"] == "zendure_mqtt"
        assert by_serial[serial]["selectionOrigin"] == "priority"
    # One transport per serial: nothing is both dropped-as-http and kept-as-http.
    assert not (set(plan["dropHttpSourceIds"]) & set(plan.get("dropMqttSelectionIds", [])))


def test_reconcile_is_idempotent():
    """Running the reconciler twice yields the same selection: the second pass
    (with the MQTT selection already applied and the HTTP items gone) produces
    no further HTTP drops and no re-selection."""
    plan = _reconcile(LATE_MQTT_STATE)
    second_state = dict(LATE_MQTT_STATE)
    second_state["httpInverters"] = []
    second_state["mqttSelections"] = [
        {
            "id": p["id"],
            "serial_number": p["serial_number"],
            "connection_source": "zendure_cloud_mqtt",
            "selection_origin": p["selection_origin"],
        }
        for p in plan["selectMqttProposalIds"]
    ]
    second = _reconcile(second_state)
    assert second["dropHttpSourceIds"] == []
    assert second["selectMqttProposalIds"] == []
    assert second["dropMqttSelectionIds"] == []


def test_serialless_setup_reconciliation_uses_server_token_only():
    state = {
        "httpInverters": [],
        "mqttSelections": [
            {
                "id": "selected",
                "physical_identity_token": "opaque:v1:scope-a",
                "connection_source": "zendure_cloud_mqtt",
                "selection_origin": "manual",
            }
        ],
        "httpCandidateSerials": [],
        "mqttProposals": [
            {
                "id": "same-scope",
                "physical_identity_token": "opaque:v1:scope-a",
                "connection_source": "zendure_cloud_mqtt",
            },
            {
                "id": "other-scope",
                "physical_identity_token": "opaque:v1:scope-b",
                "connection_source": "zendure_cloud_mqtt",
            },
        ],
        "priority": ["zendure_mqtt"],
        "enabledSources": {"zendure_mqtt": True},
        "dismissedSerials": [],
    }

    plan = _reconcile(state)

    # The stale selection (token scope-a) is normalized to the current same-scope
    # proposal; the other-scope proposal stays a separate physical device.
    assert plan["dropMqttSelectionIds"] == ["selected"]
    assert [s["id"] for s in plan["selectMqttProposalIds"]] == ["same-scope"]
    assert {device["serial"] for device in plan["physicalDevices"]} == {
        "opaque:v1:scope-a",
        "opaque:v1:scope-b",
    }


@pytest.mark.parametrize("placeholder", ["<redacted>", "[redacted]", "redacted"])
def test_reconcile_never_groups_transports_by_redaction_placeholder(placeholder):
    plan = _reconcile(
        {
            "httpInverters": [
                {
                    "source_id": "http-placeholder",
                    "serial_number": placeholder,
                    "auto_added": True,
                }
            ],
            "mqttSelections": [],
            "httpCandidateSerials": [placeholder],
            "mqttProposals": [
                {
                    "id": "mqtt-placeholder",
                    "serial_number": placeholder,
                    "connection_source": "zendure_cloud_mqtt",
                }
            ],
            "priority": ["zendure_mqtt", "local_api"],
            "enabledSources": {"local_api": True, "zendure_mqtt": True},
            "dismissedSerials": [placeholder],
        }
    )

    assert plan["physicalDevices"] == []
    assert plan["dropHttpSourceIds"] == []
    assert plan["dropMqttSelectionIds"] == []
    assert plan["selectMqttProposalIds"] == []


def test_fresh_setup_keeps_serialless_same_route_from_two_scopes_separate():
    out = _run_named(
        ("normalizeInverterAliasTokens", "serializeMqttProposalSelection"),
        """
const zendureMqttPreviewProposals = new Map();
const latestMqttProposals = [
  {
    id: "zendure-mqtt:opaque:v1:cloud_scope:cloud",
    physical_identity_token: "opaque:v1:cloud_scope",
    connection_source: "zendure_cloud_mqtt",
    broker_ref: "cloud",
    device_id: "…7501",
    target: "device",
    config_fragment: { type: "zendure_mqtt" },
  },
  {
    id: "zendure-mqtt:opaque:v1:local_scope:garage",
    physical_identity_token: "opaque:v1:local_scope",
    connection_source: "local_mqtt",
    broker_ref: "garage",
    device_id: "…7501",
    target: "device",
    config_fragment: { type: "zendure_mqtt" },
  },
];
let nextName = 1;
function inverterConfigNameForSerial() { return ""; }
function nextInverterName() { return "INV_" + nextName++; }
// serializeMqttProposalSelection normalizes alias tokens through this helper.
for (const proposal of latestMqttProposals) {
  const entry = serializeMqttProposalSelection(proposal, { target: "device" });
  zendureMqttPreviewProposals.set(String(proposal.id), entry);
}
console.log(JSON.stringify({
  size: zendureMqttPreviewProposals.size,
  entries: [...zendureMqttPreviewProposals.entries()].map(([id, entry]) => ({
    id,
    token: entry.physical_identity_token,
    broker_ref: entry.broker_ref,
  })),
}));
""",
    )

    assert out["size"] == 2
    assert {entry["token"] for entry in out["entries"]} == {
        "opaque:v1:cloud_scope",
        "opaque:v1:local_scope",
    }
    assert {entry["broker_ref"] for entry in out["entries"]} == {
        "cloud",
        "garage",
    }


def test_reconcile_manual_local_api_not_overridden_by_priority():
    """A manually kept Local-API inverter (auto_added false) survives even when a
    higher-priority MQTT proposal exists for the same serial, and the MQTT
    proposal is NOT auto-selected (no duplicate transport)."""
    state = dict(LATE_MQTT_STATE)
    state["httpInverters"] = [
        {"source_id": "zendure:EOD1AAA", "serial_number": "EOD1AAA", "auto_added": False},
    ]
    state["httpCandidateSerials"] = ["EOD1AAA"]
    state["mqttProposals"] = [
        {"id": "zendure-mqtt:EOD1AAA", "serial_number": "EOD1AAA", "connection_source": "zendure_cloud_mqtt"},
    ]
    plan = _reconcile(state)
    assert "zendure:EOD1AAA" not in plan["dropHttpSourceIds"]
    assert plan["selectMqttProposalIds"] == []
    dev = {d["serial"]: d for d in plan["physicalDevices"]}["eod1aaa"]
    assert dev["selectedSource"] == "local_api"
    assert dev["selectionOrigin"] == "manual"


def test_reconcile_priority_flip_to_local_api_drops_mqtt_selection():
    """When priority moves back to Local API for a serial selected over MQTT with
    no HTTP draft item, the reconciler drops the MQTT selection and resolves the
    device to Local API. applyAutoConfig then re-runs autoAddInverters to add the
    HTTP item so the device is never dropped from both stores (see the static
    test below)."""
    state = {
        "httpInverters": [],
        "mqttSelections": [
            {
                "id": "zendure-mqtt:EOD1AAA",
                "serial_number": "EOD1AAA",
                "connection_source": "zendure_cloud_mqtt",
                "selection_origin": "priority",
            }
        ],
        "httpCandidateSerials": ["EOD1AAA"],
        "mqttProposals": [
            {"id": "zendure-mqtt:EOD1AAA", "serial_number": "EOD1AAA", "connection_source": "zendure_cloud_mqtt"},
        ],
        "priority": ["local_api", "zendure_mqtt", "local_mqtt"],
        "enabledSources": {"local_api": True, "zendure_mqtt": True, "local_mqtt": False},
        "dismissedSerials": [],
    }
    plan = _reconcile(state)
    assert "zendure-mqtt:EOD1AAA" in plan["dropMqttSelectionIds"]
    dev = {d["serial"]: d for d in plan["physicalDevices"]}["eod1aaa"]
    assert dev["selectedSource"] == "local_api"


def test_apply_auto_config_readds_http_after_reconcile():
    js = _read("admin.js")
    fn = js.split("function applyAutoConfig", 1)[1].split("\nfunction ", 1)[0]
    # autoAddInverters runs twice: once before reconcile and once after, so a
    # serial the reconciler freed from MQTT gets its HTTP item and is never
    # dropped from both stores in a single pass.
    assert fn.count("autoAddInverters()") == 2


def test_manual_add_and_toggle_clear_serial_dismissal():
    js = _read("admin.js")
    add = js.split("function addDeviceToDraft", 1)[1].split("\nfunction ", 1)[0]
    # A removed device dismisses its identity; re-adding it manually must clear the
    # dismissal or the reconciler would drop the re-add on the next pass.
    assert "undismissSerial(device)" in add


def test_device_proposal_toggle_reconciles_to_drop_http_twin():
    js = _read("admin.js")
    fn = js.split("function toggleMqttPreviewProposal", 1)[1].split("\nfunction ", 1)[0]
    # Selecting a device proposal reconciles, so a same-serial Local-API draft
    # item is dropped and the two-transport duplicate is never submitted.
    assert "syncConfigFromDiscovery()" in fn


def test_reconcile_manual_mqtt_kept_when_local_api_becomes_first():
    """A manually selected MQTT proposal is preserved after the user later moves
    Local API back above Zendure MQTT — the manual transport choice wins."""
    state = dict(LATE_MQTT_STATE)
    state["httpInverters"] = []
    state["mqttSelections"] = [
        {
            "id": "zendure-mqtt:EOD1AAA",
            "serial_number": "EOD1AAA",
            "connection_source": "zendure_cloud_mqtt",
            "selection_origin": "manual",
        }
    ]
    state["httpCandidateSerials"] = ["EOD1AAA"]
    state["mqttProposals"] = [
        {"id": "zendure-mqtt:EOD1AAA", "serial_number": "EOD1AAA", "connection_source": "zendure_cloud_mqtt"},
    ]
    state["priority"] = ["local_api", "zendure_mqtt", "local_mqtt"]
    plan = _reconcile(state)
    assert plan["dropMqttSelectionIds"] == []
    dev = {d["serial"]: d for d in plan["physicalDevices"]}["eod1aaa"]
    assert dev["selectedSource"] == "zendure_mqtt"
    assert dev["selectionOrigin"] == "manual"


def test_priority_change_reasserts_priority_over_manual_markers():
    """A priority change re-expresses which transport wins, so any earlier
    per-device manual transport pick yields to the new priority. The reconciler
    itself still honors manual origin (tested above); this reassert step runs on
    a priority change and clears the manual markers before the reconciler re-runs
    so priority decides. Manual removals (dismissedSerials) are untouched."""
    out = _run_named(
        ("reassertPriorityOverManualTransport",),
        """
let configDraftItems = [
  { role: "inverter", serial_number: "EOD1AAA", auto_added: false },
  { role: "inverter", serial_number: "EOD1CCC", auto_added: true },
  { role: "grid_meter", auto_selected: false },
];
const zendureMqttPreviewProposals = new Map([
  ["m:bbb", { serial_number: "EOD1BBB", selection_origin: "manual" }],
  ["m:ddd", { serial_number: "EOD1DDD", selection_origin: "priority" }],
]);
let savedDraft = false;
let savedMqtt = false;
function saveConfigDraft() { savedDraft = true; }
function saveMqttPreviewProposals() { savedMqtt = true; }
reassertPriorityOverManualTransport();
console.log(JSON.stringify({
  draft: configDraftItems,
  mqtt: [...zendureMqttPreviewProposals.values()],
  savedDraft,
  savedMqtt,
}));
""",
    )
    draft = {item.get("serial_number") or "grid": item for item in out["draft"]}
    # The manual HTTP inverter loses its manual marker; the already-auto one and
    # the grid meter are untouched.
    assert draft["EOD1AAA"]["auto_added"] is True
    assert draft["EOD1CCC"]["auto_added"] is True
    assert draft["grid"]["auto_selected"] is False
    mqtt = {entry["serial_number"]: entry for entry in out["mqtt"]}
    # The manual MQTT selection is demoted so priority can re-decide it.
    assert mqtt["EOD1BBB"]["selection_origin"] == "priority"
    assert mqtt["EOD1DDD"]["selection_origin"] == "priority"
    assert out["savedDraft"] is True
    assert out["savedMqtt"] is True


def test_move_discovery_source_reasserts_priority_before_sync():
    js = _read("admin.js")
    fn = js.split("function moveDiscoverySource", 1)[1].split("\nfunction ", 1)[0]
    # A priority reorder reasserts priority over prior manual transport picks
    # before it persists (which triggers syncConfigFromDiscovery -> reconcile).
    assert "reassertPriorityOverManualTransport()" in fn


def test_reconcile_dismissed_serial_selects_nothing():
    """A device the user removed entirely is not re-added over either transport."""
    state = dict(LATE_MQTT_STATE)
    state["dismissedSerials"] = ["EOD1AAA"]
    plan = _reconcile(state)
    assert "zendure:EOD1AAA" in plan["dropHttpSourceIds"]
    ids = {p["id"] for p in plan["selectMqttProposalIds"]}
    assert "zendure-mqtt:EOD1AAA" not in ids
    assert "zendure-mqtt:EOD1BBB" in ids


def test_reconcile_different_serials_stay_separate():
    """Two different serials with the same model are never merged into one
    physical device."""
    plan = _reconcile(LATE_MQTT_STATE)
    serials = {d["serial"] for d in plan["physicalDevices"]}
    assert serials == {"eod1aaa", "eod1bbb"}


def test_reconcile_serialless_devices_not_merged():
    """Local-API drafts without a serial cannot be unified across transports and
    are never merged with a serial-bearing MQTT proposal."""
    state = {
        "httpInverters": [
            {"source_id": "manual:192.168.1.5:80", "serial_number": "", "auto_added": False},
        ],
        "mqttSelections": [],
        "httpCandidateSerials": [],
        "mqttProposals": [
            {"id": "zendure-mqtt:EOD1AAA", "serial_number": "EOD1AAA", "connection_source": "zendure_cloud_mqtt"},
        ],
        "priority": ["zendure_mqtt", "local_api", "local_mqtt"],
        "enabledSources": {"local_api": True, "zendure_mqtt": True, "local_mqtt": False},
        "dismissedSerials": [],
    }
    plan = _reconcile(state)
    # The serial-less manual HTTP item is never dropped in favor of an unrelated
    # MQTT proposal.
    assert plan["dropHttpSourceIds"] == []


def _resolve(available, priority, previous):
    return _run(
        "console.log(JSON.stringify(resolveSelectedDeviceSource({"
        f"available: {json.dumps(available)},"
        f"sourcePriority: {json.dumps(priority)},"
        f"previous: {json.dumps(previous)}"
        "})));"
    )


def test_resolve_prefers_higher_priority_available_source():
    out = _resolve(
        ["local_api", "zendure_mqtt"],
        ["zendure_mqtt", "local_api", "local_mqtt"],
        None,
    )
    assert out["selectedSource"] == "zendure_mqtt"
    assert out["selectionOrigin"] == "priority"


def test_resolve_single_source_is_automatic():
    out = _resolve(["local_api"], ["zendure_mqtt", "local_api"], None)
    assert out["selectedSource"] == "local_api"
    assert out["selectionOrigin"] == "automatic"


def test_resolve_manual_choice_preserved_when_available():
    out = _resolve(
        ["local_api", "zendure_mqtt"],
        ["zendure_mqtt", "local_api"],
        {"selectedSource": "local_api", "selectionOrigin": "manual"},
    )
    assert out["selectedSource"] == "local_api"
    assert out["selectionOrigin"] == "manual"


def test_resolve_manual_choice_unavailable_does_not_switch():
    out = _resolve(
        ["local_api"],
        ["zendure_mqtt", "local_api"],
        {"selectedSource": "zendure_mqtt", "selectionOrigin": "manual"},
    )
    assert out["selectedSource"] == "zendure_mqtt"
    assert out["available"] is False


def test_resolve_automatic_falls_back_when_source_disappears():
    out = _resolve(
        ["local_api"],
        ["zendure_mqtt", "local_api"],
        {"selectedSource": "zendure_mqtt", "selectionOrigin": "priority"},
    )
    assert out["selectedSource"] == "local_api"


# --- Config hardware list (Phase 6) ---------------------------------------


def _run_named(names, setup):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the hardware-list behavior tests")
    js = _read("admin.js")
    helpers = "\n".join(_extract_fn(js, name) for name in names)
    result = subprocess.run(
        [node, "-e", helpers + "\n" + setup], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_selected_inverter_cards_include_mqtt_and_dedupe_by_serial():
    out = _run_named(
        (
            "normalizeSerial",
            "usableSerialValue",
            "inverterVisibleSerial",
            "inverterIdentityTokens",
            "inverterIdentitySet",
            "inverterIdentityConflict",
            "inverterIdentitiesMatch",
            "selectedInverterCards",
        ),
        """
function inverterItems() { return [{ source_id: "http:A", serial_number: "EOD1AAA" }]; }
function selectedMqttDeviceEntries() {
  return [
    { id: "m:A", serial_number: "EOD1AAA" },
    { id: "m:B", serial_number: "EOD1BBB" },
  ];
}
const cards = selectedInverterCards().map((card) => ({
  kind: card.kind,
  serial: (card.item || card.entry).serial_number,
}));
console.log(JSON.stringify(cards));
""",
    )
    # The HTTP item and the MQTT selection share serial EOD1AAA, so only one card
    # is produced for it; the MQTT-only EOD1BBB adds a second card.
    assert out == [
        {"kind": "http", "serial": "EOD1AAA"},
        {"kind": "mqtt", "serial": "EOD1BBB"},
    ]


def test_mqtt_inverter_card_shows_transport_and_supports_remove_and_switch():
    js = _read("admin.js")
    card = js.split("function renderMqttInverterCard", 1)[1].split("\nfunction ", 1)[0]
    # The card renders the transport label, a remove action distinct from the HTTP
    # remove, and (when a Local-API alternative exists) a transport-switch control.
    assert "transportLabelFor(source)" in card
    assert "config-mqtt-remove" in card
    # Switching passes a stable identity reference (serial or opaque token), never
    # a raw route id, so a route-only inverter can still switch transport.
    assert "renderTransportSwitchButton(switchRef, source)" in card
    # The serial is escaped, never interpolated raw into the card HTML.
    assert "escapeHtml(serial)" in card


def test_http_inverter_body_offers_transport_switch():
    js = _read("admin.js")
    body = js.split("function renderInverterBody", 1)[1].split("\nfunction ", 1)[0]
    assert 'renderTransportSwitchButton(item.serial_number, "local_api")' in body


# --- Add more devices (Phase 7) -------------------------------------------


def test_add_more_devices_includes_all_unselected_transports():
    out = _run_named(
        ("normalizeSerial", "unselectedMqttDeviceProposals"),
        """
const zendureMqttPreviewProposals = new Map([["m:sel", {}]]);
function availableMqttDeviceProposals() {
  return [
    { id: "m:sel", serial_number: "EOD1SEL", connection_source: "zendure_cloud_mqtt" },
    { id: "m:http", serial_number: "EOD1HTTP", connection_source: "zendure_cloud_mqtt" },
    { id: "m:new", serial_number: "EOD1NEW", connection_source: "zendure_cloud_mqtt" },
  ];
}
console.log(JSON.stringify(unselectedMqttDeviceProposals().map((p) => p.id)));
""",
    )
    # Every discovered transport is offered under Add more devices; only a proposal
    # already selected over MQTT (m:sel, its id is in the selection map) is hidden.
    # A serial configured over another transport (m:http) is still shown so the
    # user can add it over MQTT — no per-serial suppression.
    assert out == ["m:http", "m:new"]


def test_mqtt_candidate_card_uses_uniform_add_label_and_escapes():
    js = _read("admin.js")
    card = js.split("function renderMqttCandidateCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "config-mqtt-add" in card
    # The action is uniform with the HTTP candidate card ("Add as inverter"); no
    # transport-specific "Use over X" button.
    assert "Add as inverter" in card
    assert "Use over " not in card
    # Transport stays visible as metadata on the card, serial stays escaped.
    assert "transportLabelFor(source)" in card
    assert "escapeHtml(serial)" in card


def test_transport_switch_button_escapes_serial_and_source():
    js = _read("admin.js")
    fn = js.split("function renderTransportSwitchButton", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "escapeHtml(alternative)" in fn
    assert 'escapeHtml(String(serial || ""))' in fn


# --- Config navigation gating (Phase 9) -----------------------------------


def test_config_continue_gate_follows_backend_preview_only():
    js = _read("admin.js")
    nav = js.split("function renderSetupNav", 1)[1].split("\nfunction ", 1)[0]
    # The Config-step Continue gate no longer disables when MQTT proposals are
    # selected; it follows the backend preview readiness alone (a ready preview
    # already includes the selected MQTT devices and rejects genuine blockers).
    assert "hasMqttPreviewProposals()" not in nav
    assert "latestConfigPreview && latestConfigPreview.ready" in nav


# --- Priority reconciliation + preview invalidation (Phase 8/10) -----------


def test_priority_change_reconciles_config_and_invalidates_preview():
    js = _read("admin.js")
    fn = js.split("async function persistDiscoveryPreparation", 1)[1].split(
        "\nfunction ", 1
    )[0]
    # A priority change re-runs the transport reconciler and regenerates the
    # Config preview through syncConfigFromDiscovery.
    assert "syncConfigFromDiscovery()" in fn


def test_mqtt_selection_changes_invalidate_preview():
    js = _read("admin.js")
    # Selecting/removing/switching MQTT transport all re-render the draft (which
    # nulls latestConfigPreview and regenerates), so Continue re-evaluates.
    for name in ("removeMqttInverter", "switchInverterTransport"):
        fn = js.split("function " + name, 1)[1].split("\nfunction ", 1)[0]
        assert "renderConfigDraft()" in fn
    toggle = js.split("function toggleMqttPreviewProposal", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "renderConfigPreview()" in toggle


# --- Alias-aware Fresh Setup identity enrichment (Archive 78) ---------------

# The pure identity helpers Setup shares with Maintenance; extracted together so
# node has every dependency the alias-aware Setup state needs.
_IDENTITY_HELPERS = (
    "normalizeSerial",
    "usableSerialValue",
    "physicalInverterIdentity",
    "inverterVisibleSerial",
    "inverterIdentityTokens",
    "inverterIdentitySet",
    "inverterHasIdentity",
    "inverterIdentityConflict",
    "inverterIdentitiesMatch",
    "inverterIdentitySetOf",
    "mqttSourceOfConnection",
)


def test_reconcile_route_only_selection_and_serial_enriched_proposal_is_one_group():
    # Defect 1: a stored route-only selection and a later serial-bearing proposal
    # of the same scoped route are one physical device (identity enrichment), not
    # two. The stale selection id is normalized to the current proposal so exactly
    # one selected entry remains (its manual origin preserved).
    state = {
        "httpInverters": [],
        "mqttSelections": [
            {
                "id": "old-route-id",
                "physical_identity_token": "opaque:v1:route",
                "connection_source": "zendure_cloud_mqtt",
                "selection_origin": "manual",
            }
        ],
        "httpCandidateSerials": [],
        "mqttProposals": [
            {
                "id": "new-serial-id",
                "serial_number": "SERIAL-1",
                "physical_identity_token": "opaque:v1:serial",
                "physical_identity_alias_tokens": [
                    "opaque:v1:serial",
                    "opaque:v1:route",
                ],
                "connection_source": "zendure_cloud_mqtt",
            }
        ],
        "priority": ["zendure_mqtt"],
        "enabledSources": {"zendure_mqtt": True},
        "dismissedSerials": [],
    }
    plan = _reconcile(state)
    assert len(plan["physicalDevices"]) == 1
    # The stale route-only selection is replaced by the current enriched proposal.
    assert plan["dropMqttSelectionIds"] == ["old-route-id"]
    assert len(plan["selectMqttProposalIds"]) == 1
    remap = plan["selectMqttProposalIds"][0]
    assert remap["id"] == "new-serial-id"
    assert remap["selection_origin"] == "manual"
    dev = plan["physicalDevices"][0]
    assert dev["selectedSource"] == "zendure_mqtt"
    # The enriched physical serial becomes the group's stable reference.
    assert dev["serial"] == "serial-1"


def test_reconcile_same_route_two_broker_scopes_stays_separate():
    # Defect 1 / scope separation: the same raw route on two broker/account scopes
    # carries distinct opaque tokens, so it stays two physical devices.
    state = {
        "httpInverters": [],
        "mqttSelections": [],
        "httpCandidateSerials": [],
        "mqttProposals": [
            {
                "id": "cloud",
                "physical_identity_token": "opaque:v1:cloud_scope",
                "connection_source": "zendure_cloud_mqtt",
            },
            {
                "id": "local",
                "physical_identity_token": "opaque:v1:local_scope",
                "connection_source": "local_mqtt",
            },
        ],
        "priority": ["zendure_mqtt", "local_mqtt"],
        "enabledSources": {"zendure_mqtt": True, "local_mqtt": True},
        "dismissedSerials": [],
    }
    plan = _reconcile(state)
    assert {d["serial"] for d in plan["physicalDevices"]} == {
        "opaque:v1:cloud_scope",
        "opaque:v1:local_scope",
    }


def test_reconcile_conflicting_serials_on_one_route_never_merge():
    # Two contradictory serials on one route must stay two physical devices even
    # when a bridging route-only observation shares the route.
    state = {
        "httpInverters": [],
        "mqttSelections": [],
        "httpCandidateSerials": [],
        "mqttProposals": [
            {
                "id": "a",
                "serial_number": "SERIAL-1",
                "physical_identity_token": "opaque:v1:serial-1",
                "physical_identity_alias_tokens": [
                    "opaque:v1:serial-1",
                    "opaque:v1:route",
                ],
                "connection_source": "zendure_cloud_mqtt",
            },
            {
                "id": "b",
                "serial_number": "SERIAL-2",
                "physical_identity_token": "opaque:v1:serial-2",
                "physical_identity_alias_tokens": [
                    "opaque:v1:serial-2",
                    "opaque:v1:route",
                ],
                "connection_source": "zendure_cloud_mqtt",
            },
        ],
        "priority": ["zendure_mqtt"],
        "enabledSources": {"zendure_mqtt": True},
        "dismissedSerials": [],
    }
    plan = _reconcile(state)
    assert {d["serial"] for d in plan["physicalDevices"]} == {"serial-1", "serial-2"}


def test_alias_tokens_survive_serialize_storage_reload_and_preview():
    # Defect 2: alias tokens are normalized (valid opaque tokens only, deduped) and
    # carried through serialize -> localStorage -> reload -> preview payload.
    out = _run_named(
        (
            "normalizeInverterAliasTokens",
            "serializeMqttProposalSelection",
            "mqttPreviewPayload",
        ),
        """
let nextName = 1;
function inverterConfigNameForSerial() { return ""; }
function nextInverterName() { return "INV_" + nextName++; }
const proposal = {
  id: "zendure-mqtt:opaque:v1:route:cloud",
  serial_number: "SERIAL-1",
  physical_identity_token: "opaque:v1:serial",
  physical_identity_alias_tokens: [
    "opaque:v1:serial", "opaque:v1:route", "raw-route-id", "opaque:v1:serial",
  ],
  connection_source: "zendure_cloud_mqtt",
  broker_ref: "cloud",
  target: "device",
  config_fragment: { type: "zendure_mqtt" },
};
const stored = serializeMqttProposalSelection(proposal, { target: "device" });
const reloaded = JSON.parse(JSON.stringify([stored]));
const zendureMqttPreviewProposals = new Map(
  reloaded.map((entry) => [String(entry.id), entry])
);
const payload = mqttPreviewPayload();
console.log(JSON.stringify({
  stored: stored.physical_identity_alias_tokens,
  payload: payload[0].physical_identity_alias_tokens,
}));
""",
    )
    # Invalid raw ids are dropped, duplicates removed, valid tokens preserved.
    assert out["stored"] == ["opaque:v1:serial", "opaque:v1:route"]
    assert out["payload"] == ["opaque:v1:serial", "opaque:v1:route"]


def test_setup_name_survives_serial_enrichment():
    # Defect 5: a route-only inverter name (stored under its route token) is still
    # found once the serial and its token are added.
    out = _run_named(
        _IDENTITY_HELPERS
        + ("rememberInverterName", "rememberedInverterName", "inverterConfigNameForSerial"),
        """
const transportInverterNames = new Map();
function inverterItems() { return []; }
function selectedMqttDeviceEntries() { return []; }
const routeOnly = { physical_identity_token: "opaque:v1:route" };
rememberInverterName(routeOnly, "My Inverter");
const enriched = {
  serial_number: "SERIAL-1",
  physical_identity_token: "opaque:v1:serial",
  physical_identity_alias_tokens: ["opaque:v1:serial", "opaque:v1:route"],
};
console.log(JSON.stringify({
  byRouteOnly: rememberedInverterName(routeOnly),
  byEnriched: rememberedInverterName(enriched),
  byConfigName: inverterConfigNameForSerial(enriched),
}));
""",
    )
    assert out["byRouteOnly"] == "My Inverter"
    assert out["byEnriched"] == "My Inverter"
    assert out["byConfigName"] == "My Inverter"


def test_setup_dismissal_survives_serial_enrichment():
    # Defect 5: dismissing a route-only device keeps it dismissed after a serial
    # appears, and never dismisses an unrelated device.
    out = _run_named(
        _IDENTITY_HELPERS
        + (
            "dismissalStorageKey",
            "dismissalKeysForInverter",
            "dismissSerial",
            "inverterDismissed",
        ),
        """
const dismissedSerials = new Set();
function saveDismissedSerials() {}
const routeOnly = { physical_identity_token: "opaque:v1:route" };
dismissSerial(routeOnly);
const enriched = {
  serial_number: "SERIAL-1",
  physical_identity_token: "opaque:v1:serial",
  physical_identity_alias_tokens: ["opaque:v1:serial", "opaque:v1:route"],
};
console.log(JSON.stringify({
  routeOnly: inverterDismissed(routeOnly),
  enriched: inverterDismissed(enriched),
  unrelated: inverterDismissed({ serial_number: "OTHER" }),
}));
""",
    )
    assert out["routeOnly"] is True
    assert out["enriched"] is True
    assert out["unrelated"] is False


def test_dual_transport_dismissal_cleared_by_local_api_readd():
    # Removing a dual-transport inverter over MQTT dismisses it by serial (not by
    # its MQTT tokens), so re-adding it over Local API — whose scan device carries
    # only the serial — clears the dismissal and the reconciler keeps the re-add
    # even while the enriched MQTT proposal is still present.
    out = _run_named(
        _IDENTITY_HELPERS
        + (
            "dismissalStorageKey",
            "dismissalKeysForInverter",
            "dismissSerial",
            "undismissSerial",
            "inverterDismissed",
            "resolveSelectedDeviceSource",
            "reconcileTransportSelection",
        ),
        """
const dismissedSerials = new Set();
function saveDismissedSerials() {}
const mqttEntry = {
  serial_number: "SERIAL-1",
  physical_identity_token: "opaque:v1:serial",
  physical_identity_alias_tokens: ["opaque:v1:serial", "opaque:v1:route"],
};
dismissSerial(mqttEntry);
undismissSerial({ serial_number: "SERIAL-1" });
const plan = reconcileTransportSelection({
  priority: ["local_api", "zendure_mqtt", "local_mqtt"],
  enabledSources: { local_api: true, zendure_mqtt: true, local_mqtt: false },
  dismissedSerials: [...dismissedSerials],
  httpInverters: [{ source_id: "http:1", serial_number: "SERIAL-1", auto_added: false }],
  httpCandidateSerials: ["SERIAL-1"],
  mqttSelections: [],
  mqttProposals: [{
    id: "m", serial_number: "SERIAL-1",
    physical_identity_token: "opaque:v1:serial",
    physical_identity_alias_tokens: ["opaque:v1:serial", "opaque:v1:route"],
    connection_source: "zendure_cloud_mqtt",
  }],
});
console.log(JSON.stringify({
  remaining: [...dismissedSerials],
  dropHttp: plan.dropHttpSourceIds,
}));
""",
    )
    assert out["remaining"] == []
    assert out["dropHttp"] == []


def test_selected_cards_render_one_inverter_after_enrichment():
    # Defect 5: a route-only entry and its serial-enriched entry are one physical
    # inverter, so exactly one card is rendered.
    out = _run_named(
        _IDENTITY_HELPERS + ("selectedInverterCards",),
        """
function inverterItems() { return []; }
function selectedMqttDeviceEntries() {
  return [
    { id: "route", physical_identity_token: "opaque:v1:route", connection_source: "zendure_cloud_mqtt" },
    {
      id: "serial",
      serial_number: "SERIAL-1",
      physical_identity_token: "opaque:v1:serial",
      physical_identity_alias_tokens: ["opaque:v1:serial", "opaque:v1:route"],
      connection_source: "zendure_cloud_mqtt",
    },
  ];
}
const cards = selectedInverterCards().map((card) => ({
  kind: card.kind,
  id: (card.item || card.entry).id,
}));
console.log(JSON.stringify(cards));
""",
    )
    assert len(out) == 1


def test_transport_switch_discovery_accepts_route_only_identity_token():
    # Defect 5 / defect 10: alternative-transport discovery accepts an opaque
    # identity token (not a physical serial), so a route-only inverter can switch
    # transport. Both proposals share the route token but differ by source.
    out = _run_named(
        _IDENTITY_HELPERS + ("alternativeTransportsForSerial",),
        """
function availableConfigDevices() { return []; }
function isAutoConfigReady() { return true; }
function availableMqttDeviceProposals() {
  return [
    { id: "cloud", physical_identity_token: "opaque:v1:route", connection_source: "zendure_cloud_mqtt" },
    { id: "local", physical_identity_token: "opaque:v1:route", connection_source: "local_mqtt" },
  ];
}
console.log(JSON.stringify(
  alternativeTransportsForSerial("opaque:v1:route", "zendure_mqtt")
));
""",
    )
    assert out == ["local_mqtt"]


# --- Transitive connected-component grouping (Archive 79 / defect 1) ---------


def test_three_node_bridge_is_one_connected_component():
    # Defect 1 reproduction: a Local-API serial group, a stored route-only Cloud
    # selection, and a serial+route bridge proposal must become ONE physical
    # device. The bridge connects the serial group and the route-only group, so a
    # non-transitive "first match wins" grouping would leave two groups.
    state = {
        "httpInverters": [
            {"source_id": "http:S1", "serial_number": "SERIAL-1", "auto_added": True},
        ],
        "mqttSelections": [
            {
                "id": "old-route-selection",
                "physical_identity_token": "opaque:v1:route",
                "connection_source": "zendure_cloud_mqtt",
                "selection_origin": "manual",
            }
        ],
        "httpCandidateSerials": ["SERIAL-1"],
        "mqttProposals": [
            {
                "id": "current-enriched-proposal",
                "serial_number": "SERIAL-1",
                "physical_identity_token": "opaque:v1:serial",
                "physical_identity_alias_tokens": [
                    "opaque:v1:serial",
                    "opaque:v1:route",
                ],
                "connection_source": "zendure_cloud_mqtt",
            }
        ],
        "priority": ["zendure_mqtt", "local_api", "local_mqtt"],
        "enabledSources": {"local_api": True, "zendure_mqtt": True, "local_mqtt": False},
        "dismissedSerials": [],
    }
    plan = _reconcile(state)
    assert len(plan["physicalDevices"]) == 1
    # One transport: the Local-API twin is dropped, the stale route-only selection
    # is replaced by the current enriched proposal (exactly one selected entry).
    assert "http:S1" in plan["dropHttpSourceIds"]
    assert plan["dropMqttSelectionIds"] == ["old-route-selection"]
    assert [s["id"] for s in plan["selectMqttProposalIds"]] == ["current-enriched-proposal"]
    dev = plan["physicalDevices"][0]
    assert dev["serial"] == "serial-1"
    assert dev["selectedSource"] == "zendure_mqtt"


def _bridge_nodes():
    serial_only = {
        "id": "serial-only",
        "serial_number": "SERIAL-1",
        "physical_identity_token": "opaque:v1:serial",
        "connection_source": "zendure_cloud_mqtt",
    }
    route_only = {
        "id": "route-only",
        "physical_identity_token": "opaque:v1:route",
        "connection_source": "zendure_cloud_mqtt",
    }
    bridge = {
        "id": "bridge",
        "serial_number": "SERIAL-1",
        "physical_identity_token": "opaque:v1:serial",
        "physical_identity_alias_tokens": ["opaque:v1:serial", "opaque:v1:route"],
        "connection_source": "zendure_cloud_mqtt",
    }
    return serial_only, route_only, bridge


@pytest.mark.parametrize(
    "order",
    [
        ("serial_only", "route_only", "bridge"),
        ("route_only", "serial_only", "bridge"),
        ("bridge", "serial_only", "route_only"),
    ],
)
def test_bridge_union_is_order_independent(order):
    # The connected-component union is order independent: the serial group and the
    # route-only group collapse into one physical device however the bridging
    # observation is ordered relative to them.
    nodes = dict(zip(("serial_only", "route_only", "bridge"), _bridge_nodes()))
    plan = _reconcile(
        {
            "httpInverters": [],
            "mqttSelections": [],
            "httpCandidateSerials": [],
            "mqttProposals": [nodes[name] for name in order],
            "priority": ["zendure_mqtt"],
            "enabledSources": {"zendure_mqtt": True},
            "dismissedSerials": [],
        }
    )
    assert len(plan["physicalDevices"]) == 1


def test_transitive_chain_of_four_is_one_group():
    # A four-observation chain a-b-c-d-e (each shares one token with the next)
    # collapses to a single physical device.
    def prop(pid, tokens):
        return {
            "id": pid,
            "physical_identity_token": tokens[0],
            "physical_identity_alias_tokens": tokens,
            "connection_source": "zendure_cloud_mqtt",
        }

    plan = _reconcile(
        {
            "httpInverters": [],
            "mqttSelections": [],
            "httpCandidateSerials": [],
            "mqttProposals": [
                prop("p1", ["opaque:v1:a", "opaque:v1:b"]),
                prop("p2", ["opaque:v1:b", "opaque:v1:c"]),
                prop("p3", ["opaque:v1:c", "opaque:v1:d"]),
                prop("p4", ["opaque:v1:d", "opaque:v1:e"]),
            ],
            "priority": ["zendure_mqtt"],
            "enabledSources": {"zendure_mqtt": True},
            "dismissedSerials": [],
        }
    )
    assert len(plan["physicalDevices"]) == 1


def test_transitive_chain_with_serial_conflict_never_merges_the_serials():
    # The same chain but with two conflicting serials joined by a route-only
    # bridge: the bridge must not unite the two serials into one component.
    plan = _reconcile(
        {
            "httpInverters": [],
            "mqttSelections": [],
            "httpCandidateSerials": [],
            "mqttProposals": [
                {
                    "id": "p1",
                    "serial_number": "SERIAL-1",
                    "physical_identity_token": "opaque:v1:serial-1",
                    "physical_identity_alias_tokens": ["opaque:v1:serial-1", "opaque:v1:b"],
                    "connection_source": "zendure_cloud_mqtt",
                },
                {
                    "id": "bridge",
                    "physical_identity_token": "opaque:v1:b",
                    "physical_identity_alias_tokens": ["opaque:v1:b", "opaque:v1:c"],
                    "connection_source": "zendure_cloud_mqtt",
                },
                {
                    "id": "p3",
                    "serial_number": "SERIAL-2",
                    "physical_identity_token": "opaque:v1:serial-2",
                    "physical_identity_alias_tokens": ["opaque:v1:serial-2", "opaque:v1:c"],
                    "connection_source": "zendure_cloud_mqtt",
                },
            ],
            "priority": ["zendure_mqtt"],
            "enabledSources": {"zendure_mqtt": True},
            "dismissedSerials": [],
        }
    )
    serials = {d["serial"] for d in plan["physicalDevices"]}
    assert "serial-1" in serials
    assert "serial-2" in serials
    # No device carries both serials: the conflicting serials are never one group.
    assert len([d for d in plan["physicalDevices"] if d["serial"] in {"serial-1", "serial-2"}]) == 2


def test_legacy_id_remap_leaves_exactly_one_selected_proposal():
    # A stored selection carrying a legacy id and the current proposal (unique alias
    # match) leave exactly one selected proposal after normalization.
    state = {
        "httpInverters": [
            {"source_id": "http:S1", "serial_number": "SERIAL-1", "auto_added": True},
        ],
        "mqttSelections": [
            {
                "id": "legacy-route-id",
                "physical_identity_token": "opaque:v1:route",
                "connection_source": "zendure_cloud_mqtt",
                "selection_origin": "priority",
            }
        ],
        "httpCandidateSerials": ["SERIAL-1"],
        "mqttProposals": [
            {
                "id": "zendure-mqtt:opaque:v1:anchor:zendure_cloud",
                "serial_number": "SERIAL-1",
                "physical_identity_token": "opaque:v1:serial",
                "physical_identity_alias_tokens": ["opaque:v1:serial", "opaque:v1:route"],
                "connection_source": "zendure_cloud_mqtt",
            }
        ],
        "priority": ["zendure_mqtt", "local_api", "local_mqtt"],
        "enabledSources": {"local_api": True, "zendure_mqtt": True, "local_mqtt": False},
        "dismissedSerials": [],
    }
    plan = _reconcile(state)
    assert plan["dropMqttSelectionIds"] == ["legacy-route-id"]
    assert [s["id"] for s in plan["selectMqttProposalIds"]] == [
        "zendure-mqtt:opaque:v1:anchor:zendure_cloud"
    ]
    # Exactly one physical device, selected over MQTT.
    assert len(plan["physicalDevices"]) == 1


def test_setup_name_survives_product_key_anchor_enrichment():
    # Defect (name migration): a route-only inverter's name stored under its stable
    # device-anchor token is still found once a product key is enriched — the
    # anchor token is unchanged, only a precise-route alias is added.
    out = _run_named(
        _IDENTITY_HELPERS
        + ("rememberInverterName", "rememberedInverterName", "inverterConfigNameForSerial"),
        """
const transportInverterNames = new Map();
function inverterItems() { return []; }
function selectedMqttDeviceEntries() { return []; }
const routeOnly = { physical_identity_token: "opaque:v1:anchor" };
rememberInverterName(routeOnly, "Garage");
const enriched = {
  physical_identity_token: "opaque:v1:anchor",
  physical_identity_alias_tokens: ["opaque:v1:anchor", "opaque:v1:precise-route"],
};
console.log(JSON.stringify({
  byRouteOnly: rememberedInverterName(routeOnly),
  byEnriched: rememberedInverterName(enriched),
}));
""",
    )
    assert out["byRouteOnly"] == "Garage"
    assert out["byEnriched"] == "Garage"


def test_setup_dismissal_survives_product_key_anchor_enrichment():
    # Defect (dismissal migration): dismissing a route-only device keeps it
    # dismissed after a product key is enriched (same stable anchor token), and
    # never dismisses an unrelated device.
    out = _run_named(
        _IDENTITY_HELPERS
        + (
            "dismissalStorageKey",
            "dismissalKeysForInverter",
            "dismissSerial",
            "inverterDismissed",
        ),
        """
const dismissedSerials = new Set();
function saveDismissedSerials() {}
const routeOnly = { physical_identity_token: "opaque:v1:anchor" };
dismissSerial(routeOnly);
const enriched = {
  physical_identity_token: "opaque:v1:anchor",
  physical_identity_alias_tokens: ["opaque:v1:anchor", "opaque:v1:precise-route"],
};
console.log(JSON.stringify({
  routeOnly: inverterDismissed(routeOnly),
  enriched: inverterDismissed(enriched),
  unrelated: inverterDismissed({ physical_identity_token: "opaque:v1:other" }),
}));
""",
    )
    assert out["routeOnly"] is True
    assert out["enriched"] is True
    assert out["unrelated"] is False
