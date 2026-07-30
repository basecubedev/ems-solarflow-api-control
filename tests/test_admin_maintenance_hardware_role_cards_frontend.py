# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance reuses the one shared hardware-role card system.

Guided Setup, Maintenance discovery and the Maintenance draft editor resolve a
card's colour through the same helpers: the hardware role decides the class, the
connection source only decides a label, a pill and a data attribute. A
configured MQTT inverter therefore looks exactly like a configured Local API
inverter, and an MQTT device the backend did not classify stays neutral.

The real admin.js renderers are executed against a minimal DOM shim
(tests/js/maintenance_card_runner.js) so the contract is tested against the
shipped code and not a rebuilt copy.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.simulation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "admin", "static")
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_card_runner.js")


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


def _render(card, payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the maintenance hardware role tests")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"card": card, "payload": payload}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _proposal_card(proposal, state="new"):
    return _render("mqtt_proposal", {"state": state, "mqttProposal": proposal})


def _hardware_card(**options):
    options.setdefault("id", "maintenance-card-1")
    options.setdefault("title", "Inverter 1")
    options.setdefault("model", "Zendure SolarFlow inverter")
    options.setdefault("meta", "SN-1")
    options.setdefault("enabled", True)
    return _render("hardware", options)


# --- proposal fixtures ---------------------------------------------------


def _mqtt_inverter(source="local_mqtt", **extra):
    proposal = {
        "id": "zendure-mqtt:" + source + ":INV",
        "connection_source": source,
        "display_name": "MQTT inverter",
        "serial_number": "SN-INV",
        "device_id": "DEV-INV",
        "target": "device",
        "role_hint": "battery_inverter_candidate",
        "output_control_supported": True,
        "config_fragment": {"mqtt": {"write_protocol": "legacy_properties_write"}},
    }
    proposal.update(extra)
    return proposal


def _mqtt_grid_meter(source="local_mqtt", **extra):
    proposal = {
        "id": "zendure-mqtt:" + source + ":D0",
        "connection_source": source,
        "display_name": "MQTT smart meter",
        "serial_number": "SN-D0",
        "target": "grid_meter",
        "role_hint": "grid_meter_candidate",
        "output_control_supported": False,
        "grid_meter_fragment": {
            "mqtt": {"topic": "Zendure/sensor/D0/totalPower", "broker_ref": "default"}
        },
    }
    proposal.update(extra)
    return proposal


def _mqtt_unknown(source="local_mqtt", role_hint="unknown_candidate", **extra):
    proposal = {
        "id": "zendure-mqtt:" + source + ":UNK",
        "connection_source": source,
        "display_name": "MQTT device",
        "serial_number": "SN-UNK",
        "target": "device",
        "role_hint": role_hint,
        "output_control_supported": False,
        "config_fragment": {"mqtt": {}},
    }
    proposal.update(extra)
    return proposal


# --- Maintenance discovery proposals -------------------------------------


@pytest.mark.parametrize("source", ["local_mqtt", "zendure_cloud_mqtt"])
def test_maintenance_mqtt_inverter_proposal_is_an_inverter_card(source):
    card = _proposal_card(_mqtt_inverter(source))
    assert "hardware-card" in card["classes"]
    assert "hardware-card-inverter" in card["classes"]
    assert "hardware-card-grid-meter" not in card["classes"]
    assert card["dataset"]["role"] == "inverter"


@pytest.mark.parametrize("source", ["local_mqtt", "zendure_cloud_mqtt"])
def test_maintenance_mqtt_grid_meter_proposal_is_a_grid_meter_card(source):
    card = _proposal_card(_mqtt_grid_meter(source))
    assert "hardware-card" in card["classes"]
    assert "hardware-card-grid-meter" in card["classes"]
    assert "hardware-card-inverter" not in card["classes"]
    assert card["dataset"]["role"] == "grid_meter"


@pytest.mark.parametrize("role_hint", ["unknown_candidate", "telemetry_only_candidate"])
@pytest.mark.parametrize("source", ["local_mqtt", "zendure_cloud_mqtt"])
def test_maintenance_unclassified_mqtt_proposal_stays_neutral(source, role_hint):
    card = _proposal_card(_mqtt_unknown(source, role_hint))
    assert "hardware-card" in card["classes"]
    assert "hardware-card-inverter" not in card["classes"]
    assert "hardware-card-grid-meter" not in card["classes"]
    assert card["dataset"]["role"] == "unknown"


def test_maintenance_telemetry_only_inverter_keeps_the_inverter_card():
    card = _proposal_card(
        _mqtt_inverter(
            "zendure_cloud_mqtt",
            output_control_supported=False,
            control_block_reason="mqtt_device_id_missing",
        )
    )
    assert "hardware-card-inverter" in card["classes"]
    assert "Telemetry only" in card["text"]


def test_maintenance_connection_source_never_changes_the_role_class():
    for factory in (_mqtt_inverter, _mqtt_grid_meter, _mqtt_unknown):
        local = _proposal_card(factory("local_mqtt"))
        cloud = _proposal_card(factory("zendure_cloud_mqtt"))
        assert local["classes"] == cloud["classes"], factory.__name__
        assert local["dataset"]["role"] == cloud["dataset"]["role"]


def test_maintenance_proposal_transport_stays_visible_and_distinguishable():
    local = _proposal_card(_mqtt_inverter("local_mqtt"))
    cloud = _proposal_card(_mqtt_inverter("zendure_cloud_mqtt"))
    assert local["dataset"]["connection"] == "local_mqtt"
    assert cloud["dataset"]["connection"] == "zendure_mqtt"
    assert local["transportPill"] == {"text": "MQTT", "connection": "local_mqtt"}
    assert cloud["transportPill"] == {
        "text": "Zendure MQTT",
        "connection": "zendure_mqtt",
    }
    assert "Transport" in local["text"]


def test_maintenance_proposal_keeps_its_facts_and_action():
    controllable = _proposal_card(_mqtt_inverter())
    for expected in ("Device/SN", "Hardware generation", "Transport", "Output control"):
        assert expected in controllable["text"], expected
    assert "Write protocol" in controllable["text"]
    blocked = _proposal_card(
        _mqtt_inverter(output_control_supported=False, control_block_reason="identity_conflict")
    )
    assert "Reason" in blocked["text"]
    assert "Add inverter" in controllable["text"]
    switch = _proposal_card(_mqtt_inverter(), state="transport")
    assert "Use connection" in switch["text"]


# --- configured / draft Maintenance devices ------------------------------


@pytest.mark.parametrize("connection", ["local_api", "local_mqtt", "zendure_mqtt"])
def test_configured_inverters_share_the_inverter_card_across_transports(connection):
    card = _hardware_card(role="inverter", connectionSource=connection)
    assert "hardware-card" in card["classes"]
    assert "hardware-card-inverter" in card["classes"]
    assert card["dataset"]["connection"] == connection
    # Transport stays visible next to the role, never instead of it.
    assert card["transportPill"]["connection"] == connection


def test_configured_mqtt_inverter_looks_like_the_api_inverter():
    api = _hardware_card(role="inverter", connectionSource="local_api")
    mqtt = _hardware_card(role="inverter", connectionSource="zendure_mqtt")
    assert api["classes"] == mqtt["classes"]
    assert api["transportPill"]["text"] != mqtt["transportPill"]["text"]


@pytest.mark.parametrize("connection", ["local_api", "local_mqtt", "zendure_mqtt"])
def test_configured_grid_meters_share_the_grid_meter_card(connection):
    card = _hardware_card(
        role="grid_meter", title="Grid meter", connectionSource=connection
    )
    assert "hardware-card-grid-meter" in card["classes"]
    assert "hardware-card-inverter" not in card["classes"]
    assert card["dataset"]["connection"] == connection


def test_maintenance_card_without_a_known_role_stays_neutral():
    card = _hardware_card(role="unknown")
    assert card["classes"] == ["hardware-card"]
    assert "connection" not in card["dataset"]


# --- one shared mapping ---------------------------------------------------


def test_maintenance_renderers_use_the_shared_card_class_builder():
    js = _read()
    for name in ("mconfigHardwareCard", "renderMaintenanceMqttProposalCard"):
        fn = _extract_fn(js, name)
        assert "hardwareCardClass(" in fn, name
        assert "hardware-card hardware-card-" not in fn, name
    proposal = _extract_fn(js, "renderMaintenanceMqttProposalCard")
    assert "mqttProposalHardwareRole(proposal)" in proposal


def test_no_parallel_maintenance_role_resolver_exists():
    js = _read()
    for forbidden in (
        "maintenanceHardwareRole",
        "maintenanceMqttCardClass",
        "mqttMaintenanceKind",
        "mconfigHardwareRole",
        "mconfigCardClass",
    ):
        assert forbidden not in js, forbidden
    # One card-class builder for the whole Admin UI.
    assert js.count("function hardwareCardClass(") == 1
    assert js.count("function hardwareCardKindForRole(") == 1
    assert js.count("function mqttProposalHardwareRole(") == 1


def test_no_transport_named_hardware_card_class_remains():
    js = _read()
    html = _read("index.html")
    css = _read("admin.css")
    for forbidden in (
        "hardware-card-zendure-mqtt",
        "hardware-card-local-mqtt",
        "hardware-card-mqtt",
        "hardware-card-local-api",
    ):
        assert forbidden not in js, forbidden
        assert forbidden not in html, forbidden
        assert forbidden not in css, forbidden


def test_maintenance_role_and_transport_are_separate_card_metadata():
    js = _read()
    card = _extract_fn(js, "mconfigHardwareCard")
    assert "hardwareCardClass(options.role)" in card
    assert "options.connectionSource" in card
    # The transport never feeds the class builder.
    assert "hardwareCardClass(options.connectionSource)" not in card


# --- CSS stays the single role-colour source -----------------------------


def _css_block(css, selector):
    assert selector + " {" in css, selector
    return css.split(selector + " {", 1)[1].split("}", 1)[0]


def test_maintenance_css_never_declares_role_colours():
    css = _read("admin.css")
    block = _css_block(css, ".mconfig-discovery-proposal-card")
    for forbidden in ("border", "background", "var(--output)", "var(--grid)"):
        assert forbidden not in block, forbidden
    # The two shared classes remain the only role-colour source.
    assert "var(--output)" in _css_block(css, ".hardware-card-inverter")
    assert "var(--grid)" in _css_block(css, ".hardware-card-grid-meter")
    for forbidden in (
        ".mconfig-hardware-card-inverter",
        ".mconfig-discovery-device-card.hardware-card-inverter",
        ".maintenance-inverter-card",
        ".mconfig-mqtt-card",
    ):
        assert forbidden not in css, forbidden
