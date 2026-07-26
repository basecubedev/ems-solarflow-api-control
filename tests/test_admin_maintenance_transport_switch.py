# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance transport switching must be lossless and identity-safe.

A physical inverter has one identity, one common EMS configuration and one
selected transport. Switching MQTT <-> Local API replaces the connection of the
same logical device: the configured name and common tuning values survive,
stale transport-only fields are removed, and the result is exactly one config
entry. Ambiguous identity evidence fails closed instead of silently merging.
"""

import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
    preview_maintenance_config,
)

pytestmark = pytest.mark.simulation


@pytest.fixture(autouse=True)
def _isolate(isolated_install_root):
    return isolated_install_root


def _write_config(base_dir, data):
    config_dir = base_dir / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _broker_block():
    return {
        "brokers": {
            "local_b1": {
                "enabled": True,
                "source": "local_mqtt",
                "host": "10.0.0.10",
                "port": 1883,
            }
        }
    }


def _mqtt_device(name="WR1", serial="PHYSICAL-001", **tuning):
    device = {
        "type": "zendure_mqtt",
        "name": name,
        "enabled": True,
        "serial_number": serial,
        "mqtt": {
            "broker_ref": "local_b1",
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": serial,
        },
        "capabilities": {"read_power": True, "read_soc": True, "write_output_limit": False},
    }
    device.update(tuning)
    return device


def _api_device(name="WR1", serial="PHYSICAL-001", ip="192.168.1.100", **tuning):
    device = {"name": name, "ip": ip, "sn": serial, "enabled": True}
    device.update(tuning)
    return device


def _config(devices, grid=True):
    config = {
        "system": {"max_total_power": 1600},
        "devices": devices,
        "zendure_mqtt": _broker_block(),
    }
    if grid:
        config["grid_meter"] = {"type": "shelly", "ip": "192.168.1.50"}
    return config


_CUSTOM_TUNING = {
    "smart_mode": 1,
    "max_power": 640,
    "pv_kwp": 3.2,
    "pv_priority_factor": 1.4,
    "battery_kwh": 7.7,
    "min_soc": 22,
    "max_soc": 92,
}


def _switch_to_api_item(original_name, name, serial="PHYSICAL-001", ip="192.0.2.10"):
    """Draft entry after the browser switched an MQTT device to Local API."""

    return {
        "kind": "local_api",
        "original_name": original_name,
        "name": name,
        "ip": ip,
        "sn": serial,
        "enabled": True,
        "has_enabled_key": True,
    }


def _switch_to_mqtt_item(original_name, name, serial="PHYSICAL-001"):
    """Draft entry after the browser switched a Local API device to MQTT."""

    return {
        "kind": "zendure_mqtt",
        "original_name": original_name,
        "name": name,
        "enabled": True,
        "has_enabled_key": True,
        "serial_number": serial,
        "device_id": serial,
        "hardware_generation": "solarflow_zensdk",
        "hardware_model": "",
        "output_control": False,
        "mqtt": {
            "broker_ref": "local_b1",
            "source": "local_mqtt",
            "topic_family": "zensdk_ha_scalar",
            "base_topic": "Zendure",
            "device_id": serial,
        },
        "capabilities": {
            "read_power": True,
            "read_soc": True,
            "write_output_limit": False,
        },
    }


def _preview(draft, tmp_path):
    result = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert result["status"] == "ok"
    return result


# --- duplicate prevention: same physical serial over two transports ---------


def test_same_serial_api_and_mqtt_fails_preview(tmp_path):
    _write_config(tmp_path, _config([_mqtt_device(name="WR1", **_CUSTOM_TUNING)]))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"].append(
        {
            "original_name": None,
            "name": "INV_2",
            "ip": "192.0.2.10",
            "sn": "PHYSICAL-001",
            "enabled": True,
        }
    )
    result = _preview(draft, tmp_path)
    codes = [issue["code"] for issue in result["validation"]["errors"]]
    assert "zendure_device_identity_duplicate" in codes


# --- MQTT -> Local API -------------------------------------------------------


def test_switch_mqtt_to_api_preserves_name_and_tuning(tmp_path):
    _write_config(tmp_path, _config([_mqtt_device(name="WR1", **_CUSTOM_TUNING)]))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"] = [_switch_to_api_item("WR1", "WR1")]
    result = _preview(draft, tmp_path)
    assert result["validation"]["ok"], result["validation"]["errors"]
    devices = result["preview"]["devices"]
    assert len(devices) == 1
    device = devices[0]
    assert device["name"] == "WR1"
    assert device["ip"] == "192.0.2.10"
    assert device["sn"] == "PHYSICAL-001"
    for key, value in _CUSTOM_TUNING.items():
        assert device.get(key) == value, f"{key} lost during MQTT->API switch"
    # Stale MQTT-only configuration must be gone.
    assert "type" not in device
    assert "mqtt" not in device
    assert "serial_number" not in device
    assert "capabilities" not in device
    assert "device_id" not in device


def test_switch_mqtt_to_api_removes_profile_fields(tmp_path):
    device = _mqtt_device(name="WR1", **_CUSTOM_TUNING)
    device["hardware_profile"] = "solarFlow800Pro2"
    device["power_write_profile"] = "zensdk_properties_write"
    _write_config(tmp_path, _config([device]))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"] = [_switch_to_api_item("WR1", "WR1")]
    result = _preview(draft, tmp_path)
    out = result["preview"]["devices"][0]
    assert "hardware_profile" not in out
    assert "power_write_profile" not in out


# --- Local API -> MQTT -------------------------------------------------------


def test_switch_api_to_mqtt_preserves_name_and_tuning(tmp_path):
    _write_config(
        tmp_path,
        _config(
            [
                _api_device(name="WR1", **_CUSTOM_TUNING),
                _api_device(name="WR2", serial="PHYSICAL-002", ip="192.168.1.101"),
            ]
        ),
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"] = [_switch_to_mqtt_item("WR1", "WR1"), draft["devices"][1]]
    result = _preview(draft, tmp_path)
    assert result["validation"]["ok"], result["validation"]["errors"]
    devices = result["preview"]["devices"]
    assert len(devices) == 2
    device = devices[0]
    assert device["name"] == "WR1"
    assert device["type"] == "zendure_mqtt"
    assert device["serial_number"] == "PHYSICAL-001"
    assert device["mqtt"]["topic_family"] == "zensdk_ha_scalar"
    for key, value in _CUSTOM_TUNING.items():
        assert device.get(key) == value, f"{key} lost during API->MQTT switch"
    # Stale Local API connection fields must be gone.
    assert "ip" not in device
    assert "sn" not in device


def test_switch_api_to_cloud_mqtt_preserves_name_and_tuning(tmp_path):
    config = _config(
        [
            _api_device(name="WR1", **_CUSTOM_TUNING),
            _api_device(name="WR2", serial="PHYSICAL-002", ip="192.168.1.101"),
        ]
    )
    config["zendure_mqtt"]["brokers"]["cloud_b"] = {
        "enabled": True,
        "source": "zendure_cloud_mqtt",
        "host": "mqtt.zen-iot.com",
        "port": 8883,
        "tls": True,
        "credentials_ref": "zendure-cloud",
    }
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    switched = _switch_to_mqtt_item("WR1", "WR1")
    switched["mqtt"]["broker_ref"] = "cloud_b"
    switched["mqtt"]["source"] = "zendure_cloud_mqtt"
    switched["mqtt"]["topic_family"] = "zendure_cloud_scalar"
    draft["devices"] = [switched, draft["devices"][1]]
    result = _preview(draft, tmp_path)
    assert result["validation"]["ok"], result["validation"]["errors"]
    device = result["preview"]["devices"][0]
    assert device["name"] == "WR1"
    assert device["type"] == "zendure_mqtt"
    assert device["mqtt"]["broker_ref"] == "cloud_b"
    assert device["mqtt"]["source"] == "zendure_cloud_mqtt"
    for key, value in _CUSTOM_TUNING.items():
        assert device.get(key) == value, f"{key} lost during API->cloud MQTT switch"
    assert "ip" not in device
    assert "sn" not in device


# --- rename + transport switch ----------------------------------------------


def test_rename_plus_switch_replaces_single_entry(tmp_path):
    _write_config(tmp_path, _config([_mqtt_device(name="WR1", **_CUSTOM_TUNING)]))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    draft["devices"] = [_switch_to_api_item("WR1", "INV_1")]
    prepared = prepare_maintenance_config_apply(
        draft, loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    names = [device["name"] for device in merged["devices"]]
    assert names == ["INV_1"]
    device = merged["devices"][0]
    assert "mqtt" not in device
    for key, value in _CUSTOM_TUNING.items():
        assert device.get(key) == value


# --- identity fallback and fail-closed conflicts ------------------------------


def test_identity_fallback_matches_original_without_original_name(tmp_path):
    """A draft that lost original_name still replaces its original by serial."""

    _write_config(tmp_path, _config([_mqtt_device(name="WR1", **_CUSTOM_TUNING)]))
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    item = _switch_to_api_item("", "INV_1")
    item["original_name"] = None
    draft["devices"] = [item]
    result = _preview(draft, tmp_path)
    assert result["validation"]["ok"], result["validation"]["errors"]
    devices = result["preview"]["devices"]
    assert len(devices) == 1
    assert devices[0]["name"] == "INV_1"
    for key, value in _CUSTOM_TUNING.items():
        assert devices[0].get(key) == value


def test_conflicting_identity_evidence_fails_closed(tmp_path):
    """original_name and physical serial pointing at different originals is an error."""

    _write_config(
        tmp_path,
        _config(
            [
                _mqtt_device(name="WR1", serial="PHYSICAL-001"),
                _api_device(name="WR2", serial="PHYSICAL-002", ip="192.168.1.101"),
            ]
        ),
    )
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    # Claims WR2 as its original but carries WR1's physical serial.
    conflicting = _switch_to_api_item("WR2", "WR2", serial="PHYSICAL-001")
    draft["devices"] = [draft["devices"][0], conflicting]
    result = _preview(draft, tmp_path)
    assert not result["validation"]["ok"]
    codes = [issue["code"] for issue in result["validation"]["errors"]]
    assert any(
        code in ("device_identity_conflict", "zendure_device_identity_duplicate")
        for code in codes
    ), codes
