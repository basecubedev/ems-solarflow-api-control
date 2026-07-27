# SPDX-License-Identifier: AGPL-3.0-or-later
"""Added Zendure MQTT devices default to output control when control-ready.

A user who adds a control-ready MQTT inverter in Maintenance must not have to
flip a second toggle to make it a controllable EMS device: the discovery
proposal path already defaults control on (``proposal_output_control``), and the
Maintenance device card now auto-enables output control once a manually
configured device resolves to a writable model with a write target. The
auto-default never touches an already-saved device and always yields to an
explicit operator choice.
"""

import json

import pytest

from tests.test_admin_frontend import _extract_fn, _read, _run_node

pytestmark = pytest.mark.simulation


def _run_should_default(cases):
    js = _extract_fn(_read("admin.js"), "mconfigMqttShouldDefaultControl")
    script = js + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(" + (
        "(c) => mconfigMqttShouldDefaultControl(c.device, c.supported, c.hasWriteTarget)"
    ) + ")));"
    return _run_node(script)


def test_new_control_ready_device_defaults_to_control_on():
    result = _run_should_default(
        [{"device": {"original_name": None}, "supported": True, "hasWriteTarget": True}]
    )
    assert result == [True]


def test_unsupported_or_no_write_target_stays_telemetry_only():
    result = _run_should_default(
        [
            {"device": {"original_name": None}, "supported": False, "hasWriteTarget": True},
            {"device": {"original_name": None}, "supported": True, "hasWriteTarget": False},
        ]
    )
    assert result == [False, False]


def test_existing_saved_device_is_never_auto_changed():
    result = _run_should_default(
        [{"device": {"original_name": "INV_2"}, "supported": True, "hasWriteTarget": True}]
    )
    assert result == [False]


def test_explicit_operator_choice_is_respected():
    result = _run_should_default(
        [
            {
                "device": {"original_name": None, "output_control_user_set": True},
                "supported": True,
                "hasWriteTarget": True,
            },
            {
                "device": {"original_name": None, "output_control": True},
                "supported": True,
                "hasWriteTarget": True,
            },
        ]
    )
    assert result == [False, False]


def test_maintenance_card_wires_the_auto_default_and_syncs_the_checkbox():
    js = _read("admin.js")
    render = _extract_fn(js, "renderMaintenanceZendureMqttDevice")
    # The auto-default is gated on a *complete route* (route id + write target),
    # never a write target alone — the physical serial is never the route id.
    assert "mconfigMqttShouldDefaultControl(device, supported, routeComplete)" in render
    assert "const routeComplete = !!routeDeviceId && hasWriteTarget" in render
    # routeDeviceId derives only from the explicit mqtt.device_id.
    assert "device.mqtt.device_id.trim()" in render
    assert "outputControlInput.checked = device.output_control === true" in render
    assert "device.output_control_user_set = true" in render


def test_control_ready_proposal_is_added_control_on():
    from admin.zendure_mqtt_config_proposals import build_proposals
    from admin.zendure_mqtt_config_draft import apply_zendure_mqtt_draft_fields
    from tests.test_admin_frontend import run_mconfig_add_mqtt_proposal

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
