# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connection labels and candidate cards in Setup and Maintenance.

Which state a discovered connection is in — new / active / alternative /
identity_conflict — is decided by ``admin/setup_planner.py`` and pinned in
``tests/test_admin_setup_batch_planner.py``. What is left here is the rendering:
one contextual action per state, the short user-facing labels (API / MQTT /
Zendure MQTT) in both flows, and the fact that a switch is addressed by an
issued id rather than by anything the card displays.

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


_STATE_HELPERS = (
    "emptySetupPlanOperations",
    "emptySetupPlan",
    "emptySetupPlanIndex",
    "indexSetupPlan",
    "issuedIdentityOf",
    "setupCandidateState",
    "mqttSourceOfConnection",
    "connectionLabelFor",
    "normalizeInverterAliasTokens",
    "inverterCandidateConnectionState",
)


def _state_env(draft_items, candidates):
    """The browser's state: its own draft rows plus the plan it was handed."""

    return (
        "let setupPlanIndex = emptySetupPlanIndex();\n"
        "indexSetupPlan(Object.assign(emptySetupPlan(), { candidates: "
        + json.dumps(candidates)
        + " }));\n"
        "let configDraftItems = " + json.dumps(draft_items) + ";\n"
        "const zendureMqttPreviewProposals = new Map();\n"
    )


_CONFIGURED_ROW = {
    "draft_item_id": "item-1",
    "role": "inverter",
    "config_name": "INV_1",
}


def _planned(candidate_id, state, **extra):
    return dict({"id": candidate_id, "state": state}, **extra)


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


# --- Setup candidate cards -----------------------------------------------
#
# The card renders the state the backend plan assigned to this candidate. The
# plan index is stubbed here exactly as the browser holds it, so the card is
# tested against the classification it is given rather than one it derives.

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
    "renderConnectionCandidateAction",
    "connectionCandidateNote",
    "renderConnectionPill",
    "renderMqttCandidateCard",
    "renderConfigAvailableCard",
)


def _render(fn_call, draft_items, candidates, payload):
    js = _read()
    helpers = "\n".join(_extract_fn(js, name) for name in _CARD_HELPERS)
    stub = (
        "const DEFAULT_INVERTER_DISPLAY = 'SolarFlow 800 Pro 2';\n"
        "const openHardwareCards = new Set();\n"
        "const SOURCE_LABELS = {};\n"
        "function normalizeDiscoverySource(value) { return String(value || ''); }\n"
    )
    script = (
        helpers
        + "\n"
        + stub
        + _state_env(draft_items, candidates)
        + "console.log(JSON.stringify("
        + fn_call
        + "("
        + json.dumps(payload)
        + ")));"
    )
    return _node(script)


_CLOUD_CANDIDATE = {
    "id": "zendure-cloud:PHYS-1",
    "connection_source": "zendure_cloud_mqtt",
    "broker_ref": "zendure_cloud",
}

_API_CANDIDATE = {
    "id": "zendure:PHYS-1",
    "observation_id": "obs:v1:api-phys-1",
    "api_family": "zendure",
    "role_suggestion": "inverter",
    "ip": "192.168.1.100",
}


def test_new_mqtt_candidate_offers_add_inverter():
    card = _render("renderMqttCandidateCard", [], [], _CLOUD_CANDIDATE)
    assert "Add inverter" in card
    assert "Use connection" not in card
    assert "Add as inverter" not in card
    assert "Zendure MQTT" in card


def test_alternative_mqtt_candidate_offers_use_connection():
    card = _render(
        "renderMqttCandidateCard",
        [_CONFIGURED_ROW],
        [
            _planned(
                "zendure-cloud:PHYS-1",
                "alternative",
                current_ref="item-1",
                current_source="local_api",
            )
        ],
        _CLOUD_CANDIDATE,
    )
    assert "Use connection" in card
    assert "Add inverter" not in card
    # The relationship note names the configured inverter and its connection.
    assert "Already configured as INV_1 via API" in card
    # The switch addresses the connection by its issued id — never a serial, a
    # route id or anything else the card displays.
    assert 'data-action="use-connection"' in card
    assert 'data-candidate-id="zendure-cloud:PHYS-1"' in card
    assert "identity-ref" not in card


def test_alternative_api_candidate_offers_use_connection():
    card = _render(
        "renderConfigAvailableCard",
        [_CONFIGURED_ROW],
        [
            _planned(
                "obs:v1:api-phys-1",
                "alternative",
                current_ref="item-1",
                current_source="zendure_mqtt",
            )
        ],
        _API_CANDIDATE,
    )
    assert "Use connection" in card
    assert "Already configured as INV_1 via Zendure MQTT" in card
    assert 'data-candidate-id="obs:v1:api-phys-1"' in card


def test_active_api_candidate_renders_active_and_no_action():
    card = _render(
        "renderConfigAvailableCard",
        [_CONFIGURED_ROW],
        [_planned("obs:v1:api-phys-1", "active", current_ref="item-1",
                  current_source="local_api")],
        _API_CANDIDATE,
    )
    assert "Active" in card
    assert "Use connection" not in card
    assert "Add inverter" not in card
    assert "disabled" in card


def test_conflicting_candidate_is_disabled():
    card = _render(
        "renderMqttCandidateCard",
        [_CONFIGURED_ROW],
        [_planned("zendure-cloud:PHYS-1", "identity_conflict")],
        _CLOUD_CANDIDATE,
    )
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
    common_values = {"max_power": 642, "min_soc": 22, "pv_kwp": 3.2}
    entry = {
        "id": "zendure-cloud:PHYS-1",
        "connection_source": "zendure_cloud_mqtt",
        "broker_ref": "zendure_cloud",
        "config_fragment": {"type": "zendure_mqtt"},
        "config_name": "INV_1",
        "enabled": False,
        "config_values": dict(common_values),
    }
    out = _node(
        helpers
        + "\nconsole.log(JSON.stringify(serializeMqttProposalSelection("
        + json.dumps(entry)
        + ", { target: 'device' })));"
    )
    assert out["config_values"] == common_values
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


# --- confirmation follows the planner, never the card ---------------------


def test_connection_switching_never_opens_a_confirmation():
    """Nothing is installed yet: the click on a draft is the operator's answer."""

    js = _read()
    for name in (
        "switchInverterTransport",
        "applyConnectionSwitch",
        "mconfigSwitchInverterTransport",
        "renderMqttCandidateCard",
        "renderConfigAvailableCard",
        "renderMaintenanceMqttProposalCard",
        "renderConnectionCandidateAction",
    ):
        source = _extract_fn(js, name)
        assert "confirm(" not in source, name
        assert "window.confirm" not in source, name


def test_a_refused_switch_reports_the_planner_reason():
    js = _read()
    source = _extract_fn(js, "switchInverterTransport")
    assert 'verdict.action !== "use_candidate"' in source
    assert "connectionSwitchBlockedText(verdict)" in source
    reasons = js.split("const CONNECTION_SWITCH_REASONS", 1)[1].split("};", 1)[0]
    for reason in (
        "contradictory_physical_serials",
        "ambiguous_mqtt_write_route",
        "candidate_cannot_write_output_limit",
    ):
        assert reason in reasons


def test_use_connection_handler_switches_through_the_existing_helper():
    js = _read()
    handler = js.split("configEls.availableList.addEventListener", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert 'data-action="use-connection"' in handler
    assert "switchInverterTransport(" in handler
    # The alternative connection is never routed through the add path.
    assert "addMqttInverterFromCandidate(useConnection" not in handler
