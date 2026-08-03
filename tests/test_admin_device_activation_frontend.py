# SPDX-License-Identifier: AGPL-3.0-or-later
"""Activation survives a Maintenance transport switch, in both directions.

Switching an inverter between the local API and Zendure MQTT keeps one logical
device, so it must also keep one activation state: an active device stays
active — including output control when the new transport can control it — and a
device the operator deactivated stays deactivated.

Activation is the logical device's ``enabled`` flag and nothing else. Output
control is a capability EMS/Core derives from the pinned model, the transport
and the write route, exactly as a Local API inverter has no such switch, so it
never doubles as an activation decision (``agent-rules.md`` §2).
"""

import json
import shutil
import subprocess

import pytest

from tests.test_admin_maintenance_transport_switch_frontend import (
    _CENTRAL,
    _DEVICE_FIELDS,
    _extract_fn,
    _read,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.setup,
    pytest.mark.contract,
    pytest.mark.simulation,
]

_HELPERS = (
    "nextCompactInverterName",
    "mconfigNextInverterName",
    "issuedPhysicalIdentity",
    "issuedConnectionId",
    "issuedIdentityTokens",
    "isConfirmedIdentity",
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
    "normalizeInverterAliasTokens",
    "mconfigZendureMqttDraftFromProposal",
    "mconfigIsMqttDevice",
    "mconfigDeviceIsActive",
    "mconfigDeviceInactiveByChoice",
    "mconfigApplyTransportSwitchActivation",
    "mconfigSwitchInverterTransport",
)


def _run(devices, action):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the activation frontend tests")
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


# The identity the backend issued for this device; the browser switches on it.
PHYSICAL_ID = "opaque:v1:PHYS-1"


def _api_device(**overrides):
    device = {
        "kind": "local_api",
        "original_name": "WR1",
        "name": "WR1",
        "physical_device_id": PHYSICAL_ID,
        "identity_status": "confirmed",
        "ip": "192.168.1.100",
        "sn": "PHYS-1",
        "enabled": True,
        "has_enabled_key": True,
    }
    device.update(overrides)
    return device


def _mqtt_device(**overrides):
    device = {
        "kind": "zendure_mqtt",
        "original_name": "WR1",
        "name": "WR1",
        "physical_device_id": PHYSICAL_ID,
        "identity_status": "confirmed",
        "enabled": True,
        "has_enabled_key": True,
        "serial_number": "PHYS-1",
        "device_id": "PHYS-1",
        "supports_output_control": True,
        "output_control": True,
        "mqtt": {
            "broker_ref": "local_b1",
            "source": "local_mqtt",
            "topic_family": "legacy_zendure_json",
            "device_id": "PHYS-1",
            "product_key": "PK1",
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": True,
        },
    }
    device.update(overrides)
    return device


def _proposal(*, controllable=True):
    return {
        "id": "local-mqtt:PHYS-1",
        "serial_number": "PHYS-1",
        "device_id": "PHYS-1",
        "connection_source": "local_mqtt",
        "broker_ref": "local_b1",
        "hardware_model": "solarflow_800_pro_2" if controllable else "",
        "output_control_supported": controllable,
        "config_fragment": {
            "type": "zendure_mqtt",
            "serial_number": "PHYS-1",
            "hardware_profile": "solarflow_800_pro_2" if controllable else "",
            "mqtt": {
                "broker_ref": "local_b1",
                "source": "local_mqtt",
                "topic_family": "legacy_zendure_json",
                "device_id": "PHYS-1",
                "product_key": "PK1",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": controllable,
            },
        },
    }


def _switch_to_mqtt(devices, *, controllable=True):
    return _run(
        devices,
        "const changed = mconfigSwitchInverterTransport('opaque:v1:PHYS-1', 'local_mqtt', {\n"
        "  proposal: " + json.dumps(_proposal(controllable=controllable)) + ",\n"
        "});\n"
        "console.log(JSON.stringify({ changed, devices: mconfigState.draft.devices }));",
    )


def _switch_to_api(devices):
    return _run(
        devices,
        "const changed = mconfigSwitchInverterTransport('opaque:v1:PHYS-1', 'local_api', {\n"
        "  discovered: { ip: '192.0.2.10', serial_number: 'PHYS-1' },\n"
        "});\n"
        "console.log(JSON.stringify({ changed, devices: mconfigState.draft.devices }));",
    )


def test_active_api_device_switched_to_mqtt_keeps_controlling():
    out = _switch_to_mqtt([_api_device()])

    device = out["devices"][0]
    assert device["kind"] == "zendure_mqtt"
    assert device["enabled"] is True
    assert device["output_control"] is True
    assert device["capabilities"]["write_output_limit"] is True


def test_disabled_api_device_switched_to_mqtt_stays_disabled():
    out = _switch_to_mqtt([_api_device(enabled=False)])

    assert out["devices"][0]["enabled"] is False


def test_active_mqtt_device_switched_to_api_stays_active():
    out = _switch_to_api([_mqtt_device()])

    device = out["devices"][0]
    assert device["kind"] == "local_api"
    assert device["enabled"] is True


def test_a_control_capable_device_not_controlling_is_still_active():
    """Output control is a transport capability, never an activation choice.

    Local API inverters have no such switch: a configured, enabled device is
    controlled. MQTT now answers the same way, so ``write_output_limit=false``
    on a control-capable device no longer reads as "the operator turned this
    device off" and does not deactivate it on the next transport.
    """

    out = _switch_to_api(
        [
            _mqtt_device(
                output_control=False,
                capabilities={"read_power": True, "write_output_limit": False},
            )
        ]
    )

    assert out["devices"][0]["enabled"] is True


def test_uncontrollable_telemetry_only_device_switched_to_api_becomes_active():
    out = _switch_to_api(
        [
            _mqtt_device(
                supports_output_control=False,
                output_control=False,
                capabilities={"read_power": True, "write_output_limit": False},
            )
        ]
    )

    assert out["devices"][0]["enabled"] is True


def test_only_an_explicit_disable_deactivates_a_device():
    """The one activation authority is the logical device's enabled flag."""

    for device in (
        _mqtt_device(enabled=False, output_control=True),
        _mqtt_device(enabled=False, output_control=False),
    ):
        assert _switch_to_api([device])["devices"][0]["enabled"] is False


def test_disabled_mqtt_device_switched_to_api_stays_disabled():
    out = _switch_to_api([_mqtt_device(enabled=False)])

    assert out["devices"][0]["enabled"] is False


def test_switching_to_an_uncontrollable_connection_stays_telemetry_only():
    out = _switch_to_mqtt([_api_device()], controllable=False)

    device = out["devices"][0]
    assert device["output_control"] is False
    assert device["capabilities"]["write_output_limit"] is False


def test_switch_marks_a_fresh_control_decision_for_the_renderer():
    """The replacement is a fresh connection, so its capability is re-derived.

    ``transport_switched`` tells the card renderer not to carry the previous
    entry's stored control state into the new transport.
    """

    out = _run(
        [_api_device()],
        "mconfigSwitchInverterTransport('opaque:v1:PHYS-1', 'local_mqtt', {\n"
        "  proposal: " + json.dumps(_proposal()) + ",\n"
        "});\n"
        "const device = mconfigState.draft.devices[0];\n"
        "console.log(JSON.stringify({\n"
        "  transportSwitched: device.transport_switched === true,\n"
        "  outputControl: device.output_control === true,\n"
        "}));",
    )

    assert out["transportSwitched"] is True
    assert out["outputControl"] is True
