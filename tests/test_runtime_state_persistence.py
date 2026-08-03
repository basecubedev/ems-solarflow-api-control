# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import logging

import pytest

from ems.runtime_state import RuntimeState, merge_runtime_defaults

pytestmark = [
    pytest.mark.contract,
]


DEFAULTS = {
    "system": {
        "enabled": True,
        "max_total_power": 800,
        "loop_interval": 5,
    },
    "ha": {
        "enabled": False,
        "control_enabled": False,
    },
    "winter": {
        "enabled": False,
    },
    "devices": {
        "WR1": {
            "enabled": True,
            "max_power": 800,
            "offgrid_socket_mode": "off",
            "pv_priority_factor": 1.0,
        }
    },
}


def test_merge_runtime_defaults_preserves_unknown_keys_and_removes_legacy_socket():
    merged = merge_runtime_defaults(
        {
            "unknown": {"keep": True},
            "system": {
                "enabled": False,
                "extra": "preserved",
            },
            "devices": {
                "WR1": {
                    "max_power": 600,
                    "ac_charge_power_w": 200,
                    "offgrid_socket": True,
                },
                "WR2": {
                    "enabled": False,
                    "offgrid_socket": True,
                },
            },
        },
        DEFAULTS,
    )

    assert merged["unknown"] == {"keep": True}
    assert merged["system"]["enabled"] is False
    assert merged["system"]["loop_interval"] == 5
    assert merged["system"]["extra"] == "preserved"
    assert merged["devices"]["WR1"]["max_power"] == 600
    assert merged["devices"]["WR1"]["ac_charge_power_w"] == 200
    assert "offgrid_socket" not in merged["devices"]["WR1"]
    assert "offgrid_socket" not in merged["devices"]["WR2"]
    assert merged["devices"]["WR2"]["enabled"] is False


def test_runtime_state_creates_default_file_and_saves_atomically(tmp_path):
    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)

    data = state.load_or_create()

    assert path.exists()
    assert not (tmp_path / "runtime-state.json.tmp").exists()
    assert data["system"]["enabled"] is True
    assert data["devices"]["WR1"]["max_power"] == 800

    assert state.set_system("enabled", False) is True
    assert state.set_system("enabled", False) is False
    assert state.set_section("ha", "enabled", True) is True
    assert state.set_device("WR1", "max_power", 650) is True
    state.save_atomic()

    saved = json.loads(path.read_text())
    assert saved["system"]["enabled"] is False
    assert saved["ha"]["enabled"] is True
    assert saved["devices"]["WR1"]["max_power"] == 650


def test_runtime_state_recreates_configured_data_path_without_root_migration(tmp_path):
    data_path = tmp_path / "data" / "runtime-state.json"
    old_root_path = tmp_path / "runtime-state.json"
    old_root_payload = {
        "system": {
            "enabled": False,
            "max_total_power": 123,
        }
    }
    old_root_path.write_text(json.dumps(old_root_payload))

    state = RuntimeState(str(data_path), DEFAULTS)
    data = state.load_or_create()

    assert data_path.exists()
    assert json.loads(old_root_path.read_text()) == old_root_payload
    assert data["system"]["enabled"] is True
    assert data["system"]["max_total_power"] == 800
    assert data["devices"]["WR1"]["enabled"] is True


def test_runtime_state_load_if_changed_merges_external_updates(tmp_path):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({
        "system": {"enabled": False},
        "devices": {"WR1": {"max_power": 700}},
    }))
    state = RuntimeState(str(path), DEFAULTS)

    data = state.load_or_create()

    assert data["system"]["enabled"] is False
    assert data["system"]["loop_interval"] == 5
    assert data["devices"]["WR1"]["max_power"] == 700
    assert data["devices"]["WR1"]["pv_priority_factor"] == 1.0

    path.write_text(json.dumps({
        "system": {"enabled": True},
        "devices": {"WR1": {"max_power": 500}},
    }))

    data = state.load_if_changed(force=True)

    assert data["system"]["enabled"] is True
    assert data["devices"]["WR1"]["max_power"] == 500


def test_runtime_state_invalid_json_keeps_current_data_and_logs_warning(tmp_path, caplog):
    path = tmp_path / "runtime-state.json"
    state = RuntimeState(str(path), DEFAULTS)
    state.load_or_create()
    state.set_system("enabled", False)
    state.save_atomic()

    path.write_text("{not valid json")

    caplog.set_level(logging.WARNING)
    data = state.load_if_changed(force=True)

    assert data["system"]["enabled"] is False
    assert "event=runtime_state_load_error" in caplog.text
