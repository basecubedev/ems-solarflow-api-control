# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance frontend device creation and duplicate handling contracts.

Drives the real admin.js maintenance helpers in Node. Two families of defects
are pinned here:

- Every creation path (manual API, discovered API, manual MQTT, MQTT proposal)
  must initialize the same common tuning values from the server-supplied
  central default payload (``catalog.default_device.common``), never from the
  first configured device and never as blanks. Sentinel values prove the data
  comes from the payload, not from frontend literals.
- Duplicate detection must recognize one physical inverter across transports:
  a configured MQTT serial blocks "Add as inverter" for the same Local API
  serial and vice versa.
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
    """Extract exactly one function body (brace-balanced, no trailing consts)."""

    marker = "function " + name
    assert marker in js, f"{name} is missing from admin.js"
    idx = js.index(marker)
    prefix = "async " if js[idx - 6 : idx] == "async " else ""
    body = js[idx:]
    depth = 0
    end = None
    for position, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = position + 1
                break
    assert end is not None, f"unbalanced braces while extracting {name}"
    return prefix + body[:end]


def _extract_optional(js, name):
    marker = "function " + name
    if marker not in js:
        return ""
    return _extract_fn(js, name)


def _run(names, setup, optional=()):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the maintenance frontend tests")
    js = _read()
    seen = []
    for name in names:
        if name not in seen:
            seen.append(name)
    parts = [_extract_fn(js, name) for name in seen]
    for name in optional:
        if name in seen:
            continue
        seen.append(name)
        extracted = _extract_optional(js, name)
        if extracted:
            parts.append(extracted)
    helpers = "\n".join(parts)
    result = subprocess.run(
        [node, "-e", helpers + "\n" + setup], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# Sentinel central defaults: distinct from any frontend literal so the test
# proves values flow from the server payload.
_CENTRAL = {
    "smart_mode": 1,
    "max_power": 777,
    "pv_kwp": 1.5,
    "pv_priority_factor": 1.25,
    "battery_kwh": 2.5,
    "min_soc": 17,
    "max_soc": 99,
}

_DEVICE_FIELDS = [
    {"path": "devices[].name", "type": "text"},
    {"path": "devices[].ip", "type": "text"},
    {"path": "devices[].sn", "type": "text"},
    {"path": "devices[].smart_mode", "type": "number"},
    {"path": "devices[].max_power", "type": "number"},
    {"path": "devices[].pv_kwp", "type": "number"},
    {"path": "devices[].pv_priority_factor", "type": "number"},
    {"path": "devices[].battery_kwh", "type": "number"},
    {"path": "devices[].min_soc", "type": "number"},
    {"path": "devices[].max_soc", "type": "number"},
]


def _state_stub(devices):
    return (
        "const MCONFIG_DEVICE_IDENTITY_KEYS = new Set(['name', 'ip', 'sn']);\n"
        "const mconfigState = {\n"
        "  loaded: true,\n"
        "  pristine: null,\n"
        "  openHardware: new Set(),\n"
        "  draft: { devices: " + json.dumps(devices) + " },\n"
        "  catalog: {\n"
        "    default_device: { common: " + json.dumps(_CENTRAL) + " },\n"
        "    hardware_sections: [\n"
        "      { id: 'devices', fields: " + json.dumps(_DEVICE_FIELDS) + " }\n"
        "    ],\n"
        "  },\n"
        "};\n"
        "function renderMaintenanceInverters() {}\n"
        "function mconfigRerenderDiscoveryReview() {}\n"
        "function mconfigMarkDraftChanged() {}\n"
    )


_COMMON_HELPERS = (
    "nextCompactInverterName",
    "mconfigNextInverterName",
    "mconfigDeviceCatalogFields",
    "mconfigHardwareSection",
    "deviceFieldKey",
    "mconfigIdentity",
)

_OPTIONAL_HELPERS = (
    "normalizeSerial",
    "usableSerialValue",
    "physicalInverterIdentity",
    "inverterVisibleSerial",
    "inverterIdentityTokens",
    "inverterIdentitySet",
    "inverterHasIdentity",
    "inverterIdentityConflict",
    "inverterIdentitiesMatch",
    "mconfigProposalIdentityView",
    "mconfigDeviceCommonDefaults",
    "mconfigApplyCommonDefaults",
    "mconfigIsMqttDevice",
    "mconfigMqttDeviceIdentity",
    "mconfigMqttProposalIdentity",
    "mqttProposalBrokerRef",
    "mqttProposalBrokerProfile",
    "mconfigZendureMqttDraftFromProposal",
)


def _assert_central_defaults(device):
    for key, expected in _CENTRAL.items():
        assert device.get(key) == expected, (
            f"{key} must be initialized from the central default payload, "
            f"got {device.get(key)!r}"
        )


def test_manual_api_add_uses_central_defaults_not_first_device():
    first = {
        "original_name": "INV_1",
        "name": "INV_1",
        "ip": "192.168.1.100",
        "sn": "AAA",
        "enabled": True,
        "max_power": 321,
        "pv_kwp": 4.7,
        "battery_kwh": 9.6,
        "min_soc": 27,
    }
    out = _run(
        _COMMON_HELPERS + ("mconfigAddInverter",),
        _state_stub([first])
        + "mconfigAddInverter();\n"
        + "console.log(JSON.stringify(mconfigState.draft.devices));",
        optional=_OPTIONAL_HELPERS,
    )
    assert len(out) == 2
    added = out[1]
    _assert_central_defaults(added)
    assert added["max_power"] != 321
    assert added["pv_kwp"] != 4.7
    assert added["battery_kwh"] != 9.6
    assert added["min_soc"] != 27
    assert added["name"] == "INV_2"


def test_discovered_api_add_materializes_central_defaults():
    out = _run(
        _COMMON_HELPERS + ("mconfigDiscoveredAlreadyInDraft", "mconfigAddDiscovered"),
        _state_stub([])
        + "const changed = mconfigAddDiscovered({\n"
        + "  role: 'inverter',\n"
        + "  discovered: { ip: '192.0.2.10', serial_number: 'SER-1' },\n"
        + "});\n"
        + "console.log(JSON.stringify({ changed, devices: mconfigState.draft.devices }));",
        optional=_OPTIONAL_HELPERS,
    )
    assert out["changed"] is True
    device = out["devices"][0]
    assert device["ip"] == "192.0.2.10"
    assert device["sn"] == "SER-1"
    _assert_central_defaults(device)


def test_manual_mqtt_add_materializes_central_defaults():
    out = _run(
        _COMMON_HELPERS + ("mconfigAddZendureMqttDevice",),
        _state_stub([])
        + "function mconfigGenerations() {\n"
        + "  return [{ id: 'solarflow_zensdk', label: 'ZenSDK', default: true }];\n"
        + "}\n"
        + "mconfigAddZendureMqttDevice();\n"
        + "console.log(JSON.stringify(mconfigState.draft.devices));",
        optional=_OPTIONAL_HELPERS,
    )
    device = out[0]
    assert device["kind"] == "zendure_mqtt"
    _assert_central_defaults(device)


def test_mqtt_proposal_add_materializes_central_defaults():
    proposal = {
        "id": "local-mqtt:SER-9",
        "serial_number": "SER-9",
        "device_id": "SER-9",
        "connection_source": "local_mqtt",
        "broker_ref": "local_b1",
        "output_control_supported": False,
        "config_fragment": {
            "type": "zendure_mqtt",
            "serial_number": "SER-9",
            "mqtt": {
                "broker_ref": "local_b1",
                "source": "local_mqtt",
                "topic_family": "zensdk_ha_scalar",
                "device_id": "SER-9",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": False,
            },
        },
    }
    out = _run(
        _COMMON_HELPERS
        + (
            "mconfigIsMqttDevice",
            "mconfigDraftDevicesMatchingCandidate",
            "mconfigPristineHasCandidateConnection",
            "mconfigMqttProposalState",
            "mconfigAddZendureMqttProposal",
        ),
        _state_stub([])
        + "const proposal = " + json.dumps(proposal) + ";\n"
        + "const added = mconfigAddZendureMqttProposal(proposal);\n"
        + "console.log(JSON.stringify({ added, devices: mconfigState.draft.devices }));",
        optional=_OPTIONAL_HELPERS,
    )
    assert out["added"] is True
    device = out["devices"][0]
    assert device["kind"] == "zendure_mqtt"
    assert device["serial_number"] == "SER-9"
    _assert_central_defaults(device)


# --- cross-transport duplicate prevention -----------------------------------


def test_configured_mqtt_serial_blocks_api_add():
    mqtt_device = {
        "kind": "zendure_mqtt",
        "original_name": "WR1",
        "name": "WR1",
        "serial_number": "PHYSICAL-001",
        "device_id": "PHYSICAL-001",
        "enabled": True,
    }
    out = _run(
        _COMMON_HELPERS
        + ("mconfigIsMqttDevice", "mconfigDiscoveredAlreadyInDraft"),
        _state_stub([mqtt_device])
        + "const inDraft = mconfigDiscoveredAlreadyInDraft({\n"
        + "  role: 'inverter',\n"
        + "  state: 'new',\n"
        + "  discovered: { ip: '192.0.2.10', serial_number: 'PHYSICAL-001' },\n"
        + "});\n"
        + "console.log(JSON.stringify({ inDraft }));",
        optional=_OPTIONAL_HELPERS,
    )
    assert out["inDraft"] is True, (
        "a Local API discovery of a configured MQTT serial must not be offered "
        "as a new inverter"
    )


def test_configured_api_serial_blocks_mqtt_proposal_add():
    api_device = {
        "original_name": "WR1",
        "name": "WR1",
        "ip": "192.168.1.100",
        "sn": "PHYSICAL-001",
        "enabled": True,
    }
    out = _run(
        _COMMON_HELPERS
        + (
            "mconfigIsMqttDevice",
            "mconfigDraftDevicesMatchingCandidate",
            "mconfigPristineHasCandidateConnection",
            "mconfigMqttProposalState",
        ),
        _state_stub([api_device])
        + "const state = mconfigMqttProposalState({\n"
        + "  id: 'local-mqtt:PHYSICAL-001',\n"
        + "  serial_number: 'PHYSICAL-001',\n"
        + "  connection_source: 'local_mqtt',\n"
        + "});\n"
        + "console.log(JSON.stringify({ state }));",
        optional=_OPTIONAL_HELPERS,
    )
    assert out["state"] != "new", (
        "an MQTT proposal for a configured Local API serial must not be "
        "offered as a brand-new device"
    )
