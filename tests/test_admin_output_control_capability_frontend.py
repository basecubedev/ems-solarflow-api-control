# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write eligibility is decided by EMS/Core, never re-derived in the browser.

``power_capability`` decides from the pinned hardware profile and the observed
topic family, and ships the verdict as ``output_control_supported`` /
``control_block_reason`` on the proposal and ``capabilities.write_output_limit``
in its fragment. The Admin frontend may render that verdict; it must not
compute a competing one (see ``docs/developer/agent-rules.md`` §2/§3).

The shipped predicates are executed through tests/js/output_control_capability_runner.js
so the contract holds against admin.js and not a rebuilt copy.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, "admin", "static")
RUNNER = os.path.join(ROOT, "tests", "js", "output_control_capability_runner.js")

# The whole current SolarFlow line: every registry model is control capable while
# the generation's *default* transport is scalar, so a generation-level answer
# contradicts the model on nine of thirteen control-capable models.
ZENSDK_GENERATION = {"id": "solarflow_zensdk", "supports_output_control": False}
LEGACY_GENERATION = {"id": "hub_hyper_legacy", "supports_output_control": True}
PRO_2_MODEL = {
    "id": "solarflow_800_pro_2",
    "control_supported": True,
    "power_write_profile": "zensdk_properties_write",
}
UNCONTROLLABLE_MODEL = {"id": "ace_1500", "control_supported": False}

CATALOG = {"default_device": {"common": {}}}


def _read_admin_js():
    with open(os.path.join(STATIC_DIR, "admin.js"), encoding="utf-8") as handle:
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


def _run(payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the output-control capability tests")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _proposal(**overrides):
    """A trusted proposal the backend classified as control capable.

    The browser-facing fragment carries no route id and no product key: raw
    identifiers never cross the HTTP boundary, the server restores them from
    in-memory discovery at preview/apply.
    """

    proposal = {
        "id": "zendure-mqtt:opaque:v1:AAAA:zendure_cloud",
        "serial_number": "TESTSN000001",
        "device_id": "TESTSN000001",
        "broker_ref": "zendure_cloud",
        "hardware_generation": "solarflow_zensdk",
        "hardware_model": "solarflow_800_pro_2",
        "output_control_supported": True,
        "control_block_reason": None,
        "output_control_reason": "zensdk_properties_write",
        "config_fragment": {
            "serial_number": "TESTSN000001",
            "power_write_profile": "zensdk_properties_write",
            "mqtt": {
                "broker_ref": "zendure_cloud",
                "source": "zendure_cloud_mqtt",
                "topic_family": "legacy_zendure_json_alt",
                "base_topic": None,
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": True,
            },
        },
    }
    proposal.update(overrides)
    return proposal


def _telemetry_only_proposal():
    proposal = _proposal(
        output_control_supported=False,
        control_block_reason="transport_incompatible",
        output_control_reason="transport_incompatible",
    )
    proposal["config_fragment"]["mqtt"]["topic_family"] = "zendure_cloud_scalar"
    proposal["config_fragment"]["capabilities"]["write_output_limit"] = False
    return proposal


# --- the capability predicate ----------------------------------------------


def test_a_pinned_control_capable_model_is_control_capable():
    """The concrete model is the authority; the generation flag is not.

    ``generation_supports_output_control`` only reports whether the
    generation's *default* topic family is a JSON family, and its own docstring
    says a generation never authorizes a write. Letting it override a pinned,
    control-capable model is the browser deciding write eligibility.
    """

    answer = _run(
        {
            "scenario": "capability",
            "device": {"mqtt": {}},
            "generation": ZENSDK_GENERATION,
            "model": PRO_2_MODEL,
        }
    )

    assert answer["supported"] is True


def test_a_model_that_cannot_control_stays_uncontrollable_on_a_json_generation():
    """Fail-closed direction: a permissive generation never lifts a model."""

    answer = _run(
        {
            "scenario": "capability",
            "device": {"mqtt": {}},
            "generation": LEGACY_GENERATION,
            "model": UNCONTROLLABLE_MODEL,
        }
    )

    assert answer["supported"] is False


def test_without_a_pinned_model_nothing_is_control_capable():
    """No model, no answer: an unknown state must never default to enabled."""

    for model in (None, {}, {"id": "", "control_supported": True}):
        answer = _run(
            {
                "scenario": "capability",
                "device": {"mqtt": {}},
                "generation": LEGACY_GENERATION,
                "model": model,
            }
        )
        assert answer["supported"] is False, model


# --- transport switch and proposal add --------------------------------------


def test_switching_a_controlling_device_to_a_writable_mqtt_connection_keeps_control():
    """The reported defect: control is set by the switch and must survive it."""

    answer = _run(
        {
            "scenario": "switch",
            "proposal": _proposal(),
            "current": {"kind": "local_api", "name": "INV_2", "enabled": True},
            "generation": ZENSDK_GENERATION,
            "model": PRO_2_MODEL,
            "catalog": CATALOG,
        }
    )

    assert answer["output_control"] is True
    assert answer["write_output_limit"] is True
    assert answer["supported"] is True, (
        "a render that recomputes capability would clear output control again"
    )
    assert answer["enabled"] is True
    assert answer["active"] is True


def test_switching_to_a_telemetry_only_connection_does_not_claim_control():
    """The counter-case must not move: a scalar transport stays telemetry only."""

    answer = _run(
        {
            "scenario": "switch",
            "proposal": _telemetry_only_proposal(),
            "current": {"kind": "local_api", "name": "INV_2", "enabled": True},
            "generation": ZENSDK_GENERATION,
            "model": PRO_2_MODEL,
            "catalog": CATALOG,
        }
    )

    assert answer["output_control"] is not True
    assert answer["write_output_limit"] is not True


def test_adding_a_writable_proposal_arrives_control_capable():
    answer = _run(
        {
            "scenario": "proposal_add",
            "proposal": _proposal(),
            "generation": ZENSDK_GENERATION,
            "model": PRO_2_MODEL,
            "catalog": CATALOG,
        }
    )

    assert answer["output_control"] is True
    assert answer["write_output_limit"] is True
    assert answer["supported"] is True


# --- the route id is never invented in the browser ---------------------------


def test_a_proposal_device_never_carries_the_serial_as_its_route_id():
    """A physical serial is not an MQTT route id, and the browser has neither.

    The fragment omits the route id on purpose; falling back to the serial
    would let the browser present an invented identifier as the write route,
    which the render guard then reads as a complete route.
    """

    answer = _run(
        {
            "scenario": "proposal_add",
            "proposal": _proposal(),
            "generation": ZENSDK_GENERATION,
            "model": PRO_2_MODEL,
            "catalog": CATALOG,
        }
    )

    assert answer["route_device_id"] != "TESTSN000001"
    assert not answer["route_device_id"]
    assert answer["trusted_write_target"] is True, (
        "the trusted proposal, not a browser-held route id, proves the write target"
    )


# --- Guided Setup uses the same authority ------------------------------------


def test_setup_manual_mqtt_control_is_not_gated_on_the_generation_flag():
    """Fresh Install's manual MQTT path shares the defect and the fix.

    Both the field visibility and the recorded intent gate on the generation,
    so a zensdk model cannot be given output control at all in Setup.
    """

    js = _read_admin_js()

    for name in (
        "syncMqttGenerationDetails",
        "addManualMqttDevice",
        "manualMqttControlAvailable",
    ):
        assert "generation.supports_output_control" not in _extract_fn(js, name), (
            f"{name} still decides write eligibility from the generation flag"
        )
    assert "control_supported" in _extract_fn(js, "manualMqttControlAvailable"), (
        "the pinned concrete model must remain the authority"
    )


# --- a telemetry-only connection is not an equivalent replacement ------------


def _card(payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the output-control capability tests")
    result = subprocess.run(
        [node, os.path.join(ROOT, "tests", "js", "maintenance_card_runner.js")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _controlling_api_device():
    return {
        "kind": "local_api",
        "name": "INV_2",
        "enabled": True,
        "serial_number": "TESTSN000001",
        "sn": "TESTSN000001",
    }


def _candidate(proposal):
    return {
        "state": "transport",
        "mqttProposal": proposal,
        "configured": _controlling_api_device(),
    }


def test_a_telemetry_only_connection_names_the_replacement_it_performs():
    """The action replaces the transport, so it must not read as an addition.

    "Add as telemetry source" described something the action never did: it
    delegates to the transport switch and removes the controlling entry.
    """

    proposal = _telemetry_only_proposal()
    proposal["connection_source"] = "zendure_cloud_mqtt"
    card = _card(
        {
            "card": "mqtt_proposal",
            "payload": _candidate(proposal),
            "draft": {"devices": [_controlling_api_device()]},
        }
    )

    assert card["action"]["text"] == "Replace control connection"
    assert "Add as telemetry source" not in card["text"]
    assert "telemetry" in card["text"].lower()
    assert "can no longer be controlled by ems" in card["text"].lower()


def test_a_control_capable_connection_is_still_offered_as_a_connection_swap():
    proposal = _proposal()
    proposal["connection_source"] = "zendure_cloud_mqtt"
    card = _card(
        {
            "card": "mqtt_proposal",
            "payload": _candidate(proposal),
            "draft": {"devices": [_controlling_api_device()]},
        }
    )

    assert card["action"]["text"] == "Use connection"
    assert "cannot replace" not in card["text"].lower()


# --- capability is derived, never an operator toggle -------------------------


def test_the_maintenance_card_shows_capability_instead_of_offering_a_choice():
    """Local API inverters have no output-control switch; MQTT must match.

    Output control follows the model, the transport and the write route, so the
    card reports the verdict and its reason instead of asking the operator to
    confirm a decision EMS/Core already made.
    """

    js = _read_admin_js()
    render = _extract_fn(js, "renderMaintenanceZendureMqttDevice")

    assert "outputControlInput" not in render, (
        "the operator must not be offered a capability as a choice"
    )
    assert "device.output_control_user_set" not in render
    assert "mqttControlReasonLabel(" in render, (
        "a blocked device must name the backend block reason"
    )


def test_capability_is_derived_by_an_explicit_action_not_by_rendering():
    """One pure projection to display, one explicit writer to normalize."""

    js = _read_admin_js()
    render = _extract_fn(js, "renderMaintenanceZendureMqttDevice")
    projection = _extract_fn(js, "mconfigMqttControlProjection")
    normalize = _extract_fn(js, "mconfigNormalizeMqttControl")

    assert "shouldControl: supported && routeComplete" in projection
    assert "device.capabilities.write_output_limit = shouldControl" in normalize
    assert "device.capabilities.write_output_limit =" not in render
    assert "device.output_control =" not in render
    assert "mconfigNormalizeMqttControl(device)" in render


def test_the_separate_default_control_helper_is_gone():
    """Two places deciding one thing is what produced the original defect."""

    js = _read_admin_js()
    assert "function mconfigMqttShouldDefaultControl(" not in js


def test_activation_is_transport_independent():
    js = _read_admin_js()
    active = _extract_fn(js, "mconfigDeviceIsActive")
    by_choice = _extract_fn(js, "mconfigDeviceInactiveByChoice")

    assert "output_control" not in active, (
        "activation authority must not live in a transport-specific field"
    )
    assert "output_control" not in by_choice
    assert "enabled" in active


# --- Guided Setup derives control the same way, without a silent downgrade ---


def test_setup_manual_form_has_no_output_control_choice():
    """Same rule on every layer: capability decides, the operator does not."""

    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as handle:
        html = handle.read()

    assert 'id="config-mqtt-device-control"' not in html
    assert 'id="config-mqtt-device-control-help"' in html


def test_setup_manual_control_is_derived_from_model_and_write_route():
    js = _read_admin_js()
    add = _extract_fn(js, "addManualMqttDevice")

    assert "mqttManualEls.deviceControl" not in add
    assert "const wantsControl = manualMqttControlAvailable(" in add


def test_setup_manual_form_says_what_is_still_missing_for_control():
    """A capable model without a route must not degrade silently.

    Maintenance learns the route from a trusted proposal; here the operator
    types it, so the form states the outcome before the device is added.
    """

    js = _read_admin_js()
    sync = _extract_fn(js, "syncMqttGenerationDetails")
    available = _extract_fn(js, "manualMqttControlAvailable")

    assert "deviceControlHelp" in sync
    assert "telemetry source" in sync
    assert "MQTT device ID" in available
    assert "product key" in available


def test_setup_manual_control_availability_is_one_helper():
    """The form hint and the recorded intent must not drift apart."""

    js = _read_admin_js()
    assert js.count("function manualMqttControlAvailable(") == 1
    assert js.count("manualMqttControlAvailable(") >= 3
