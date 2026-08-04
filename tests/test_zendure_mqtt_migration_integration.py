# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration is reachable through EMS-owned tooling and guards startup.

Normal startup must not silently rewrite the config. An unsafe, unmigrated
control config is rejected at startup with an actionable migration-required
error, while telemetry-only devices (and safely pinned control devices) do not
block. The read-only dry-run is reachable through emsctl and never writes.
"""

import json

import pytest

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.e2e,
    pytest.mark.simulation,
]


def _control_device(**over):
    device = {
        "type": "zendure_mqtt",
        "name": "Legacy",
        "mqtt": {
            "broker_ref": "local_a",
            "source": "local_mqtt",
            "topic_family": "legacy_zendure_json",
            "device_id": "DEV",
            "product_key": "PK",
        },
        "capabilities": {"write_output_limit": True},
    }
    device.update(over)
    return device


# --- startup guard ----------------------------------------------------------


def test_startup_rejects_unmigrated_control_config():
    from ems.zendure_mqtt.migration import (
        zendure_mqtt_control_migration_startup_error,
    )

    config = {"devices": [_control_device()]}
    error = zendure_mqtt_control_migration_startup_error(config)
    assert error is not None
    assert error["code"] == "zendure_mqtt_control_migration_required"
    assert error["count"] == 1


def test_startup_allows_safe_pinned_control_config():
    from ems.zendure_mqtt.migration import (
        zendure_mqtt_control_migration_startup_error,
    )

    config = {
        "devices": [
            _control_device(
                hardware_profile="hyper_2000",
                power_write_profile="legacy_object_device_automation",
            )
        ]
    }
    assert zendure_mqtt_control_migration_startup_error(config) is None


def test_startup_ignores_disabled_and_telemetry_only_devices():
    from ems.zendure_mqtt.migration import (
        zendure_mqtt_control_migration_startup_error,
    )

    telemetry = {
        "type": "zendure_mqtt",
        "name": "Telem",
        "mqtt": {"topic_family": "legacy_zendure_json", "device_id": "T"},
        "capabilities": {"read_power": True},
    }
    disabled_control = _control_device(name="Off", enabled=False)
    config = {"devices": [telemetry, disabled_control]}
    assert zendure_mqtt_control_migration_startup_error(config) is None


# --- emsctl dry-run ---------------------------------------------------------


def test_migration_is_available_through_emsctl_dry_run(tmp_path):
    import emsctl

    config = {"devices": [_control_device(product="Hyper 2000")]}
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    before = config_path.read_text(encoding="utf-8")

    rc = emsctl.main(
        [
            "--config",
            str(config_path),
            "config",
            "migrate-zendure-mqtt",
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    # A dry-run never writes the config file.
    assert config_path.read_text(encoding="utf-8") == before
