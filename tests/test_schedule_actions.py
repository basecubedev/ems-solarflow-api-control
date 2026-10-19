# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a schedule rule can do, and that it covers every control point.

The owner asked for one scheduler for everything the EMS can control. The
catalogue is that promise made checkable: every key the runtime whitelist
accepts is an action's or excluded with its reason, and every action does
exactly what the emsctl commands it names do.
"""

import json

import pytest

import emsctl
from ems import runtime_commands
from ems.runtime_commands import build_validation_context, command_values, run_command
from ems.runtime_state import RuntimeState
from ems.schedule_actions import (
    ACTIONS,
    EXCLUDED_KEYS,
    GROUPS,
    ScheduleActionError,
    action_commands,
    action_key_paths,
    schedule_action,
)
from tests._emsctl_test_helpers import write_config

pytestmark = [pytest.mark.contract, pytest.mark.config]

SAMPLES = {
    "total_output_limit": [600],
    "device_output_limit": [0, 500],
    "pv_priority": [1.3],
    "offgrid_socket": ["standard", "off"],
    "charge_from_grid": [600, 0],
    "surplus_charging": [False, True],
    "device_surplus_charging": [False, True],
    "winter_mode": [True, False],
    "ems_control": [False, True],
    "device_control": [False, True],
    "light": ["off", "on", "unmanaged"],
    "fan": ["auto", "1", "2", "off"],
    "buzzer": ["off", "on"],
    "auto_heat": ["on", "off"],
    "minimum_output_limit": [40],
}


def _whitelisted_paths():
    paths = {f"system.{key}" for key in runtime_commands.SYSTEM_FIELDS}
    for section, fields in runtime_commands.SECTION_FIELDS.items():
        paths |= {f"{section}.{key}" for key in fields}
    paths |= {f"devices.WR1.{key}" for key in runtime_commands.WRITABLE_DEVICE_FIELDS}
    return paths


def test_every_whitelisted_key_is_an_action_or_excluded_with_its_reason():
    covered = {path for action in ACTIONS for path in action_key_paths(action, "WR1")}
    excluded = {path.replace("devices.", "devices.WR1.", 1) for path in EXCLUDED_KEYS}

    assert sorted(_whitelisted_paths() - covered - excluded) == []
    assert sorted(covered - _whitelisted_paths()) == []
    assert covered.isdisjoint(excluded)
    assert all(reason.strip() for reason in EXCLUDED_KEYS.values())


def test_every_action_has_a_place_and_says_what_it_does():
    ids = [action.id for action in ACTIONS]

    assert len(ids) == len(set(ids))
    assert set(SAMPLES) == set(ids)
    for action in ACTIONS:
        assert action.group in GROUPS, action.id
        assert action.label.strip() and action.help.strip(), action.id
        assert action.per_device == (action.scope == "device"), action.id
        assert schedule_action(action.id) is action


@pytest.mark.parametrize(
    "action,value",
    [(action, value) for action in ACTIONS for value in SAMPLES[action.id]],
    ids=lambda item: getattr(item, "id", repr(item)),
)
def test_every_action_runs_commands_of_the_one_path_that_set_its_keys(action, value):
    set_keys = set()
    for command, argument in action_commands(action, value):
        set_keys |= set(command_values(action.scope, command, argument))

    assert set_keys <= set(action.keys)
    assert set_keys


def _emsctl_command(action):
    return {"system": "system", "device": "device", "winter": "winter", "ac_charge_control": "ac-charge"}[
        action.scope
    ]


@pytest.mark.parametrize(
    "action,value",
    [(action, value) for action in ACTIONS for value in SAMPLES[action.id]],
    ids=lambda item: getattr(item, "id", repr(item)),
)
def test_a_scheduled_action_leaves_the_state_the_emsctl_commands_leave(tmp_path, action, value):
    by_emsctl = tmp_path / "by-emsctl"
    by_schedule = tmp_path / "by-schedule"
    for root in (by_emsctl, by_schedule):
        root.mkdir()
        write_config(root / "config.json")
        assert emsctl.main(["--config", str(root / "config.json"), "--runtime-state", str(root / "runtime-state.json"), "status"]) == 0
    config = json.loads((by_schedule / "config.json").read_text())
    device = "WR1" if action.per_device else None
    commands = action_commands(action, value)

    for command, argument in commands:
        argv = [_emsctl_command(action), *([device] if device else []), command]
        argv += [] if argument is None else [str(argument)]
        assert emsctl.main(["--config", str(by_emsctl / "config.json"), "--runtime-state", str(by_emsctl / "runtime-state.json"), *argv]) == 0
    runtime_state = RuntimeState(str(by_schedule / "runtime-state.json"), {"devices": {}})
    runtime_state.load_if_changed(force=True)
    for command, argument in commands:
        run_command(
            runtime_state,
            action.scope,
            command,
            argument,
            device=device,
            validation_context=build_validation_context(config),
            source="schedule",
        )

    emsctl_state = json.loads((by_emsctl / "runtime-state.json").read_text())
    schedule_state = json.loads((by_schedule / "runtime-state.json").read_text())
    if "runtime_role_reason" in schedule_state["devices"]["WR1"]:
        assert schedule_state["devices"]["WR1"].pop("runtime_role_reason") == "schedule"
        assert emsctl_state["devices"]["WR1"].pop("runtime_role_reason") == "emsctl"
    assert schedule_state == emsctl_state


def test_charging_from_the_grid_sets_the_power_before_the_role():
    """The role is what starts the charge, so the power it charges at is in
    place by the time the EMS reads it."""

    commands = action_commands(schedule_action("charge_from_grid"), 600)

    assert commands == (("ac-charge-power", 600), ("ac-mode", "input"))
    assert action_commands(schedule_action("charge_from_grid"), 0) == (("ac-mode", "output"),)


@pytest.mark.parametrize(
    "action_id,value,message",
    [
        ("winter_mode", "on", "takes on or off"),
        ("device_output_limit", -1, "whole watts from 0"),
        ("device_output_limit", 500.5, "whole watts from 0"),
        ("device_output_limit", True, "whole watts from 0"),
        ("charge_from_grid", "600", "whole watts from 0"),
        ("offgrid_socket", "always", "takes one of: off, eco, standard"),
        ("pv_priority", "high", "takes a number"),
    ],
)
def test_a_value_an_action_cannot_take_is_refused(action_id, value, message):
    with pytest.raises(ScheduleActionError, match=message):
        action_commands(schedule_action(action_id), value)


def test_an_unknown_action_is_refused():
    with pytest.raises(ScheduleActionError, match="unknown schedule action reboot"):
        schedule_action("reboot")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_factor_that_is_no_finite_number_is_refused(value):
    with pytest.raises(ScheduleActionError, match="takes a number"):
        action_commands(schedule_action("pv_priority"), value)
