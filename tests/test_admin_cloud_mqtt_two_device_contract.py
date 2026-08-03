# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin/live-config contract: two Zendure Cloud MQTT control devices survive
preview, apply, reload and runtime construction intact.

The live no-power-change symptom could equally have been a configuration
persistence bug (a UI/apply step silently dropping the control capability, the
pinned hardware profile, the broker reference or a write gate). This contract
pins every field the cloud write path depends on across the full journey:

    maintenance preview -> apply payload -> runtime config load -> control runtime

so a runtime-writer defect can never mask a persistence defect (or vice versa).
"""

import copy
import json

import pytest

from admin.credential_store import CredentialStore
from admin.maintenance_config import (
    load_maintenance_config,
    prepare_maintenance_config_apply,
)
from ems import config as ems_config
from ems.mqtt_credentials import FileMqttCredentialResolver
from ems.zendure_mqtt.control_runtime import build_zendure_mqtt_control_runtime

pytestmark = [
    pytest.mark.admin,
    pytest.mark.mqtt,
    pytest.mark.contract,
    pytest.mark.simulation,
    pytest.mark.power_control,
]

CLOUD_REF = "zendure_cloud"
CREDENTIALS_REF = "zendure-cloud-account"


def _two_device_cloud_config(*, cloud_gate, dry_run, simulation_mode):
    def _device(name, device_id, serial):
        return {
            "type": "zendure_mqtt",
            "name": name,
            "enabled": True,
            "serial_number": serial,
            "hardware_profile": "solarflow_800_pro_2",
            "power_write_profile": "zensdk_properties_write",
            "mqtt": {
                "broker_ref": CLOUD_REF,
                "topic_family": "legacy_zendure_json_alt",
                "device_id": device_id,
                "product_key": "PKCLOUD",
            },
            "capabilities": {
                "read_power": True,
                "read_soc": True,
                "write_output_limit": True,
            },
        }

    return {
        "system": {
            "max_total_power": 1600,
            "dry_run": dry_run,
            "simulation_mode": simulation_mode,
            "allow_hardware_writes": True,
            "allow_mqtt_local_control_writes": True,
            "allow_mqtt_zendure_control_writes": cloud_gate,
            "allow_state_reconciliation_writes": True,
        },
        "devices": [
            {"name": "WR1", "ip": "10.0.0.1", "sn": "REALHTTP1", "max_power": 800},
            _device("INV_2", "CLOUDDEV1", "SNCLOUD1"),
            _device("INV_3", "CLOUDDEV2", "SNCLOUD2"),
        ],
        "grid_meter": {"type": "shelly", "ip": "10.0.0.9"},
        "zendure_mqtt": {
            "enabled": True,
            "brokers": {
                CLOUD_REF: {
                    "enabled": True,
                    "source": "zendure_cloud_mqtt",
                    "host": "mqtteu.zen-iot.com",
                    "port": 8883,
                    "tls": True,
                    "tls_insecure": True,
                    "credentials_ref": CREDENTIALS_REF,
                },
            },
        },
    }


def _write_config(tmp_path, config):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return config_dir


def _cloud_devices(config):
    return [
        d
        for d in config.get("devices", [])
        if d.get("type") == "zendure_mqtt"
        and (d.get("mqtt") or {}).get("broker_ref") == CLOUD_REF
    ]


def _assert_control_fields_intact(device):
    assert device["enabled"] is True
    assert device["capabilities"]["write_output_limit"] is True
    assert device["hardware_profile"] == "solarflow_800_pro_2"
    mqtt = device["mqtt"]
    assert mqtt["broker_ref"] == CLOUD_REF
    assert mqtt["product_key"] == "PKCLOUD"
    assert mqtt["device_id"] in ("CLOUDDEV1", "CLOUDDEV2")


@pytest.mark.parametrize("cloud_gate", [True, False])
def test_two_cloud_control_devices_survive_preview_apply_reload_runtime(
    tmp_path, monkeypatch, isolated_install_root, cloud_gate
):
    monkeypatch.setenv("EMS_CONFIG_DIR", str(tmp_path / "config"))
    original = _two_device_cloud_config(
        cloud_gate=cloud_gate, dry_run=False, simulation_mode=False
    )
    _write_config(tmp_path, copy.deepcopy(original))

    # Preview: the maintenance draft must surface both cloud control devices.
    loaded = load_maintenance_config(base_dir=str(tmp_path))
    assert loaded["status"] == "ok"
    draft_entries = [
        d for d in loaded["draft"]["devices"] if d.get("kind") == "zendure_mqtt"
    ]
    assert len(draft_entries) == 2

    # Apply (no-op maintenance roundtrip): every control field must survive.
    prepared = prepare_maintenance_config_apply(
        loaded["draft"], loaded["revision"], base_dir=str(tmp_path)
    )
    assert prepared["status"] == "ok", prepared
    merged = json.loads(prepared["payload"])
    applied_devices = _cloud_devices(merged)
    assert len(applied_devices) == 2
    for device in applied_devices:
        _assert_control_fields_intact(device)
    broker = merged["zendure_mqtt"]["brokers"][CLOUD_REF]
    assert broker["source"] == "zendure_cloud_mqtt"
    assert broker["credentials_ref"] == CREDENTIALS_REF

    # The operator's explicit gate and mode values are preserved verbatim —
    # an explicit False is never "upgraded" back to the release default.
    system = merged["system"]
    assert system["allow_mqtt_zendure_control_writes"] is cloud_gate
    assert system["dry_run"] is False
    assert system["simulation_mode"] is False

    # Reload: the runtime config-defaults pass must keep the same values.
    runtime_config = ems_config.apply_runtime_config_defaults(
        copy.deepcopy(merged)
    )
    runtime_system = runtime_config["system"]
    assert runtime_system["allow_mqtt_zendure_control_writes"] is cloud_gate
    assert runtime_system["dry_run"] is False
    assert runtime_system["simulation_mode"] is False

    # Runtime construction: both devices become accepted cloud control devices.
    store = CredentialStore(config_dir=str(tmp_path / "config"))
    store.save_mqtt_cloud_runtime_secret(
        CREDENTIALS_REF,
        username="cloud-user",
        password="cloud-pass-SECRET",
        client_id="cloud-client",
        app_key="cloud-app-key-SECRET",
    )
    resolver = FileMqttCredentialResolver(tmp_path / "config" / "secrets")
    runtime = build_zendure_mqtt_control_runtime(
        runtime_config, credential_resolver=resolver
    )
    assert [r.name for r in runtime.rejected] == []
    assert sorted(dev.name for dev in runtime.devices) == ["INV_2", "INV_3"]
    for dev in runtime.devices:
        assert dev.source == "zendure_cloud_mqtt"
        assert dev.control_gate == "mqtt_zendure"
        assert dev.broker_ref == CLOUD_REF
        assert dev.hardware_profile == "solarflow_800_pro_2"

    # No stage may leak the stored secret into config or status payloads.
    for blob in (json.dumps(merged), json.dumps(runtime.status())):
        assert "cloud-pass-SECRET" not in blob
        assert "cloud-app-key-SECRET" not in blob
