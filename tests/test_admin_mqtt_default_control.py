# SPDX-License-Identifier: AGPL-3.0-or-later
"""A control-ready Zendure MQTT device is controlled, without a second toggle.

A user who adds or switches to a control-ready MQTT inverter must not have to
flip anything to make it a controllable EMS device. Output control is not an
operator choice at all: EMS/Core derives it from the pinned hardware model, the
transport and a complete write route, exactly as a Local API inverter has no
such switch. The Maintenance card reports that verdict; the derivation itself
runs on an explicit action (field edit, transport switch, proposal adoption,
draft load), never as a render side effect.
"""

import json

import pytest

from tests.test_admin_frontend import _extract_fn, _read, run_mconfig_add_mqtt_proposal

pytestmark = pytest.mark.simulation


def test_control_is_derived_from_model_and_route():
    js = _read("admin.js")
    projection = _extract_fn(js, "mconfigMqttControlProjection")
    normalize = _extract_fn(js, "mconfigNormalizeMqttControl")

    # A complete route is a known route plus a write target; the physical serial
    # is never the route id, and a proposal-backed device is routed by the
    # trusted proposal the server resolves at preview/apply.
    assert "const routeComplete = (!!routeDeviceId || proposalRouted) && hasWriteTarget" in projection
    assert "device.mqtt.device_id.trim()" in projection
    assert "shouldControl: supported && routeComplete" in projection
    assert "device.capabilities.write_output_limit = shouldControl" in normalize


def test_no_operator_opt_out_remains():
    """The state the toggle used to express is gone from the editor entirely."""

    js = _read("admin.js")

    assert "output_control_user_set" not in js
    assert "function mconfigMqttShouldDefaultControl(" not in js


def test_control_ready_proposal_is_added_control_on():
    from admin.zendure_mqtt_config_proposals import build_proposals
    from admin.zendure_mqtt_config_draft import apply_zendure_mqtt_draft_fields

    observation = {
        "source_type": "local_mqtt",
        "broker_host": "10.0.0.10",
        "broker_port": 1883,
        "topic_family": "legacy_zendure_json_alt",
        "serial_number": "P2SN",
        "device_id": "P2DEV",
        "product_key": "PKP2",
        "model_hint": "SolarFlow 800 Pro 2",
        "metrics_seen": ["outputLimit", "electricLevel", "outputHomePower"],
    }
    proposal = build_proposals([observation])[0]
    assert proposal["output_control_supported"] is True

    draft = run_mconfig_add_mqtt_proposal(proposal)["device"]
    assert draft["output_control"] is True

    device = {}
    apply_zendure_mqtt_draft_fields(device, draft)
    assert device["capabilities"]["write_output_limit"] is True


def test_a_telemetry_only_proposal_is_added_without_control():
    from admin.zendure_mqtt_config_proposals import build_proposals

    observation = {
        "source_type": "local_mqtt",
        "broker_host": "10.0.0.10",
        "broker_port": 1883,
        "topic_family": "zensdk_ha_scalar",
        "serial_number": "SCALARSN",
        "device_id": "SCALARDEV",
        "metrics_seen": ["electricLevel"],
    }
    proposal = build_proposals([observation])[0]
    assert proposal["output_control_supported"] is False

    draft = run_mconfig_add_mqtt_proposal(proposal)["device"]
    assert draft["output_control"] is False
    assert json.loads(json.dumps(draft))["capabilities"]["write_output_limit"] is False
