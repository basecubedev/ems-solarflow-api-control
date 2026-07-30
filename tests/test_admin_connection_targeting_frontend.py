# SPDX-License-Identifier: AGPL-3.0-or-later
"""A connection candidate action targets the exact discovered connection.

The clicked card, not "the first proposal with the same source", decides what
the draft ends up using: two brokers for one physical inverter are two concrete
connections. Maintenance applies the same rule — it compares MQTT source and
broker scope, and it resolves the configured device through trusted identity
aliases instead of primary-key equality.

The real admin.js helpers are extracted (brace-matched) and executed in node.
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
        pytest.skip("node is required for the connection targeting tests")
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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
    "normalizeInverterAliasTokens",
    "mqttSourceOfConnection",
)


# --- Finding 1: the clicked connection is the one that gets selected -------

_SWITCH_HELPERS = _IDENTITY_HELPERS + (
    "connectionLabelFor",
    "dismissalStorageKey",
    "dismissalKeysForInverter",
    "dismissSerial",
    "undismissSerial",
    "nextCompactInverterName",
    "inverterItems",
    "selectedMqttDeviceEntries",
    "rememberedInverterName",
    "rememberInverterName",
    "inverterConfigNameForSerial",
    "draftHasSource",
    "deviceKey",
    "sourcesOf",
    "uniqueDisplayName",
    "draftItemFromDevice",
    "preservedInverterValues",
    "connectionBrokerScope",
    "configuredInverterConnection",
    "serializeMqttProposalSelection",
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
function forgetInverterName() {}
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
        "let configDraftItems = " + json.dumps(draft_items) + ";\n"
        "const zendureMqttPreviewProposals = new Map("
        + json.dumps([[str(e.get("id", "")), e] for e in mqtt_entries])
        + ");\n"
        "const _devices = " + json.dumps(devices) + ";\n"
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


def _local_proposal(scope, route):
    return {
        "id": "local-mqtt:PHYS-1:" + scope,
        "serial_number": "PHYS-1",
        "device_id": route,
        "display_name": "SolarFlow 800 Pro 2",
        "connection_source": "local_mqtt",
        "broker_ref": scope,
        "config_fragment": {
            "type": "zendure_mqtt",
            "serial_number": "PHYS-1",
            "mqtt": {"broker_ref": scope, "source": "local_mqtt", "device_id": route},
        },
    }


_B1 = _local_proposal("local_b1", "ROUTE-B1")
_B2 = _local_proposal("local_b2", "ROUTE-B2")

_SELECTED_B1 = {
    "id": _B1["id"],
    "target": "device",
    "config_name": "INV_1",
    "serial_number": "PHYS-1",
    "connection_source": "local_mqtt",
    "broker_ref": "local_b1",
    "device_id": "ROUTE-B1",
    "config_fragment": _B1["config_fragment"],
    "config_values": {"max_power": 640},
}

_API_DEVICE = {
    "id": "zendure:PHYS-1",
    "ip": "192.168.1.100",
    "serial_number": "PHYS-1",
    "api_family": "zendure",
    "role_suggestion": "inverter",
    "display_name": "SolarFlow 800 Pro 2",
}


def test_clicking_the_second_local_broker_selects_that_exact_connection():
    out = _switch(
        [],
        [_SELECTED_B1],
        [_API_DEVICE],
        [_B1, _B2],
        "switchInverterTransport('PHYS-1', 'local_mqtt', "
        "{ candidateRef: " + json.dumps(_B2["id"]) + " });",
    )
    assert len(out["mqtt"]) == 1
    entry = out["mqtt"][0]
    assert entry["broker_ref"] == "local_b2"
    assert entry["device_id"] == "ROUTE-B2"
    assert entry["config_fragment"]["mqtt"]["broker_ref"] == "local_b2"
    assert entry["config_name"] == "INV_1"
    assert entry["config_values"] == {"max_power": 640}
    assert out["draft"] == []


def test_switching_back_selects_the_first_broker_exactly():
    selected_b2 = dict(
        _SELECTED_B1,
        id=_B2["id"],
        broker_ref="local_b2",
        device_id="ROUTE-B2",
        config_fragment=_B2["config_fragment"],
    )
    out = _switch(
        [],
        [selected_b2],
        [_API_DEVICE],
        [_B1, _B2],
        "switchInverterTransport('PHYS-1', 'local_mqtt', "
        "{ candidateRef: " + json.dumps(_B1["id"]) + " });",
    )
    assert len(out["mqtt"]) == 1
    assert out["mqtt"][0]["broker_ref"] == "local_b1"
    assert out["mqtt"][0]["device_id"] == "ROUTE-B1"


def test_an_ambiguous_mqtt_target_without_a_reference_changes_nothing():
    # Two concrete connections share identity and source: picking "the first"
    # would silently bind the wrong broker/route.
    out = _switch(
        [],
        [_SELECTED_B1],
        [_API_DEVICE],
        [_B1, _B2],
        "switchInverterTransport('PHYS-1', 'local_mqtt');",
    )
    assert len(out["mqtt"]) == 1
    assert out["mqtt"][0]["broker_ref"] == "local_b1"


def test_a_stale_candidate_reference_never_changes_the_draft():
    out = _switch(
        [],
        [_SELECTED_B1],
        [_API_DEVICE],
        [_B1, _B2],
        "switchInverterTransport('PHYS-1', 'local_mqtt', "
        "{ candidateRef: 'local-mqtt:PHYS-1:gone' });",
    )
    assert len(out["mqtt"]) == 1
    assert out["mqtt"][0]["broker_ref"] == "local_b1"


def test_a_reference_whose_source_disagrees_is_refused():
    out = _switch(
        [],
        [_SELECTED_B1],
        [_API_DEVICE],
        [_B1, _B2],
        "switchInverterTransport('PHYS-1', 'zendure_mqtt', "
        "{ candidateRef: " + json.dumps(_B2["id"]) + " });",
    )
    assert len(out["mqtt"]) == 1
    assert out["mqtt"][0]["broker_ref"] == "local_b1"


def test_candidate_actions_never_expose_a_proposal_id_in_the_dom():
    # A serial-less Cloud proposal id falls back to the raw route device id or
    # product key, so only an opaque per-render token may reach the DOM.
    js = _read()
    action = _extract_fn(js, "renderConnectionCandidateAction")
    assert "data-candidate-token" in action
    assert "data-proposal-id" not in action
    assert "connectionCandidateToken(" in action
    for name in (
        "connectionCandidateToken",
        "resolveConnectionCandidateToken",
        "resetConnectionCandidateTokens",
    ):
        assert "function " + name + "(" in js, name
    # The pool render mints a fresh generation, so a click on a stale card fails.
    pool = _extract_fn(js, "renderConfigAvailable")
    assert "resetConnectionCandidateTokens()" in pool


# --- Finding 4: duplicate observations of the active connection -----------

_POOL_HELPERS = _IDENTITY_HELPERS + (
    "connectionBrokerScope",
    "sameMqttConnectionScope",
    "concreteMqttConnectionKey",
    "selectedMqttDeviceEntries",
    "unselectedMqttDeviceProposals",
)


def _pool(selected, proposals):
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _POOL_HELPERS)
    stub = (
        "const zendureMqttPreviewProposals = new Map("
        + json.dumps([[str(e.get("id", "")), e] for e in selected])
        + ");\n"
        "const latestMqttProposals = " + json.dumps(proposals) + ";\n"
        "function isMqttGridMeterProposal() { return false; }\n"
        "function availableMqttDeviceProposals() {\n"
        "  return latestMqttProposals.filter((p) => !isMqttGridMeterProposal(p) && p.config_fragment);\n"
        "}\n"
        "console.log(JSON.stringify(unselectedMqttDeviceProposals().map((p) => p.id)));"
    )
    return _node(helpers + "\n" + stub)


_CLOUD_P1 = {
    "id": "zendure-mqtt:PHYS-1",
    "serial_number": "PHYS-1",
    "connection_source": "zendure_cloud_mqtt",
    "broker_ref": "zendure_cloud",
    "config_fragment": {},
}
_CLOUD_P2 = dict(_CLOUD_P1, id="zendure-mqtt:PHYS-1:dup")


def test_a_duplicate_observation_of_the_active_connection_is_hidden():
    selected = dict(_CLOUD_P1, target="device", config_name="INV_1")
    assert _pool([selected], [_CLOUD_P1, _CLOUD_P2]) == []


def test_a_distinct_local_connection_stays_visible_next_to_a_cloud_selection():
    selected = dict(_CLOUD_P1, target="device", config_name="INV_1")
    assert _pool([selected], [_CLOUD_P1, _B1]) == [_B1["id"]]


def test_a_second_local_broker_stays_visible():
    selected = dict(_SELECTED_B1)
    assert _pool([selected], [_B1, _B2]) == [_B2["id"]]


def test_an_alias_only_duplicate_of_the_active_connection_is_hidden():
    selected = {
        "id": "zendure-mqtt:route",
        "target": "device",
        "config_name": "INV_1",
        "serial_number": "PHYS-1",
        "physical_identity_alias_tokens": ["opaque:v1:routeA"],
        "connection_source": "zendure_cloud_mqtt",
        "broker_ref": "zendure_cloud",
    }
    duplicate = {
        "id": "zendure-mqtt:route:other",
        "physical_identity_token": "opaque:v1:routeA",
        "connection_source": "zendure_cloud_mqtt",
        "broker_ref": "zendure_cloud",
        "config_fragment": {},
    }
    assert _pool([selected], [duplicate]) == []


# --- Finding 2: Maintenance compares source and broker scope --------------

_MSTATE_HELPERS = _IDENTITY_HELPERS + (
    "connectionBrokerScope",
    "mconfigIsMqttDevice",
    "mconfigDeviceMqttSource",
    "mconfigDeviceConnectionSource",
    "mconfigSameMqttConnection",
    "mconfigProposalIdentityView",
    "mconfigDraftDevicesMatchingCandidate",
    "mconfigPristineHasCandidateConnection",
    "mconfigMqttProposalState",
)


def _mstate(devices, proposal, pristine=None):
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _MSTATE_HELPERS)
    stub = (
        "function maintenanceMqttProposals() { return []; }\n"
        "const mconfigState = { pristine: "
        + (json.dumps({"devices": pristine}) if pristine is not None else "null")
        + ", draft: { devices: " + json.dumps(devices) + " } };\n"
        "console.log(JSON.stringify(mconfigMqttProposalState("
        + json.dumps(proposal)
        + ")));"
    )
    return _node(helpers + "\n" + stub)


def _mqtt_device(scope, source="local_mqtt", serial="PHYS-1"):
    return {
        "kind": "zendure_mqtt",
        "name": "INV_1",
        "serial_number": serial,
        "mqtt": {"broker_ref": scope, "source": source, "device_id": "ROUTE"},
    }


def _mqtt_candidate(scope, source="local_mqtt", serial="PHYS-1"):
    return {
        "id": "mqtt:" + scope,
        "serial_number": serial,
        "connection_source": source,
        "broker_ref": scope,
        "config_fragment": {"mqtt": {"broker_ref": scope, "source": source}},
    }


def test_maintenance_same_broker_scope_is_already_configured():
    assert _mstate([_mqtt_device("local_b1")], _mqtt_candidate("local_b1")) == "added"


def test_maintenance_other_broker_scope_is_an_alternative_connection():
    assert _mstate([_mqtt_device("local_b1")], _mqtt_candidate("local_b2")) == "transport"


def test_maintenance_other_mqtt_source_is_an_alternative_connection():
    device = _mqtt_device("zendure_cloud", source="zendure_cloud_mqtt")
    assert _mstate([device], _mqtt_candidate("local_b1")) == "transport"


def test_maintenance_configured_api_stays_an_alternative_connection():
    api = {"name": "INV_1", "ip": "192.168.1.100", "sn": "PHYS-1"}
    assert _mstate([api], _mqtt_candidate("local_b1")) == "transport"


def test_maintenance_applied_config_reports_the_same_connection_as_found():
    device = _mqtt_device("local_b1")
    assert _mstate([device], _mqtt_candidate("local_b1"), pristine=[device]) == "found"


def test_maintenance_new_physical_inverter_stays_new():
    assert _mstate([], _mqtt_candidate("local_b1")) == "new"


# --- Finding 3: Maintenance switches through trusted aliases --------------

_MSWITCH_HELPERS = _IDENTITY_HELPERS + (
    "nextCompactInverterName",
    "mconfigNextInverterName",
    "mconfigHardwareSection",
    "mconfigDeviceCatalogFields",
    "deviceFieldKey",
    "mconfigDeviceCommonDefaults",
    "mconfigApplyCommonDefaults",
    "mqttProposalBrokerRef",
    "mqttProposalBrokerProfile",
    "mconfigZendureMqttDraftFromProposal",
    "mconfigSwitchInverterTransport",
)

_MSWITCH_FIELDS = [
    {"path": "devices[].name", "type": "text"},
    {"path": "devices[].ip", "type": "text"},
    {"path": "devices[].sn", "type": "text"},
    {"path": "devices[].max_power", "type": "number"},
    {"path": "devices[].min_soc", "type": "number"},
]
_MSWITCH_CENTRAL = {"max_power": 800, "min_soc": 10}


def _mswitch(devices, action):
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _MSWITCH_HELPERS)
    stub = (
        "const MCONFIG_DEVICE_IDENTITY_KEYS = new Set(['name', 'ip', 'sn']);\n"
        "function renderMaintenanceInverters() {}\n"
        "function mconfigRerenderDiscoveryReview() {}\n"
        "function mconfigMarkDraftChanged() {}\n"
        "function mconfigHardwareModelLabel(v) { return String(v || ''); }\n"
        "const mconfigState = {\n"
        "  pristine: null,\n"
        "  openHardware: new Set(),\n"
        "  draft: { devices: " + json.dumps(devices) + " },\n"
        "  catalog: {\n"
        "    default_device: { common: " + json.dumps(_MSWITCH_CENTRAL) + " },\n"
        "    hardware_sections: [\n"
        "      { id: 'devices', fields: " + json.dumps(_MSWITCH_FIELDS) + " }\n"
        "    ],\n"
        "  },\n"
        "};\n"
    )
    return _node(helpers + "\n" + stub + action)


_ALIAS_API_DEVICE = {
    "name": "INV_1",
    "original_name": "INV_1",
    "ip": "192.168.1.100",
    "sn": "PHYS-1",
    "physical_identity_token": "opaque:v1:routeA",
    "enabled": True,
    "max_power": 642,
    "min_soc": 22,
}

_ALIAS_PROPOSAL = {
    "id": "zendure-mqtt:routeA",
    "physical_identity_token": "opaque:v1:routeA",
    "connection_source": "zendure_cloud_mqtt",
    "broker_ref": "zendure_cloud",
    "config_fragment": {
        "type": "zendure_mqtt",
        "mqtt": {
            "broker_ref": "zendure_cloud",
            "source": "zendure_cloud_mqtt",
            "device_id": "ROUTE-A",
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
    },
}


def test_maintenance_switches_a_route_only_alternative_through_its_alias():
    out = _mswitch(
        [_ALIAS_API_DEVICE],
        "const changed = mconfigSwitchInverterTransport('opaque:v1:routeA', "
        "'zendure_mqtt', { proposal: " + json.dumps(_ALIAS_PROPOSAL) + " });\n"
        "console.log(JSON.stringify({ changed, devices: mconfigState.draft.devices }));",
    )
    assert out["changed"] is True
    assert len(out["devices"]) == 1
    device = out["devices"][0]
    assert device["kind"] == "zendure_mqtt"
    assert device["name"] == "INV_1"
    assert device["original_name"] == "INV_1"
    assert device["max_power"] == 642
    assert device["min_soc"] == 22
    assert device["mqtt"]["device_id"] == "ROUTE-A"


def test_maintenance_refuses_a_switch_when_the_alias_contradicts_the_serial():
    conflicting = dict(_ALIAS_API_DEVICE, sn="PHYS-2")
    out = _mswitch(
        [conflicting],
        "const changed = mconfigSwitchInverterTransport('PHYS-1', 'zendure_mqtt', "
        "{ proposal: " + json.dumps(dict(_ALIAS_PROPOSAL, serial_number="PHYS-1")) + " });\n"
        "console.log(JSON.stringify({ changed, devices: mconfigState.draft.devices }));",
    )
    assert out["changed"] is False
    assert out["devices"][0]["sn"] == "PHYS-2"
    assert "mqtt" not in out["devices"][0]


def test_maintenance_fails_closed_when_several_configured_devices_match():
    first = dict(_ALIAS_API_DEVICE, name="INV_1")
    second = dict(_ALIAS_API_DEVICE, name="INV_2", ip="192.168.1.101")
    out = _mswitch(
        [first, second],
        "const changed = mconfigSwitchInverterTransport('opaque:v1:routeA', "
        "'zendure_mqtt', { proposal: " + json.dumps(_ALIAS_PROPOSAL) + " });\n"
        "console.log(JSON.stringify({ changed, devices: mconfigState.draft.devices }));",
    )
    assert out["changed"] is False
    assert len(out["devices"]) == 2
    assert all("mqtt" not in device for device in out["devices"])


# --- Finding 5: Maintenance presentation ---------------------------------


def test_maintenance_inverter_candidates_carry_the_api_connection_pill():
    js = _read()
    card = _extract_fn(js, "renderMaintenanceDiscoveryCard")
    assert "connectionSource:" in card
    assert "mconfigCandidateConnectionSource(" in card


def test_maintenance_alternative_candidates_name_the_configured_connection():
    js = _read()
    assert "function mconfigConnectionRelationshipNote(" in js
    note = _extract_fn(js, "mconfigConnectionRelationshipNote")
    assert "Already configured as " in note
    assert "connectionLabelFor(" in note
    # Text nodes only: no innerHTML interpolation of device-supplied values.
    assert "textContent" in note
    assert "innerHTML" not in note
    for name in ("renderMaintenanceDiscoveryCard", "renderMaintenanceMqttProposalCard"):
        assert "mconfigConnectionRelationshipNote(" in _extract_fn(js, name), name
