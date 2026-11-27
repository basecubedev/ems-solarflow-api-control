# SPDX-FileCopyrightText: 2026 basecubedev
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
    DEVICE_FIELDS,
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
    ("device", "WR1", "ac-charge-power", "max"),
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
        "device": runtime_commands.WRITABLE_DEVICE_FIELDS,
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
    "update_configured_device": {"ems/runtime_commands.py"},
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


@pytest.mark.parametrize("configured", [0, -5, True, "x", None])
def test_a_device_without_an_own_ceiling_is_limited_by_the_installations(configured):
    """The EMS caps such a device at system.max_device_power (device_power_ceiling); the runtime writer takes the same limit."""

    context = build_validation_context({"system": {"max_device_power": 800}, "devices": [{"name": "WR1", "max_power": configured}]})

    assert context["device_max_power"]["WR1"] == 800


def test_the_catalogue_bounds_the_pv_priority_as_the_runtime_writer_does():
    from ems.config_catalog import get_config_feature_field_index

    field = get_config_feature_field_index()["devices[].pv_priority_factor"]

    assert field["max"] == DEVICE_FIELDS["pv_priority_factor"][1][1]


@pytest.mark.parametrize("configured", [0, None])
def test_a_config_without_a_device_ceiling_takes_the_ems_default(configured):
    """emsctl validates against config.json as saved, where an old one may lack system.max_device_power."""

    from ems.config import default_runtime_config

    context = build_validation_context({"devices": [{"name": "WR1", "max_power": configured}]})

    assert context["device_max_power"]["WR1"] == default_runtime_config()["system"]["max_device_power"]


def test_the_admin_form_offers_each_operating_value_the_range_the_writer_accepts():
    """The Admin saves an operating value through the writer; a wider form range makes the save fail."""

    from ems.config_catalog import _SECTIONS

    fields = {field["path"]: field for section in _SECTIONS for field in section.get("fields", [])}
    paths = sorted(runtime_commands.OPERATION_SETTING_PATHS) + [
        f"devices[].{key}" for key in sorted(runtime_commands.OPERATION_DEVICE_KEYS)
    ]
    checked = 0
    for path in paths:
        scope, key = path.split(".", 1)
        if scope == "system":
            value_type, rule = runtime_commands.SYSTEM_FIELDS[key]
        elif scope == "devices[]":
            value_type, rule = runtime_commands.WRITABLE_DEVICE_FIELDS[key]
        else:
            value_type, rule = runtime_commands.SECTION_FIELDS[scope][key]
        if value_type in ("int", "float") and path in fields:
            assert (fields[path].get("min"), fields[path].get("max")) == rule, path
            checked += 1
    assert checked >= 10


@pytest.mark.parametrize(
    "months,accepted",
    [
        ([11, 12, 1], [11, 12, 1]),
        (["11", 12, 12], [11, 12]),
        ("11, 12, 1", [11, 12, 1]),
        ("11 12", [11, 12]),
        ("", None),
        ("winter", None),
        ([], None),
        ([13], None),
        ([0], None),
        ([True], None),
        ([1.5], None),
        (None, None),
    ],
)
def test_the_winter_months_are_a_list_of_months_each_once(months, accepted):
    if accepted is None:
        with pytest.raises(RuntimeWriteError, match="months"):
            runtime_commands.validate_section_values("winter", {"months": months})
    else:
        assert runtime_commands.validate_section_values("winter", {"months": months}) == {"months": accepted}


@pytest.mark.parametrize("source", ["dashboard", "emsctl", "schedule"])
def test_a_value_config_json_keeps_a_copy_of_is_written_by_the_admin_only(tmp_path, source):
    """Another source would leave the copy a rollback runs behind."""

    state = RuntimeState(str(tmp_path / "runtime-state.json"), {"devices": {}})
    state.load_if_changed(force=True)

    with pytest.raises(RuntimeWriteError, match="winter.months can be changed in the Admin console only"):
        runtime_commands.apply_section_update(state, "winter", {"months": [11, 12]}, source=source)
    runtime_commands.apply_section_update(state, "winter", {"enabled": False}, source=source)
    runtime_commands.apply_section_update(state, "winter", {"months": [11, 12]}, source="admin")

    assert state.data["winter"]["months"] == [11, 12]
    assert state.data["winter"]["enabled"] is False


def test_the_writer_accepts_exactly_the_offgrid_modes_the_ems_can_write():
    from ems.config import OFFGRID_SOCKET_MODES

    assert set(runtime_commands.OFFGRID_SOCKET_MODES) == set(OFFGRID_SOCKET_MODES)
    assert runtime_commands.field_specs()["devices"]["offgrid_socket_mode"]["options"] == ["off", "eco", "standard"]


def test_the_limits_carry_each_runtime_field_as_the_writer_checks_it():
    fields = runtime_commands.effective_limits()["fields"]

    assert fields["system"]["loop_interval"] == {"type": "int", "min": 1, "max": 3600}
    assert fields["devices"]["pv_priority_factor"] == {"type": "float", "min": 0.01, "max": 100.0}
    assert fields["devices"]["runtime_role"]["options"] == ["ac_output", "ac_input"]
    assert set(fields["sections"]) == set(runtime_commands.SECTION_FIELDS)


def test_catalogue_runtime_defaults_leave_out_installation_values():
    from ems.config_catalog import get_config_feature_field_index

    index = get_config_feature_field_index()
    expected = {
        "system": ("loop_interval", "min_output_limit", "grid_target_discharge_w", "grid_target_charge_w"),
        "devices": ("pv_priority_factor",),
        "battery_care": (
            "summer_full_points",
            "winter_full_points",
            "pv_only_points",
            "min_discharge_w",
            "pv_margin_percent",
            "pv_margin_min_w",
        ),
        "winter": ("summer_min_soc", "winter_min_soc", "ramp_step_percent", "adjust_hour"),
    }
    prefixes = {"system": "system", "devices": "devices[]", "battery_care": "battery_care", "winter": "winter"}
    assert runtime_commands.catalogue_runtime_defaults() == {
        scope: {key: index[f"{prefixes[scope]}.{key}"]["default"] for key in keys}
        for scope, keys in expected.items()
    }
    for path in ("system.max_total_power", "devices[].max_power"):
        assert index[path]["installation_value"] is True, path
    examples = {path for path, field in index.items() if field.get("example_default")}
    assert examples == {"grid_meter.ip", "devices[].name", "devices[].ip", "devices[].sn"}
    assert all(index[path]["installation_value"] for path in examples)


def test_an_installation_setting_names_the_fallback_the_ems_runs():
    from ems.config import apply_runtime_config_defaults
    from ems.config_catalog import get_config_feature_field_index

    fallback = apply_runtime_config_defaults({})
    named = {
        path: field["default"]
        for path, field in get_config_feature_field_index().items()
        if field.get("installation_value") and not field.get("example_default") and not path.startswith("devices[]")
    }
    assert named
    for path, default in named.items():
        section, key = path.split(".", 1)
        assert fallback[section][key] == default, path


def test_catalogue_runtime_defaults_travel_with_the_limits_and_cannot_be_changed_through_them():
    limits = runtime_commands.attach_limits({"system": {}}, None)["_limits"]
    assert limits["catalogue_defaults"] == runtime_commands.catalogue_runtime_defaults()
    limits["catalogue_defaults"]["system"]["min_output_limit"] = 0
    assert runtime_commands.catalogue_runtime_defaults()["system"]["min_output_limit"] == 35


def test_the_validation_context_follows_a_config_saved_while_the_ems_runs(tmp_path):
    """The Admin console applies system.max_total_power live; a context built once kept the old limit."""

    import json

    from ems.runtime_commands import LiveValidationContext

    path = tmp_path / "config.json"
    config = {"system": {"max_total_power": 800}, "devices": [{"name": "WR1", "max_power": 600}]}
    path.write_text(json.dumps(config), encoding="utf-8")
    live = LiveValidationContext(str(path), config)

    assert live()["system_max_total_power"] == 800
    path.write_text(json.dumps({**config, "system": {"max_total_power": 1600}}), encoding="utf-8")
    assert live()["system_max_total_power"] == 1600
    assert live()["device_max_power"]["WR1"] == 600

    path.write_text("{ not json", encoding="utf-8")
    assert live()["system_max_total_power"] == 1600
    path.unlink()
    assert live()["system_max_total_power"] == 1600


def test_the_dashboard_asks_a_live_validation_context_for_its_current_limits():
    from dashboard.server import DashboardHTTPServer

    server = DashboardHTTPServer.__new__(DashboardHTTPServer)
    answers = iter([{"system_max_total_power": 800}, {"system_max_total_power": 1600}])
    server.runtime_validation = lambda: next(answers)

    assert server.runtime_validation["system_max_total_power"] == 800
    assert server.runtime_validation["system_max_total_power"] == 1600
    server.runtime_validation = {"system_max_total_power": 900}
    assert server.runtime_validation == {"system_max_total_power": 900}


def _live_context(tmp_path, config):
    import json

    from ems.runtime_commands import LiveValidationContext

    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path, LiveValidationContext(str(path), config)


def test_a_saved_transport_switch_does_not_let_an_mqtt_device_take_an_ac_role_before_the_restart(tmp_path):
    """Over MQTT the EMS cannot switch acMode; the device stays on MQTT until the EMS restarts."""

    import json

    mqtt = {"name": "WR1", "type": "zendure_mqtt", "max_power": 800, "mqtt": {"device_id": "abc"}}
    config = {"system": {"max_total_power": 800}, "devices": [mqtt]}
    path, live = _live_context(tmp_path, config)
    assert live()["ac_role_unsupported"] == ["WR1"]

    switched = {"name": "WR1", "ip": "192.0.2.10", "sn": "SN", "max_power": 800}
    path.write_text(json.dumps({**config, "system": {"max_total_power": 900}, "devices": [switched]}), encoding="utf-8")

    assert live()["system_max_total_power"] == 900
    assert live()["ac_role_unsupported"] == ["WR1"]
    with pytest.raises(runtime_commands.RuntimeWriteError):
        runtime_commands.validate_device_values("WR1", {"runtime_role": "ac_input"}, live())


def test_only_the_limits_the_admin_applies_live_follow_the_file(tmp_path):
    import json

    config = {
        "system": {"max_total_power": 800, "max_device_power": 600},
        "devices": [{"name": "WR1", "max_power": 700}, {"name": "WR2"}],
    }
    path, live = _live_context(tmp_path, config)
    changed = {
        "system": {"max_total_power": 1600, "max_device_power": 1200},
        "devices": [{"name": "WR1", "max_power": 750}, {"name": "WR2"}, {"name": "WR3", "max_power": 500}],
    }
    path.write_text(json.dumps(changed), encoding="utf-8")

    context = live()
    assert context["system_max_total_power"] == 1600
    assert context["min_output_limit_max"] == 1600
    assert context["device_max_power"] == {"WR1": 750, "WR2": 1200}
    assert context["fallback_device_max_power"] == 600


def test_the_live_limits_are_the_ones_the_admin_applies_without_a_restart():
    """Paired with the Admin's applies_live and live_device_paths: a limit that follows the file must apply live there."""

    from admin.config_runtime_overlap import applies_live, live_device_paths

    devices = [{"name": "WR1", "ip": "192.0.2.10", "sn": "S1", "max_power": 700}, {"name": "WR2", "ip": "192.0.2.11", "sn": "S2"}]
    before = {"system": {"max_total_power": 800, "max_device_power": 600}, "devices": devices}
    after = {**before, "system": {"max_total_power": 800, "max_device_power": 1200}}
    runtime = {"WR1": {"identity": "S1"}, "WR2": {"identity": "S2"}}

    assert applies_live("system.max_total_power")
    assert applies_live("devices[0].max_power", before)
    assert "devices[1].max_power" in live_device_paths(before, after, runtime)
    assert "devices[0].max_power" not in live_device_paths(before, after, runtime)


def test_the_ems_hands_the_live_context_itself_to_every_reader():
    """A snapshot taken at start, ``LiveValidationContext(...)()``, would freeze the limits again."""

    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "ems-solarflow-api-control.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    built = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "runtime_validation" for target in node.targets)
    ]
    assert len(built) == 1
    assert isinstance(built[0].value, ast.Call) and getattr(built[0].value.func, "id", "") == "LiveValidationContext"
    called = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "runtime_validation"
    ]
    assert called == []
    handed = [
        keyword.arg for node in ast.walk(tree) if isinstance(node, ast.Call)
        for keyword in node.keywords
        if isinstance(keyword.value, ast.Name) and keyword.value.id == "runtime_validation"
    ]
    assert sorted(handed) == ["power_limits", "runtime_validation", "validation_context", "validation_context"]
