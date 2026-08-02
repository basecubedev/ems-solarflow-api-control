# SPDX-License-Identifier: AGPL-3.0-or-later
"""One hardware-card visual system for Local API, Local MQTT and Zendure MQTT.

The card colour is owned by the hardware role, never by the transport a device
was discovered over: a recognized inverter is blue and a recognized grid meter
purple from either MQTT source, exactly as a Local API candidate. Anything the
backend did not positively classify stays neutral instead of being coloured as
an inverter.

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
        pytest.skip("node is required for the hardware role card tests")
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_ROLE_HELPERS = (
    "hardwareCardKindForRole",
    "hardwareCardClass",
    "isMqttGridMeterProposal",
    "mqttProposalHardwareRole",
)

_PROPOSAL_CARD_HELPERS = _ROLE_HELPERS + (
    "escapeHtml",
    "fact",
    "mqttSourceOfConnection",
    "connectionLabelFor",
    "mqttTransportLabel",
    "mqttGenerationLabel",
    "mqttGridMeterProposalTopic",
    "mqttWriteProtocolLabel",
    "mqttControlReasonLabel",
    "mqttProposalControlReason",
    "mqttProposalWriteProtocol",
    "renderMqttProposalPill",
    "isMqttPreviewProposalSelected",
    "renderMqttProposalCard",
)

_AVAILABLE_CARD_HELPERS = _ROLE_HELPERS + (
    "escapeHtml",
    "fact",
    "observationKey",
    "renderConfigAvailableCard",
)

# Catalog/state the two renderers read but that this contract does not exercise.
_PROPOSAL_ENV = """
const zendureMqttPreviewProposals = new Map();
function generationLabel(id) { return id || ""; }
"""

_AVAILABLE_ENV = """
const openHardwareCards = new Set();
function draftHasSource() { return false; }
function inverterCandidateConnectionState() {
  return { state: "new", configuredName: "", currentSource: null };
}
function renderConnectionCandidateAction(state, addButton) { return addButton; }
function renderConnectionPill(source) {
  return '<span class="connection-pill" data-connection="' + source + '"></span>';
}
function connectionCandidateNote() { return ""; }
function sourceBadges() { return ""; }
"""


def _render(helpers, env, call):
    js = _read()
    script = (
        "\n".join(_extract_fn(js, name) for name in helpers)
        + "\n"
        + env
        + "\nconsole.log(JSON.stringify(" + call + "));"
    )
    return _node(script)


def _render_proposal_card(proposal):
    return _render(
        _PROPOSAL_CARD_HELPERS,
        _PROPOSAL_ENV,
        "renderMqttProposalCard(" + json.dumps(proposal) + ")",
    )


def _render_available_card(device):
    return _render(
        _AVAILABLE_CARD_HELPERS,
        _AVAILABLE_ENV,
        "renderConfigAvailableCard(" + json.dumps(device) + ")",
    )


def _root_classes(html):
    """The class list of the rendered card's root <article>."""

    assert html.startswith("<article class=\""), html[:120]
    return html.split('"', 2)[1].split()


def _resolve_roles(proposals):
    js = _read()
    script = (
        "\n".join(_extract_fn(js, name) for name in _ROLE_HELPERS)
        + "\nconsole.log(JSON.stringify("
        + json.dumps(proposals)
        + ".map(mqttProposalHardwareRole)));"
    )
    return _node(script)


# --- proposal fixtures ---------------------------------------------------


def _inverter_proposal(source="local_mqtt", **extra):
    proposal = {
        "id": "zendure-mqtt:" + source + ":INV",
        "serial_number": "SN-INV",
        "device_id": "DEV-INV",
        "target": "device",
        "connection_source": source,
        "display_name": "Zendure MQTT inverter",
        "confidence": "high",
        "role_hint": "battery_inverter_candidate",
        "capabilities": ["battery_storage", "output_control"],
        "metrics": ["outputLimit", "electricLevel"],
        "warnings": [],
        "output_control_supported": True,
        "config_fragment": {
            "type": "zendure_mqtt",
            "mqtt": {"device_id": "DEV-INV", "write_protocol": "legacy_properties_write"},
        },
    }
    proposal.update(extra)
    return proposal


def _grid_meter_proposal(source="local_mqtt", **extra):
    proposal = {
        "id": "zendure-mqtt:" + source + ":D0",
        "serial_number": "SN-D0",
        "target": "grid_meter",
        "connection_source": source,
        "display_name": "Zendure MQTT smart meter",
        "confidence": "high",
        "role_hint": "grid_meter_candidate",
        "capabilities": [],
        "metrics": ["totalPower"],
        "warnings": [],
        "output_control_supported": False,
        "grid_meter_fragment": {
            "type": "zendure_smartmeter_d0",
            "mqtt": {"topic": "Zendure/sensor/D0/totalPower", "broker_ref": "default"},
        },
    }
    proposal.update(extra)
    return proposal


def _unknown_proposal(source="local_mqtt", role_hint="unknown_candidate", **extra):
    proposal = {
        "id": "zendure-mqtt:" + source + ":UNK",
        "serial_number": "SN-UNK",
        "target": "device",
        "connection_source": source,
        "display_name": "Zendure MQTT device",
        "confidence": "low",
        "role_hint": role_hint,
        "capabilities": [],
        "metrics": ["someMetric"],
        "warnings": ["insufficient_telemetry"],
        "output_control_supported": False,
        "config_fragment": {"type": "zendure_mqtt", "mqtt": {}},
    }
    proposal.update(extra)
    return proposal


# --- the shared role resolver --------------------------------------------


def test_role_resolver_maps_the_authoritative_backend_fields():
    roles = _resolve_roles(
        [
            {"connection_source": "local_mqtt", "role_hint": "battery_inverter_candidate"},
            {
                "connection_source": "zendure_cloud_mqtt",
                "role_hint": "battery_inverter_candidate",
            },
            {
                "connection_source": "local_mqtt",
                "target": "grid_meter",
                "role_hint": "grid_meter_candidate",
                "grid_meter_fragment": {"mqtt": {"topic": "Zendure/sensor/D0/totalPower"}},
            },
            {"connection_source": "zendure_cloud_mqtt", "role_hint": "unknown_candidate"},
            {"connection_source": "local_mqtt", "role_hint": "telemetry_only_candidate"},
        ]
    )
    assert roles == ["inverter", "inverter", "grid_meter", "unknown", "unknown"]


def test_role_resolver_never_guesses_from_name_model_serial_or_topic():
    # Every non-authoritative hint says "inverter"; without a positive role the
    # answer stays unknown.
    roles = _resolve_roles(
        [
            {
                "connection_source": "zendure_cloud_mqtt",
                "display_name": "Zendure SolarFlow 800 Pro 2 inverter",
                "hardware_model": "solarflow_800_pro_2",
                "serial_number": "INVERTER-1",
                "seen_topics": ["iot/inverter/outputLimit"],
                "metrics": ["outputLimit", "packInputPower"],
                "capabilities": ["battery_storage", "output_control"],
                "output_control_supported": True,
            },
            {},
            None,
        ]
    )
    assert roles == ["unknown", "unknown", "unknown"]


def test_role_resolver_reuses_the_grid_meter_predicate():
    helper = _extract_fn(_read(), "mqttProposalHardwareRole")
    assert "isMqttGridMeterProposal(" in helper
    # No second grid-meter predicate is reimplemented inside the adapter.
    assert "grid_meter_fragment" not in helper


def test_grid_meter_role_survives_a_weak_d0_target():
    # A grid meter whose D0 topic was not safely observed stays a device target
    # (backend behavior), but the hardware itself is still a grid meter.
    roles = _resolve_roles(
        [{"connection_source": "local_mqtt", "target": "device", "role_hint": "grid_meter_candidate"}]
    )
    assert roles == ["grid_meter"]


def test_connection_source_alone_never_changes_the_role():
    for factory in (_inverter_proposal, _grid_meter_proposal, _unknown_proposal):
        local, cloud = _resolve_roles(
            [factory("local_mqtt"), factory("zendure_cloud_mqtt")]
        )
        assert local == cloud, factory.__name__


def test_card_kind_helper_only_knows_the_two_role_colours():
    js = _read()
    kinds = _node(
        _extract_fn(js, "hardwareCardKindForRole")
        + "\nconsole.log(JSON.stringify(["
        "hardwareCardKindForRole('inverter'),"
        "hardwareCardKindForRole('grid_meter'),"
        "hardwareCardKindForRole('unknown'),"
        "hardwareCardKindForRole('telemetry_only'),"
        "hardwareCardKindForRole(''),"
        "hardwareCardKindForRole(undefined)]));"
    )
    assert kinds == ["inverter", "grid-meter", None, None, None, None]


# --- rendered proposal cards ---------------------------------------------


def test_proposal_card_uses_the_shared_hardware_card_base():
    classes = _root_classes(_render_proposal_card(_unknown_proposal()))
    assert "hardware-card" in classes
    # The old proposal-only shell is gone; the proposal class keeps content
    # layout only.
    assert "mqtt-device-card" not in classes
    assert "mqtt-proposal-card" in classes


@pytest.mark.parametrize("source", ["local_mqtt", "zendure_cloud_mqtt"])
def test_recognized_inverter_proposals_use_the_inverter_role_class(source):
    classes = _root_classes(_render_proposal_card(_inverter_proposal(source)))
    assert "hardware-card" in classes
    assert "hardware-card-inverter" in classes
    assert "hardware-card-grid-meter" not in classes


@pytest.mark.parametrize("source", ["local_mqtt", "zendure_cloud_mqtt"])
def test_recognized_grid_meter_proposals_use_the_grid_meter_role_class(source):
    classes = _root_classes(_render_proposal_card(_grid_meter_proposal(source)))
    assert "hardware-card" in classes
    assert "hardware-card-grid-meter" in classes
    assert "hardware-card-inverter" not in classes


@pytest.mark.parametrize("role_hint", ["unknown_candidate", "telemetry_only_candidate"])
@pytest.mark.parametrize("source", ["local_mqtt", "zendure_cloud_mqtt"])
def test_unclassified_proposals_stay_neutral(source, role_hint):
    classes = _root_classes(_render_proposal_card(_unknown_proposal(source, role_hint)))
    assert "hardware-card" in classes
    assert "hardware-card-inverter" not in classes
    assert "hardware-card-grid-meter" not in classes


def test_telemetry_only_inverter_keeps_the_inverter_colour():
    # Output-control capability is separate from hardware role: a positively
    # identified inverter stays blue even when it cannot be controlled.
    proposal = _inverter_proposal(
        "zendure_cloud_mqtt",
        output_control_supported=False,
        control_block_reason="mqtt_device_id_missing",
    )
    html = _render_proposal_card(proposal)
    assert "hardware-card-inverter" in _root_classes(html)
    assert "Telemetry only" in html


def test_local_and_cloud_inverter_cards_differ_only_in_the_transport_label():
    local = _render_proposal_card(_inverter_proposal("local_mqtt"))
    cloud = _render_proposal_card(_inverter_proposal("zendure_cloud_mqtt"))
    assert _root_classes(local)[:2] == _root_classes(cloud)[:2]
    assert ">MQTT<" in local and ">Zendure MQTT<" in cloud


def test_proposal_card_keeps_its_facts_actions_and_fragment_preview():
    html = _render_proposal_card(_inverter_proposal())
    for expected in (
        "Device/SN",
        "Hardware generation",
        "Transport",
        "Output control",
        "Write protocol",
        "Role hint",
        "Capabilities",
        "Metrics seen",
        "Add to config preview",
        "Config fragment (preview)",
    ):
        assert expected in html, expected
    grid = _render_proposal_card(_grid_meter_proposal())
    assert "Use as grid meter" in grid
    assert "Zendure/sensor/D0/totalPower" in grid


def test_proposal_card_escapes_every_dynamic_value():
    hostile = '"><script>alert(1)</script>'
    proposal = _inverter_proposal(
        "local_mqtt",
        display_name=hostile,
        serial_number=hostile,
        confidence=hostile,
        role_hint="battery_inverter_candidate",
        capabilities=[hostile],
        metrics=[hostile],
        warnings=[hostile],
        hardware_generation_label=hostile,
    )
    html = _render_proposal_card(proposal)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    # The role class is still resolved from the authoritative field, not the
    # hostile display values.
    assert "hardware-card-inverter" in _root_classes(html)


# --- the API renderer shares the same contract ---------------------------


def test_api_candidate_cards_resolve_through_the_same_helper():
    inverter = _render_available_card(
        {
            "serial_number": "API-1",
            "role_suggestion": "inverter",
            "ip": "192.168.1.10",
            "api_family": "zendure_local",
            "device_type": "solarflow",
            "usable_for_config": True,
        }
    )
    meter = _render_available_card(
        {
            "serial_number": "API-2",
            "role_suggestion": "grid_meter",
            "ip": "192.168.1.11",
            "api_family": "shelly_gen2",
            "device_type": "shelly_pro_3em",
            "usable_for_config": True,
        }
    )
    assert "hardware-card-inverter" in _root_classes(inverter)
    assert "hardware-card-grid-meter" in _root_classes(meter)


def test_api_and_mqtt_renderers_share_one_card_class_builder():
    js = _read()
    for name in (
        "renderConfigAvailableCard",
        "renderMqttProposalCard",
        "renderMqttCandidateCard",
        "renderHardwareCard",
        "renderUnifiedDeviceCard",
    ):
        fn = _extract_fn(js, name)
        assert "hardwareCardClass(" in fn, name
        # No renderer builds the role class inline any more.
        assert "hardware-card hardware-card-" not in fn, name
    assert "hardwareCardClass(mqttProposalHardwareRole(" in _extract_fn(
        js, "renderMqttProposalCard"
    )


def test_no_transport_specific_role_mapping_helper_exists():
    js = _read()
    for forbidden in (
        "localMqttCardClass",
        "zendureMqttCardClass",
        "mqttInverterCardStyle",
        "mqttGridMeterCardStyle",
    ):
        assert forbidden not in js, forbidden


# --- CSS stays the single role-colour source -----------------------------


def _css_block(css, selector):
    assert selector + " {" in css, selector
    return css.split(selector + " {", 1)[1].split("}", 1)[0]


def test_role_colours_live_only_on_the_shared_hardware_card_classes():
    css = _read("admin.css")
    assert "var(--output)" in _css_block(css, ".hardware-card-inverter")
    assert "var(--grid)" in _css_block(css, ".hardware-card-grid-meter")
    for forbidden in (
        ".mqtt-proposal-inverter",
        ".mqtt-proposal-grid-meter",
        ".local-mqtt-inverter-card",
        ".zendure-mqtt-inverter-card",
        ".mqtt-device-card-inverter",
        ".mqtt-device-card-grid-meter",
    ):
        assert forbidden not in css, forbidden


def test_proposal_card_css_never_overrides_the_hardware_role():
    block = _css_block(_read("admin.css"), ".mqtt-proposal-card")
    for forbidden in ("border", "background", "var(--output)", "var(--grid)", "var(--accent2)"):
        assert forbidden not in block, forbidden
