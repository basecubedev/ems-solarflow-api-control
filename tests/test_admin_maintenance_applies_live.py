# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which saved settings are live at once, and which wait for an EMS restart.

A small whitelist of keys is writable in both config.json and runtime-state.json,
and the Admin apply mirrors exactly those into runtime state. Everything else —
every write gate, dry_run and simulation_mode included — is inert until the EMS
container is recreated. The console used to say that nowhere, so an owner could
turn a gate off, see "Config applied", and still be controlled.

The whitelist has one owner (dashboard.runtime_write, read through
admin.config_runtime_overlap). This asks that question of the owner rather than
keeping a second copy of the answer.
"""

import pytest

from admin.config_runtime_overlap import applies_live
from admin.maintenance_config import summarize_config_changes

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
        "ha.enabled",
        "ha.control_enabled",
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


def test_the_answer_comes_from_the_whitelist_and_not_from_a_copy():
    from dashboard.runtime_write import DEVICE_FIELDS, SECTION_FIELDS, SYSTEM_FIELDS

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
