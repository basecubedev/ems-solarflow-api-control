import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ems import config as cfg
from ems.config import (
    DASHBOARD_DEFAULTS,
    ENERGY_SAVINGS_DEFAULTS,
    OUTPUT_CONTROL_DEFAULTS,
    WINTER_DEFAULTS,
)


def without_comment_keys(values):
    return {
        key: value
        for key, value in values.items()
        if not key.startswith("_comment")
    }


def snapshot_config_module():
    names = [
        name for name in dir(cfg)
        if name.isupper() or name in ("ARGS", "BASE_DIR", "CONFIG")
    ]
    return {name: getattr(cfg, name) for name in names}


def restore_config_module(snapshot):
    for name, value in snapshot.items():
        setattr(cfg, name, value)


def test_config_template_output_control_matches_code_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert (
        without_comment_keys(template["system"]["output_control"])
        == OUTPUT_CONTROL_DEFAULTS
    )


def test_config_template_winter_matches_code_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert without_comment_keys(template["winter"]) == WINTER_DEFAULTS


def test_config_template_dashboard_matches_code_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert without_comment_keys(template["dashboard"]) == DASHBOARD_DEFAULTS


def test_config_template_energy_savings_matches_code_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert without_comment_keys(template["energy_savings"]) == ENERGY_SAVINGS_DEFAULTS


def test_config_template_standalone_live_control_defaults():
    template = json.loads(Path("config.template.json").read_text())

    assert template["ha"]["enabled"] is False
    assert template["ha"]["control_enabled"] is False
    assert template["grid_meter"]["type"] == "shelly"
    assert template["grid_meter"]["ip"] == template["shelly"]["ip"]
    removed_key = "chan" + "nel"
    assert removed_key not in template["grid_meter"]
    assert template["system"]["dry_run"] is False
    assert template["system"]["allow_hardware_writes"] is True
    assert template["system"]["allow_state_reconciliation_writes"] is True
    assert template["system"]["reconcile_ac_mode_on_start"] is True
    assert template["system"]["reconcile_smart_mode"] is True


def base_minimal_config():
    return {
        "ha": {
            "enabled": False,
            "control_enabled": False,
            "url": "",
            "token": "",
        },
        "system": {
            "enabled": True,
            "dry_run": False,
            "simulation_mode": False,
            "allow_hardware_writes": True,
            "allow_state_reconciliation_writes": True,
            "reconcile_ac_mode_on_start": True,
            "reconcile_smart_mode": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
            "loop_interval": 5,
        },
        "devices": [],
        "shelly": {
            "ip": "192.168.1.50",
        },
    }


def initialize_config_from_dict(tmp_path, values):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(values))
    args = SimpleNamespace(
        config=str(config_path),
        dry_run=False,
        simulate=False,
        replay=None,
        self_test=False,
        no_ha=False,
    )
    cfg.initialize(args, str(tmp_path))


def test_grid_meter_defaults_to_shelly_compatible_safe_config():
    safe_config = cfg.default_safe_config()

    assert safe_config["grid_meter"] == {"type": "shelly", "ip": ""}
    assert safe_config["shelly"] == {"ip": ""}


def test_legacy_shelly_ip_fallback_populates_grid_meter(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values.pop("grid_meter", None)
    values["shelly"]["ip"] = "192.168.1.51"

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.GRID_METER_CONFIG == {
            "type": "shelly",
            "ip": "192.168.1.51",
        }
        assert cfg.SHELLY_IP == "192.168.1.51"
    finally:
        restore_config_module(snapshot)


def test_explicit_grid_meter_overrides_legacy_shelly(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["shelly"]["ip"] = "192.168.1.51"
    values["grid_meter"] = {
        "type": "ecotracker",
        "ip": "192.168.1.60",
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.GRID_METER_CONFIG == {
            "type": "ecotracker",
            "ip": "192.168.1.60",
        }
        assert cfg.SHELLY_IP == "192.168.1.51"
    finally:
        restore_config_module(snapshot)


def test_shelly_grid_meter_config_preserves_channels(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["grid_meter"] = {
        "type": "shelly",
        "ip": "192.168.1.50",
        "channels": ["A", "C"],
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.GRID_METER_CONFIG == {
            "type": "shelly",
            "ip": "192.168.1.50",
            "channels": ["a", "c"],
        }
    finally:
        restore_config_module(snapshot)


def test_shelly_grid_meter_config_rejects_channels_string(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["grid_meter"] = {
        "type": "shelly",
        "ip": "192.168.1.50",
        "channels": "c",
    }

    try:
        with pytest.raises(ValueError, match="grid_meter.channels must be a list"):
            initialize_config_from_dict(tmp_path, values)
    finally:
        restore_config_module(snapshot)


def test_shelly_grid_meter_config_rejects_empty_channel_entry(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["grid_meter"] = {
        "type": "shelly",
        "ip": "192.168.1.50",
        "channels": ["c", ""],
    }

    try:
        with pytest.raises(
            ValueError,
            match="grid_meter.channels must not contain empty values",
        ):
            initialize_config_from_dict(tmp_path, values)
    finally:
        restore_config_module(snapshot)


def test_tasmota_grid_meter_config_preserves_url_ip_and_power_path(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["grid_meter"] = {
        "type": "tasmota_http",
        "url": "http://192.168.1.70/cm?cmnd=Status%2010",
        "ip": "192.168.1.71",
        "power_path": "StatusSNS.SM.16_7_0",
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.GRID_METER_CONFIG == {
            "type": "tasmota_http",
            "url": "http://192.168.1.70/cm?cmnd=Status%2010",
            "ip": "192.168.1.71",
            "power_path": "StatusSNS.SM.16_7_0",
        }
    finally:
        restore_config_module(snapshot)


def test_omitted_ha_keys_default_to_disabled(tmp_path):
    snapshot = snapshot_config_module()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "ha": {
            "url": "http://homeassistant.local:8123",
            "token": "TOKEN"
        },
        "system": {
            "enabled": True,
            "dry_run": False,
            "simulation_mode": False,
            "allow_hardware_writes": True,
            "allow_state_reconciliation_writes": True,
            "reconcile_ac_mode_on_start": True,
            "reconcile_smart_mode": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
            "loop_interval": 5
        },
        "devices": [],
        "shelly": {
            "ip": "192.168.1.50"
        }
    }))
    args = SimpleNamespace(
        config=str(config_path),
        dry_run=False,
        simulate=False,
        replay=None,
        self_test=False,
        no_ha=False
    )

    try:
        cfg.initialize(args, str(tmp_path))

        assert cfg.HA_ENABLED is False
        assert cfg.HA_CONTROL_ENABLED is False
        assert cfg.RECONCILE_AC_MODE_ON_START is True
        assert cfg.RECONCILE_SMART_MODE is True
    finally:
        restore_config_module(snapshot)


def test_omitted_ha_section_defaults_to_disabled(tmp_path):
    snapshot = snapshot_config_module()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "system": {
            "enabled": True,
            "dry_run": False,
            "simulation_mode": False,
            "allow_hardware_writes": True,
            "allow_state_reconciliation_writes": True,
            "reconcile_ac_mode_on_start": True,
            "reconcile_smart_mode": True,
            "max_total_power": 800,
            "max_device_power": 800,
            "deadband": 10,
            "loop_interval": 5
        },
        "devices": [],
        "shelly": {
            "ip": "192.168.1.50"
        }
    }))
    args = SimpleNamespace(
        config=str(config_path),
        dry_run=False,
        simulate=False,
        replay=None,
        self_test=False,
        no_ha=False
    )

    try:
        cfg.initialize(args, str(tmp_path))

        assert cfg.HA_ENABLED is False
        assert cfg.HA_CONTROL_ENABLED is False
        assert cfg.HA_URL == ""
        assert cfg.HA_TOKEN == ""
    finally:
        restore_config_module(snapshot)
