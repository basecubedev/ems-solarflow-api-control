# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup / Maintenance / Manual MQTT config parity.

The three Admin paths that create a Zendure MQTT device — Fresh Setup discovery,
Maintenance discovery projection, and manual entry — must agree on the control
capability of the same logical device. There must not be separate MQTT control
rules per workflow; they all resolve capability through the shared EMS helper.
"""

import copy

import pytest

from admin.models import MqttHardwareCandidate
from admin.zendure_mqtt_config_draft import (
    apply_zendure_mqtt_draft_fields,
    build_manual_zendure_mqtt_fragment,
)
from admin.zendure_mqtt_config_proposals import build_proposals

pytestmark = pytest.mark.simulation


def _legacy_inverter_candidate():
    return MqttHardwareCandidate(
        broker_id="b1",
        broker_host="10.0.0.5",
        broker_port=1883,
        topic_family="legacy_zendure_json",
        device_id="DEV9",
        serial_number="LEG123",
        product_key="PKKEY",
        model_hint="Hyper 2000",
        metrics_seen=["electricLevel", "outputHomePower", "outputLimit"],
    )


def _control_signature(device):
    mqtt = device.get("mqtt") or {}
    caps = device.get("capabilities") or {}
    # A control device is identified by its transport and whether it pins a write
    # method. Discovery pins a concrete hardware_profile while the ambiguous
    # manual generation flow pins an explicit write_protocol; both are safe,
    # controllable and equivalent for parity — the exact representation differs.
    has_write_method = bool(mqtt.get("write_protocol") or device.get("hardware_profile"))
    return (
        device.get("type"),
        bool(caps.get("write_output_limit")),
        mqtt.get("topic_family"),
        has_write_method,
    )


def test_setup_discovery_and_maintenance_projection_agree():
    # Fresh Setup discovery fragment for a supported inverter.
    proposal = build_proposals([_legacy_inverter_candidate().to_dict()])[0]
    setup_fragment = proposal["config_fragment"]

    # Maintenance projects the same proposal into a new device draft.
    draft = {
        "name": proposal["display_name"],
        "serial_number": proposal["serial_number"],
        "hardware_generation": proposal["hardware_generation"],
        "hardware_model": proposal["hardware_model"],
        "output_control": proposal["output_control_supported"],
        "product_key": setup_fragment["mqtt"].get("product_key"),
        "mqtt": {
            "broker_ref": setup_fragment["mqtt"].get("broker_ref"),
            "topic_family": setup_fragment["mqtt"].get("topic_family"),
            "device_id": setup_fragment["mqtt"].get("device_id"),
            "product_key": setup_fragment["mqtt"].get("product_key"),
        },
    }
    maintenance_device = {}
    apply_zendure_mqtt_draft_fields(maintenance_device, draft)

    assert setup_fragment["capabilities"]["write_output_limit"] is True
    assert _control_signature(setup_fragment) == _control_signature(maintenance_device)


def test_manual_entry_matches_discovery_for_supported_inverter():
    proposal = build_proposals([_legacy_inverter_candidate().to_dict()])[0]
    setup_fragment = proposal["config_fragment"]

    manual_fragment, issues = build_manual_zendure_mqtt_fragment(
        {
            "name": "Manual Hyper",
            "generation": "hub_hyper_legacy",
            "power_hardware_profile": "hyper_2000",
            "serial_number": "LEG123",
            "product_key": "PKKEY",
            "output_control": True,
        },
        setup_fragment["mqtt"].get("broker_ref"),
    )
    assert issues == []
    assert _control_signature(manual_fragment) == _control_signature(setup_fragment)


def test_setup_and_maintenance_generate_equivalent_broker_and_device(
    tmp_path, isolated_install_root
):
    """The same discovery result must yield the same complete MQTT config.

    Compares the full ``zendure_mqtt`` block (broker profiles included) and the
    complete device entry — broker_ref, source, credentials_ref, topic_family,
    write_protocol, capabilities and identifiers — never only the device object.
    """

    import json

    from admin.config_preview import ConfigPreviewGenerator
    from admin.maintenance_config import (
        load_maintenance_config,
        prepare_maintenance_config_apply,
    )
    from tests.test_admin_maintenance_config import (
        _draft_item_from_proposal,
        _write_config,
    )

    proposal = build_proposals([_legacy_inverter_candidate().to_dict()])[0]

    class _ReleaseManager:
        def config_template(self):
            return {
                "tag": "test",
                "template": {
                    "system": {"max_total_power": 1600},
                    "devices": [
                        {"name": "WR1", "ip": "192.0.2.1", "sn": "YOUR_SN"}
                    ],
                    "grid_meter": {"type": "shelly", "ip": "192.0.2.3"},
                },
            }

    meter = {
        "config_name": "grid_meter",
        "display_name": "Shelly Pro 3EM",
        "role": "grid_meter",
        "enabled": True,
        "ip": "192.168.1.50",
        "api_family": "shelly_gen2",
        "device_type": "shelly_pro_3em",
    }
    local = {
        "config_name": "WR1",
        "display_name": "SolarFlow 800",
        "role": "inverter",
        "enabled": True,
        "ip": "192.168.1.100",
        "serial_number": "AAA",
    }
    setup = ConfigPreviewGenerator(_ReleaseManager()).generate(
        [local, meter], 1, zendure_mqtt_proposals=[copy.deepcopy(proposal)]
    )
    assert setup["ready"] is True, setup["validation"]["errors"]
    setup_config = setup["config"]

    _write_config(
        tmp_path,
        {
            "system": {"max_total_power": 1600},
            "devices": [
                {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}
            ],
            "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
        },
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(_draft_item_from_proposal(copy.deepcopy(proposal)))
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    maintenance_config = json.loads(prepared["payload"])

    # Broker profiles must match completely, not merely exist.
    assert maintenance_config.get("zendure_mqtt") == setup_config.get("zendure_mqtt")

    def _mqtt_devices(config):
        return [d for d in config["devices"] if d.get("type") == "zendure_mqtt"]

    assert _mqtt_devices(maintenance_config) == _mqtt_devices(setup_config)


def test_maintenance_does_not_make_supported_inverter_telemetry_only():
    # Regression: a supported inverter added through Maintenance must not become
    # telemetry-only merely because of the workflow.
    proposal = build_proposals([_legacy_inverter_candidate().to_dict()])[0]
    fragment = proposal["config_fragment"]
    device = {}
    apply_zendure_mqtt_draft_fields(
        device,
        {
            "name": proposal["display_name"],
            "serial_number": proposal["serial_number"],
            "hardware_generation": proposal["hardware_generation"],
            "hardware_model": proposal["hardware_model"],
            "output_control": True,
            "product_key": fragment["mqtt"].get("product_key"),
            "mqtt": {
                "broker_ref": fragment["mqtt"].get("broker_ref"),
                "topic_family": fragment["mqtt"].get("topic_family"),
                "device_id": fragment["mqtt"].get("device_id"),
                "product_key": fragment["mqtt"].get("product_key"),
            },
        },
    )
    assert device["capabilities"]["write_output_limit"] is True
