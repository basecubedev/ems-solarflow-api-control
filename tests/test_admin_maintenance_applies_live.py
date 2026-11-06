# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which saved settings are live at once, and which wait for an EMS restart.

A small whitelist of keys is writable in both config.json and runtime-state.json,
and the Admin apply mirrors exactly those into runtime state. Everything else —
every write gate, dry_run and simulation_mode included — is inert until the EMS
container is recreated. The console used to say that nowhere, so an owner could
turn a gate off, see "Config applied", and still be controlled.

The whitelist has one owner (ems.runtime_commands, read through
admin.config_runtime_overlap). This asks that question of the owner rather than
keeping a second copy of the answer.
"""

import json
import os
from types import SimpleNamespace

import pytest

from admin.config_runtime_overlap import applies_live, resolve_runtime_state_path, runtime_device_names
from admin.install_context import detect_install_context
from admin.maintenance_config import load_maintenance_config, preview_maintenance_config, summarize_config_changes

pytestmark = [
    pytest.mark.admin,
    pytest.mark.config,
    pytest.mark.maintenance,
    pytest.mark.contract,
    pytest.mark.simulation,
]


@pytest.mark.parametrize(
    "path",
    [
        "system.enabled",
        "system.max_total_power",
        "system.loop_interval",
        "system.min_output_limit",
        "devices[0].enabled",
        "devices[2].max_power",
        "devices[0].offgrid_socket_mode",
        "devices[0].pv_priority_factor",
        "winter.enabled",
    ],
)
def test_the_mirrored_keys_take_effect_without_a_restart(path):
    assert applies_live(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "system.dry_run",
        "system.simulation_mode",
        "system.allow_hardware_writes",
        "system.allow_mqtt_local_control_writes",
        "system.allow_mqtt_zendure_control_writes",
        "system.allow_state_reconciliation_writes",
        "system.log_level",
        "system.output_control.target_deadband_w",
        "system.max_device_power",
        "devices[0].name",
        "devices[0].ip",
        "winter.min_soc",
        "grid_meter.ip",
        "influxdb.enabled",
        "zendure_mqtt.brokers.default.host",
        "",
        "system",
    ],
)
def test_everything_else_waits_for_a_restart(path):
    """dry_run and the write gates are the ones this must never get wrong."""

    assert applies_live(path) is False


def test_a_device_key_is_live_only_for_a_device_runtime_state_holds_an_entry_for():
    """runtime-state holds what the mirror can write: a device EMS built to control, switched off since or not."""

    config = {"devices": [
        {"name": "WR1", "ip": "192.0.2.1"},
        {"name": "Balkon", "type": "external_mqtt", "mqtt": {"broker_ref": "ems-mqtt", "device_id": "balkon"}},
        {"name": "WR2", "ip": "192.0.2.2", "enabled": True},
        {"name": "WR1 Nord", "ip": "192.0.2.3"},
    ]}
    runtime = frozenset({"WR1", "WR2"})

    assert [applies_live(f"devices[{index}].enabled", config, runtime) for index in range(4)] == [True, False, True, False]
    assert applies_live("devices[0].enabled", config) is True
    assert applies_live("system.loop_interval", config, frozenset()) is True


def test_the_change_preview_asks_the_runtime_state_file_of_the_install(tmp_path):
    current = {
        "system": {"max_total_power": 800},
        "grid_meter": {"type": "shelly", "ip": "192.0.2.9"},
        "devices": [
            {"name": "WR1", "ip": "192.0.2.1", "sn": "AAA", "enabled": False},
            {"name": "WR2", "ip": "192.0.2.2", "sn": "BBB", "enabled": False},
            {"name": "WR3", "ip": "192.0.2.3", "sn": "CCC", "pv_priority_factor": 2.0},
        ],
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text(json.dumps(current), encoding="utf-8")
    context = detect_install_context(base_dir=str(tmp_path))
    runtime_path = resolve_runtime_state_path(context, current)
    os.makedirs(os.path.dirname(runtime_path), exist_ok=True)
    with open(runtime_path, "w", encoding="utf-8") as handle:
        json.dump({"devices": {"WR1": {"enabled": False}, "WR3": {"enabled": True}}}, handle)
    draft = load_maintenance_config(base_dir=str(tmp_path))["draft"]
    for device in draft["devices"]:
        device["enabled"] = True
    draft["devices"][2]["removed"] = True

    diff = preview_maintenance_config(draft, base_dir=str(tmp_path))["diff"]
    marked = {entry["path"]: entry["applies_live"] for bucket in ("changes", "added", "removed") for entry in diff[bucket]}

    assert runtime_device_names(context, current) == frozenset({"WR1", "WR3"})
    assert (marked["devices[0].enabled"], marked["devices[1].enabled"]) == (True, False)
    assert marked["devices[2].pv_priority_factor"] is False
    assert runtime_device_names(SimpleNamespace(install_root=tmp_path / "elsewhere"), current) == frozenset()


def test_the_answer_comes_from_the_whitelist_and_not_from_a_copy():
    from ems.runtime_commands import DEVICE_FIELDS, SECTION_FIELDS, SYSTEM_FIELDS

    for key in SYSTEM_FIELDS:
        assert applies_live("system." + key) is True
    for key in DEVICE_FIELDS:
        assert applies_live("devices[7]." + key) is True
    for section, fields in SECTION_FIELDS.items():
        for key in fields:
            assert applies_live(section + "." + key) is True


def test_the_preview_diff_marks_every_row():
    before = {
        "system": {"max_total_power": 1600, "allow_hardware_writes": True},
        "devices": [{"name": "WR1", "max_power": 800}],
    }
    after = {
        "system": {"max_total_power": 1400, "allow_hardware_writes": False},
        "devices": [{"name": "WR1", "max_power": 600, "ip": "10.0.0.9"}],
    }
    diff = summarize_config_changes(before, after)
    marked = {
        entry["path"]: entry["applies_live"]
        for bucket in ("changes", "added", "removed")
        for entry in diff[bucket]
    }
    assert marked == {
        "system.max_total_power": True,
        "system.allow_hardware_writes": False,
        "devices[0].max_power": True,
        "devices[0].ip": False,
    }


# --- the preview shows the split, and shows everything --------------------


def _read_js():
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "admin", "static", "admin.js"), encoding="utf-8") as h:
        return h.read()


def test_the_preview_groups_the_diff_into_live_and_restart():
    js = _read_js()
    assert '["live", "Takes effect immediately"]' in js
    assert '["restart", "Needs an EMS restart"]' in js


def test_the_grouping_never_drops_a_row():
    """A change the console hides is a change the owner applies without seeing."""

    js = _read_js()
    body = js.split("function renderMaintenanceConfigChangeGroups", 1)[1].split(
        "\n}", 1
    )[0]
    # Rows are partitioned by the server's marker, and both partitions render.
    assert "entry.applies_live === true" in body
    assert "MCONFIG_DIFF_GROUPS.forEach" in body
    # No level, mode or search filter reaches the diff.
    for forbidden in ("data-search-hit", "mconfigLevelledFields", "settingsTab"):
        assert forbidden not in body


def test_the_change_summary_names_both_halves():
    js = _read_js()
    body = js.split("function mconfigChangeSummaryText", 1)[1].split("\n}", 1)[0]
    assert "immediate" in body
    assert "need a restart" in body
