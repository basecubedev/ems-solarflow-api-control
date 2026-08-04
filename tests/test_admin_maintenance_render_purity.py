# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rendering a Maintenance device card never mutates the draft it renders.

Capability is derived — but derivation is an *action*: a field edit, a transport
switch, proposal adoption or a draft load. A render pass only projects the
current verdict, so redrawing a card (which happens on every unrelated draft
change) can never flip ``output_control`` or
``capabilities.write_output_limit`` behind the operator's back.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.maintenance,
    pytest.mark.contract,
    pytest.mark.simulation,
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tests", "js", "maintenance_device_card_runner.js")

CATALOG = {
    "zendure_mqtt_generations": [
        {"id": "solarflow_zensdk", "label": "New SolarFlow / ZenSDK generation"},
        {"id": "hub_hyper_legacy", "label": "Older Zendure Hub / Hyper generation"},
    ],
    "zendure_mqtt_hardware_models": [
        {
            "id": "solarflow_800_pro_2",
            "label": "SolarFlow 800 Pro 2",
            "compatible_generations": ["solarflow_zensdk", "zendure_cloud"],
            "control_supported": True,
            "power_write_profile": "zensdk_properties_write",
            "validation_maturity": "existing_support",
            "supported_operations": ["discharge", "idle"],
        },
        {
            "id": "ace_1500",
            "label": "ACE 1500 — telemetry only",
            "compatible_generations": ["hub_hyper_legacy", "zendure_cloud"],
            "control_supported": False,
            "power_write_profile": "telemetry_only",
            "validation_maturity": "deferred",
            "supported_operations": [],
        },
    ],
}


def _render(device, renders=2):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the renderer purity contract")
    result = subprocess.run(
        [node, RUNNER],
        input=json.dumps({"device": device, "catalog": CATALOG, "renders": renders}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _controllable_device(**overrides):
    device = {
        "kind": "zendure_mqtt",
        "name": "INV_2",
        "original_name": "INV_2",
        "enabled": True,
        "has_enabled_key": True,
        "serial_number": "TESTSN000001",
        "product_key": "TESTPK0001",
        "hardware_generation": "solarflow_zensdk",
        "hardware_model": "solarflow_800_pro_2",
        "power_write_profile": "zensdk_properties_write",
        "alternative_layout": False,
        "output_control": True,
        "supports_output_control": True,
        "control_readiness": {"ready": True, "reason": "zensdk_properties_write"},
        "mqtt": {
            "broker_ref": "zendure_cloud",
            "source": "zendure_cloud_mqtt",
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": "TESTROUTE01",
            "product_key": "TESTPK0001",
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": True,
        },
    }
    device.update(overrides)
    return device


def test_rendering_a_controllable_card_twice_leaves_the_device_unchanged():
    answer = _render(_controllable_device())

    assert answer["mutated"] is False
    assert answer["after"] == answer["before"]


def test_rendering_never_enables_control_on_a_stored_telemetry_only_device():
    """A render must not flip a stored value — only a draft load or an edit may."""

    device = _controllable_device()
    device["output_control"] = False
    device["capabilities"]["write_output_limit"] = False

    answer = _render(device)

    assert answer["mutated"] is False
    assert answer["after"]["capabilities"]["write_output_limit"] is False


def test_rendering_never_disables_control_on_a_proposal_backed_device():
    """The browser holds no route id for a proposal device; that is not a gap."""

    device = _controllable_device(proposal_id="zendure-mqtt:opaque:v1:AAAA")
    device["mqtt"]["device_id"] = ""
    device["mqtt"].pop("product_key")
    device["product_key"] = ""
    device["trusted_write_target"] = True

    answer = _render(device)

    assert answer["mutated"] is False
    assert answer["after"]["capabilities"]["write_output_limit"] is True


def test_rendering_an_uncontrollable_card_twice_leaves_the_device_unchanged():
    device = _controllable_device(
        hardware_generation="hub_hyper_legacy",
        hardware_model="ace_1500",
        power_write_profile="telemetry_only",
        output_control=False,
        supports_output_control=False,
        control_readiness={"ready": False, "reason": "hardware_profile_deferred"},
    )
    device["capabilities"]["write_output_limit"] = False

    answer = _render(device)

    assert answer["mutated"] is False
    assert "Not available" in " ".join(answer["readonlyValues"])


def test_the_card_reports_the_capability_verdict_without_naming_a_topic_family():
    answer = _render(_controllable_device())

    assert "Available" in " ".join(answer["readonlyValues"])
    assert "topic family" not in answer["note"].lower()
