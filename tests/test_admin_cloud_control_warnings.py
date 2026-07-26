# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cloud-MQTT control single-controller advisory (Admin + diagnostics)."""

import json

import pytest

from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
)
from ems.diagnostics import diagnose_zendure_mqtt_device_config

pytestmark = [pytest.mark.simulation]

WARNING_CODE = "zendure_cloud_mqtt_single_controller"


def _config(source="zendure_cloud_mqtt", control=True, enabled=True):
    return {
        "system": {"max_total_power": 1600},
        "devices": [
            {"name": "WR1", "ip": "10.0.0.1", "sn": "REAL1", "max_power": 800},
            {
                "type": "zendure_mqtt",
                "name": "INV_2",
                "enabled": enabled,
                "serial_number": "SN2",
                "hardware_profile": "solarflow_800_pro_2",
                "mqtt": {
                    "broker_ref": "broker_a",
                    "topic_family": "legacy_zendure_json_alt",
                    "device_id": "DEV2",
                    "product_key": "PK",
                },
                "capabilities": {
                    "read_power": True,
                    "read_soc": True,
                    "write_output_limit": control,
                },
            },
        ],
        "grid_meter": {"type": "shelly", "ip": "10.0.0.9"},
        "zendure_mqtt": {
            "brokers": {
                "broker_a": {
                    "enabled": True,
                    "source": source,
                    "host": "mqtteu.zen-iot.com" if source == "zendure_cloud_mqtt" else "10.0.0.10",
                    "port": 8883 if source == "zendure_cloud_mqtt" else 1883,
                    **(
                        {"tls": True, "credentials_ref": "cloud-cred"}
                        if source == "zendure_cloud_mqtt"
                        else {}
                    ),
                },
            },
        },
    }


def _maintenance_warnings(tmp_path, config):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    return {w["code"] for w in prepared["validation"]["warnings"]}


def test_maintenance_warns_for_enabled_cloud_control_device(
    tmp_path, isolated_install_root
):
    assert WARNING_CODE in _maintenance_warnings(tmp_path, _config())


def test_maintenance_does_not_warn_for_local_mqtt_control(
    tmp_path, isolated_install_root
):
    assert WARNING_CODE not in _maintenance_warnings(
        tmp_path, _config(source="local_mqtt")
    )


def test_maintenance_does_not_warn_for_telemetry_only_cloud_device(
    tmp_path, isolated_install_root
):
    assert WARNING_CODE not in _maintenance_warnings(
        tmp_path, _config(control=False)
    )


def test_diagnostics_reports_cloud_control_single_controller_notice():
    checks = []
    device = _config()["devices"][1]
    diagnose_zendure_mqtt_device_config(
        checks, 1, device, broker_sources={"broker_a": "zendure_cloud_mqtt"}
    )
    codes = {(c["code"], c["level"]) for c in checks}
    assert (WARNING_CODE, "info") in codes


def test_diagnostics_stays_quiet_for_local_control():
    checks = []
    device = _config(source="local_mqtt")["devices"][1]
    diagnose_zendure_mqtt_device_config(
        checks, 1, device, broker_sources={"broker_a": "local_mqtt"}
    )
    assert WARNING_CODE not in {c["code"] for c in checks}
