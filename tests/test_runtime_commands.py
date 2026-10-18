# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every runtime-state edit takes one path.

emsctl, the dashboard, the Admin's config->runtime mirror and the scheduler
change operator values only through ``ems.runtime_commands``. emsctl used to
load, merge and save the file on its own: a change saved between its read and
its write was lost, and its switches skipped the validators.
"""

import ast
import json
import logging
from pathlib import Path

import pytest

import emsctl
from ems import runtime_commands
from ems.runtime_commands import (
    DEVICE_COMMANDS,
    SECTION_COMMANDS,
    SYSTEM_COMMANDS,
    RuntimeWriteError,
    apply_device_update,
    build_validation_context,
    command_values,
    run_command,
)
from ems.runtime_state import RuntimeState
from tests._emsctl_test_helpers import write_config

pytestmark = [pytest.mark.contract, pytest.mark.config]

ROOT = Path(__file__).resolve().parents[1]

COMMANDS = [
    ("system", None, "enable", None),
    ("system", None, "disable", None),
    ("system", None, "max-power", "600"),
    ("system", None, "loop-interval", "7"),
    ("system", None, "min-output-limit", "40"),
    ("device", "WR1", "enable", None),
    ("device", "WR1", "disable", None),
    ("device", "WR1", "max-power", "500"),
    ("device", "WR1", "offgrid", "eco"),
    ("device", "WR1", "pv-priority-factor", "1.3"),
    ("device", "WR1", "ac-mode", "input"),
    ("device", "WR1", "ac-mode", "output"),
    ("device", "WR1", "ac-charge", "off"),
    ("device", "WR1", "ac-charge-power", "300"),
    ("winter", None, "enable", None),
    ("winter", None, "disable", None),
    ("ac-charge", None, "enable", None),
    ("ac-charge", None, "disable", None),
]


def _install(tmp_path):
    config_path = tmp_path / "config.json"
    write_config(config_path)
    runtime_path = tmp_path / "runtime-state.json"
    assert _emsctl(tmp_path, "status") == 0
    return json.loads(config_path.read_text()), runtime_path


def _emsctl(tmp_path, *argv):
    return emsctl.main(
        [
            "--config",
            str(tmp_path / "config.json"),
            "--runtime-state",
            str(tmp_path / "runtime-state.json"),
            *argv,
        ]
    )


def _argv(command, device, action, value):
    return [command, *([device] if device else []), action, *([value] if value else [])]


def test_every_runtime_edit_emsctl_offers_is_a_command_of_the_one_path():
    parser = emsctl.build_parser()

    assert tuple(emsctl.SYSTEM_ACTIONS) == tuple(SYSTEM_COMMANDS)
    assert tuple(emsctl.DEVICE_ACTIONS) == tuple(DEVICE_COMMANDS)
    for command in ("winter", "ac-charge"):
        for action in SECTION_COMMANDS:
            assert parser.parse_args([command, action]).action == action
    assert set(emsctl.RUNTIME_COMMAND_SCOPES.values()) == {
        "system",
        "device",
        *runtime_commands.SECTION_FIELDS,
    }


def test_every_command_sets_a_whitelisted_key():
    fields = {
        "system": runtime_commands.SYSTEM_FIELDS,
        "device": {**runtime_commands.DEVICE_FIELDS, **runtime_commands.DEVICE_AC_FIELDS},
    }
    for scope, table in (("system", SYSTEM_COMMANDS), ("device", DEVICE_COMMANDS)):
        for action, command in table.items():
            assert command.key in fields[scope], (scope, action)
    for section, section_fields in runtime_commands.SECTION_FIELDS.items():
        for action, command in SECTION_COMMANDS.items():
            assert command.key in section_fields, (section, action)


@pytest.mark.parametrize("command,device,action,value", COMMANDS)
def test_emsctl_and_the_dashboard_save_the_same_values_for_a_command(
    tmp_path, command, device, action, value
):
    config, runtime_path = _install(tmp_path)
    dashboard_path = tmp_path / "dashboard-runtime-state.json"
    dashboard_path.write_bytes(runtime_path.read_bytes())

    assert _emsctl(tmp_path, *_argv(command, device, action, value)) == 0

    scope = emsctl.RUNTIME_COMMAND_SCOPES[command]
    dashboard = RuntimeState(str(dashboard_path), {"devices": {}})
    dashboard.load_if_changed(force=True)
    payload = command_values(scope, action, value)
    context = build_validation_context(config)
    if scope == "system":
        runtime_commands.apply_system_update(dashboard, payload, context, source="dashboard")
    elif scope == "device":
        apply_device_update(dashboard, device, payload, context, source="dashboard")
    else:
        runtime_commands.apply_section_update(dashboard, scope, payload, context, source="dashboard")

    by_emsctl = json.loads(runtime_path.read_text())
    by_dashboard = json.loads(dashboard_path.read_text())
    if action == "ac-mode":
        assert by_emsctl["devices"]["WR1"].pop("runtime_role_reason") == "emsctl"
        assert by_dashboard["devices"]["WR1"].pop("runtime_role_reason") == "dashboard"
    assert by_emsctl == by_dashboard


def test_emsctl_keeps_a_change_saved_between_its_read_and_its_write(tmp_path, monkeypatch):
    """Interleaving: emsctl reads the file, the EMS saves a field, emsctl saves."""

    _, runtime_path = _install(tmp_path)
    limits = emsctl.runtime_limits

    def limits_once_the_ems_has_saved(config):
        ems = RuntimeState(str(runtime_path), {"devices": {}})
        ems.load_if_changed(force=True)
        ems.update_device("WR1", {"max_power": 500})
        return limits(config)

    monkeypatch.setattr(emsctl, "runtime_limits", limits_once_the_ems_has_saved)

    assert _emsctl(tmp_path, "device", "WR1", "pv-priority-factor", "1.3") == 0

    device = json.loads(runtime_path.read_text())["devices"]["WR1"]
    assert device["max_power"] == 500
    assert device["pv_priority_factor"] == 1.3


def test_an_ac_role_left_at_ac_input_is_clearable_once_the_device_is_on_mqtt(tmp_path):
    """``ac_output`` stops nothing, so neither tool refuses it over MQTT;
    refusing it would strand a role set while the device was on the local API."""

    config_path = tmp_path / "config.json"
    write_config(config_path)
    config = json.loads(config_path.read_text())
    config["devices"][0].update(
        {
            "type": "zendure_mqtt",
            "capabilities": {"write_output_limit": True},
            "mqtt": {"broker_ref": "home", "device_id": "D1"},
        }
    )
    config_path.write_text(json.dumps(config))
    assert _emsctl(tmp_path, "status") == 0
    runtime_path = tmp_path / "runtime-state.json"
    stale = json.loads(runtime_path.read_text())
    stale["devices"]["WR1"]["runtime_role"] = "ac_input"
    runtime_path.write_text(json.dumps(stale))
    context = build_validation_context(config)
    dashboard = RuntimeState(str(runtime_path), {"devices": {}})
    dashboard.load_if_changed(force=True)

    apply_device_update(dashboard, "WR1", {"runtime_role": "ac_output"}, context, source="dashboard")
    assert dashboard.snapshot()["devices"]["WR1"]["runtime_role"] == "ac_output"

    with pytest.raises(RuntimeWriteError, match="connected over MQTT"):
        apply_device_update(dashboard, "WR1", {"runtime_role": "ac_input"}, context, source="dashboard")
    assert _emsctl(tmp_path, "device", "WR1", "ac-mode", "output") == 0
    assert json.loads(runtime_path.read_text())["devices"]["WR1"]["runtime_role"] == "ac_output"


@pytest.mark.parametrize("source", ["emsctl", "dashboard", "admin", "schedule"])
def test_the_caller_is_recorded_with_the_ac_role_and_logged(tmp_path, caplog, source):
    config, runtime_path = _install(tmp_path)
    runtime_state = RuntimeState(str(runtime_path), {"devices": {}})
    runtime_state.load_if_changed(force=True)

    with caplog.at_level(logging.INFO):
        run_command(
            runtime_state,
            "device",
            "ac-mode",
            "input",
            device="WR1",
            validation_context=build_validation_context(config),
            source=source,
        )

    assert runtime_state.snapshot()["devices"]["WR1"]["runtime_role_reason"] == source
    logged = [record.getMessage() for record in caplog.records]
    assert any(
        "event=runtime_values_updated" in line
        and f"source={source}" in line
        and "device=WR1" in line
        and "runtime_role=ac_input" in line
        for line in logged
    ), logged


@pytest.mark.parametrize(
    "scope,action,value,message",
    [
        ("system", "enable", "1", "does not take a value"),
        ("system", "max-power", None, "requires a value"),
        ("system", "max-power", " ", "requires a value"),
        ("device", "ac-mode", "sideways", "value must be 'output' or 'input'"),
        ("device", "ac-charge", "maybe", "value must be 'on' or 'off'"),
        ("device", "reboot", None, "unknown device command reboot"),
        ("ha", "enable", None, "unsupported runtime scope ha"),
    ],
)
def test_a_malformed_command_is_refused_before_anything_is_read(scope, action, value, message):
    with pytest.raises(RuntimeWriteError, match=message):
        command_values(scope, action, value)


def test_emsctl_names_the_command_a_refusal_belongs_to(tmp_path, capsys):
    _, runtime_path = _install(tmp_path)
    before = runtime_path.read_bytes()

    assert _emsctl(tmp_path, "device", "WR1", "max-power", "801") != 0

    assert "device WR1 max-power: max_power must be between 0 and 800" in capsys.readouterr().err
    assert runtime_path.read_bytes() == before


def _production_modules():
    yield ROOT / "emsctl.py"
    yield ROOT / "ems-solarflow-api-control.py"
    for package in ("ems", "dashboard", "admin"):
        yield from sorted((ROOT / package).rglob("*.py"))


WRITERS = {
    "update_device": {"ems/runtime_state.py", "ems/runtime_commands.py"},
    "update_section": {"ems/runtime_state.py", "ems/runtime_commands.py"},
    "set_device": {"ems/runtime_state.py", "ems/simulation.py"},
}


def test_nothing_outside_the_command_path_changes_runtime_values():
    """``simulation.py`` toggles a device in its self-test's own runtime state."""

    found = set()
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITERS
            ):
                found.add((node.func.attr, path.relative_to(ROOT).as_posix()))

    assert sorted(
        (name, where) for name, where in found if where not in WRITERS[name]
    ) == []


@pytest.mark.parametrize(
    "name",
    ["load_runtime_state", "merge_defaults", "save_atomic", "update_system", "update_device", "set_bool_section"],
)
def test_emsctl_keeps_no_runtime_reader_or_writer_of_its_own(name):
    assert not hasattr(emsctl, name)
