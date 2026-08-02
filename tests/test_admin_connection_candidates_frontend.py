# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connection labels and direct connection switching in Setup and Maintenance.

Every discovered inverter connection resolves to exactly one candidate state
(new / active / alternative / identity_conflict) and renders one contextual
action. An alternative connection for an already configured physical inverter
switches that logical inverter in place instead of adding a duplicate, keeps the
config name and every common EMS value, and stays offered so the user can switch
back. The user-facing connection labels are API / MQTT / Zendure MQTT in both
flows.

The real admin.js helpers are extracted (brace-matched) and executed in node, so
the contract is tested against the shipped code and not a rebuilt copy.
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
    """Brace-match one function out of admin.js.

    The parameter list is skipped by parentheses first, so a destructured
    options argument does not close the body brace early.
    """

    marker = "function " + name + "("
    assert marker in js, f"{name} is missing from admin.js"
    start = js.index(marker)
    depth = 0
    cursor = js.index("(", start)
    for position in range(cursor, len(js)):
        if js[position] == "(":
            depth += 1
        elif js[position] == ")":
            depth -= 1
            if depth == 0:
                cursor = position
                break
    depth = 0
    for position in range(js.index("{", cursor), len(js)):
        if js[position] == "{":
            depth += 1
        elif js[position] == "}":
            depth -= 1
            if depth == 0:
                return js[start : position + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}")


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the connection candidate tests")
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_IDENTITY_HELPERS = (
    "normalizeSerial",
    "usableSerialValue",
    "issuedPhysicalIdentity",
    "physicalInverterIdentity",
    "inverterVisibleSerial",
    "inverterIdentityTokens",
    "inverterIdentitySet",
    "inverterHasIdentity",
    "inverterIdentityConflict",
    "inverterIdentitiesMatch",
    "inverterIdentitySetOf",
    "normalizeInverterAliasTokens",
    "mqttSourceOfConnection",
)

_STATE_HELPERS = _IDENTITY_HELPERS + (
    "connectionLabelFor",
    "connectionBrokerScope",
    "sameMqttConnectionScope",
    "inverterItems",
    "selectedMqttDeviceEntries",
    "configuredInverterConnection",
    "sameConcreteConnection",
    "inverterCandidateConnectionState",
)


def _state_env(draft_items, mqtt_entries):
    return (
        "let configDraftItems = " + json.dumps(draft_items) + ";\n"
        "const zendureMqttPreviewProposals = new Map("
        + json.dumps([[str(e.get("id", "")), e] for e in mqtt_entries])
        + ");\n"
    )


def _candidate_ref(candidate, source):
    """The reference the production renderers pass: proposal id, or observationKey.

    For a discovered Local-API connection that is the server-issued observation
    id — never a serial-derived key, which two redacted devices would share.
    """

    if source != "local_api":
        return str(candidate.get("id") or "")
    return str(candidate.get("observation_id") or candidate.get("id") or "")


def _resolve(draft_items, mqtt_entries, candidate, source):
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _STATE_HELPERS)
    script = (
        helpers
        + "\n"
        + _state_env(draft_items, mqtt_entries)
        + "console.log(JSON.stringify(inverterCandidateConnectionState("
        + json.dumps(candidate)
        + ", "
        + json.dumps(source)
        + ", "
        + json.dumps(_candidate_ref(candidate, source))
        + ")));"
    )
    return _node(script)


# --- Phase 1: one connection label helper --------------------------------


def test_connection_label_helper_uses_the_short_user_facing_labels():
    js = _read()
    helper = _extract_fn(js, "connectionLabelFor")
    out = _node(
        helper + "\nconsole.log(JSON.stringify(["
        "connectionLabelFor('local_api'),"
        "connectionLabelFor('local_mqtt'),"
        "connectionLabelFor('zendure_mqtt'),"
        "connectionLabelFor('something_else'),"
        "connectionLabelFor(''),"
        "connectionLabelFor(undefined)]));"
    )
    assert out[:3] == ["API", "MQTT", "Zendure MQTT"]
    # Unknown sources fall back safely instead of claiming a concrete transport.
    assert out[3:] == ["Unknown", "Unknown", "Unknown"]


def test_no_second_connection_label_map_remains():
    js = _read()
    # The long labels must be gone from the card/candidate label helpers; only
    # the discovery-source preparation panel keeps its own descriptive names.
    assert "function transportLabelFor" not in js
    label_helper = _extract_fn(js, "mqttTransportLabel")
    assert "connectionLabelFor" in label_helper
    assert "Zendure Cloud MQTT" not in label_helper
    assert "Local MQTT" not in label_helper


def test_setup_and_maintenance_cards_render_the_short_labels():
    js = _read()
    for name in (
        "renderMqttCandidateCard",
        "renderMqttInverterCard",
        "renderInverterBody",
        "renderMaintenanceMqttProposalCard",
        "mconfigDiscoveryActionState",
    ):
        card = _extract_fn(js, name)
        assert "Zendure Cloud MQTT" not in card, name
        assert '"Local MQTT"' not in card, name
        assert '"Local API"' not in card, name


# --- Phase 2: candidate-state resolution ---------------------------------


_API_ITEM = {
    "source_id": "obs:v1:api-phys-1",
    "role": "inverter",
    "config_name": "INV_1",
    "serial_number": "PHYS-1",
    "enabled": True,
    "config_values": {"max_power": 700, "min_soc": 18},
}

_MQTT_ENTRY = {
    "id": "zendure-cloud:PHYS-1",
    "target": "device",
    "config_name": "INV_1",
    "serial_number": "PHYS-1",
    "connection_source": "zendure_cloud_mqtt",
    "broker_ref": "zendure_cloud",
}

_CLOUD_CANDIDATE = {
    "id": "zendure-cloud:PHYS-1",
    "serial_number": "PHYS-1",
    "connection_source": "zendure_cloud_mqtt",
    "broker_ref": "zendure_cloud",
}

_API_CANDIDATE = {
    "id": "zendure:PHYS-1",
    "observation_id": "obs:v1:api-phys-1",
    "serial_number": "PHYS-1",
    "api_family": "zendure",
    "role_suggestion": "inverter",
    "ip": "192.168.1.100",
}


def test_candidate_state_new_when_no_configured_inverter_matches():
    out = _resolve([], [], _CLOUD_CANDIDATE, "zendure_mqtt")
    assert out["state"] == "new"
    assert out["configuredItem"] is None
    assert out["currentSource"] is None
    assert out["candidateSource"] == "zendure_mqtt"


def test_candidate_state_active_for_the_same_concrete_connection():
    out = _resolve([_API_ITEM], [], _API_CANDIDATE, "local_api")
    assert out["state"] == "active"
    assert out["currentSource"] == "local_api"
    assert out["configuredName"] == "INV_1"


def test_candidate_state_alternative_for_another_connection():
    out = _resolve([_API_ITEM], [], _CLOUD_CANDIDATE, "zendure_mqtt")
    assert out["state"] == "alternative"
    assert out["currentSource"] == "local_api"
    assert out["configuredName"] == "INV_1"
    assert out["candidateSource"] == "zendure_mqtt"
    assert out["identityRef"] == "phys-1"


def test_candidate_state_alternative_from_mqtt_back_to_api():
    out = _resolve([], [_MQTT_ENTRY], _API_CANDIDATE, "local_api")
    assert out["state"] == "alternative"
    assert out["currentSource"] == "zendure_mqtt"
    assert out["configuredName"] == "INV_1"


def test_candidate_state_identity_conflict_is_fail_closed():
    configured = dict(_API_ITEM, serial_number="PHYS-1")
    configured["physical_identity_token"] = "opaque:v1:route"
    candidate = dict(
        _CLOUD_CANDIDATE,
        serial_number="PHYS-2",
        physical_identity_token="opaque:v1:route",
    )
    out = _resolve([configured], [], candidate, "zendure_mqtt")
    assert out["state"] == "identity_conflict"
    assert out["configuredItem"] is None


def test_candidate_state_route_only_candidate_matches_by_opaque_token():
    configured = dict(_API_ITEM)
    configured["physical_identity_alias_tokens"] = ["opaque:v1:routeA"]
    candidate = {
        "id": "zendure-cloud:route",
        "physical_identity_token": "opaque:v1:routeA",
        "connection_source": "zendure_cloud_mqtt",
    }
    out = _resolve([configured], [], candidate, "zendure_mqtt")
    assert out["state"] == "alternative"
    assert out["identityRef"] == "opaque:v1:routeA"


def test_candidate_state_other_broker_scope_is_an_alternative_not_active():
    entry = dict(_MQTT_ENTRY, connection_source="local_mqtt", broker_ref="local_b1")
    candidate = dict(
        _CLOUD_CANDIDATE, connection_source="local_mqtt", broker_ref="local_b2"
    )
    out = _resolve([], [entry], candidate, "local_mqtt")
    assert out["state"] == "alternative"


# --- Phase 3 / 8: Setup candidate cards ----------------------------------

_CARD_HELPERS = _STATE_HELPERS + (
    "escapeHtml",
    "fact",
    "observationKey",
    "hardwareCardKindForRole",
    "hardwareCardClass",
    "sourcesOf",
    "sourceBadges",
    "draftHasSource",
    "connectionCandidateToken",
    "renderConnectionCandidateAction",
    "connectionCandidateNote",
    "renderConnectionPill",
    "renderMqttCandidateCard",
    "renderConfigAvailableCard",
)


def _render(fn_call, draft_items, mqtt_entries, payload):
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _CARD_HELPERS)
    stub = (
        "const DEFAULT_INVERTER_DISPLAY = 'SolarFlow 800 Pro 2';\n"
        "const openHardwareCards = new Set();\n"
        "const SOURCE_LABELS = {};\n"
        "let connectionCandidateTokens = new Map();\n"
        "function normalizeDiscoverySource(value) { return String(value || ''); }\n"
    )
    script = (
        helpers
        + "\n"
        + stub
        + _state_env(draft_items, mqtt_entries)
        + "console.log(JSON.stringify("
        + fn_call
        + "("
        + json.dumps(payload)
        + ")));"
    )
    return _node(script)


def test_new_mqtt_candidate_offers_add_inverter():
    card = _render("renderMqttCandidateCard", [], [], _CLOUD_CANDIDATE)
    assert "Add inverter" in card
    assert "Use connection" not in card
    assert "Add as inverter" not in card
    assert "Zendure MQTT" in card


def test_alternative_mqtt_candidate_offers_use_connection():
    card = _render("renderMqttCandidateCard", [_API_ITEM], [], _CLOUD_CANDIDATE)
    assert "Use connection" in card
    assert "Add inverter" not in card
    # The relationship note names the configured inverter and its connection.
    assert "Already configured as INV_1 via API" in card
    # The switch carries a trusted identity reference, never a raw route id.
    assert 'data-action="use-connection"' in card
    assert 'data-identity-ref="phys-1"' in card
    assert 'data-connection-source="zendure_mqtt"' in card


def test_alternative_api_candidate_offers_use_connection():
    card = _render("renderConfigAvailableCard", [], [_MQTT_ENTRY], _API_CANDIDATE)
    assert "Use connection" in card
    assert "Already configured as INV_1 via Zendure MQTT" in card
    assert 'data-connection-source="local_api"' in card


def test_active_api_candidate_renders_active_and_no_action():
    card = _render("renderConfigAvailableCard", [_API_ITEM], [], _API_CANDIDATE)
    assert "Active" in card
    assert "Use connection" not in card
    assert "Add inverter" not in card
    assert "disabled" in card


def test_conflicting_candidate_is_disabled():
    configured = dict(_API_ITEM)
    configured["physical_identity_token"] = "opaque:v1:route"
    candidate = dict(
        _CLOUD_CANDIDATE,
        serial_number="PHYS-2",
        physical_identity_token="opaque:v1:route",
    )
    card = _render("renderMqttCandidateCard", [configured], [], candidate)
    assert "Identity conflict" in card
    assert "disabled" in card
    assert "Use connection" not in card
    assert "Add inverter" not in card


def test_candidate_actions_are_real_buttons_with_readable_text():
    js = _read()
    action = _extract_fn(js, "renderConnectionCandidateAction")
    assert '<button type="button"' in action
    # Labels are text, never encoded through a class or colour alone.
    for label in ("Use connection", "Active", "Identity conflict"):
        assert label in action


# --- Phase 7: the candidate pool keeps alternative connections -----------


def test_candidate_pool_keeps_one_entry_per_concrete_connection():
    js = _read()
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in _IDENTITY_HELPERS
        + (
            "connectionBrokerScope",
            "sameMqttConnectionScope",
            "concreteMqttConnectionKey",
            "selectedMqttDeviceEntries",
            "unselectedMqttDeviceProposals",
        )
    )
    proposals = [
        dict(_CLOUD_CANDIDATE, config_fragment={}),
        # Same physical inverter over a different broker scope: a distinct
        # connection candidate, not a duplicate observation.
        {
            "id": "local-mqtt:PHYS-1",
            "serial_number": "PHYS-1",
            "connection_source": "local_mqtt",
            "broker_ref": "local_b1",
            "config_fragment": {},
        },
        # A duplicate observation of the very same connection collapses.
        dict(_CLOUD_CANDIDATE, id="zendure-cloud:PHYS-1-dup", config_fragment={}),
    ]
    stub = (
        "const zendureMqttPreviewProposals = new Map();\n"
        "const latestMqttProposals = " + json.dumps(proposals) + ";\n"
        "function isMqttGridMeterProposal() { return false; }\n"
        "function availableMqttDeviceProposals() {\n"
        "  return latestMqttProposals.filter((p) => !isMqttGridMeterProposal(p) && p.config_fragment);\n"
        "}\n"
        "console.log(JSON.stringify("
        "unselectedMqttDeviceProposals().map((p) => p.id)));"
    )
    ids = _node(helpers + "\n" + stub)
    assert ids == ["zendure-cloud:PHYS-1", "local-mqtt:PHYS-1"]


# --- Phase 4 / 5: one logical inverter, common values preserved ----------

_SWITCH_HELPERS = _IDENTITY_HELPERS + (
    "dismissalStorageKey",
    "dismissalKeysForInverter",
    "dismissSerial",
    "undismissSerial",
    "inverterDismissed",
    "nextCompactInverterName",
    "inverterItems",
    "selectedMqttDeviceEntries",
    "rememberedInverterName",
    "rememberInverterName",
    "forgetInverterName",
    "inverterConfigNameForSerial",
    "draftHasSource",
    "observationKey",
    "sourcesOf",
    "uniqueDisplayName",
    "draftItemFromDevice",
    "preservedInverterValues",
    "configuredInverterConnection",
    "serializeMqttProposalSelection",
    "mconfigIsMqttDevice",
    "mconfigDeviceIsActive",
    "mconfigDeviceInactiveByChoice",
    "inverterActivationView",
    "switchInverterTransport",
)

_SWITCH_STUB = """
const DEVICE_MAPPED_FIELD_KEYS = { name: 'config_name', ip: 'ip', sn: 'serial_number' };
const DEFAULT_INVERTER_DISPLAY = 'SolarFlow 800 Pro 2';
const DEFAULT_GRID_METER_DISPLAY = 'Grid meter';
const transportInverterNames = new Map();
const dismissedSerials = new Set();
const configDismissed = new Set();
function saveDismissedSerials() {}
function saveConfigDismissed() {}
function saveConfigDraft() {}
function saveMqttPreviewProposals() {}
function renderMqttProposals() {}
function renderConfigDraft() {}
function renderConfigAvailable() {}
function syncGridMeterFeatureValues() {}
function nextInverterName(excludeEntry) {
  const names = [
    ...inverterItems().filter((i) => i !== excludeEntry).map((i) => i.config_name),
    ...selectedMqttDeviceEntries().filter((e) => e !== excludeEntry).map((e) => e.config_name),
  ].filter(Boolean);
  return nextCompactInverterName(names, names.length);
}
"""


def _switch(draft_items, mqtt_entries, devices, proposals, call):
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _SWITCH_HELPERS)
    env = (
        _state_env(draft_items, mqtt_entries)
        + "const _devices = " + json.dumps(devices) + ";\n"
        "const _proposals = " + json.dumps(proposals) + ";\n"
        "function availableConfigDevices() { return _devices; }\n"
        "function availableMqttDeviceProposals() { return _proposals; }\n"
        "const latestMqttProposals = _proposals;\n"
    )
    script = (
        helpers
        + "\n"
        + _SWITCH_STUB
        + env
        + call
        + "\nconsole.log(JSON.stringify({\n"
        "  draft: configDraftItems,\n"
        "  mqtt: Array.from(zendureMqttPreviewProposals.values()),\n"
        "}));"
    )
    return _node(script)


_COMMON_VALUES = {
    "max_power": 777,
    "min_soc": 18,
    "max_soc": 96,
    "pv_kwp": 3.2,
    "pv_priority_factor": 1.25,
    "battery_kwh": 7.7,
    "smart_mode": 1,
}

_SWITCH_API_ITEM = {
    "source_id": "obs:v1:api-phys-1",
    "role": "inverter",
    "config_name": "INV_1",
    "display_name": "SolarFlow 800 Pro 2",
    "serial_number": "PHYS-1",
    "ip": "192.168.1.100",
    "port": 80,
    "enabled": False,
    "config_values": dict(_COMMON_VALUES),
}

_SWITCH_PROPOSAL = {
    "id": "zendure-cloud:PHYS-1",
    "serial_number": "PHYS-1",
    "display_name": "SolarFlow 800 Pro 2",
    "connection_source": "zendure_cloud_mqtt",
    "broker_ref": "zendure_cloud",
    "config_fragment": {"type": "zendure_mqtt", "mqtt": {"device_id": "ROUTE-1"}},
}

_SWITCH_DEVICE = {
    "id": "zendure:PHYS-1",
    "observation_id": "obs:v1:api-phys-1",
    "ip": "192.168.1.100",
    "port": 80,
    "serial_number": "PHYS-1",
    "role_suggestion": "inverter",
    "display_name": "SolarFlow 800 Pro 2",
    "device_type": "zendure",
    "api_family": "zendure",
}


def test_switch_api_to_mqtt_keeps_one_inverter_with_all_common_values():
    out = _switch(
        [_SWITCH_API_ITEM],
        [],
        [_SWITCH_DEVICE],
        [_SWITCH_PROPOSAL],
        "switchInverterTransport('PHYS-1', 'zendure_mqtt');",
    )
    assert out["draft"] == []
    assert len(out["mqtt"]) == 1
    entry = out["mqtt"][0]
    assert entry["config_name"] == "INV_1"
    assert entry["config_values"] == _COMMON_VALUES
    assert entry["enabled"] is False
    # Stale Local API connection fields never travel to an MQTT entry.
    assert "ip" not in entry
    assert "port" not in entry


def test_switch_mqtt_to_api_keeps_one_inverter_with_all_common_values():
    entry = {
        "id": "zendure-cloud:PHYS-1",
        "target": "device",
        "config_name": "INV_9",
        "serial_number": "PHYS-1",
        "connection_source": "zendure_cloud_mqtt",
        "broker_ref": "zendure_cloud",
        "enabled": False,
        "config_values": dict(_COMMON_VALUES),
        "config_fragment": {"type": "zendure_mqtt"},
    }
    out = _switch(
        [],
        [entry],
        [_SWITCH_DEVICE],
        [_SWITCH_PROPOSAL],
        "switchInverterTransport('PHYS-1', 'local_api');",
    )
    assert out["mqtt"] == []
    assert len(out["draft"]) == 1
    item = out["draft"][0]
    assert item["config_name"] == "INV_9"
    assert item["config_values"] == _COMMON_VALUES
    assert item["enabled"] is False
    assert item["ip"] == "192.168.1.100"
    # Stale MQTT-only fields never travel to a Local API draft item.
    assert "connection_source" not in item
    assert "config_fragment" not in item
    assert "broker_ref" not in item


def test_switching_back_and_forth_never_creates_a_second_inverter():
    out = _switch(
        [_SWITCH_API_ITEM],
        [],
        [_SWITCH_DEVICE],
        [_SWITCH_PROPOSAL],
        "switchInverterTransport('PHYS-1', 'zendure_mqtt');\n"
        "switchInverterTransport('PHYS-1', 'local_api');\n"
        "switchInverterTransport('PHYS-1', 'zendure_mqtt');",
    )
    assert out["draft"] == []
    assert len(out["mqtt"]) == 1
    assert out["mqtt"][0]["config_name"] == "INV_1"
    assert out["mqtt"][0]["config_values"] == _COMMON_VALUES


_LOCAL_MQTT_PROPOSAL = {
    "id": "local-mqtt:PHYS-1",
    "serial_number": "PHYS-1",
    "display_name": "SolarFlow 800 Pro 2",
    "connection_source": "local_mqtt",
    "broker_ref": "local_b1",
    "config_fragment": {"type": "zendure_mqtt", "mqtt": {"device_id": "ROUTE-1"}},
}


def test_switch_api_to_local_mqtt_keeps_one_inverter_with_all_common_values():
    # Local MQTT uses the identical contract; only a really discovered Local
    # MQTT proposal can be selected.
    out = _switch(
        [_SWITCH_API_ITEM],
        [],
        [_SWITCH_DEVICE],
        [_LOCAL_MQTT_PROPOSAL],
        "switchInverterTransport('PHYS-1', 'local_mqtt');",
    )
    assert out["draft"] == []
    assert len(out["mqtt"]) == 1
    entry = out["mqtt"][0]
    assert entry["config_name"] == "INV_1"
    assert entry["connection_source"] == "local_mqtt"
    assert entry["config_values"] == _COMMON_VALUES
    assert "ip" not in entry


def test_switch_local_mqtt_back_to_api_keeps_one_inverter():
    entry = {
        "id": "local-mqtt:PHYS-1",
        "target": "device",
        "config_name": "INV_1",
        "serial_number": "PHYS-1",
        "connection_source": "local_mqtt",
        "broker_ref": "local_b1",
        "config_values": dict(_COMMON_VALUES),
        "config_fragment": {"type": "zendure_mqtt"},
    }
    out = _switch(
        [],
        [entry],
        [_SWITCH_DEVICE],
        [_LOCAL_MQTT_PROPOSAL],
        "switchInverterTransport('PHYS-1', 'local_api');",
    )
    assert out["mqtt"] == []
    assert len(out["draft"]) == 1
    assert out["draft"][0]["config_name"] == "INV_1"
    assert out["draft"][0]["config_values"] == _COMMON_VALUES


def test_switching_to_an_undiscovered_connection_does_nothing():
    # Only actually discovered connections may be selected: with no Local MQTT
    # proposal the draft is left untouched rather than half-switched.
    out = _switch(
        [_SWITCH_API_ITEM],
        [],
        [_SWITCH_DEVICE],
        [_SWITCH_PROPOSAL],
        "switchInverterTransport('PHYS-1', 'local_mqtt');",
    )
    assert out["mqtt"] == []
    assert len(out["draft"]) == 1
    assert out["draft"][0]["config_name"] == "INV_1"


def test_setup_mqtt_card_can_re_enable_a_switched_over_inverter():
    # The enabled state survives a switch, so the MQTT card must be able to
    # change it back — otherwise a disabled inverter is stuck.
    js = _read()
    card = _extract_fn(js, "renderMqttInverterCard")
    assert "data-mqtt-enable" in card
    assert "entry.enabled !== false" in card
    handler = js.split('configEls.draftList.addEventListener("change"', 1)[1].split(
        "\n  });", 1
    )[0]
    assert "data-mqtt-enable" in handler
    assert "entry.enabled = target.checked" in handler
    assert "saveMqttPreviewProposals()" in handler


def test_serialized_mqtt_selection_carries_the_common_values():
    js = _read()
    helpers = "\n".join(
        _extract_fn(js, name)
        for name in ("normalizeInverterAliasTokens", "serializeMqttProposalSelection")
    )
    entry = dict(
        _SWITCH_PROPOSAL,
        config_name="INV_1",
        enabled=False,
        config_values=dict(_COMMON_VALUES),
    )
    out = _node(
        helpers
        + "\nconsole.log(JSON.stringify(serializeMqttProposalSelection("
        + json.dumps(entry)
        + ", { target: 'device' })));"
    )
    assert out["config_values"] == _COMMON_VALUES
    assert out["enabled"] is False


# --- Phase 6: Maintenance parity -----------------------------------------


def test_maintenance_alternative_connection_action_text():
    js = _read()
    state = _extract_fn(js, "mconfigDiscoveryActionState")
    assert "Use connection" in state
    assert "instead" not in state
    proposal_card = _extract_fn(js, "renderMaintenanceMqttProposalCard")
    assert "instead" not in proposal_card
    assert "MCONFIG_MQTT_PROPOSAL_ACTIONS" in js
    actions = js.split("const MCONFIG_MQTT_PROPOSAL_ACTIONS", 1)[1].split("};", 1)[0]
    assert '"Use connection"' in actions
    assert "Add inverter" in actions


def test_setup_and_maintenance_use_one_add_action_label():
    js = _read()
    assert "Add as inverter" not in js
    assert "Add inverter" in js


# --- No confirmation popup ------------------------------------------------


def test_connection_switching_never_opens_a_confirmation():
    js = _read()
    for name in (
        "switchInverterTransport",
        "mconfigSwitchInverterTransport",
        "renderMqttCandidateCard",
        "renderConfigAvailableCard",
        "renderMaintenanceMqttProposalCard",
        "renderConnectionCandidateAction",
    ):
        source = _extract_fn(js, name)
        assert "confirm(" not in source, name
        assert "window.confirm" not in source, name


def test_use_connection_handler_switches_through_the_existing_helper():
    js = _read()
    handler = js.split("configEls.availableList.addEventListener", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert 'data-action="use-connection"' in handler
    assert "switchInverterTransport(" in handler
    # The alternative connection is never routed through the add path.
    assert "addMqttInverterFromCandidate(useConnection" not in handler
