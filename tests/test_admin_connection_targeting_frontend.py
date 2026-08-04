# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance targets the exact discovered connection.

Two brokers for one physical inverter are two concrete connections, and the
clicked card — not "the first proposal with the same source" — decides which one
the draft ends up using. Maintenance resolves the configured device through the
identity the backend issued for it and compares connections by their issued
connection id, so an enriched route is recognized and a contradicting one is
refused.

Setup's half of this rule moved to the backend: candidate classification is
pinned in ``tests/test_admin_setup_batch_planner.py`` and its application in
``tests/test_admin_setup_transport_selection.py``.

The real admin.js helpers are extracted (brace-matched) and executed in node.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.mqtt,
    pytest.mark.contract,
    pytest.mark.simulation,
]

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


_CONSTANTS = None


def _constants(js):
    return "\n".join(
        line
        for line in js.split("\n")
        if line.startswith("const ") and "PATTERN = /" in line
    )


_IDENTITY_HELPERS = (
    "issuedPhysicalIdentity",
    "issuedConnectionId",
    "issuedIdentityTokens",
    "isConfirmedIdentity",
    "inverterHasIdentity",
    "inverterIdentityConflict",
    "inverterIdentitiesMatch",
    "normalizeInverterAliasTokens",
    "mqttSourceOfConnection",
)


# --- Finding 2: Maintenance compares source and broker scope --------------

_MSTATE_HELPERS = _IDENTITY_HELPERS + (
    "mconfigIsMqttDevice",
    "connectionBrokerScope",
    "mconfigDeviceMqttSource",
    "mconfigDeviceConnectionSource",
    "mconfigSameMqttConnection",
    "mconfigProposalIdentityView",
    "mconfigDraftDevicesMatchingCandidate",
    "mconfigPristineHasCandidateConnection",
    "mconfigDraftHasProposal",
    "mconfigMqttProposalState",
)


def _mstate(devices, proposal, pristine=None):
    js = _read()
    helpers = _constants(js) + "\n" + "\n".join(
        _extract_fn(js, name) for name in _MSTATE_HELPERS
    )
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


# Both sides carry what the backend issued for them: the physical identity, the
# connection id of the route they answer on, and why that identity is what it is.
def _mqtt_device(scope, source="local_mqtt", serial="PHYS-1"):
    return {
        "kind": "zendure_mqtt",
        "name": "INV_1",
        "physical_device_id": "opaque:v1:" + serial,
        "identity_status": "confirmed",
        "connection_id": "conn:v1:" + source + "-" + scope,
        "mqtt": {"broker_ref": scope, "source": source, "device_id": "ROUTE"},
    }


def _mqtt_candidate(scope, source="local_mqtt", serial="PHYS-1"):
    return {
        "id": "mqtt:" + scope,
        "physical_device_id": "opaque:v1:" + serial,
        "identity_status": "confirmed",
        "connection_id": "conn:v1:" + source + "-" + scope,
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
    api = {
        "name": "INV_1",
        "ip": "192.168.1.100",
        "sn": "PHYS-1",
        "physical_device_id": "opaque:v1:PHYS-1",
        "identity_status": "confirmed",
        "connection_id": "conn:v1:local_api-192.168.1.100",
    }
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
    "mconfigIsMqttDevice",
    "mconfigDeviceIsActive",
    "mconfigDeviceInactiveByChoice",
    "mconfigApplyTransportSwitchActivation",
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
    helpers = _constants(js) + "\n" + "\n".join(
        _extract_fn(js, name) for name in _MSWITCH_HELPERS
    )
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
