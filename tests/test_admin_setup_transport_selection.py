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
    # A removed device dismisses its serial; re-adding it manually must clear the
    # dismissal or the reconciler would drop the re-add on the next pass.
    assert "undismissSerial(device.serial_number)" in add


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
        ("normalizeSerial", "selectedInverterCards"),
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
    assert "renderTransportSwitchButton(serial, source)" in card
    # The serial is escaped, never interpolated raw into the card HTML.
    assert "escapeHtml(serial)" in card


def test_http_inverter_body_offers_transport_switch():
    js = _read("admin.js")
    body = js.split("function renderInverterBody", 1)[1].split("\nfunction ", 1)[0]
    assert 'renderTransportSwitchButton(item.serial_number, "local_api")' in body


# --- Add more devices (Phase 7) -------------------------------------------


def test_add_more_devices_includes_only_unconfigured_mqtt_proposals():
    out = _run_named(
        ("normalizeSerial", "unselectedMqttDeviceProposals"),
        """
function inverterItems() { return [{ serial_number: "EOD1HTTP" }]; }
function selectedMqttDeviceEntries() { return [{ serial_number: "EOD1SEL" }]; }
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
    # Already-selected (m:sel) and already-configured-over-HTTP (m:http, offered as
    # a transport alternative on the selected card) are excluded; only the
    # genuinely new MQTT device (m:new) is offered under Add more devices.
    assert out == ["m:new"]


def test_mqtt_candidate_card_offers_add_over_transport_and_escapes():
    js = _read("admin.js")
    card = js.split("function renderMqttCandidateCard", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "config-mqtt-add" in card
    assert "Use over " in card
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
