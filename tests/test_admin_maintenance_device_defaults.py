# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance device creation must materialize central common defaults.

Every newly added inverter — manual or discovered, Local API or Zendure MQTT —
must receive the same transport-independent EMS tuning values (smart_mode,
max_power, pv_kwp, pv_priority_factor, battery_kwh, min_soc, max_soc) from the
central config catalog/template, never from another configured device and never
as blank omissions the runtime silently papers over.

The expected values are derived from the central template prototype
(``ems.config_catalog``), not hardcoded, so a future catalog change keeps this
contract intact without editing literals here.
"""

import copy
import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)
from ems.config_catalog import build_default_template

pytestmark = [
    pytest.mark.admin,
    pytest.mark.maintenance,
    pytest.mark.integration,
    pytest.mark.simulation,
]


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _expected_common_defaults():
    """Central default tuning values: the template prototype minus identity."""

    prototype = build_default_template(device_count=1)["devices"][0]
    return {
        key: value
        for key, value in prototype.items()
        if not str(key).startswith("_") and key not in ("name", "ip", "sn")
    }


def _base_config(devices):
    return {
        "system": {"max_total_power": 1600},
        "devices": devices,
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
    }


def _merged_devices(draft, tmp_path):
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["status"] == "ok"
    return preview["preview"]["devices"], preview


def _local_broker_config(devices):
    config = _base_config(devices)
    config["zendure_mqtt"] = {
        "brokers": {
            "local_b1": {
                "enabled": True,
                "source": "local_mqtt",
                "host": "10.0.0.10",
                "port": 1883,
            }
        }
    }
    return config


def _mqtt_proposal_draft_item(name="INV_2", serial="MQTT-SN-1"):
    """Draft entry shaped like the browser's trusted-proposal add path."""

    return {
        "kind": "zendure_mqtt",
        "original_name": None,
        "name": name,
        "enabled": True,
        "has_enabled_key": True,
        "serial_number": serial,
        "device_id": serial,
        "product_key": "PK1",
        "hardware_generation": "solarflow_zensdk",
        "hardware_model": "",
        "power_write_profile": "",
        "output_control": False,
        "mqtt": {
            "broker_ref": "local_b1",
            "source": "local_mqtt",
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": serial,
            "product_key": "PK1",
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": False,
        },
    }


# --- required regression: API discovery defaults ---------------------------


def test_discovered_local_api_device_gets_central_defaults(tmp_path):
    _write_config(tmp_path, _base_config([]))
    draft = {
        "devices": [
            {
                "original_name": None,
                "name": "INV_1",
                "ip": "192.0.2.10",
                "sn": "SERIAL-1",
                "enabled": True,
            }
        ]
    }
    devices, _ = _merged_devices(draft, tmp_path)
    assert len(devices) == 1
    device = devices[0]
    for key, expected in _expected_common_defaults().items():
        assert device.get(key) == expected, f"missing central default for {key}"
    assert device["name"] == "INV_1"
    assert device["ip"] == "192.0.2.10"
    assert device["sn"] == "SERIAL-1"


# --- required regression: manual API defaults -------------------------------


def test_manual_local_api_device_gets_central_defaults_without_prototype(tmp_path):
    _write_config(
        tmp_path,
        _base_config(
            [
                {
                    "name": "INV_1",
                    "ip": "192.168.1.100",
                    "sn": "AAA",
                    "max_power": 321,
                    "pv_kwp": 4.7,
                    "battery_kwh": 9.6,
                    "min_soc": 27,
                }
            ]
        ),
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(
        {
            "original_name": None,
            "name": "INV_2",
            "ip": "192.0.2.20",
            "sn": "SERIAL-2",
            "enabled": True,
        }
    )
    devices, _ = _merged_devices(draft, tmp_path)
    assert len(devices) == 2
    second = devices[1]
    defaults = _expected_common_defaults()
    for key, expected in defaults.items():
        assert second.get(key) == expected, f"{key} must come from central defaults"
    # The first device's custom values must never leak into a new inverter.
    assert second["max_power"] != 321
    assert second["pv_kwp"] != 4.7
    assert second["battery_kwh"] != 9.6
    assert second["min_soc"] != 27
    # The existing device keeps its explicit values untouched.
    first = devices[0]
    assert first["max_power"] == 321
    assert first["pv_kwp"] == 4.7
    assert first["battery_kwh"] == 9.6
    assert first["min_soc"] == 27


def test_first_device_unknown_custom_keys_do_not_leak(tmp_path):
    _write_config(
        tmp_path,
        _base_config(
            [
                {
                    "name": "INV_1",
                    "ip": "192.168.1.100",
                    "sn": "AAA",
                    "max_power": 800,
                    "vendor_custom_flag": {"nested": True},
                }
            ]
        ),
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(
        {
            "original_name": None,
            "name": "INV_2",
            "ip": "192.0.2.20",
            "sn": "SERIAL-2",
            "enabled": True,
        }
    )
    devices, _ = _merged_devices(draft, tmp_path)
    assert "vendor_custom_flag" not in devices[1]
    assert devices[0]["vendor_custom_flag"] == {"nested": True}


def test_explicit_zero_values_are_preserved_not_defaulted(tmp_path):
    _write_config(tmp_path, _base_config([]))
    draft = {
        "devices": [
            {
                "original_name": None,
                "name": "INV_1",
                "ip": "192.0.2.10",
                "sn": "SERIAL-1",
                "enabled": True,
                "min_soc": 0,
                "pv_kwp": 0,
            }
        ]
    }
    devices, _ = _merged_devices(draft, tmp_path)
    assert devices[0]["min_soc"] == 0
    assert devices[0]["pv_kwp"] == 0
    # Other untouched values still materialize from the central defaults.
    assert devices[0]["max_power"] == _expected_common_defaults()["max_power"]


# --- required regression: MQTT defaults -------------------------------------


def test_mqtt_proposal_device_gets_central_defaults(tmp_path):
    _write_config(tmp_path, _local_broker_config([]))
    draft = {"devices": [_mqtt_proposal_draft_item(name="INV_1")]}
    devices, _ = _merged_devices(draft, tmp_path)
    assert len(devices) == 1
    device = devices[0]
    assert device["type"] == "zendure_mqtt"
    for key, expected in _expected_common_defaults().items():
        assert device.get(key) == expected, f"MQTT device missing default {key}"
    # Transport identity from the trusted proposal stays intact.
    assert device["serial_number"] == "MQTT-SN-1"
    assert device["mqtt"]["topic_family"] == "zensdk_ha_scalar"
    assert device["mqtt"]["broker_ref"] == "local_b1"


def test_cloud_mqtt_proposal_device_gets_central_defaults(tmp_path):
    config = _base_config([])
    config["zendure_mqtt"] = {
        "brokers": {
            "cloud_b": {
                "enabled": True,
                "source": "zendure_cloud_mqtt",
                "host": "mqtt.zen-iot.com",
                "port": 8883,
                "tls": True,
                "credentials_ref": "zendure-cloud",
            }
        }
    }
    _write_config(tmp_path, config)
    item = _mqtt_proposal_draft_item(name="INV_1", serial="CLOUD-SN-1")
    item["mqtt"]["broker_ref"] = "cloud_b"
    item["mqtt"]["source"] = "zendure_cloud_mqtt"
    item["mqtt"]["topic_family"] = "zendure_cloud_scalar"
    devices, _ = _merged_devices({"devices": [item]}, tmp_path)
    device = devices[0]
    assert device["type"] == "zendure_mqtt"
    for key, expected in _expected_common_defaults().items():
        assert device.get(key) == expected, f"cloud MQTT missing default {key}"
    assert device["mqtt"]["source"] == "zendure_cloud_mqtt"


def test_manual_mqtt_device_gets_central_defaults(tmp_path):
    _write_config(tmp_path, _local_broker_config([]))
    item = {
        "kind": "zendure_mqtt",
        "original_name": None,
        "name": "INV_1",
        "enabled": True,
        "has_enabled_key": True,
        "serial_number": "MANUAL-SN",
        "device_id": "MANUAL-SN",
        "hardware_generation": "solarflow_zensdk",
        "hardware_model": "",
        "output_control": False,
        "mqtt": {"broker_ref": "local_b1", "device_id": "MANUAL-SN"},
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": False,
        },
    }
    devices, _ = _merged_devices({"devices": [item]}, tmp_path)
    device = devices[0]
    for key, expected in _expected_common_defaults().items():
        assert device.get(key) == expected, f"manual MQTT missing default {key}"


def test_mqtt_defaults_do_not_copy_existing_api_device_values(tmp_path):
    _write_config(
        tmp_path,
        _local_broker_config(
            [
                {
                    "name": "INV_1",
                    "ip": "192.168.1.100",
                    "sn": "AAA",
                    "max_power": 321,
                    "pv_kwp": 4.7,
                }
            ]
        ),
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(_mqtt_proposal_draft_item(name="INV_2"))
    devices, _ = _merged_devices(draft, tmp_path)
    mqtt_device = devices[1]
    defaults = _expected_common_defaults()
    assert mqtt_device["max_power"] == defaults["max_power"]
    assert mqtt_device["max_power"] != 321
    assert mqtt_device["pv_kwp"] == defaults["pv_kwp"]


# --- MQTT draft round-trip of common values ----------------------------------


def test_mqtt_draft_roundtrips_common_values(tmp_path):
    device = {
        "type": "zendure_mqtt",
        "name": "WR_MQTT",
        "enabled": True,
        "serial_number": "S1",
        "max_power": 450,
        "smart_mode": 1,
        "pv_kwp": 2.5,
        "pv_priority_factor": 1.0,
        "battery_kwh": 3.8,
        "min_soc": 12,
        "max_soc": 96,
        "mqtt": {
            "broker_ref": "local_b1",
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": "S1",
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }
    api_device = {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800}
    config = _local_broker_config([api_device, copy.deepcopy(device)])
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft_device = loaded["draft"]["devices"][1]
    assert draft_device["kind"] == "zendure_mqtt"
    # Loading must surface the stored common values in the editable draft.
    assert draft_device.get("max_power") == 450
    assert draft_device.get("pv_kwp") == 2.5
    assert draft_device.get("battery_kwh") == 3.8
    assert draft_device.get("min_soc") == 12
    assert draft_device.get("max_soc") == 96

    # Editing one value and applying preserves the others byte-for-byte.
    draft_device["max_power"] = 600
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    out = merged["devices"][1]
    assert out["max_power"] == 600
    assert out["pv_kwp"] == 2.5
    assert out["battery_kwh"] == 3.8
    assert out["min_soc"] == 12
    assert out["max_soc"] == 96
    assert out["mqtt"]["base_topic"] == "Zendure"


def test_existing_incomplete_device_stays_untouched_on_noop(tmp_path):
    """An existing device missing common values round-trips byte-identically.

    Missing defaults materialize only for newly created devices, explicit
    edits and transport switches — never behind the operator's back on an
    unchanged draft.
    """

    config = _local_broker_config(
        [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800},
            {
                "type": "zendure_mqtt",
                "name": "WR_MQTT",
                "enabled": True,
                "serial_number": "S1",
                "mqtt": {
                    "broker_ref": "local_b1",
                    "topic_family": "zensdk_ha_scalar",
                    "base_topic": "Zendure",
                    "device_id": "S1",
                },
                "capabilities": {
                    "read_power": True,
                    "read_soc": True,
                    "write_output_limit": False,
                },
            },
        ]
    )
    original = copy.deepcopy(config)
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"]) == original
