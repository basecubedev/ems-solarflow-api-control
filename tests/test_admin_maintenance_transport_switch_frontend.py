# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance transport switch behavior in the real admin.js helpers.

mconfigSwitchInverterTransport replaces the connection of one logical device:
name, original_name reference, enabled state and common tuning values survive;
only transport-specific fields change; the draft still holds exactly one entry
for the physical inverter. The editors keep transport-specific connection
fields separated: the MQTT editor never renders a Local API IP input.
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


def _read():
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
        return handle.read()


def _extract_fn(js, name):
    marker = "function " + name
    assert marker in js, f"{name} is missing from admin.js"
    idx = js.index(marker)
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


_HELPERS = (
    "nextCompactInverterName",
    "mconfigNextInverterName",
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

_CENTRAL = {
    "smart_mode": 1,
    "max_power": 777,
    "pv_kwp": 1.5,
    "pv_priority_factor": 1.25,
    "battery_kwh": 2.5,
    "min_soc": 17,
    "max_soc": 99,
}


def _run(devices, action):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the transport switch tests")
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _HELPERS)
    stub = (
        "const MCONFIG_DEVICE_IDENTITY_KEYS = new Set(['name', 'ip', 'sn']);\n"
        "function renderMaintenanceInverters() {}\n"
        "function mconfigRerenderDiscoveryReview() {}\n"
        "function mconfigMarkDraftChanged() {}\n"
        "const mconfigState = {\n"
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
    )
    result = subprocess.run(
        [node, "-e", helpers + "\n" + stub + action],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_MQTT_DEVICE = {
    "kind": "zendure_mqtt",
    "original_name": "WR1",
    "name": "WR1",
    "enabled": True,
    "has_enabled_key": True,
    "serial_number": "PHYS-1",
    "device_id": "PHYS-1",
    "max_power": 640,
    "pv_kwp": 3.2,
    "battery_kwh": 7.7,
    "min_soc": 22,
    "mqtt": {
        "broker_ref": "local_b1",
        "source": "local_mqtt",
        "topic_family": "zensdk_ha_scalar",
        "device_id": "PHYS-1",
    },
    "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
}

_PROPOSAL = {
    "id": "local-mqtt:PHYS-1",
    "serial_number": "PHYS-1",
    "device_id": "PHYS-1",
    "connection_source": "local_mqtt",
    "broker_ref": "local_b1",
    "output_control_supported": False,
    "config_fragment": {
        "type": "zendure_mqtt",
        "serial_number": "PHYS-1",
        "mqtt": {
            "broker_ref": "local_b1",
            "source": "local_mqtt",
            "topic_family": "zensdk_ha_scalar",
            "device_id": "PHYS-1",
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": False,
        },
    },
}


def test_switch_mqtt_to_api_preserves_identity_and_common_values():
    out = _run(
        [_MQTT_DEVICE],
        "const changed = mconfigSwitchInverterTransport('PHYS-1', 'local_api', {\n"
        "  discovered: { ip: '192.0.2.10', serial_number: 'PHYS-1' },\n"
        "});\n"
        "console.log(JSON.stringify({ changed, devices: mconfigState.draft.devices }));",
    )
    assert out["changed"] is True
    assert len(out["devices"]) == 1
    device = out["devices"][0]
    assert device["kind"] == "local_api"
    assert device["original_name"] == "WR1"
    assert device["name"] == "WR1"
    assert device["ip"] == "192.0.2.10"
    assert device["sn"] == "PHYS-1"
    # Custom common values survive; untouched ones inherit central defaults.
    assert device["max_power"] == 640
    assert device["pv_kwp"] == 3.2
    assert device["battery_kwh"] == 7.7
    assert device["min_soc"] == 22
    assert device["max_soc"] == _CENTRAL["max_soc"]
    # Stale MQTT-only draft fields are gone.
    assert "mqtt" not in device
    assert "serial_number" not in device
    assert "capabilities" not in device


def test_switch_api_to_mqtt_preserves_identity_and_common_values():
    api_device = {
        "original_name": "WR1",
        "name": "INV_1",
        "ip": "192.168.1.100",
        "sn": "PHYS-1",
        "enabled": True,
        "max_power": 640,
        "pv_kwp": 3.2,
    }
    out = _run(
        [api_device],
        "const changed = mconfigSwitchInverterTransport('PHYS-1', 'local_mqtt', {\n"
        "  proposal: " + json.dumps(_PROPOSAL) + ",\n"
        "});\n"
        "console.log(JSON.stringify({ changed, devices: mconfigState.draft.devices }));",
    )
    assert out["changed"] is True
    assert len(out["devices"]) == 1
    device = out["devices"][0]
    assert device["kind"] == "zendure_mqtt"
    # The rename (INV_1) and the original reference (WR1) both survive, so the
    # backend replaces the one original entry instead of creating a second.
    assert device["original_name"] == "WR1"
    assert device["name"] == "INV_1"
    assert device["serial_number"] == "PHYS-1"
    assert device["mqtt"]["topic_family"] == "zensdk_ha_scalar"
    assert device["max_power"] == 640
    assert device["pv_kwp"] == 3.2
    assert device["battery_kwh"] == _CENTRAL["battery_kwh"]
    assert "ip" not in device
    assert "sn" not in device


def test_switch_unknown_identity_is_a_noop():
    out = _run(
        [_MQTT_DEVICE],
        "const changed = mconfigSwitchInverterTransport('OTHER-SN', 'local_api', {\n"
        "  discovered: { ip: '192.0.2.10', serial_number: 'OTHER-SN' },\n"
        "});\n"
        "console.log(JSON.stringify({ changed, devices: mconfigState.draft.devices }));",
    )
    assert out["changed"] is False
    assert out["devices"][0]["kind"] == "zendure_mqtt"


def test_mqtt_editor_never_renders_local_api_ip_field():
    js = _read()
    mqtt_editor = js.split("function renderMaintenanceZendureMqttDevice", 1)[1].split(
        "\nfunction mconfigIsMqttDevice", 1
    )[0]
    assert "renderCommonInverterFields" in mqtt_editor
    assert "renderLocalApiConnectionFields" not in mqtt_editor
    # The shared common renderer excludes identity fields (name/ip/sn), so the
    # MQTT card cannot inherit an IP input through it.
    common = js.split("function renderCommonInverterFields", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "MCONFIG_DEVICE_IDENTITY_KEYS.has(deviceFieldKey(field.path))" in common
