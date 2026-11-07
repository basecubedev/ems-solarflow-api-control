# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config -> runtime-state convergence (Tier 2) for Admin maintenance."""

import copy
import json

import pytest

from admin.install_context import detect_install_context
from admin.runtime_convergence import (
    mirror_changed_keys_to_runtime,
    reset_targets_to_config,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.authority,
    pytest.mark.config,
    pytest.mark.maintenance,
    pytest.mark.integration,
    pytest.mark.simulation,
]


def _config(**system):
    base_system = {
        "enabled": True,
        "max_total_power": 1600,
        "loop_interval": 3,
        "min_output_limit": 35,
    }
    base_system.update(system)
    return {
        "system": base_system,
        "winter": {"enabled": False},
        "devices": [
            {
                "name": "WR1",
                "ip": "192.168.1.100",
                "sn": "AAA",
                "max_power": 800,
                "pv_priority_factor": 1.0,
                "enabled": True,
                "min_soc": 10,
            },
        ],
    }


def _seed_runtime(tmp_path, *, system=None, winter=None, devices=None):
    data = {
        "system": {
            "enabled": True,
            "max_total_power": 1600,
            "loop_interval": 3,
            "min_output_limit": 35,
        },
        "winter": {"enabled": False},
        "devices": {
            "WR1": {
                "enabled": True,
                "max_power": 800,
                "offgrid_socket_mode": "off",
                "pv_priority_factor": 1.0,
            }
        },
    }
    if system:
        data["system"].update(system)
    if winter:
        data["winter"].update(winter)
    if devices is not None:
        data["devices"] = devices
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _context(tmp_path):
    return detect_install_context(base_dir=str(tmp_path))


def _runtime(tmp_path):
    return json.loads((tmp_path / "runtime-state.json").read_text())


def _two_devices():
    config = _config()
    config["devices"].append(
        {
            "name": "WR2",
            "ip": "192.168.1.101",
            "sn": "BBB",
            "max_power": 600,
            "pv_priority_factor": 1.0,
            "enabled": True,
        }
    )
    return config


def _seed_two(tmp_path, **changes):
    devices = {
        "WR1": {"identity": "AAA", "enabled": True, "max_power": 800, "offgrid_socket_mode": "off", "pv_priority_factor": 1.0},
        "WR2": {"identity": "BBB", "enabled": True, "max_power": 600, "offgrid_socket_mode": "off", "pv_priority_factor": 1.0},
    }
    for name, values in changes.items():
        devices[name].update(values)
    return _seed_runtime(tmp_path, devices=devices)


def _mirror(tmp_path, previous, merged):
    return mirror_changed_keys_to_runtime(_context(tmp_path), previous=previous, merged=merged)


def test_mirror_changed_system_key(tmp_path):
    _seed_runtime(tmp_path)
    config = _config(loop_interval=7)
    result = _mirror(tmp_path, _config(), config)
    assert result["applied"] == ["system.loop_interval"]
    assert result["skipped"] == []
    assert _runtime(tmp_path)["system"]["loop_interval"] == 7


def test_mirror_winter_enabled(tmp_path):
    _seed_runtime(tmp_path)
    config = _config()
    config["winter"]["enabled"] = True
    result = _mirror(tmp_path, _config(), config)
    assert result["applied"] == ["winter.enabled"]
    assert _runtime(tmp_path)["winter"]["enabled"] is True


def test_mirror_device_key(tmp_path):
    _seed_runtime(tmp_path)
    config = _config()
    config["devices"][0]["max_power"] = 600
    result = _mirror(tmp_path, _config(), config)
    assert result["applied"] == ["devices[0].max_power"]
    assert _runtime(tmp_path)["devices"]["WR1"]["max_power"] == 600


def test_pure_config_key_is_not_mirrored(tmp_path):
    _seed_runtime(tmp_path)
    config = _config()
    config["grid_meter"] = {"type": "shelly", "ip": "192.168.1.50"}
    config["devices"][0]["min_soc"] = 20
    result = _mirror(tmp_path, _config(), config)
    assert result["applied"] == []
    assert _runtime(tmp_path)["system"]["loop_interval"] == 3


def test_value_over_runtime_ceiling_is_skipped(tmp_path):
    _seed_runtime(tmp_path)
    config = _config(max_total_power=6000, max_total_power_limit=3000)
    result = _mirror(tmp_path, _config(), config)
    assert result["applied"] == []
    assert result["skipped"][0]["path"] == "system.max_total_power"
    assert _runtime(tmp_path)["system"]["max_total_power"] == 1600


def test_a_configured_total_above_5000_w_reaches_the_runtime(tmp_path):
    _seed_runtime(tmp_path)
    config = _config(max_total_power=7200)
    result = _mirror(tmp_path, _config(), config)
    assert result["applied"] == ["system.max_total_power"]
    assert _runtime(tmp_path)["system"]["max_total_power"] == 7200


def test_a_new_device_is_skipped_and_not_created(tmp_path):
    _seed_runtime(tmp_path)
    config = _config()
    config["devices"].append(
        {"name": "WR2", "ip": "192.168.1.101", "sn": "BBB", "max_power": 700}
    )
    result = _mirror(tmp_path, _config(), config)
    assert result["applied"] == []
    assert result["skipped"][0]["path"] == "devices[1].max_power"
    assert "WR2" not in _runtime(tmp_path)["devices"]


def test_missing_runtime_file_is_skipped_and_not_created(tmp_path):
    config = _config(loop_interval=7)
    result = _mirror(tmp_path, _config(), config)
    assert result["applied"] == []
    assert result["skipped"][0]["reason"] == "runtime_state_absent"
    assert not (tmp_path / "runtime-state.json").exists()


def test_mirror_preserves_untouched_override(tmp_path):
    _seed_runtime(tmp_path, system={"max_total_power": 900})
    config = _config(loop_interval=7)
    _mirror(tmp_path, _config(), config)
    data = _runtime(tmp_path)
    assert data["system"]["loop_interval"] == 7
    assert data["system"]["max_total_power"] == 900


def test_reset_writes_config_value_not_delete(tmp_path):
    _seed_runtime(tmp_path, system={"loop_interval": 5})
    config = _config(loop_interval=3)
    result = reset_targets_to_config(
        _context(tmp_path), config, [{"scope": "system", "key": "loop_interval"}]
    )
    assert result["applied"] == ["system.loop_interval"]
    data = _runtime(tmp_path)
    assert "loop_interval" in data["system"]
    assert data["system"]["loop_interval"] == 3


def test_reset_device_key_to_config(tmp_path):
    _seed_runtime(
        tmp_path,
        devices={
            "WR1": {
                "enabled": True,
                "max_power": 600,
                "offgrid_socket_mode": "off",
                "pv_priority_factor": 1.0,
            }
        },
    )
    config = _config()
    result = reset_targets_to_config(
        _context(tmp_path),
        config,
        [{"scope": "device", "name": "WR1", "key": "max_power"}],
    )
    assert result["applied"] == ["devices.WR1.max_power"]
    assert _runtime(tmp_path)["devices"]["WR1"]["max_power"] == 800


def test_a_renamed_device_whose_entry_carries_no_serial_is_skipped(tmp_path):
    """An entry from before serials were stamped is matched by name, as at the EMS's start, which gives the renamed device fresh defaults."""

    _seed_runtime(tmp_path)
    config = _config()
    config["devices"][0]["name"] = "WRX"
    config["devices"][0]["max_power"] = 500
    result = _mirror(tmp_path, _config(), config)
    assert result["applied"] == []
    assert result["skipped"][0]["path"] == "devices[0].max_power"
    data = _runtime(tmp_path)
    assert "WRX" not in data["devices"]
    assert data["devices"]["WR1"]["max_power"] == 800


def test_a_name_swap_switches_off_the_device_that_was_switched_off(tmp_path):
    """The running EMS knows WR1 as AAA; a save that swaps the names must not switch BBB off."""

    _seed_two(tmp_path)
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    merged["devices"][0]["name"], merged["devices"][1]["name"] = "WR2", "WR1"
    merged["devices"][0]["enabled"] = False

    result = _mirror(tmp_path, previous, merged)

    devices = _runtime(tmp_path)["devices"]
    assert (devices["WR1"]["enabled"], devices["WR2"]["enabled"]) == (False, True)
    assert result["applied"] == ["devices[0].enabled"]


def test_removing_a_device_and_taking_its_name_reaches_the_renamed_device(tmp_path):
    _seed_two(tmp_path)
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    del merged["devices"][0]
    merged["devices"][0]["name"] = "WR1"
    merged["devices"][0]["enabled"] = False

    _mirror(tmp_path, previous, merged)

    devices = _runtime(tmp_path)["devices"]
    assert devices["WR2"]["enabled"] is False
    assert devices["WR1"] == {"identity": "AAA", "enabled": True, "max_power": 800, "offgrid_socket_mode": "off", "pv_priority_factor": 1.0}


def test_a_rename_and_a_new_value_in_one_save_reach_the_renamed_device(tmp_path):
    """The EMS keeps a renamed device's runtime entry at its next start, values included, so the new value has to land there now."""

    _seed_two(tmp_path)
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    merged["devices"][0]["name"] = "WRX"
    merged["devices"][0]["max_power"] = 500

    result = _mirror(tmp_path, previous, merged)

    devices = _runtime(tmp_path)["devices"]
    assert "WRX" not in devices
    assert devices["WR1"]["max_power"] == 500
    assert result["applied"] == ["devices[0].max_power"]


def test_removing_a_device_leaves_the_next_devices_live_override_alone(tmp_path):
    _seed_two(tmp_path, WR2={"max_power": 400})
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    del merged["devices"][0]

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR2"]["max_power"] == 400
    assert result["applied"] == []


def test_removing_a_device_does_not_hide_the_next_devices_change(tmp_path):
    """List positions shift: the removed WR1 had 800 W where WR2 now asks for 800 W."""

    _seed_two(tmp_path)
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    del merged["devices"][0]
    merged["devices"][0]["max_power"] = 800

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR2"]["max_power"] == 800
    assert result["applied"] == ["devices[0].max_power"]


def test_a_device_without_a_serial_never_reaches_an_entry_another_serial_holds(tmp_path):
    _seed_two(tmp_path)
    previous = _two_devices()
    del previous["devices"][0]["sn"]
    merged = copy.deepcopy(previous)
    merged["devices"][0]["enabled"] = False

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is True
    assert [entry["path"] for entry in result["skipped"]] == ["devices[0].enabled"]


def test_a_reset_after_a_name_swap_reaches_the_device_by_its_serial(tmp_path):
    _seed_two(tmp_path, WR1={"max_power": 400})
    config = _two_devices()
    config["devices"][0]["name"], config["devices"][1]["name"] = "WR2", "WR1"

    result = reset_targets_to_config(
        _context(tmp_path),
        config,
        [{"scope": "device", "name": "WR2", "key": "max_power"}],
    )

    devices = _runtime(tmp_path)["devices"]
    assert (devices["WR1"]["max_power"], devices["WR2"]["max_power"]) == (800, 600)
    assert result["applied"] == ["devices.WR2.max_power"]


def _with_spare(config):
    """A switched-off second entry for WR1's serial: validation and the EMS start both let it through, the EMS leaves it out."""

    config["devices"].append(
        {"name": "WR1 spare", "ip": "192.168.1.102", "sn": "AAA", "max_power": 1200, "enabled": False}
    )
    return config


def test_a_switched_off_duplicate_of_a_running_device_never_writes_into_its_entry(tmp_path):
    _seed_two(tmp_path)
    previous = _two_devices()
    merged = _with_spare(copy.deepcopy(previous))

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"] == {"identity": "AAA", "enabled": True, "max_power": 800, "offgrid_socket_mode": "off", "pv_priority_factor": 1.0}
    assert (result["applied"], result["skipped"]) == ([], [])


def test_an_unrelated_save_leaves_a_device_with_a_switched_off_duplicate_alone(tmp_path):
    """WR1 was switched off from the Dashboard; comparing it with its duplicate would switch it back on."""

    _seed_two(tmp_path, WR1={"enabled": False})
    previous = _with_spare(_two_devices())
    merged = copy.deepcopy(previous)
    merged["system"]["loop_interval"] = 7

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is False
    assert (result["applied"], result["skipped"]) == (["system.loop_interval"], [])


def test_a_reset_for_a_switched_off_duplicate_does_not_reach_the_running_device(tmp_path):
    _seed_two(tmp_path, WR1={"max_power": 400})
    config = _with_spare(_two_devices())

    result = reset_targets_to_config(
        _context(tmp_path),
        config,
        [{"scope": "device", "name": "WR1 spare", "key": "max_power"}],
    )

    assert _runtime(tmp_path)["devices"]["WR1"]["max_power"] == 400
    assert result["applied"] == []
    assert result["skipped"][0]["reason"] == "device WR1 spare does not speak for a device with a runtime entry"


def test_an_entry_whose_sn_and_serial_number_disagree_reaches_no_entry(tmp_path):
    """The EMS stamps an HTTP device with sn and an MQTT one with serial_number first, so neither serial is safe to follow."""

    _seed_two(tmp_path)
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    merged["devices"][0]["serial_number"] = "BBB"
    merged["devices"][0]["enabled"] = False

    result = _mirror(tmp_path, previous, merged)

    devices = _runtime(tmp_path)["devices"]
    assert (devices["WR1"]["enabled"], devices["WR2"]["enabled"]) == (True, True)
    assert (result["applied"], result["skipped"]) == ([], [])


def test_switching_a_device_off_while_adding_a_duplicate_reaches_the_running_ems(tmp_path):
    """Neither entry is built after the save, so the device leaves control; the running EMS hears it now."""

    _seed_two(tmp_path)
    previous = _two_devices()
    merged = _with_spare(copy.deepcopy(previous))
    merged["devices"][0]["enabled"] = False

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is False
    assert result["applied"] == ["devices[0].enabled"]


def _telemetry(name, serial, *, enabled=True):
    return {
        "name": name,
        "type": "zendure_mqtt",
        "serial_number": serial,
        "enabled": enabled,
        "mqtt": {"broker_ref": "default", "device_id": "dev-" + serial.lower()},
    }


def test_switching_a_device_off_while_adding_its_telemetry_entry_reaches_the_running_ems(tmp_path):
    _seed_two(tmp_path)
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    merged["devices"][0]["enabled"] = False
    merged["devices"].append(_telemetry("WR1 MQTT", "AAA"))

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is False
    assert result["applied"] == ["devices[0].enabled"]


def test_turning_the_only_entry_telemetry_only_takes_the_device_out_of_control_now(tmp_path):
    """The EMS leaves a telemetry-only device out at its next start; until then it would keep writing to it."""

    _seed_two(tmp_path)
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    merged["devices"][0] = _telemetry("WR1", "AAA")

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is False
    assert result["applied"] == ["devices[0].enabled"]


def test_a_telemetry_entry_switched_on_never_switches_control_back_on(tmp_path):
    _seed_two(tmp_path, WR1={"enabled": False})
    previous = _two_devices()
    previous["devices"][0]["enabled"] = False
    merged = copy.deepcopy(previous)
    merged["devices"][0] = _telemetry("WR1", "AAA")

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is False
    assert result["applied"] == []


def test_the_entry_the_ems_builds_next_speaks_for_the_device_now(tmp_path):
    """WR1 hands its serial to its duplicate: the device stays controlled, with the duplicate's values."""

    _seed_two(tmp_path)
    previous = _with_spare(_two_devices())
    previous["devices"][2]["max_power"] = 400
    merged = copy.deepcopy(previous)
    merged["devices"][0]["enabled"] = False
    merged["devices"][2]["enabled"] = True

    result = _mirror(tmp_path, previous, merged)

    assert (_runtime(tmp_path)["devices"]["WR1"]["enabled"], _runtime(tmp_path)["devices"]["WR1"]["max_power"]) == (True, 400)
    assert result["applied"] == ["devices[2].max_power"]


def test_a_transport_switch_that_drops_the_serial_waits_for_the_restart(tmp_path):
    """Without its serial the new entry cannot be tied to the running device; it is skipped, not guessed by name."""

    _seed_two(tmp_path)
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    merged["devices"][0] = {"name": "WR1", "type": "zendure_mqtt", "enabled": False, "mqtt": {"broker_ref": "default", "device_id": "dev-x"}}

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is True
    assert (result["applied"], result["skipped"]) == ([], [])


def test_switching_a_device_back_on_leaves_its_live_overrides_alone(tmp_path):
    """The Dashboard lowered WR1 and stopped its grid charging; a save that only switches it on again must keep that."""

    _seed_two(tmp_path, WR1={"enabled": False, "max_power": 300, "ac_charge_enabled": False})
    previous = _two_devices()
    previous["devices"][0].update(enabled=False, ac_charge_enabled=True)
    merged = copy.deepcopy(previous)
    merged["devices"][0]["enabled"] = True

    result = _mirror(tmp_path, previous, merged)

    wr1 = _runtime(tmp_path)["devices"]["WR1"]
    assert (wr1["enabled"], wr1["max_power"], wr1["ac_charge_enabled"]) == (True, 300, False)
    assert result["applied"] == ["devices[0].enabled"]


def test_a_switch_on_comes_after_the_values_it_brings(tmp_path):
    from admin.config_runtime_overlap import device_runtime_plan

    previous = _two_devices()
    previous["devices"][0]["enabled"] = False
    merged = copy.deepcopy(previous)
    merged["devices"][0].update(enabled=True, max_power=600)

    assert [key for *_rest, key, _value in device_runtime_plan(previous, merged)] == ["max_power", "enabled"]


def test_a_device_the_replaced_config_built_twice_is_left_alone(tmp_path):
    """Two built entries for one serial leave the EMS unable to tell its devices apart; nothing is guessed."""

    _seed_two(tmp_path)
    previous = _two_devices()
    previous["devices"][1]["sn"] = "AAA"
    merged = copy.deepcopy(previous)
    merged["devices"][0]["enabled"] = False

    result = _mirror(tmp_path, previous, merged)

    devices = _runtime(tmp_path)["devices"]
    assert (devices["WR1"]["enabled"], devices["WR1"]["max_power"]) == (True, 800)
    assert (result["applied"], result["skipped"]) == ([], [])


def test_a_serial_written_as_a_number_is_the_serial_the_ems_stamps(tmp_path):
    """A hand-written number turns into text in the next save; the device stays the same device."""

    _seed_two(tmp_path, WR1={"identity": "1234567", "enabled": False})
    previous = _two_devices()
    previous["devices"][0]["sn"] = 1234567
    merged = copy.deepcopy(previous)
    merged["devices"][0]["sn"] = "1234567"
    merged["devices"][1]["max_power"] = 500

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is False
    assert result["applied"] == ["devices[1].max_power"]


def test_a_reset_for_a_device_the_config_does_not_hold_writes_nothing(tmp_path):
    _seed_runtime(tmp_path, devices={"WR9": {"enabled": False, "max_power": 400}})

    result = reset_targets_to_config(
        _context(tmp_path),
        _config(),
        [{"scope": "device", "name": "WR9", "key": "enabled"}],
    )

    assert _runtime(tmp_path)["devices"]["WR9"]["enabled"] is False
    assert result["applied"] == []


def test_a_value_saved_while_a_device_is_switched_off_is_there_when_it_comes_back(tmp_path):
    """Off, then grid charging stopped, then on: three saves and no EMS restart."""

    _seed_two(tmp_path)
    first = _two_devices()
    first["devices"][0]["ac_charge_enabled"] = True
    second = copy.deepcopy(first)
    second["devices"][0]["enabled"] = False
    third = copy.deepcopy(second)
    third["devices"][0]["ac_charge_enabled"] = False
    fourth = copy.deepcopy(third)
    fourth["devices"][0]["enabled"] = True

    applied = [_mirror(tmp_path, before, after)["applied"] for before, after in ((first, second), (second, third), (third, fourth))]

    wr1 = _runtime(tmp_path)["devices"]["WR1"]
    assert (wr1["enabled"], wr1["ac_charge_enabled"]) == (True, False)
    assert applied == [["devices[0].enabled"], ["devices[0].ac_charge_enabled"], ["devices[0].enabled"]]


def test_a_telemetry_entry_listed_first_does_not_speak_for_a_device_switched_off_and_on(tmp_path):
    _seed_two(tmp_path, WR1={"max_power": 350, "pv_priority_factor": 3.0})
    first = _two_devices()
    first["devices"].insert(0, dict(_telemetry("WR1 telemetry", "AAA"), max_power=1200, pv_priority_factor=2.0))
    second = copy.deepcopy(first)
    second["devices"][1]["enabled"] = False
    third = copy.deepcopy(second)
    third["devices"][1]["enabled"] = True

    applied = [_mirror(tmp_path, before, after)["applied"] for before, after in ((first, second), (second, third))]

    wr1 = _runtime(tmp_path)["devices"]["WR1"]
    assert (wr1["enabled"], wr1["max_power"], wr1["pv_priority_factor"]) == (True, 350, 3.0)
    assert applied == [["devices[1].enabled"], ["devices[1].enabled"]]


def test_a_handover_over_two_saves_brings_the_new_entrys_values(tmp_path):
    """HTTP off, then the MQTT control entry for the same route on: with nothing built the MQTT entry speaks, so its values arrive with the switch-off."""

    _seed_two(tmp_path)
    control = {
        "name": "WR1 MQTT",
        "type": "zendure_mqtt",
        "enabled": False,
        "max_power": 500,
        "ac_charge_enabled": False,
        "capabilities": {"write_output_limit": True},
        "mqtt": {"broker_ref": "default", "device_id": "AAA"},
    }
    first = _two_devices()
    first["devices"].insert(0, control)
    second = copy.deepcopy(first)
    second["devices"][1]["enabled"] = False
    third = copy.deepcopy(second)
    third["devices"][0]["enabled"] = True

    applied = [_mirror(tmp_path, before, after)["applied"] for before, after in ((first, second), (second, third))]

    wr1 = _runtime(tmp_path)["devices"]["WR1"]
    assert (wr1["enabled"], wr1["max_power"], wr1["ac_charge_enabled"]) == (True, 500, False)
    assert applied == [["devices[0].enabled", "devices[0].max_power", "devices[0].ac_charge_enabled"], ["devices[0].enabled"]]


def test_a_device_switched_on_under_another_entry_asks_for_the_restart(tmp_path):
    """The running EMS drives the inverter as WR1 until it restarts and builds WR1 MQTT; the save says so."""

    _seed_two(tmp_path, WR1={"enabled": False})
    control = {
        "name": "WR1 MQTT",
        "type": "zendure_mqtt",
        "enabled": True,
        "capabilities": {"write_output_limit": True},
        "mqtt": {"broker_ref": "default", "device_id": "AAA"},
    }
    previous = _two_devices()
    previous["devices"][0]["enabled"] = False
    previous["devices"].insert(0, dict(control, enabled=False))
    merged = copy.deepcopy(previous)
    merged["devices"][0]["enabled"] = True

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is True
    assert result["warnings"] == ["device WR1 MQTT runs as WR1 in the EMS until it restarts"]


def test_a_transport_switch_that_adds_defaults_writes_no_value(tmp_path):
    """A key the old entry left to the EMS default and the new one names with that default is no change."""

    _seed_two(tmp_path, WR1={"pv_priority_factor": 1.5, "ac_charge_enabled": False})
    previous = _two_devices()
    del previous["devices"][0]["pv_priority_factor"]
    merged = copy.deepcopy(previous)
    merged["devices"][0].update(pv_priority_factor=1.0, ac_charge_enabled=True)

    result = _mirror(tmp_path, previous, merged)

    wr1 = _runtime(tmp_path)["devices"]["WR1"]
    assert (wr1["pv_priority_factor"], wr1["ac_charge_enabled"]) == (1.5, False)
    assert result["applied"] == []


def test_a_telemetry_entry_for_the_same_route_leaves_the_devices_values_live(tmp_path):
    """The API device and its MQTT telemetry entry are one device; the telemetry entry never speaks for it."""

    _seed_two(tmp_path, WR1={"ac_charge_enabled": True})
    previous = _two_devices()
    previous["devices"].append({"name": "WR1 MQTT", "type": "zendure_mqtt", "enabled": True, "mqtt": {"broker_ref": "default", "device_id": "AAA"}})
    merged = copy.deepcopy(previous)
    merged["devices"][0].update(max_power=400, ac_charge_enabled=False)

    result = _mirror(tmp_path, previous, merged)

    wr1 = _runtime(tmp_path)["devices"]["WR1"]
    assert (wr1["max_power"], wr1["ac_charge_enabled"]) == (400, False)
    assert result["applied"] == ["devices[0].max_power", "devices[0].ac_charge_enabled"]


def test_a_removed_key_brings_the_default_the_ems_falls_back_to(tmp_path):
    """Cleared, the field leaves WR1 at the installation's device ceiling; typing that ceiling later changes nothing more."""

    _seed_two(tmp_path, WR1={"max_power": 1200})
    first = _two_devices()
    first["devices"][0]["max_power"] = 1200
    second = copy.deepcopy(first)
    del second["devices"][0]["max_power"]
    third = copy.deepcopy(second)
    third["devices"][0]["max_power"] = 800

    applied = [_mirror(tmp_path, before, after)["applied"] for before, after in ((first, second), (second, third))]

    assert _runtime(tmp_path)["devices"]["WR1"]["max_power"] == 800
    assert applied == [["devices[0].max_power"], []]


def test_a_lower_device_ceiling_reaches_a_device_that_takes_it(tmp_path):
    _seed_two(tmp_path, WR1={"max_power": 1200})
    previous = _two_devices()
    previous["system"]["max_device_power"] = 1200
    del previous["devices"][0]["max_power"]
    merged = copy.deepcopy(previous)
    merged["system"]["max_device_power"] = 800

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["max_power"] == 800
    assert result["applied"] == ["devices[0].max_power"]


def test_a_serial_moved_to_another_entry_reaches_no_running_device(tmp_path):
    """Crosswise serials corrected in two saves: the running EMS still knows the .100 inverter as AAA, and it stays off."""

    _seed_two(tmp_path, WR1={"enabled": False})
    first = _two_devices()
    second = copy.deepcopy(first)
    second["devices"][0]["sn"] = "CCC"
    third = copy.deepcopy(second)
    third["devices"][1]["sn"] = "AAA"

    applied = [_mirror(tmp_path, before, after)["applied"] for before, after in ((first, second), (second, third))]

    assert _runtime(tmp_path)["devices"]["WR1"] == {"identity": "AAA", "enabled": False, "max_power": 800, "offgrid_socket_mode": "off", "pv_priority_factor": 1.0}
    assert applied == [[], []]


def test_a_device_that_gains_its_serial_keeps_its_live_entry(tmp_path):
    """An entry from before serials were stamped: the EMS backfills it by name, so the save reaches it the same way."""

    _seed_runtime(tmp_path)
    previous = _config()
    del previous["devices"][0]["sn"]
    merged = copy.deepcopy(previous)
    merged["devices"][0].update(sn="AAA", max_power=500)

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["max_power"] == 500
    assert result["applied"] == ["devices[0].max_power"]


def test_a_re_added_device_brings_its_values_to_the_entry_its_serial_still_owns(tmp_path):
    """Removed and added again without a restart: the EMS would keep the old entry's values, so the new ones go there now."""

    _seed_two(tmp_path, WR1={"max_power": 1200, "enabled": False})
    first = _two_devices()
    first["devices"][0]["max_power"] = 1200
    second = copy.deepcopy(first)
    del second["devices"][0]
    third = copy.deepcopy(second)
    third["devices"].append({"name": "WR1 new", "ip": "192.168.1.100", "sn": "AAA", "max_power": 400, "enabled": True})

    applied = [_mirror(tmp_path, before, after)["applied"] for before, after in ((first, second), (second, third))]

    wr1 = _runtime(tmp_path)["devices"]["WR1"]
    assert (wr1["max_power"], wr1["enabled"]) == (400, False)
    assert applied == [[], ["devices[1].max_power", "devices[1].offgrid_socket_mode", "devices[1].pv_priority_factor", "devices[1].ac_charge_enabled"]]


def test_the_mirror_writes_what_the_ems_would_seed(tmp_path):
    """No own ceiling (0) is the installation's, and a fraction of a watt is dropped, as at the EMS's start."""

    _seed_two(tmp_path, WR1={"max_power": 300})
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    merged["devices"][0]["max_power"] = 0
    merged["devices"][1]["max_power"] = 599.5

    result = _mirror(tmp_path, previous, merged)

    devices = _runtime(tmp_path)["devices"]
    assert (devices["WR1"]["max_power"], devices["WR2"]["max_power"]) == (300, 599)
    assert result["applied"] == ["devices[1].max_power"]


def test_no_own_ceiling_brings_the_installations_ceiling(tmp_path):
    """A max_power of 0 leaves WR1 at system.max_device_power; the runtime validation takes it as the EMS does."""

    _seed_two(tmp_path, WR1={"max_power": 1200})
    previous = _two_devices()
    previous["devices"][0]["max_power"] = 1200
    merged = copy.deepcopy(previous)
    merged["devices"][0]["max_power"] = 0

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["max_power"] == 800
    assert (result["applied"], result["skipped"]) == (["devices[0].max_power"], [])


def test_a_new_entry_under_a_serial_less_devices_name_at_another_address_reaches_nothing(tmp_path):
    """The running WR1 has no serial yet; a new WR1 elsewhere is another inverter, so its values wait for the restart."""

    _seed_runtime(tmp_path, devices={"WR1": {"enabled": True, "max_power": 600, "ac_charge_enabled": False}})
    previous = _config()
    del previous["devices"][0]["sn"]
    previous["devices"][0]["max_power"] = 600
    merged = copy.deepcopy(previous)
    merged["devices"][0].update(name="WR3", sn="AAA")
    merged["devices"].append({"name": "WR1", "ip": "192.168.1.109", "sn": "CCC", "max_power": 1600, "enabled": False})

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"] == {"enabled": True, "max_power": 600, "ac_charge_enabled": False}
    assert result["applied"] == []


def test_a_save_that_only_turns_1_0_into_1_writes_nothing(tmp_path):
    """The browser sends the draft back as JSON, where 1.0 and 1 are one number."""

    _seed_two(tmp_path, WR1={"pv_priority_factor": 3.0})
    previous = _two_devices()
    merged = copy.deepcopy(previous)
    merged["devices"][0]["pv_priority_factor"] = 1

    result = _mirror(tmp_path, previous, merged)

    assert _runtime(tmp_path)["devices"]["WR1"]["pv_priority_factor"] == 3.0
    assert result["applied"] == []


def test_mixed_valid_and_invalid_keys_apply_per_key(tmp_path):
    _seed_runtime(tmp_path)
    config = _config(loop_interval=7, max_total_power=6000, max_total_power_limit=3000)
    result = _mirror(tmp_path, _config(), config)
    assert result["applied"] == ["system.loop_interval"]
    assert [entry["path"] for entry in result["skipped"]] == ["system.max_total_power"]
    data = _runtime(tmp_path)
    assert data["system"]["loop_interval"] == 7
    assert data["system"]["max_total_power"] == 1600


def test_reset_writes_config_default_when_config_silent(tmp_path):
    _seed_runtime(
        tmp_path,
        devices={
            "WR1": {
                "enabled": False,
                "max_power": 800,
                "offgrid_socket_mode": "off",
                "pv_priority_factor": 1.0,
            }
        },
    )
    config = _config()
    del config["devices"][0]["enabled"]
    result = reset_targets_to_config(
        _context(tmp_path),
        config,
        [{"scope": "device", "name": "WR1", "key": "enabled"}],
    )
    assert result["applied"] == ["devices.WR1.enabled"]
    assert _runtime(tmp_path)["devices"]["WR1"]["enabled"] is True


@pytest.mark.power_control
def test_converged_value_wins_over_config_default_in_resolver(tmp_path):
    from ems.runtime_state import RuntimeState

    _seed_runtime(tmp_path)
    config = _config(loop_interval=7)
    _mirror(tmp_path, _config(), config)

    runtime_state = RuntimeState(
        str(tmp_path / "runtime-state.json"),
        {"system": {"loop_interval": 3}, "devices": {}},
    )
    runtime_state.load_if_changed(force=True)
    assert runtime_state.get_system("loop_interval", 3) == 7
