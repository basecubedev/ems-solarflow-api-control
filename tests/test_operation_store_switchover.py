# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 3 of docs/developer/settings-operation-store.md: the switch-over.

At a live start the operating values still in config.json move into
runtime-state.json, which the starting EMS has filled with each of them (its
runtime value where one was set, the config's otherwise); the caps and a
device's activation stay, and a disabled device keeps its values.
"""

import json
import logging
from types import SimpleNamespace

import pytest

from ems import config as cfg
from ems.runtime_commands import OPERATION_DEVICE_KEYS, OPERATION_SETTING_PATHS

pytestmark = [
    pytest.mark.unit,
    pytest.mark.config,
    pytest.mark.power_control,
]


def _config():
    return {
        "config_defaults_revision": cfg.DEFAULTS_REVISION,
        "config_upgrade": {"on_startup": "check", "backup_before_apply": False},
        "system": {
            "enabled": True,
            "dry_run": False,
            "max_total_power": 1600,
            "loop_interval": 5,
            "min_output_limit": 35,
            "grid_target_discharge_w": 0,
        },
        "grid_meter": {"type": "shelly", "ip": "10.20.30.50"},
        "winter": {"enabled": True, "months": [10, 11]},
        "battery_care": {"enabled": True, "min_discharge_w": 50},
        "devices": [
            {"name": "WR1", "ip": "10.20.30.5", "sn": "A", "max_power": 800, "enabled": True,
             "pv_priority_factor": 1.0, "ac_charge_enabled": True, "ac_discharge_enabled": True},
            {"name": "WR2", "ip": "10.20.30.6", "sn": "B", "max_power": 600, "enabled": False,
             "pv_priority_factor": 2.0},
        ],
    }


def _runtime():
    return {
        "system": {"enabled": False, "max_total_power": 1200, "loop_interval": 7, "min_output_limit": 35,
                   "grid_target_discharge_w": 0},
        "winter": {"enabled": False},
        "battery_care": {"enabled": True, "min_discharge_w": 50},
        "devices": {"WR1": {"enabled": True, "max_power": 700, "pv_priority_factor": 1.5,
                            "ac_charge_enabled": True, "ac_discharge_enabled": False}},
    }


def _move(tmp_path, config, runtime, monkeypatch, *, may=True, started=None, loaded=True):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, indent=2))
    persisted = []
    messages = []
    monkeypatch.setattr(cfg, "START_MAY_MOVE_OPERATING_VALUES", may)
    monkeypatch.setattr(cfg, "STARTED_RAW_CONFIG", config if started is None else started)
    moved = cfg.move_startup_operating_values(
        runtime,
        lambda: persisted.append((json.loads(path.read_text()), json.loads(json.dumps(runtime)))),
        path=str(path),
        base_dir=str(tmp_path),
        emit_message=lambda level, text: messages.append((level, text)),
        loaded=loaded,
    )
    return moved, json.loads(path.read_text()), persisted, messages


def test_the_switch_over_only_removes_operating_values(tmp_path, monkeypatch):
    """Guided Setup's deployment marker relies on it (admin/deployment.py ``_is_prepared_config``)."""

    config = _config()
    _moved, after, _persisted, _messages = _move(tmp_path, config, _runtime(), monkeypatch)
    assert cfg.only_operating_values_removed(config, after) is True

    rewritten = json.loads(json.dumps(after))
    rewritten["devices"][1]["pv_priority_factor"] = 3.0
    assert cfg.only_operating_values_removed(config, rewritten) is False

    retyped = json.loads(json.dumps(after))
    retyped["devices"][1]["pv_priority_factor"] = 2
    assert cfg.only_operating_values_removed(config, retyped) is False

    added = json.loads(json.dumps(after))
    added["system"]["loop_interval"] = 6
    assert cfg.only_operating_values_removed(config, added) is False

    installation = json.loads(json.dumps(after))
    installation["grid_meter"]["ip"] = "10.20.30.51"
    assert cfg.only_operating_values_removed(config, installation) is False


def test_the_operating_values_the_runtime_state_holds_leave_the_config(tmp_path, monkeypatch):
    moved, saved, persisted, messages = _move(tmp_path, _config(), _runtime(), monkeypatch)

    assert set(moved) == {
        "system.enabled", "system.loop_interval", "system.min_output_limit", "system.grid_target_discharge_w",
        "winter.enabled", "battery_care.enabled", "battery_care.min_discharge_w",
        "devices[0].pv_priority_factor", "devices[0].ac_charge_enabled", "devices[0].ac_discharge_enabled",
    }
    assert saved["system"] == {"dry_run": False, "max_total_power": 1600}
    assert saved["winter"] == {"months": [10, 11]}
    assert saved["battery_care"] == {}
    assert saved["devices"][0] == {"name": "WR1", "ip": "10.20.30.5", "sn": "A", "max_power": 800, "enabled": True}
    assert saved["devices"][1]["pv_priority_factor"] == 2.0
    assert persisted and "loop_interval" in persisted[0][0]["system"]
    assert any("Operating values moved" in text for _, text in messages)


def test_a_value_the_runtime_state_does_not_hold_stays_in_the_config(tmp_path, monkeypatch):
    runtime = _runtime()
    del runtime["system"]["loop_interval"]
    del runtime["winter"]

    moved, saved, _, _ = _move(tmp_path, _config(), runtime, monkeypatch)

    assert "system.loop_interval" not in moved and saved["system"]["loop_interval"] == 5
    assert saved["winter"]["enabled"] is True


def test_nothing_moves_at_a_start_that_may_not_write_the_config(tmp_path, monkeypatch):
    config = _config()
    moved, saved, persisted, messages = _move(tmp_path, config, _runtime(), monkeypatch, may=False)

    assert moved == [] and saved == config and persisted == [] and messages == []


def test_a_config_without_operating_values_is_not_written(tmp_path, monkeypatch):
    config = _config()
    config = cfg._without_operating_values(config, _runtime())[0]
    moved, saved, persisted, messages = _move(tmp_path, config, _runtime(), monkeypatch)

    assert moved == [] and persisted == [] and messages == []


def _load(tmp_path, config, **flags):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    args = SimpleNamespace(config=str(path), dry_run=False, simulate=False, replay=None, self_test=False, preflight=False)
    for key, value in flags.items():
        setattr(args, key, value)
    snapshot = {name: getattr(cfg, name) for name in dir(cfg) if name.isupper()}
    try:
        cfg.load_config(args, str(tmp_path))
        return cfg.START_MAY_MOVE_OPERATING_VALUES
    finally:
        for name, value in snapshot.items():
            setattr(cfg, name, value)


def test_only_a_live_start_that_may_write_the_config_moves(tmp_path, caplog):
    caplog.set_level(logging.CRITICAL)
    assert _load(tmp_path, _config()) is True
    assert _load(tmp_path, _config(), dry_run=True) is False
    disabled = _config()
    disabled["config_upgrade"]["on_startup"] = "disabled"
    assert _load(tmp_path, disabled) is False
    dry = _config()
    dry["system"]["dry_run"] = True
    assert _load(tmp_path, dry) is False


def test_the_config_upgrade_never_adds_a_moved_operating_value_back():
    plan = cfg.build_config_upgrade_plan({"devices": [{"name": "WR1", "ip": "10.20.30.5"}]}, None)
    upgraded = plan["upgraded_config"]

    assert not [item["path"] for item in plan["add"] if item["path"] in OPERATION_SETTING_PATHS]
    assert not OPERATION_SETTING_PATHS & {
        f"{section}.{key}" for section, block in upgraded.items() if isinstance(block, dict) for key in block
    }
    assert not OPERATION_DEVICE_KEYS & set(upgraded["devices"][0])
    assert upgraded["system"]["max_total_power"] and upgraded["devices"][0]["max_power"]


def test_diagnose_does_not_miss_an_operating_value_the_config_no_longer_holds():
    from ems.diagnostics import diagnose_missing_template_keys

    template = {"system": {"loop_interval": 5, "deadband": 2}, "winter": {"enabled": True, "months": [11]}}

    assert diagnose_missing_template_keys({"system": {}, "winter": {}}, template) == [
        "system.deadband", "winter.months",
    ]


def test_nothing_moves_when_the_runtime_state_could_not_be_read(tmp_path, monkeypatch):
    config = _config()
    moved, saved, persisted, messages = _move(tmp_path, config, _runtime(), monkeypatch, loaded=False)

    assert moved == [] and saved == config and persisted == []
    assert any("could not be read" in text for _, text in messages)


def test_nothing_moves_when_the_file_changed_since_the_start_read_it(tmp_path, monkeypatch):
    config = _config()
    read_at_start = json.loads(json.dumps(config))
    read_at_start["system"]["loop_interval"] = 9
    moved, saved, persisted, _ = _move(tmp_path, config, _runtime(), monkeypatch, started=read_at_start)

    assert moved == [] and saved == config and persisted == []


def test_a_switched_off_entry_sharing_a_running_devices_name_keeps_its_values(tmp_path, monkeypatch):
    config = _config()
    config["devices"].append({"name": "WR1", "ip": "10.20.30.7", "sn": "C", "enabled": False, "pv_priority_factor": 4.0})

    _, saved, _, _ = _move(tmp_path, config, _runtime(), monkeypatch)

    assert "pv_priority_factor" not in saved["devices"][0]
    assert saved["devices"][2]["pv_priority_factor"] == 4.0


def test_a_device_without_a_runtime_entry_keeps_its_values_in_its_config_entry(tmp_path, monkeypatch):
    """It never ran: its next start seeds its entry from them."""

    _, saved, _, _ = _move(tmp_path, _config(), _runtime(), monkeypatch)

    assert saved["devices"][1] == _config()["devices"][1]


def test_a_switched_off_device_with_a_runtime_entry_loses_its_values_from_the_config(tmp_path, monkeypatch):
    """Backlog 73: its entry is its one place, switched on or off."""

    runtime = _runtime()
    runtime["devices"]["WR2"] = {"identity": "B", "enabled": True, "max_power": 600, "pv_priority_factor": 2.0}

    moved, saved, _, _ = _move(tmp_path, _config(), runtime, monkeypatch)

    assert "pv_priority_factor" not in saved["devices"][1]
    assert "devices[1].pv_priority_factor" in moved


def test_a_runtime_state_that_cannot_be_saved_moves_nothing_and_starts(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    config = _config()
    path.write_text(json.dumps(config, indent=2))
    monkeypatch.setattr(cfg, "START_MAY_MOVE_OPERATING_VALUES", True)
    monkeypatch.setattr(cfg, "STARTED_RAW_CONFIG", config)
    messages = []

    def refuse():
        raise PermissionError("read-only data directory")

    moved = cfg.move_startup_operating_values(
        _runtime(), refuse, path=str(path), base_dir=str(tmp_path),
        emit_message=lambda level, text: messages.append(text),
    )

    assert moved == [] and json.loads(path.read_text()) == config
    assert any("could not be written" in text for text in messages)


def _device_defaults(identity):
    return {"identity": identity, "enabled": True, "max_power": 800, "pv_priority_factor": 1.0,
            "ac_charge_enabled": True, "ac_discharge_enabled": True, "offgrid_socket_mode": "off"}


def test_a_switched_off_device_keeps_its_runtime_entry_and_runs_with_it_when_built_again(tmp_path, monkeypatch):
    """Backlog 73: one place per device; switching it off no longer moves its values anywhere."""

    from ems.runtime_state import RuntimeState

    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", [{"name": "WR1", "sn": "A"}, {"name": "WR2", "sn": "B", "enabled": False}])

    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {
        "WR1": _device_defaults("A"),
        "WR2": {**_device_defaults("B"), "pv_priority_factor": 3.0, "ac_discharge_enabled": False, "max_power": 500},
    }}))
    one = {"system": {}, "devices": {"WR1": _device_defaults("A")}}
    both = {"system": {}, "devices": {"WR1": _device_defaults("A"), "WR2": _device_defaults("B")}}

    switched_off = RuntimeState(str(path), one).load_or_create()

    assert switched_off["devices"]["WR2"]["pv_priority_factor"] == 3.0
    assert "parked_devices" not in json.loads(path.read_text())

    switched_on = RuntimeState(str(path), both).load_or_create()

    assert switched_on["devices"]["WR2"]["pv_priority_factor"] == 3.0
    assert switched_on["devices"]["WR2"]["ac_discharge_enabled"] is False
    assert switched_on["devices"]["WR2"]["max_power"] == 500


def test_a_parked_value_wins_over_what_the_config_entry_seeds(tmp_path, monkeypatch):
    """It is the value the device ran with, and the Admin changes it where it is parked."""

    from ems.runtime_state import RuntimeState

    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", [])

    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {"WR1": _device_defaults("A")}, "parked_devices": {
        "identity:B": {"pv_priority_factor": 3.0, "ac_discharge_enabled": False},
    }}))
    defaults = {"system": {}, "devices": {"WR1": _device_defaults("A"), "WR2": {**_device_defaults("B"), "pv_priority_factor": 2.0}}}

    built = RuntimeState(str(path), defaults).load_or_create()

    assert built["devices"]["WR2"]["pv_priority_factor"] == 3.0
    assert built["devices"]["WR2"]["ac_discharge_enabled"] is False


def test_a_device_removed_from_the_config_loses_its_runtime_entry(tmp_path, monkeypatch):
    from ems.runtime_state import RuntimeState

    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {
        "WR1": _device_defaults("A"),
        "WR2": {**_device_defaults("B"), "ac_discharge_enabled": False},
        "OLD": {"max_power": 300},
    }}))
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", [{"name": "WR1", "sn": "A"}])

    loaded = RuntimeState(str(path), {"system": {}, "devices": {"WR1": _device_defaults("A")}}).load_or_create()

    assert set(loaded["devices"]) == {"WR1"}


def test_a_device_read_without_control_keeps_its_runtime_entry_without_getting_one(tmp_path, monkeypatch):
    from ems.runtime_state import RuntimeState

    telemetry = {"type": "zendure_mqtt", "name": "MQ", "serial_number": "M1",
                 "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "M1"},
                 "capabilities": {"read_power": True, "write_output_limit": False}}
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {
        "WR1": _device_defaults("A"),
        "MQ": {**_device_defaults("M1"), "pv_priority_factor": 2.5},
    }}))
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", [{"name": "WR1", "sn": "A"}, telemetry])

    loaded = RuntimeState(str(path), {"system": {}, "devices": {"WR1": _device_defaults("A")}}).load_or_create()

    assert loaded["devices"]["MQ"]["pv_priority_factor"] == 2.5


def test_v0_10_8_parked_values_fill_the_entry_of_the_device_they_were_parked_for_once(tmp_path, monkeypatch):
    from ems.runtime_state import RuntimeState

    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {"WR1": _device_defaults("A")}, "parked_devices": {
        "identity:B": {"pv_priority_factor": 3.0}, "identity:Z": {"pv_priority_factor": 9.0},
    }}))
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", [{"name": "WR1", "sn": "A"}, {"name": "WR2", "sn": "B", "enabled": False}])
    defaults = {"system": {}, "devices": {"WR1": _device_defaults("A"), "WR2": _device_defaults("B")}}

    loaded = RuntimeState(str(path), defaults).load_or_create()

    assert loaded["devices"]["WR2"]["pv_priority_factor"] == 3.0
    assert "parked_devices" not in json.loads(path.read_text())


def test_a_device_removed_from_the_config_loses_its_parked_values(tmp_path, monkeypatch):
    """Added back later, it starts from what its entry holds, not from values it ran with before it was removed."""

    from ems.runtime_state import RuntimeState

    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {
        "WR1": _device_defaults("A"),
        "WR2": {**_device_defaults("B"), "ac_discharge_enabled": False},
    }, "parked_devices": {"identity:C": {"pv_priority_factor": 2.0}}}))
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", [{"name": "WR1", "sn": "A"}])

    RuntimeState(str(path), {"system": {}, "devices": {"WR1": _device_defaults("A")}}).load_or_create()

    assert "parked_devices" not in json.loads(path.read_text())


def test_a_value_edited_into_the_config_by_hand_is_named_when_the_running_one_stays(tmp_path, monkeypatch):
    config = _config()
    config["system"]["loop_interval"] = 9
    config["devices"][0]["ac_discharge_enabled"] = False
    runtime = _runtime()
    runtime["devices"]["WR1"]["ac_discharge_enabled"] = True

    runtime["operating_values_moved"] = True
    _, saved, _, messages = _move(tmp_path, config, runtime, monkeypatch)

    warned = [text for level, text in messages if level == logging.WARNING]
    assert len(warned) == 1
    assert "system.loop_interval: config.json 9, running 7" in warned[0]
    assert "devices[0].ac_discharge_enabled: config.json False, running True" in warned[0]
    assert "loop_interval" not in saved["system"]


def test_the_first_move_names_the_values_the_dashboard_changed_without_a_warning(tmp_path, monkeypatch):
    """Before the switch-over the dashboard's changes never reached config.json, so they differ there."""

    runtime = _runtime()
    moved, _, _, messages = _move(tmp_path, _config(), runtime, monkeypatch)

    assert moved
    assert not [text for level, text in messages if level == logging.WARNING]
    assert any("running values are kept" in text for level, text in messages if level == logging.INFO)
    assert runtime["operating_values_moved"] is True


def test_config_init_offers_winter_mode_on_for_a_fresh_install(tmp_path):
    from ems import config_init

    plan = config_init.build_plan({}, False, str(tmp_path / "config.json"), None)
    config = json.loads(json.dumps(plan["base_config"]))

    config_init.apply_system_basics(config, noninteractive=True)

    assert config["winter"]["enabled"] is True


def test_config_init_on_an_installed_config_asks_for_no_value_the_runtime_state_holds(tmp_path, capsys):
    """The next start would keep the running values and drop the answers."""

    from ems import config_init

    installed = _config()
    for key in ("enabled", "loop_interval", "min_output_limit", "grid_target_discharge_w"):
        installed["system"].pop(key)
    installed["winter"].pop("enabled")
    installed["devices"][0] = {key: value for key, value in installed["devices"][0].items() if key not in ("pv_priority_factor", "ac_charge_enabled", "ac_discharge_enabled")}

    updated = config_init.apply_answers(installed, cfg.build_config_upgrade_plan({}, None)["upgraded_config"], noninteractive=True, allow_placeholder_defaults=True, installed=True)

    assert "enabled" not in updated["system"] and "min_output_limit" not in updated["system"]
    assert "enabled" not in updated["winter"]
    assert not {"pv_priority_factor", "ac_charge_enabled", "ac_discharge_enabled"} & set(updated["devices"][0])
    assert "runtime-state.json" in capsys.readouterr().out


def test_a_runtime_value_the_ems_cannot_read_leaves_the_configs_in_place(tmp_path, monkeypatch):
    """The EMS falls back to the config's value; moving it would turn the built-in default on."""

    config = _config()
    config["devices"][0]["ac_charge_enabled"] = False
    runtime = _runtime()
    runtime["devices"]["WR1"]["ac_charge_enabled"] = "nope"

    moved, saved, _, _ = _move(tmp_path, config, runtime, monkeypatch)

    assert saved["devices"][0]["ac_charge_enabled"] is False
    assert "devices[0].ac_charge_enabled" not in moved


def test_a_serial_changed_by_hand_starts_like_a_new_device(tmp_path, monkeypatch):
    """A replaced inverter under the same name runs what its entry holds, not the values of the one it replaced."""

    from ems.runtime_state import RuntimeState

    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {
        "WR1": {**_device_defaults("OLD"), "pv_priority_factor": 2.5, "ac_discharge_enabled": False},
    }}))
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", [{"name": "WR1", "sn": "NEW"}])

    built = RuntimeState(str(path), {"system": {}, "devices": {"WR1": _device_defaults("NEW")}}).load_or_create()

    assert built["devices"]["WR1"]["identity"] == "NEW"
    assert (built["devices"]["WR1"]["pv_priority_factor"], built["devices"]["WR1"]["ac_discharge_enabled"]) == (1.0, True)
    assert "parked_devices" not in json.loads(path.read_text())

def test_a_runtime_state_first_written_from_template_placeholders_is_built_afresh(tmp_path):
    """The placeholder start forced control off; the operator's configured start must not inherit that."""

    from ems.runtime_state import start_runtime_state

    path = tmp_path / "runtime-state.json"
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    forced = {"system": {"enabled": False}, "winter": {"enabled": True}, "devices": {}}
    configured = {"system": {"enabled": True}, "winter": {"enabled": False}, "devices": {}}

    start_runtime_state(str(path), forced, str(config_path), placeholder_start=True)
    assert json.loads(path.read_text())["seeded_from_placeholders"] is True
    state = start_runtime_state(str(path), configured, str(config_path))

    assert state.data["system"]["enabled"] is True and state.data["winter"]["enabled"] is False
    assert "seeded_from_placeholders" not in json.loads(path.read_text())


def test_an_installed_runtime_state_is_never_rebuilt_by_a_placeholder_start(tmp_path):
    from ems.runtime_state import start_runtime_state

    path = tmp_path / "runtime-state.json"
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    path.write_text(json.dumps({"system": {"enabled": True}, "devices": {}}))

    start_runtime_state(str(path), {"system": {"enabled": False}, "devices": {}}, str(config_path), placeholder_start=True)
    state = start_runtime_state(str(path), {"system": {"enabled": False}, "devices": {}}, str(config_path))

    assert state.data["system"]["enabled"] is True


def test_a_device_replaced_under_the_same_name_keeps_what_was_typed_for_it(tmp_path, monkeypatch):
    """The old inverter's values are not parked for the new serial; what was typed for it runs."""

    from ems.runtime_state import RuntimeState

    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {
        "WR1": {**_device_defaults("OLD"), "pv_priority_factor": 2.5, "ac_discharge_enabled": True},
    }}))
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", [{"name": "WR1", "sn": "NEW", "ac_discharge_enabled": False}])
    monkeypatch.setattr(cfg, "device_config_entry", lambda name: {"name": "WR1", "sn": "NEW", "ac_discharge_enabled": False} if name == "WR1" else None)

    built = RuntimeState(str(path), {"system": {}, "devices": {"WR1": {**_device_defaults("NEW"), "ac_discharge_enabled": False}}}).load_or_create()

    assert built["devices"]["WR1"]["ac_discharge_enabled"] is False
    assert built["devices"]["WR1"]["pv_priority_factor"] == 1.0


def test_a_writer_that_saves_the_merge_gives_a_re_enabled_device_its_parked_values(tmp_path):
    """emsctl merges the file over defaults that include a device switched on again before the EMS restarted."""

    from ems.runtime_state import merge_runtime_defaults

    data = {"system": {}, "devices": {"WR1": _device_defaults("A")},
            "parked_devices": {"identity:B": {"pv_priority_factor": 3.0, "ac_discharge_enabled": False}}}
    defaults = {"system": {}, "devices": {"WR1": _device_defaults("A"), "WR2": _device_defaults("B")}}

    merged = merge_runtime_defaults(data, defaults)

    assert (merged["devices"]["WR2"]["pv_priority_factor"], merged["devices"]["WR2"]["ac_discharge_enabled"]) == (3.0, False)
    assert "parked_devices" not in merged
    assert data["parked_devices"]["identity:B"]["pv_priority_factor"] == 3.0
