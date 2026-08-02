# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance connection switching is draft-authoritative and reversible.

The current editable draft — not the installed pristine config — decides what a
discovered connection offers. After an in-memory switch the connection the
inverter no longer uses must become selectable again, so an operator can move
one logical inverter back and forth between its discovered connections without a
rescan. An alias that matches more than one draft device stays fail-closed.

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


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the maintenance switch-back tests")
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_HELPERS = (
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
    "connectionBrokerScope",
    "nextCompactInverterName",
    "mconfigNextInverterName",
    "mconfigIsMqttDevice",
    "mconfigDeviceMqttSource",
    "mconfigDeviceConnectionSource",
    "mconfigSameMqttConnection",
    "mconfigProposalIdentityView",
    "mconfigDraftDevicesMatchingCandidate",
    "mconfigPristineHasCandidateConnection",
    "mconfigMqttProposalState",
    "mconfigHardwareSection",
    "mconfigDeviceCatalogFields",
    "deviceFieldKey",
    "mconfigDeviceCommonDefaults",
    "mconfigApplyCommonDefaults",
    "mqttProposalBrokerRef",
    "mqttProposalBrokerProfile",
    "mconfigZendureMqttDraftFromProposal",
    "mconfigDeviceIsActive",
    "mconfigDeviceInactiveByChoice",
    "mconfigApplyTransportSwitchActivation",
    "mconfigSwitchInverterTransport",
)

_FIELDS = [
    {"path": "devices[].name", "type": "text"},
    {"path": "devices[].ip", "type": "text"},
    {"path": "devices[].sn", "type": "text"},
    {"path": "devices[].max_power", "type": "number"},
    {"path": "devices[].min_soc", "type": "number"},
    {"path": "devices[].pv_kwp", "type": "number"},
]
_CENTRAL = {"max_power": 800, "min_soc": 10, "pv_kwp": 2.0}


def _script(devices, pristine, action):
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _HELPERS)
    stub = (
        "const MCONFIG_DEVICE_IDENTITY_KEYS = new Set(['name', 'ip', 'sn']);\n"
        "function renderMaintenanceInverters() {}\n"
        "function mconfigMarkDraftChanged() {}\n"
        "function mconfigRerenderDiscoveryReview() {}\n"
        "function mconfigHardwareModelLabel(v) { return String(v || ''); }\n"
        "function maintenanceMqttProposals() { return []; }\n"
        "const mconfigState = {\n"
        "  pristine: "
        + (json.dumps({"devices": pristine}) if pristine is not None else "null")
        + ",\n"
        "  openHardware: new Set(),\n"
        "  draft: { devices: " + json.dumps(devices) + " },\n"
        "  catalog: {\n"
        "    default_device: { common: " + json.dumps(_CENTRAL) + " },\n"
        "    hardware_sections: [\n"
        "      { id: 'devices', fields: " + json.dumps(_FIELDS) + " }\n"
        "    ],\n"
        "  },\n"
        "};\n"
    )
    return _node(helpers + "\n" + stub + action)


def _state(devices, proposal, pristine=None):
    return _script(
        devices,
        pristine,
        "console.log(JSON.stringify(mconfigMqttProposalState("
        + json.dumps(proposal)
        + ")));",
    )


SERIAL = "PHYS-1"


def _mqtt_device(scope, source="local_mqtt", name="INV_1"):
    return {
        "kind": "zendure_mqtt",
        "name": name,
        "original_name": "INV_1",
        "enabled": True,
        "has_enabled_key": True,
        "serial_number": SERIAL,
        "max_power": 642,
        "min_soc": 22,
        "mqtt": {"broker_ref": scope, "source": source, "device_id": "ROUTE-" + scope},
    }


def _api_device(name="INV_1"):
    return {
        "kind": "local_api",
        "name": name,
        "original_name": "INV_1",
        "ip": "192.168.1.100",
        "sn": SERIAL,
        "enabled": True,
        "has_enabled_key": True,
        "max_power": 642,
        "min_soc": 22,
    }


def _proposal(scope, source="local_mqtt"):
    return {
        "id": "mqtt:" + scope,
        "serial_number": SERIAL,
        "device_id": "ROUTE-" + scope,
        "connection_source": source,
        "broker_ref": scope,
        "config_fragment": {
            "type": "zendure_mqtt",
            "serial_number": SERIAL,
            "mqtt": {
                "broker_ref": scope,
                "source": source,
                "topic_family": "zensdk_ha_scalar",
                "device_id": "ROUTE-" + scope,
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": False,
            },
        },
    }


_CLOUD = "zendure_cloud"
_CLOUD_SOURCE = "zendure_cloud_mqtt"


# --- Draft-authoritative candidate state ----------------------------------


def test_pristine_connection_dropped_from_draft_is_an_alternative():
    """1. pristine local_b1 + draft local_b2 + candidate local_b1 -> transport."""

    assert (
        _state(
            [_mqtt_device("local_b2")],
            _proposal("local_b1"),
            pristine=[_mqtt_device("local_b1")],
        )
        == "transport"
    )


def test_changed_draft_connection_is_the_selected_one():
    """2. pristine local_b1 + draft local_b2 + candidate local_b2 -> added."""

    assert (
        _state(
            [_mqtt_device("local_b2")],
            _proposal("local_b2"),
            pristine=[_mqtt_device("local_b1")],
        )
        == "added"
    )


def test_pristine_cloud_after_switch_to_api_is_an_alternative():
    """3. pristine Cloud + draft API + Cloud candidate -> transport."""

    assert (
        _state(
            [_api_device()],
            _proposal(_CLOUD, source=_CLOUD_SOURCE),
            pristine=[_mqtt_device(_CLOUD, source=_CLOUD_SOURCE)],
        )
        == "transport"
    )


def test_pristine_api_after_switch_to_cloud_keeps_cloud_selected():
    """4. pristine API + draft Cloud + Cloud candidate -> added, API stays open."""

    assert (
        _state(
            [_mqtt_device(_CLOUD, source=_CLOUD_SOURCE)],
            _proposal(_CLOUD, source=_CLOUD_SOURCE),
            pristine=[_api_device()],
        )
        == "added"
    )


def test_device_removed_from_draft_can_be_added_again():
    """5. pristine holds a device the operator removed -> its candidate is new."""

    assert (
        _state([], _proposal("local_b1"), pristine=[_mqtt_device("local_b1")]) == "new"
    )


def test_unchanged_configured_connection_stays_in_config():
    device = _mqtt_device("local_b1")
    assert _state([device], _proposal("local_b1"), pristine=[device]) == "found"


# --- Ambiguous identity is fail-closed ------------------------------------


def test_two_draft_devices_matching_one_alias_are_an_identity_conflict():
    """17. Two draft devices share the proposal's alias: no arbitrary target."""

    first = {
        "kind": "zendure_mqtt",
        "name": "INV_1",
        "physical_identity_token": "opaque:v1:aliasA",
        "physical_identity_alias_tokens": ["opaque:v1:aliasA"],
        "mqtt": {"broker_ref": "local_b1", "source": "local_mqtt"},
    }
    second = {
        "kind": "zendure_mqtt",
        "name": "INV_2",
        "physical_identity_token": "opaque:v1:aliasB",
        "physical_identity_alias_tokens": ["opaque:v1:aliasB"],
        "mqtt": {"broker_ref": "local_b2", "source": "local_mqtt"},
    }
    ambiguous = {
        "id": "mqtt:ambiguous",
        "connection_source": "local_mqtt",
        "broker_ref": "local_b3",
        "physical_identity_token": "opaque:v1:aliasA",
        "physical_identity_alias_tokens": ["opaque:v1:aliasA", "opaque:v1:aliasB"],
        "config_fragment": {"mqtt": {"broker_ref": "local_b3"}},
    }
    assert _state([first, second], ambiguous) == "identity_conflict"


def test_contradictory_serial_evidence_stays_an_identity_conflict():
    configured = {
        "kind": "zendure_mqtt",
        "name": "INV_1",
        "serial_number": "SERIAL-001",
        "physical_identity_token": "opaque:v1:route-1",
        "physical_identity_alias_tokens": ["opaque:v1:route-1"],
        "mqtt": {"broker_ref": "local_b1", "source": "local_mqtt"},
    }
    contradiction = {
        "id": "mqtt:contradiction",
        "serial_number": "SERIAL-002",
        "connection_source": "local_mqtt",
        "broker_ref": "local_b1",
        "physical_identity_token": "opaque:v1:route-1",
        "physical_identity_alias_tokens": ["opaque:v1:route-1"],
        "config_fragment": {"mqtt": {"broker_ref": "local_b1"}},
    }
    assert _state([configured], contradiction) == "identity_conflict"


# --- Reversible switching inside one discovery session --------------------

_COMMON = ("max_power", "min_soc", "pv_kwp")


def _switch_sequence(devices, pristine, steps):
    """Run consecutive switches, reporting candidate states after each step."""

    action = "const trace = [];\n"
    for target_source, context, probe in steps:
        action += (
            "trace.push({\n"
            "  changed: mconfigSwitchInverterTransport("
            + json.dumps(SERIAL)
            + ", "
            + json.dumps(target_source)
            + ", "
            + json.dumps(context)
            + "),\n"
            "  states: "
            + json.dumps(probe)
            + ".map((p) => mconfigMqttProposalState(p)),\n"
            "  devices: JSON.parse(JSON.stringify(mconfigState.draft.devices)),\n"
            "});\n"
        )
    action += "console.log(JSON.stringify(trace));"
    return _script(devices, pristine, action)


def _assert_single_preserved_inverter(devices, kind):
    assert len(devices) == 1
    device = devices[0]
    assert device["kind"] == kind
    assert device["original_name"] == "INV_1"
    assert device["name"] == "INV_1"
    assert device["enabled"] is True
    assert device["max_power"] == 642
    assert device["min_soc"] == 22
    # Untouched common values fall back to the central catalog default.
    assert device["pv_kwp"] == _CENTRAL["pv_kwp"]
    return device


def test_local_broker_b1_to_b2_and_back_in_one_session():
    """6/7. b1 -> b2 frees b1 immediately; b1 -> b2 -> b1 restores the route."""

    installed = _mqtt_device("local_b1")
    b1 = _proposal("local_b1")
    b2 = _proposal("local_b2")
    trace = _switch_sequence(
        [_mqtt_device("local_b1")],
        [installed],
        [
            ("local_mqtt", {"proposal": b2}, [b1, b2]),
            ("local_mqtt", {"proposal": b1}, [b1, b2]),
        ],
    )

    assert trace[0]["changed"] is True
    # b1 is free again without a rescan; b2 is the selected draft connection.
    assert trace[0]["states"] == ["transport", "added"]
    first = _assert_single_preserved_inverter(trace[0]["devices"], "zendure_mqtt")
    assert first["mqtt"]["broker_ref"] == "local_b2"
    assert first["mqtt"]["device_id"] == "ROUTE-local_b2"

    assert trace[1]["changed"] is True
    # Back on the installed connection: b1 reports the pristine config again.
    assert trace[1]["states"] == ["found", "transport"]
    second = _assert_single_preserved_inverter(trace[1]["devices"], "zendure_mqtt")
    assert second["mqtt"]["broker_ref"] == "local_b1"
    assert second["mqtt"]["device_id"] == "ROUTE-local_b1"


def test_api_to_zendure_mqtt_and_back_in_one_session():
    """8. API -> Zendure MQTT -> API works without leaving the session."""

    cloud = _proposal(_CLOUD, source=_CLOUD_SOURCE)
    trace = _switch_sequence(
        [_api_device()],
        [_api_device()],
        [
            ("zendure_mqtt", {"proposal": cloud}, [cloud]),
            (
                "local_api",
                {"discovered": {"ip": "192.168.1.100", "serial_number": SERIAL}},
                [cloud],
            ),
        ],
    )

    assert trace[0]["changed"] is True
    assert trace[0]["states"] == ["added"]
    switched = _assert_single_preserved_inverter(trace[0]["devices"], "zendure_mqtt")
    assert switched["mqtt"]["broker_ref"] == _CLOUD
    assert switched["mqtt"]["source"] == _CLOUD_SOURCE

    assert trace[1]["changed"] is True
    # Back on API: the Cloud connection is offered again straight away.
    assert trace[1]["states"] == ["transport"]
    restored = _assert_single_preserved_inverter(trace[1]["devices"], "local_api")
    assert restored["ip"] == "192.168.1.100"
    assert restored["sn"] == SERIAL


def test_zendure_mqtt_to_api_and_back_in_one_session():
    """9. Zendure MQTT -> API -> Zendure MQTT works in the same session."""

    installed = _mqtt_device(_CLOUD, source=_CLOUD_SOURCE)
    cloud = _proposal(_CLOUD, source=_CLOUD_SOURCE)
    trace = _switch_sequence(
        [_mqtt_device(_CLOUD, source=_CLOUD_SOURCE)],
        [installed],
        [
            (
                "local_api",
                {"discovered": {"ip": "192.168.1.100", "serial_number": SERIAL}},
                [cloud],
            ),
            ("zendure_mqtt", {"proposal": cloud}, [cloud]),
        ],
    )

    assert trace[0]["changed"] is True
    # The installed Cloud connection is immediately selectable again.
    assert trace[0]["states"] == ["transport"]
    _assert_single_preserved_inverter(trace[0]["devices"], "local_api")

    assert trace[1]["changed"] is True
    assert trace[1]["states"] == ["found"]
    back = _assert_single_preserved_inverter(trace[1]["devices"], "zendure_mqtt")
    assert back["mqtt"]["broker_ref"] == _CLOUD
    assert back["mqtt"]["source"] == _CLOUD_SOURCE


def test_ambiguous_alias_is_never_switched():
    """17. An ambiguous alias performs no draft mutation."""

    first = {
        "kind": "zendure_mqtt",
        "name": "INV_1",
        "sn": SERIAL,
        "mqtt": {"broker_ref": "local_b1", "source": "local_mqtt"},
    }
    second = {
        "kind": "zendure_mqtt",
        "name": "INV_2",
        "sn": SERIAL,
        "mqtt": {"broker_ref": "local_b2", "source": "local_mqtt"},
    }
    trace = _switch_sequence(
        [first, second],
        None,
        [("local_mqtt", {"proposal": _proposal("local_b3")}, [])],
    )
    assert trace[0]["changed"] is False
    assert trace[0]["devices"] == [first, second]
