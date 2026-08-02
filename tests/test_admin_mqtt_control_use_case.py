# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin MQTT-control use-case contract: MQTT is a first-class control transport.

The Admin Console (Fresh Install discovery/manual entry and Maintenance) can add
a supported MQTT inverter as a normal, controllable EMS device without editing
``config.json`` by hand. Control is enabled only where the topic family provides
a verified write method (capability-based); unsupported families stay safely
telemetry-only. Maintenance must never silently disable an existing control
entry — a no-op roundtrip preserves it.
"""

import copy
import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
)
from admin.zendure_mqtt_config_draft import (
    apply_zendure_mqtt_draft_fields,
    build_manual_zendure_mqtt_fragment,
    sanitize_zendure_mqtt_fragment,
)
from admin.zendure_mqtt_config_proposals import build_proposals

pytestmark = pytest.mark.simulation


def test_manual_supported_inverter_is_controllable_without_editing_config():
    fragment, issues = build_manual_zendure_mqtt_fragment(
        {
            "name": "Manual",
            "generation": "hub_hyper_legacy",
            "power_hardware_profile": "hyper_2000",
            "serial_number": "SN1",
            "mqtt_device_id": "DEV1",
            "product_key": "PK1",
            "output_control": True,
        },
        "local_a",
        broker_source="local_mqtt",
    )
    assert issues == []
    assert fragment["capabilities"]["write_output_limit"] is True
    assert fragment["hardware_profile"] == "hyper_2000"
    assert "write_protocol" not in fragment["mqtt"]


def test_manual_setup_device_defaults_to_telemetry_only():
    fragment, issues = build_manual_zendure_mqtt_fragment(
        {"name": "Manual", "generation": "hub_hyper_legacy",
         "serial_number": "SN1", "product_key": "PK1"},
        "local_a",
        broker_source="local_mqtt",
    )
    assert issues == []
    assert fragment["capabilities"]["write_output_limit"] is False


def test_manual_unsupported_family_stays_telemetry_only():
    # Capability-based fallback, not a global MQTT restriction.
    fragment, _ = build_manual_zendure_mqtt_fragment(
        {
            "name": "SF800",
            "generation": "solarflow_zensdk",
            "serial_number": "SN2",
            "output_control": True,
        },
        "local_a",
        broker_source="local_mqtt",
    )
    assert fragment["capabilities"]["write_output_limit"] is False


def _control_config(ref, write_output_limit):
    mqtt = {
        "broker_ref": ref,
        "topic_family": "legacy_zendure_json",
        "base_topic": "iot",
        "device_id": "CTL1",
        "product_key": "PKCTL",
    }
    device = {
        "type": "zendure_mqtt",
        "name": "Existing MQTT",
        "enabled": True,
        "serial_number": "CTL1",
        "hardware_profile": "hyper_2000",
        "power_write_profile": "legacy_object_device_automation",
    }
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "10.0.0.1", "sn": "REAL1", "max_power": 800},
            {
                **device,
                "mqtt": mqtt,
                "capabilities": {
                    "read_power": True,
                    "read_soc": True,
                    "write_output_limit": write_output_limit,
                },
            },
        ],
        "grid_meter": {"type": "shelly", "ip": "10.0.0.9"},
        "zendure_mqtt": {
            "brokers": {
                ref: {"enabled": True, "source": "local_mqtt", "host": "10.0.0.10", "port": 1883},
            },
        },
    }


def _maintenance_roundtrip(tmp_path, config, output_control):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"
    draft = loaded["draft"]
    entry = next(d for d in draft["devices"] if d.get("kind") == "zendure_mqtt")
    # Mirror the browser checkbox: it writes both the control intent and the
    # capability field on the draft entry.
    entry["output_control"] = output_control
    entry["capabilities"]["write_output_limit"] = output_control
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    return [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]


def test_maintenance_enables_control_on_existing_supported_device(
    tmp_path, isolated_install_root
):
    device = _maintenance_roundtrip(
        tmp_path, _control_config("local_mqtt_home", False), True
    )
    assert device["capabilities"]["write_output_limit"] is True
    assert device["hardware_profile"] == "hyper_2000"
    assert "write_protocol" not in device["mqtt"]


def test_maintenance_disables_control_on_existing_device(
    tmp_path, isolated_install_root
):
    device = _maintenance_roundtrip(
        tmp_path, _control_config("local_mqtt_home", True), False
    )
    assert device["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in device["mqtt"]


def test_maintenance_preserves_existing_manual_control_entry(tmp_path, isolated_install_root):
    # A manual control device already in config must survive a no-op Maintenance
    # apply (never silently downgraded to telemetry-only).
    ref = "local_mqtt_home"
    config = {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "10.0.0.1", "sn": "REAL1", "max_power": 800},
            {
                "type": "zendure_mqtt",
                "name": "Manual Control",
                "enabled": True,
                "serial_number": "CTL1",
                "hardware_profile": "hyper_2000",
                "power_write_profile": "legacy_object_device_automation",
                "mqtt": {
                    "broker_ref": ref,
                    "topic_family": "legacy_zendure_json",
                    "base_topic": "iot",
                    "device_id": "CTL1",
                    "product_key": "PKCTL",
                },
                "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": True},
            },
        ],
        "grid_meter": {"type": "shelly", "ip": "10.0.0.9"},
        "zendure_mqtt": {
            "brokers": {
                ref: {"enabled": True, "source": "local_mqtt", "host": "10.0.0.10", "port": 1883},
            },
        },
    }
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    control = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert control["capabilities"]["write_output_limit"] is True


# --- Fresh Setup / Maintenance discovery parity -------------------------------


def _soc_only_observation():
    return {
        "source_type": "local_mqtt",
        "broker_host": "10.0.0.10",
        "broker_port": 1883,
        "topic_family": "legacy_zendure_json",
        "serial_number": "SOC1",
        "device_id": "SOC1",
        "product_key": "PKSOC",
        "model_hint": "SolarFlow Hub 2000",
        "metrics_seen": ["electricLevel"],
    }


@pytest.mark.parametrize(
    "observation_name",
    ["control_inverter", "scalar_telemetry", "soc_only_telemetry"],
)
def test_setup_and_maintenance_selection_produce_equivalent_mqtt_device(
    observation_name,
):
    """Selecting the same trusted proposal through the Fresh Setup preview path
    and the real Maintenance browser path must yield the same device config.
    """

    from tests.test_admin_frontend import (
        _local_control_observation,
        _local_scalar_observation,
        run_mconfig_add_mqtt_proposal,
    )

    observation = {
        "control_inverter": _local_control_observation,
        "scalar_telemetry": _local_scalar_observation,
        "soc_only_telemetry": _soc_only_observation,
    }[observation_name]()
    proposal = build_proposals([observation])[0]

    # Fresh Setup: the sanitized trusted fragment becomes the device entry.
    setup_device = sanitize_zendure_mqtt_fragment(
        copy.deepcopy(proposal["config_fragment"])
    )
    setup_device["name"] = "INV_1"

    # Maintenance: the real browser selection function builds the draft entry,
    # then the shared draft builder projects it onto a new config device.
    draft_entry = run_mconfig_add_mqtt_proposal(proposal)["device"]
    maintenance_device = {}
    apply_zendure_mqtt_draft_fields(maintenance_device, draft_entry)

    assert maintenance_device == setup_device


# --- topic capability wins over hardware profile ------------------------------


def _hub_model_on_scalar_observation():
    # Hardware label says "controllable hub generation"; the observed transport
    # schema is the scalar family with no verified write protocol.
    return {
        "source_type": "local_mqtt",
        "broker_host": "10.0.0.10",
        "broker_port": 1883,
        "topic_family": "zensdk_ha_scalar",
        "serial_number": "HYB1",
        "device_id": "HYB1",
        "model_hint": "Hyper 2000",
        "metrics_seen": ["outputLimit", "electricLevel", "outputHomePower"],
    }


def _modern_model_on_alt_json_observation():
    # Modern ZenSDK product reporting through the legacy-compatible
    # leading-slash JSON layout: writable, and not legacy hardware.
    return {
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


def test_modern_device_on_alt_json_layout_stays_modern_and_controllable():
    from tests.test_admin_frontend import run_mconfig_add_mqtt_proposal

    proposal = build_proposals([_modern_model_on_alt_json_observation()])[0]
    assert proposal["hardware_generation"] == "solarflow_zensdk"
    assert proposal["hardware_model"] == "solarflow_800_pro_2"
    assert proposal["alternative_layout"] is True
    assert proposal["product"] == "SolarFlow 800 Pro 2"
    assert proposal["output_control_supported"] is True

    draft = run_mconfig_add_mqtt_proposal(proposal)["device"]
    device = {}
    apply_zendure_mqtt_draft_fields(device, draft)
    assert device["mqtt"]["topic_family"] == "legacy_zendure_json_alt"
    assert device["capabilities"]["write_output_limit"] is True
    # Controllable via the pinned registry model, not a topic-family write method.
    assert device["hardware_profile"] == "solarflow_800_pro_2"
    assert "write_protocol" not in device["mqtt"]


def test_a_scalar_observation_without_a_product_key_has_no_write_route():
    from tests.test_admin_frontend import run_mconfig_add_mqtt_proposal

    observation = dict(_hub_model_on_scalar_observation())
    # Judged on the cloud broker, which carries the write route on every family,
    # so the only remaining blocker is the address itself.
    observation["source_type"] = "zendure_cloud_mqtt"
    observation["tls_mode"] = "encrypted_no_verify"
    proposal = build_proposals([observation])[0]
    assert proposal["hardware_generation"] == "hub_hyper_legacy"
    assert proposal["hardware_model"] == "hyper_2000"
    assert proposal["output_control_supported"] is False
    # The blocker is the incomplete write route, not the observed family: a
    # scalar topic carries no product key, and iot/<pk>/<dev>/… needs one.
    assert proposal["output_control_reason"] == "write_target_missing"

    draft = run_mconfig_add_mqtt_proposal(proposal)["device"]
    device = {}
    apply_zendure_mqtt_draft_fields(device, draft)
    # The hardware label must not re-home the observed transport schema.
    assert device["mqtt"]["topic_family"] == "zensdk_ha_scalar"
    assert device["capabilities"]["write_output_limit"] is False
    assert "write_protocol" not in device["mqtt"]
