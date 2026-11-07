# SPDX-FileCopyrightText: 2026 basecubedev
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

from admin.config_runtime_overlap import applies_live, live_device_paths, resolve_runtime_state_path, runtime_device_entries
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


def _raised(config, **values):
    import copy

    merged = copy.deepcopy(config)
    for device in merged["devices"]:
        device.update(values)
    return merged


def test_a_device_key_is_live_only_for_a_device_that_owns_a_runtime_state_entry():
    """runtime-state holds what the mirror can write: a device EMS built to control, switched off since or not."""

    config = {"devices": [
        {"name": "WR1", "ip": "192.0.2.1", "max_power": 800},
        {"name": "Balkon", "type": "external_mqtt", "max_power": 800, "mqtt": {"broker_ref": "ems-mqtt", "device_id": "balkon"}},
        {"name": "WR2", "ip": "192.0.2.2", "enabled": True, "max_power": 800},
        {"name": "WR1 Nord", "ip": "192.0.2.3", "max_power": 800},
    ]}
    runtime = {"WR1": {"enabled": True}, "WR2": {"enabled": False}}

    assert live_device_paths(config, _raised(config, max_power=600), runtime) == {"devices[0].max_power", "devices[2].max_power"}
    assert live_device_paths(config, _raised(config, max_power=600), {}) == frozenset()
    assert applies_live("devices[0].enabled", config) is True


def test_a_device_key_is_live_for_the_entry_its_serial_owns_under_any_name():
    """The running EMS reads a renamed device under its old name, and a device that took another's name not under that name."""

    previous = {"devices": [
        {"name": "WR1", "ip": "192.0.2.1", "sn": "AAA", "max_power": 800},
        {"name": "WR2", "ip": "192.0.2.2", "sn": "BBB", "max_power": 800},
        {"name": "WR3", "ip": "192.0.2.3", "sn": "CCC", "max_power": 800},
    ]}
    merged = _raised(previous, max_power=600)
    merged["devices"][0]["name"], merged["devices"][1]["name"] = "WR2", "WRX"
    runtime = {"WR1": {"identity": "AAA"}, "WR2": {"identity": "BBB"}, "WR3": {"identity": "ZZZ"}}

    assert live_device_paths(previous, merged, runtime) == {"devices[0].max_power", "devices[1].max_power"}


def test_only_the_entry_that_speaks_for_a_device_is_live():
    """Never a switched-off duplicate or an entry with two serials; of two switched-off entries, the first."""

    config = {"devices": [
        {"name": "WR1", "ip": "192.0.2.1", "sn": "AAA", "max_power": 800},
        {"name": "WR1 spare", "ip": "192.0.2.2", "sn": "AAA", "enabled": False, "max_power": 800},
        {"name": "WR2", "ip": "192.0.2.3", "sn": "BBB", "serial_number": "CCC", "max_power": 800},
        {"name": "WR3", "ip": "192.0.2.4", "sn": "DDD", "enabled": False, "max_power": 800},
        {"name": "WR3 spare", "ip": "192.0.2.5", "sn": "DDD", "enabled": False, "max_power": 800},
        {"name": "WR4", "ip": "192.0.2.6", "sn": "EEE", "enabled": False, "max_power": 800},
    ]}
    runtime = {"WR1": {"identity": "AAA"}, "WR2": {"identity": "BBB"}, "WR3": {"identity": "DDD"}, "WR4": {"identity": "EEE"}}

    assert live_device_paths(config, _raised(config, max_power=600), runtime) == {"devices[0].max_power", "devices[3].max_power", "devices[5].max_power"}


def test_a_key_removed_from_a_device_is_live_as_its_default():
    before = {"devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "AAA", "max_power": 1200}]}
    after = {"devices": [{"name": "WR1", "ip": "192.0.2.1", "sn": "AAA"}]}

    diff = summarize_config_changes(before, after, {"WR1": {"identity": "AAA"}})

    assert [(entry["path"], entry["applies_live"]) for entry in diff["removed"]] == [("devices[0].max_power", True)]


def test_a_device_row_is_live_exactly_when_the_mirror_writes_it(tmp_path):
    """Swap two names, switch one off, add a duplicate, and give the other a factor of 0, which the EMS reads as 1."""

    from admin.runtime_convergence import mirror_changed_keys_to_runtime

    before = {"devices": [
        {"name": "WR1", "ip": "192.0.2.1", "sn": "AAA", "enabled": True, "pv_priority_factor": 1.0},
        {"name": "WR2", "ip": "192.0.2.2", "sn": "BBB", "enabled": True, "pv_priority_factor": 1.0},
    ]}
    after = _raised(before)
    after["devices"][0]["name"], after["devices"][1]["name"] = "WR2", "WR1"
    after["devices"][0]["enabled"] = False
    after["devices"][1]["pv_priority_factor"] = 0
    after["devices"].append({"name": "WR1 spare", "ip": "192.0.2.3", "sn": "AAA", "enabled": False})
    context = detect_install_context(base_dir=str(tmp_path))
    runtime_path = resolve_runtime_state_path(context, after)
    os.makedirs(os.path.dirname(runtime_path), exist_ok=True)
    with open(runtime_path, "w", encoding="utf-8") as handle:
        json.dump({"devices": {
            "WR1": {"identity": "AAA", "enabled": True, "pv_priority_factor": 1.0},
            "WR2": {"identity": "BBB", "enabled": True, "pv_priority_factor": 1.0},
        }}, handle)

    diff = summarize_config_changes(before, after, runtime_device_entries(context, after))
    written = mirror_changed_keys_to_runtime(context, previous=before, merged=after)

    marked = {entry["path"] for bucket in ("changes", "added") for entry in diff[bucket] if entry["applies_live"]}
    assert marked == set(written["applied"]) == {"devices[0].enabled"}
    assert written["skipped"] == []


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

    assert set(runtime_device_entries(context, current)) == {"WR1", "WR3"}
    assert (marked["devices[0].enabled"], marked["devices[1].enabled"]) == (True, False)
    assert marked["devices[2].pv_priority_factor"] is False
    assert runtime_device_entries(SimpleNamespace(install_root=tmp_path / "elsewhere"), current) == {}


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
