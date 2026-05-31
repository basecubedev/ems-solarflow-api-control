import json
from pathlib import Path
from types import SimpleNamespace

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
    assert template["system"]["dry_run"] is False
    assert template["system"]["allow_hardware_writes"] is True
    assert template["system"]["allow_state_reconciliation_writes"] is True
    assert template["system"]["reconcile_ac_mode_on_start"] is True
    assert template["system"]["reconcile_smart_mode"] is True


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
