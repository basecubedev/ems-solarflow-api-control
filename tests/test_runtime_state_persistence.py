# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import logging

from ems.runtime_state import RuntimeState, merge_runtime_defaults


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
