# SPDX-License-Identifier: AGPL-3.0-or-later
"""EMS-authoritative runtime-state device lifecycle reconciliation.

When a device is added, renamed, or removed in config, the EMS reconciles
``runtime-state.json`` on load. Devices are matched by a stable identity
(serial), so a rename migrates the operator's runtime settings to the new name
instead of losing them; a removed device is pruned; a new device gets defaults.
Pruning is authoritative (EMS-owned) and fail-closed (never prunes against an
empty/unreadable config), takes a one-step backup, and is audit-logged.
"""

import json
import logging

from ems.runtime_state import (
    RuntimeState,
    merge_runtime_defaults,
    reconcile_runtime_devices,
)


def _defaults(devices):
    result = {
        "system": {"enabled": True, "max_total_power": 800, "loop_interval": 5},
        "ha": {"enabled": False, "control_enabled": False},
        "winter": {"enabled": False},
        "devices": {},
    }
    for name, spec in devices.items():
        entry = {
            "enabled": True,
            "max_power": spec.get("max_power", 800),
            "offgrid_socket_mode": "off",
            "pv_priority_factor": spec.get("pv_priority_factor", 1.0),
        }
        if spec.get("identity"):
            entry["identity"] = spec["identity"]
        result["devices"][name] = entry
    return result


# --- pure reconcile ---------------------------------------------------------


def test_reconcile_adds_new_config_device():
    loaded = {"WR1": {"identity": "S1", "max_power": 700}}
    defaults = _defaults({"WR1": {"identity": "S1"}, "INV_2": {"identity": "S2"}})["devices"]

    merged, changes = reconcile_runtime_devices(loaded, defaults, prune=True)

    assert set(merged) == {"WR1", "INV_2"}
    assert merged["WR1"]["max_power"] == 700
    assert [c["name"] for c in changes["added"]] == ["INV_2"]
    assert not changes["renamed"] and not changes["pruned"]


def test_reconcile_renames_by_identity_preserving_settings():
    loaded = {
        "WR2": {
            "identity": "SNX",
            "enabled": False,
            "max_power": 650,
            "pv_priority_factor": 2.0,
            "ac_charge_power_w": 400,
        }
    }
    defaults = _defaults({"INV_2": {"identity": "SNX"}})["devices"]

    merged, changes = reconcile_runtime_devices(loaded, defaults, prune=True)

    assert set(merged) == {"INV_2"}
    assert merged["INV_2"]["max_power"] == 650
    assert merged["INV_2"]["pv_priority_factor"] == 2.0
    assert merged["INV_2"]["ac_charge_power_w"] == 400
    assert merged["INV_2"]["enabled"] is False
    assert merged["INV_2"]["identity"] == "SNX"
    assert changes["renamed"] == [{"from": "WR2", "to": "INV_2", "identity": "SNX"}]
    assert not changes["pruned"]


def test_reconcile_prunes_removed_device_only_when_pruning():
    loaded = {"WR1": {"identity": "S1"}, "WR2": {"identity": "S2", "max_power": 500}}
    defaults = _defaults({"WR1": {"identity": "S1"}})["devices"]

    pruned, changes = reconcile_runtime_devices(loaded, defaults, prune=True)
    assert set(pruned) == {"WR1"}
    assert [c["name"] for c in changes["pruned"]] == ["WR2"]

    kept, changes_kept = reconcile_runtime_devices(loaded, defaults, prune=False)
    assert set(kept) == {"WR1", "WR2"}
    assert kept["WR2"]["max_power"] == 500
    assert not changes_kept["pruned"]


def test_reconcile_name_reuse_for_different_hardware_gets_fresh_defaults():
    loaded = {"WR1": {"identity": "S1", "max_power": 650, "pv_priority_factor": 3.0}}
    defaults = _defaults({"WR1": {"identity": "S2"}})["devices"]

    merged, changes = reconcile_runtime_devices(loaded, defaults, prune=True)

    assert merged["WR1"]["identity"] == "S2"
    assert merged["WR1"]["max_power"] == 800
    assert merged["WR1"]["pv_priority_factor"] == 1.0
    assert [c["name"] for c in changes["added"]] == ["WR1"]
    assert [c["name"] for c in changes["pruned"]] == ["WR1"]


def test_reconcile_backfills_identity_by_name():
    loaded = {"WR1": {"max_power": 650}}
    defaults = _defaults({"WR1": {"identity": "S1"}})["devices"]

    merged, changes = reconcile_runtime_devices(loaded, defaults, prune=True)

    assert merged["WR1"]["identity"] == "S1"
    assert merged["WR1"]["max_power"] == 650
    assert [c["name"] for c in changes["rekeyed"]] == ["WR1"]
    assert not changes["renamed"] and not changes["pruned"]


def test_reconcile_fail_closed_when_no_configured_devices():
    loaded = {"WR1": {"identity": "S1"}, "WR2": {"identity": "S2"}}

    merged, changes = reconcile_runtime_devices(loaded, {}, prune=True)

    assert set(merged) == {"WR1", "WR2"}
    assert not changes["pruned"] and not changes["added"] and not changes["renamed"]


def test_merge_runtime_defaults_migrates_rename_without_duplicating():
    merged = merge_runtime_defaults(
        {"devices": {"WR2": {"identity": "SNX", "max_power": 650}}},
        _defaults({"INV_2": {"identity": "SNX"}}),
    )
    assert set(merged["devices"]) == {"INV_2"}
    assert merged["devices"]["INV_2"]["max_power"] == 650


# --- RuntimeState load reconciliation (file-based) --------------------------


def test_load_renames_device_by_identity_backs_up_and_audits(tmp_path, caplog):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({
        "devices": {
            "WR2": {
                "identity": "SNX",
                "enabled": True,
                "max_power": 650,
                "pv_priority_factor": 2.0,
                "ac_charge_power_w": 400,
            }
        }
    }))
    state = RuntimeState(str(path), _defaults({"INV_2": {"identity": "SNX"}}))

    caplog.set_level(logging.INFO)
    data = state.load_or_create()

    assert set(data["devices"]) == {"INV_2"}
    assert data["devices"]["INV_2"]["max_power"] == 650
    assert data["devices"]["INV_2"]["ac_charge_power_w"] == 400

    saved = json.loads(path.read_text())
    assert set(saved["devices"]) == {"INV_2"}
    assert (tmp_path / "runtime-state.json.bak").exists()
    backup = json.loads((tmp_path / "runtime-state.json.bak").read_text())
    assert "WR2" in backup["devices"]
    assert "event=runtime_device_renamed" in caplog.text


def test_load_prunes_removed_device_and_backs_up(tmp_path, caplog):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({
        "devices": {
            "WR1": {"identity": "S1", "max_power": 800},
            "WR2": {"identity": "S2", "max_power": 500},
        }
    }))
    state = RuntimeState(str(path), _defaults({"WR1": {"identity": "S1"}}))

    caplog.set_level(logging.INFO)
    data = state.load_or_create()

    assert set(data["devices"]) == {"WR1"}
    saved = json.loads(path.read_text())
    assert set(saved["devices"]) == {"WR1"}
    assert (tmp_path / "runtime-state.json.bak").exists()
    assert "event=runtime_device_pruned" in caplog.text


def test_load_is_fail_closed_and_does_not_prune_without_config_devices(tmp_path):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({
        "devices": {
            "WR1": {"identity": "S1", "max_power": 800},
            "WR2": {"identity": "S2", "max_power": 500},
        }
    }))
    state = RuntimeState(str(path), _defaults({}))

    data = state.load_or_create()

    assert set(data["devices"]) == {"WR1", "WR2"}
    assert not (tmp_path / "runtime-state.json.bak").exists()


def test_load_backfills_identity_and_persists_it(tmp_path):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"devices": {"WR1": {"max_power": 650}}}))
    state = RuntimeState(str(path), _defaults({"WR1": {"identity": "S1"}}))

    state.load_or_create()

    saved = json.loads(path.read_text())
    assert saved["devices"]["WR1"]["identity"] == "S1"
    assert saved["devices"]["WR1"]["max_power"] == 650
    # A backfill is not destructive, so no backup is written.
    assert not (tmp_path / "runtime-state.json.bak").exists()


def test_backfill_then_rename_migrates_settings_end_to_end(tmp_path):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({
        "devices": {"WR2": {"max_power": 650, "pv_priority_factor": 2.0}}
    }))

    # First EMS load stamps the identity onto the existing WR2 entry.
    RuntimeState(str(path), _defaults({"WR2": {"identity": "SNX"}})).load_or_create()

    # Config later renames WR2 -> INV_2 (same hardware / identity).
    data = RuntimeState(str(path), _defaults({"INV_2": {"identity": "SNX"}})).load_or_create()

    assert set(data["devices"]) == {"INV_2"}
    assert data["devices"]["INV_2"]["max_power"] == 650
    assert data["devices"]["INV_2"]["pv_priority_factor"] == 2.0


# --- emsctl parity ----------------------------------------------------------


def test_emsctl_merge_migrates_rename_without_losing_settings():
    import emsctl

    config = {
        "system": {"max_total_power": 800, "max_device_power": 800},
        "devices": [
            {"name": "INV_2", "ip": "10.0.0.1", "sn": "SNX", "max_power": 800},
        ],
    }
    existing = {
        "devices": {
            "WR2": {
                "identity": "SNX",
                "enabled": True,
                "max_power": 650,
                "pv_priority_factor": 2.0,
            }
        }
    }

    merged = emsctl.merge_defaults(existing, emsctl.runtime_defaults(config, existing))

    assert merged["devices"]["INV_2"]["max_power"] == 650
    assert merged["devices"]["INV_2"]["pv_priority_factor"] == 2.0
    assert "WR2" not in merged["devices"]
