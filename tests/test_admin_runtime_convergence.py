# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config -> runtime-state convergence (Tier 2) for Admin maintenance."""

import json

import pytest

from admin.install_context import detect_install_context
from admin.runtime_convergence import (
    mirror_changed_keys_to_runtime,
    reset_targets_to_config,
)

pytestmark = pytest.mark.simulation


def _config(**system):
    base_system = {
        "enabled": True,
        "max_total_power": 1600,
        "loop_interval": 3,
        "min_output_limit": 35,
    }
    base_system.update(system)
    return {
        "system": base_system,
        "winter": {"enabled": False},
        "devices": [
            {
                "name": "WR1",
                "ip": "192.168.1.100",
                "sn": "AAA",
                "max_power": 800,
                "pv_priority_factor": 1.0,
                "enabled": True,
                "min_soc": 10,
            },
        ],
    }


def _seed_runtime(tmp_path, *, system=None, winter=None, devices=None):
    data = {
        "system": {
            "enabled": True,
            "max_total_power": 1600,
            "loop_interval": 3,
            "min_output_limit": 35,
        },
        "winter": {"enabled": False},
        "devices": {
            "WR1": {
                "enabled": True,
                "max_power": 800,
                "offgrid_socket_mode": "off",
                "pv_priority_factor": 1.0,
            }
        },
    }
    if system:
        data["system"].update(system)
    if winter:
        data["winter"].update(winter)
    if devices is not None:
        data["devices"] = devices
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _context(tmp_path):
    return detect_install_context(base_dir=str(tmp_path))


def _runtime(tmp_path):
    return json.loads((tmp_path / "runtime-state.json").read_text())


def test_mirror_changed_system_key(tmp_path):
    _seed_runtime(tmp_path)
    config = _config(loop_interval=7)
    result = mirror_changed_keys_to_runtime(
        _context(tmp_path), config, ["system.loop_interval"]
    )
    assert result["applied"] == ["system.loop_interval"]
    assert result["skipped"] == []
    assert _runtime(tmp_path)["system"]["loop_interval"] == 7


def test_mirror_winter_enabled(tmp_path):
    _seed_runtime(tmp_path)
    config = _config()
    config["winter"]["enabled"] = True
    result = mirror_changed_keys_to_runtime(
        _context(tmp_path), config, ["winter.enabled"]
    )
    assert result["applied"] == ["winter.enabled"]
    assert _runtime(tmp_path)["winter"]["enabled"] is True


def test_mirror_device_key(tmp_path):
    _seed_runtime(tmp_path)
    config = _config()
    config["devices"][0]["max_power"] = 600
    result = mirror_changed_keys_to_runtime(
        _context(tmp_path), config, ["devices[0].max_power"]
    )
    assert result["applied"] == ["devices[0].max_power"]
    assert _runtime(tmp_path)["devices"]["WR1"]["max_power"] == 600


def test_pure_config_key_is_not_mirrored(tmp_path):
    _seed_runtime(tmp_path)
    config = _config()
    result = mirror_changed_keys_to_runtime(
        _context(tmp_path), config, ["grid_meter.ip", "devices[0].min_soc"]
    )
    assert result["applied"] == []
    assert _runtime(tmp_path)["system"]["loop_interval"] == 3


def test_value_over_runtime_ceiling_is_skipped(tmp_path):
    _seed_runtime(tmp_path)
    config = _config(max_total_power=6000)
    result = mirror_changed_keys_to_runtime(
        _context(tmp_path), config, ["system.max_total_power"]
    )
    assert result["applied"] == []
    assert result["skipped"][0]["path"] == "system.max_total_power"
    assert _runtime(tmp_path)["system"]["max_total_power"] == 1600


def test_new_device_is_skipped_and_not_created(tmp_path):
    _seed_runtime(tmp_path)
    config = _config()
    config["devices"].append(
        {"name": "WR2", "ip": "192.168.1.101", "sn": "BBB", "max_power": 700}
    )
    result = mirror_changed_keys_to_runtime(
        _context(tmp_path), config, ["devices[1].max_power"]
    )
    assert result["applied"] == []
    assert result["skipped"][0]["path"] == "devices[1].max_power"
    assert "WR2" not in _runtime(tmp_path)["devices"]


def test_missing_runtime_file_is_skipped_and_not_created(tmp_path):
    config = _config(loop_interval=7)
    result = mirror_changed_keys_to_runtime(
        _context(tmp_path), config, ["system.loop_interval"]
    )
    assert result["applied"] == []
    assert result["skipped"][0]["reason"] == "runtime_state_absent"
    assert not (tmp_path / "runtime-state.json").exists()


def test_mirror_preserves_untouched_override(tmp_path):
    _seed_runtime(tmp_path, system={"max_total_power": 900})
    config = _config(loop_interval=7)
    mirror_changed_keys_to_runtime(_context(tmp_path), config, ["system.loop_interval"])
    data = _runtime(tmp_path)
    assert data["system"]["loop_interval"] == 7
    assert data["system"]["max_total_power"] == 900


def test_reset_writes_config_value_not_delete(tmp_path):
    _seed_runtime(tmp_path, system={"loop_interval": 5})
    config = _config(loop_interval=3)
    result = reset_targets_to_config(
        _context(tmp_path), config, [{"scope": "system", "key": "loop_interval"}]
    )
    assert result["applied"] == ["system.loop_interval"]
    data = _runtime(tmp_path)
    assert "loop_interval" in data["system"]
    assert data["system"]["loop_interval"] == 3


def test_reset_device_key_to_config(tmp_path):
    _seed_runtime(
        tmp_path,
        devices={
            "WR1": {
                "enabled": True,
                "max_power": 600,
                "offgrid_socket_mode": "off",
                "pv_priority_factor": 1.0,
            }
        },
    )
    config = _config()
    result = reset_targets_to_config(
        _context(tmp_path),
        config,
        [{"scope": "device", "name": "WR1", "key": "max_power"}],
    )
    assert result["applied"] == ["devices.WR1.max_power"]
    assert _runtime(tmp_path)["devices"]["WR1"]["max_power"] == 800


def test_renamed_device_is_skipped_and_old_entry_untouched(tmp_path):
    _seed_runtime(tmp_path)
    config = _config()
    config["devices"][0]["name"] = "WRX"
    config["devices"][0]["max_power"] = 500
    result = mirror_changed_keys_to_runtime(
        _context(tmp_path), config, ["devices[0].max_power"]
    )
    assert result["applied"] == []
    assert result["skipped"][0]["path"] == "devices[0].max_power"
    data = _runtime(tmp_path)
    assert "WRX" not in data["devices"]
    assert data["devices"]["WR1"]["max_power"] == 800


def test_mixed_valid_and_invalid_keys_apply_per_key(tmp_path):
    _seed_runtime(tmp_path)
    config = _config(loop_interval=7, max_total_power=6000)
    result = mirror_changed_keys_to_runtime(
        _context(tmp_path),
        config,
        ["system.loop_interval", "system.max_total_power"],
    )
    assert result["applied"] == ["system.loop_interval"]
    assert [entry["path"] for entry in result["skipped"]] == ["system.max_total_power"]
    data = _runtime(tmp_path)
    assert data["system"]["loop_interval"] == 7
    assert data["system"]["max_total_power"] == 1600


def test_reset_writes_config_default_when_config_silent(tmp_path):
    _seed_runtime(
        tmp_path,
        devices={
            "WR1": {
                "enabled": False,
                "max_power": 800,
                "offgrid_socket_mode": "off",
                "pv_priority_factor": 1.0,
            }
        },
    )
    config = _config()
    del config["devices"][0]["enabled"]
    result = reset_targets_to_config(
        _context(tmp_path),
        config,
        [{"scope": "device", "name": "WR1", "key": "enabled"}],
    )
    assert result["applied"] == ["devices.WR1.enabled"]
    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is True


@pytest.mark.power_control
def test_converged_value_wins_over_config_default_in_resolver(tmp_path):
    from ems.runtime_state import RuntimeState

    _seed_runtime(tmp_path)
    config = _config(loop_interval=7)
    mirror_changed_keys_to_runtime(_context(tmp_path), config, ["system.loop_interval"])

    runtime_state = RuntimeState(
        str(tmp_path / "runtime-state.json"),
        {"system": {"loop_interval": 3}, "devices": {}},
    )
    runtime_state.load_if_changed(force=True)
    assert runtime_state.get_system("loop_interval", 3) == 7
