# SPDX-License-Identifier: AGPL-3.0-or-later
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


def test_dashboard_defaults_include_session_and_log_settings():
    assert DASHBOARD_DEFAULTS["session_idle_timeout_seconds"] == 1800
    assert DASHBOARD_DEFAULTS["session_absolute_max_seconds"] == 43200
    assert DASHBOARD_DEFAULTS["log_buffer_lines"] == 5000
    assert DASHBOARD_DEFAULTS["log_redaction"] is False


def test_dashboard_animation_mode_default_and_normalization():
    assert DASHBOARD_DEFAULTS["animation_mode"] == "normal"
    # Valid values pass through (case/space-insensitive); invalid -> normal.
    assert cfg.normalize_dashboard_config({"animation_mode": "reduced"})["animation_mode"] == "reduced"
    assert cfg.normalize_dashboard_config({"animation_mode": " OFF "})["animation_mode"] == "off"
    assert cfg.normalize_dashboard_config({"animation_mode": "bogus"})["animation_mode"] == "normal"
    assert cfg.normalize_dashboard_config({})["animation_mode"] == "normal"


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


def test_config_template_uses_persisted_data_paths():
    template = json.loads(Path("config.template.json").read_text())

    docker_only_flag = "_config" + "_initialized"
    assert docker_only_flag not in json.dumps(template)
    assert template["system"]["runtime_state_path"] == "data/runtime-state.json"
    assert template["dashboard"]["database_path"] == "data/ems_dashboard.sqlite"


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


def test_safe_session_timeout_parsing():
    # explicit positive value preserved
    assert cfg.safe_session_timeout(900, 1800) == 900
    assert cfg.safe_session_timeout("600", 1800) == 600
    # 0 is a deliberate "disabled / infinite" opt-in and is preserved
    assert cfg.safe_session_timeout(0, 1800) == 0
    # negative typo must fall back to the secure default, never silently disable
    assert cfg.safe_session_timeout(-5, 1800) == 1800
    # invalid / missing values fall back to the default
    assert cfg.safe_session_timeout("nope", 1800) == 1800
    assert cfg.safe_session_timeout(None, 43200) == 43200


def test_dashboard_config_missing_keys_fall_back_to_defaults(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["dashboard"] = {
        "enabled": True,
        "host": "127.0.0.1",
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.DASHBOARD_CONFIG["host"] == "127.0.0.1"
        assert cfg.DASHBOARD_CONFIG["session_idle_timeout_seconds"] == 1800
        assert cfg.DASHBOARD_CONFIG["session_absolute_max_seconds"] == 43200
        assert cfg.DASHBOARD_CONFIG["log_buffer_lines"] == 5000
        assert cfg.DASHBOARD_CONFIG["log_redaction"] is False
    finally:
        restore_config_module(snapshot)


def test_dashboard_session_timeout_zero_is_accepted(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["dashboard"] = {
        "session_idle_timeout_seconds": 0,
        "session_absolute_max_seconds": 0,
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.DASHBOARD_CONFIG["session_idle_timeout_seconds"] == 0
        assert cfg.DASHBOARD_CONFIG["session_absolute_max_seconds"] == 0
    finally:
        restore_config_module(snapshot)


def test_dashboard_negative_session_timeout_falls_back_to_defaults(tmp_path):
    snapshot = snapshot_config_module()
    values = base_minimal_config()
    values["dashboard"] = {
        "session_idle_timeout_seconds": -1,
        "session_absolute_max_seconds": -20,
    }

    try:
        initialize_config_from_dict(tmp_path, values)

        assert cfg.DASHBOARD_CONFIG["session_idle_timeout_seconds"] == 1800
        assert cfg.DASHBOARD_CONFIG["session_absolute_max_seconds"] == 43200
    finally:
        restore_config_module(snapshot)
