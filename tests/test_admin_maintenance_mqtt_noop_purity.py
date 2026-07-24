# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maintenance no-op purity for Zendure MQTT device fields.

Loading an existing Zendure MQTT device into a Maintenance draft and applying it
unchanged must not normalize or add optional fields: a custom ``mqtt.base_topic``
must survive, an absent ``base_topic`` must stay absent, and an absent
``capabilities.write_output_limit`` must not be injected. Topic identity is only
re-homed when the operator explicitly changes the hardware generation.
"""

import copy
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


def _config_with_device(mqtt, capabilities, broker=None):
    broker = broker or {
        "enabled": True, "source": "local_mqtt", "host": "10.0.0.10", "port": 1883,
    }
    device = {
        "type": "zendure_mqtt",
        "name": "Zendure",
        "enabled": True,
        "serial_number": "S1",
        "mqtt": mqtt,
    }
    if capabilities is not None:
        device["capabilities"] = capabilities
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "192.168.1.100", "sn": "AAA", "max_power": 800},
            device,
        ],
        "grid_meter": {"type": "shelly", "ip": "192.168.1.50"},
        "zendure_mqtt": {"enabled": True, "brokers": {mqtt["broker_ref"]: broker}},
    }


def _assert_noop(config, tmp_path):
    original = copy.deepcopy(config)
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"
    draft = loaded["draft"]
    preview = preview_maintenance_config(draft, base_dir=str(tmp_path))
    assert preview["status"] == "ok"
    assert preview["changed"] is False, preview["diff"]
    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))
    assert prepared["status"] == "ok", prepared
    assert json.loads(prepared["payload"]) == original
    return original


def _telemetry_caps():
    return {"read_power": True, "read_soc": True, "write_output_limit": False}


def test_custom_zensdk_base_topic_survives_noop(tmp_path):
    config = _config_with_device(
        {"broker_ref": "b1", "topic_family": "zensdk_ha_scalar",
         "base_topic": "my/custom/prefix", "device_id": "S1"},
        _telemetry_caps(),
    )
    _assert_noop(config, tmp_path)


def test_custom_legacy_base_topic_survives_noop(tmp_path):
    config = _config_with_device(
        {"broker_ref": "b1", "topic_family": "legacy_zendure_json",
         "base_topic": "iot2", "device_id": "S1", "product_key": "PK1"},
        _telemetry_caps(),
    )
    _assert_noop(config, tmp_path)


def test_absent_base_topic_stays_absent_on_noop(tmp_path):
    config = _config_with_device(
        {"broker_ref": "b1", "topic_family": "zensdk_ha_scalar", "device_id": "S1"},
        _telemetry_caps(),
    )
    original = _assert_noop(config, tmp_path)
    dev = [d for d in original["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert "base_topic" not in dev["mqtt"]


def test_cloud_device_without_base_topic_noop(tmp_path):
    config = _config_with_device(
        {"broker_ref": "zendure_cloud", "topic_family": "zendure_cloud_scalar",
         "device_id": "S1", "product_key": "PKCLOUD"},
        _telemetry_caps(),
        broker={"enabled": True, "source": "zendure_cloud_mqtt",
                "host": "mqtteu.zen-iot.com", "port": 8883, "tls": True,
                "tls_insecure": True, "credentials_ref": "zendure-cloud"},
    )
    original = _assert_noop(config, tmp_path)
    dev = [d for d in original["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert "base_topic" not in dev["mqtt"]


def test_absent_write_output_limit_not_injected_on_noop(tmp_path):
    config = _config_with_device(
        {"broker_ref": "b1", "topic_family": "zensdk_ha_scalar",
         "base_topic": "Zendure", "device_id": "S1"},
        {"read_power": True, "read_soc": True},
    )
    original = _assert_noop(config, tmp_path)
    dev = [d for d in original["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert "write_output_limit" not in dev["capabilities"]


def test_absent_capabilities_object_not_injected_on_noop(tmp_path):
    config = _config_with_device(
        {"broker_ref": "b1", "topic_family": "zensdk_ha_scalar",
         "base_topic": "Zendure", "device_id": "S1"},
        None,
    )
    original = _assert_noop(config, tmp_path)
    dev = [d for d in original["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert "capabilities" not in dev


# --- explicit generation change still re-homes topic identity -------------


def test_explicit_generation_change_rehomes_topic_identity(tmp_path):
    config = _config_with_device(
        {"broker_ref": "b1", "topic_family": "zensdk_ha_scalar",
         "base_topic": "Zendure", "device_id": "S1"},
        _telemetry_caps(),
    )
    _write_config(tmp_path, config)
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    draft = loaded["draft"]
    dev = [d for d in draft["devices"] if d.get("kind") == "zendure_mqtt"][0]
    dev["hardware_profile"] = "hub_hyper_legacy"
    dev["product_key"] = "PK9"
    prepared = prepare_maintenance_config_apply(draft, loaded["revision"], base_dir=str(tmp_path))
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    out = [d for d in merged["devices"] if d.get("type") == "zendure_mqtt"][0]
    assert out["mqtt"]["topic_family"] == "legacy_zendure_json"
    assert out["mqtt"]["base_topic"] == "iot"
