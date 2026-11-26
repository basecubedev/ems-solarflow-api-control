# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One place per device: which runtime entry a config entry owns, and that a switched-off device keeps it.

Backlog 73 (owner, 2026-11-25): a device's operating values lived in its
runtime entry, in ``parked_devices`` or in its config entry, depending on its
state; the identity rule was computed in four places. Every device the EMS
can control now keeps its runtime entry, switched on or off, until it is
removed from ``config.json``, and ``ems.runtime_devices`` is the one rule.
"""

import json
from types import SimpleNamespace

import pytest

from ems import config as cfg
from ems.runtime_devices import (
    config_device_keys,
    device_key,
    device_seed,
    names_a_runtime_entry,
    stored_devices,
)
from ems.runtime_state import RuntimeState, build_runtime_defaults

pytestmark = [
    pytest.mark.unit,
    pytest.mark.config,
    pytest.mark.power_control,
]

TELEMETRY = {
    "type": "zendure_mqtt", "name": "MQ", "serial_number": "M1",
    "mqtt": {"topic_family": "zensdk_ha_scalar", "device_id": "M1"},
    "capabilities": {"read_power": True, "write_output_limit": False},
}
OPENDTU = {"name": "DTU", "type": "opendtu_http", "ip": "192.0.2.70", "serial": "116491234567"}


@pytest.mark.parametrize(
    ("entry", "key"),
    [
        ({"name": "WR1", "sn": "A"}, ("serial", "A")),
        ({"name": "WR1", "sn": 1234}, ("serial", "1234")),
        ({"name": "WR1", "serial_number": "B"}, ("serial", "B")),
        ({"name": "WR1", "sn": "A", "serial_number": "A"}, ("serial", "A")),
        ({"name": "WR1", "sn": "A", "serial_number": "B"}, None),
        ({"name": "WR1", "mqtt": {"device_id": "C"}}, ("serial", "C")),
        ({"name": "WR1"}, ("name", "WR1")),
        ({"sn": ""}, None),
        (OPENDTU, None),
    ],
)
def test_a_config_entry_stands_for_its_serial_else_its_name(entry, key):
    assert device_key(entry) == key


def test_every_device_the_ems_can_control_owns_an_entry_switched_on_or_off():
    devices = [
        {"name": "WR1", "ip": "192.0.2.5", "sn": "A"},
        {"name": "WR2", "ip": "192.0.2.6", "sn": "B", "enabled": False},
        TELEMETRY,
        OPENDTU,
    ]

    assert [(name, serial) for name, serial, _entry in stored_devices(devices)] == [("WR1", "A"), ("WR2", "B")]


def test_a_switched_off_copy_under_a_running_devices_name_owns_none():
    devices = [
        {"name": "WR1", "ip": "192.0.2.7", "sn": "C", "enabled": False},
        {"name": "WR1", "ip": "192.0.2.5", "sn": "A"},
    ]

    assert [(name, serial) for name, serial, _entry in stored_devices(devices)] == [("WR1", "A")]


def test_a_device_with_two_running_entries_owns_none():
    devices = [{"name": "WR1", "ip": "192.0.2.5", "sn": "A"}, {"name": "WR9", "ip": "192.0.2.9", "sn": "A"}]

    assert stored_devices(devices) == []


def test_only_a_device_removed_from_the_config_loses_its_entry():
    keys = config_device_keys([{"name": "WR1", "sn": "A"}, {"name": "WR2", "enabled": False}, TELEMETRY])

    assert names_a_runtime_entry(keys, "WR1", {"identity": "A"})
    assert names_a_runtime_entry(keys, "renamed", {"identity": "A"})
    assert names_a_runtime_entry(keys, "WR1", {})
    assert names_a_runtime_entry(keys, "WR2", {})
    assert names_a_runtime_entry(keys, "MQ", {"identity": "M1"})
    assert not names_a_runtime_entry(keys, "WR1", {"identity": "Z"})
    assert not names_a_runtime_entry(keys, "OLD", {})


def test_a_new_entry_starts_from_what_its_config_entry_holds():
    seed = device_seed({"name": "WR2", "sn": "B", "max_power": 600, "grid_off_mode": 1, "pv_priority_factor": 2.0}, 800)

    assert seed == {
        "enabled": True, "identity": "B", "max_power": 600, "offgrid_socket_mode": cfg.offgrid_socket_mode_for(1),
        "pv_priority_factor": 2.0, "ac_charge_enabled": True, "ac_discharge_enabled": True,
    }
    assert device_seed({"name": "WR3", "max_power": 0}, 800)["max_power"] == 800


def test_the_ems_seeds_an_entry_for_a_device_it_does_not_build():
    built = SimpleNamespace(name="WR1", sn="A", max_power=800, grid_off_mode=None, pv_priority_factor=1.0)
    devices = [
        {"name": "WR1", "ip": "192.0.2.5", "sn": "A"},
        {"name": "WR2", "ip": "192.0.2.6", "sn": "B", "enabled": False, "max_power": 600, "ac_discharge_enabled": False},
        TELEMETRY,
    ]

    seeded = build_runtime_defaults([built], devices)["devices"]

    assert set(seeded) == {"WR1", "WR2"}
    assert (seeded["WR2"]["identity"], seeded["WR2"]["max_power"], seeded["WR2"]["ac_discharge_enabled"]) == ("B", 600, False)
    assert set(build_runtime_defaults([built])["devices"]) == {"WR1"}


def test_a_device_switched_off_and_on_again_keeps_what_the_operator_set(tmp_path, monkeypatch):
    """Through the EMS's own start: its entry stays with the values set before it was switched off."""

    built = SimpleNamespace(name="WR1", sn="A", max_power=800, grid_off_mode=None, pv_priority_factor=1.0)
    off = [{"name": "WR1", "ip": "192.0.2.5", "sn": "A"}, {"name": "WR2", "ip": "192.0.2.6", "sn": "B", "enabled": False}]
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {
        "WR1": {"identity": "A", "max_power": 800},
        "WR2": {"identity": "B", "max_power": 500, "pv_priority_factor": 3.0, "ac_discharge_enabled": False},
    }}))
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", off)

    RuntimeState(str(path), build_runtime_defaults([built], off)).load_or_create()

    stored = json.loads(path.read_text())["devices"]["WR2"]
    assert (stored["max_power"], stored["pv_priority_factor"], stored["ac_discharge_enabled"]) == (500, 3.0, False)


def test_values_parked_for_a_device_read_without_control_become_its_entry(tmp_path, monkeypatch):
    """Nothing waits after the first start: the Admin shows and changes the entry, and it wins when the device controls."""

    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {"WR1": {"identity": "A"}}, "parked_devices": {
        "identity:M1": {"pv_priority_factor": 2.5}, "identity:GONE": {"pv_priority_factor": 9.0},
    }}))
    devices = [{"name": "WR1", "ip": "192.0.2.5", "sn": "A"}, TELEMETRY]
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", devices)
    built = SimpleNamespace(name="WR1", sn="A", max_power=800, grid_off_mode=None, pv_priority_factor=1.0)

    RuntimeState(str(path), build_runtime_defaults([built], devices)).load_or_create()

    stored = json.loads(path.read_text())
    assert stored["devices"]["MQ"] == {"pv_priority_factor": 2.5, "identity": "M1"}
    assert "parked_devices" not in stored


def test_a_switched_off_device_whose_serials_disagree_keeps_its_entry(tmp_path, monkeypatch):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {
        "WR1": {"identity": "A"}, "WR2": {"identity": "B", "pv_priority_factor": 3.0, "max_power": 500},
    }}))
    devices = [
        {"name": "WR1", "ip": "192.0.2.5", "sn": "A"},
        {"name": "WR2", "ip": "192.0.2.6", "sn": "B", "serial_number": "B2", "enabled": False},
    ]
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", devices)
    built = SimpleNamespace(name="WR1", sn="A", max_power=800, grid_off_mode=None, pv_priority_factor=1.0)

    loaded = RuntimeState(str(path), build_runtime_defaults([built], devices)).load_or_create()

    assert loaded["devices"]["WR2"]["pv_priority_factor"] == 3.0


def test_values_parked_by_name_for_an_old_inverter_never_override_what_was_typed_for_a_switched_off_new_one(tmp_path, monkeypatch):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {"WR1": {"identity": "A"}}, "parked_devices": {
        "name:WR2": {"pv_priority_factor": 5.0, "ac_discharge_enabled": False},
    }}))
    devices = [
        {"name": "WR1", "ip": "192.0.2.5", "sn": "A"},
        {"name": "WR2", "ip": "192.0.2.6", "sn": "B", "enabled": False, "pv_priority_factor": 2.0},
    ]
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", devices)
    built = SimpleNamespace(name="WR1", sn="A", max_power=800, grid_off_mode=None, pv_priority_factor=1.0)

    loaded = RuntimeState(str(path), build_runtime_defaults([built], devices)).load_or_create()

    assert loaded["devices"]["WR2"]["pv_priority_factor"] == 2.0
    assert loaded["devices"]["WR2"]["ac_discharge_enabled"] is False


def test_values_parked_by_name_never_override_what_was_typed_for_a_device_read_without_control(tmp_path, monkeypatch):
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {"WR1": {"identity": "A"}}, "parked_devices": {
        "name:MQ": {"pv_priority_factor": 9.0, "ac_discharge_enabled": False},
    }}))
    typed = {**TELEMETRY, "pv_priority_factor": 4.0}
    devices = [{"name": "WR1", "ip": "192.0.2.5", "sn": "A"}, typed]
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", devices)
    built = SimpleNamespace(name="WR1", sn="A", max_power=800, grid_off_mode=None, pv_priority_factor=1.0)

    RuntimeState(str(path), build_runtime_defaults([built], devices)).load_or_create()

    assert json.loads(path.read_text())["devices"]["MQ"] == {"ac_discharge_enabled": False, "identity": "M1"}


def test_emsctl_keeps_switched_off_devices_values_when_every_device_is_off(tmp_path):
    import emsctl

    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {
        "WR1": {"identity": "A", "enabled": False, "pv_priority_factor": 2.0},
        "WR2": {"identity": "B", "enabled": False, "pv_priority_factor": 3.0},
    }}))
    config = {"system": {}, "devices": [
        {"name": "WR1", "ip": "192.0.2.5", "sn": "A", "enabled": False},
        {"name": "WR2", "ip": "192.0.2.6", "sn": "B", "enabled": False},
    ]}

    runtime_state, _created = emsctl.open_runtime_state(str(path), config)
    runtime_state.update_section("winter", {"enabled": True})

    stored = json.loads(path.read_text())["devices"]
    assert (stored["WR2"]["pv_priority_factor"], stored["WR2"]["enabled"], stored["WR2"]["identity"]) == (3.0, False, "B")


def test_a_load_without_configured_devices_keeps_what_v0_10_8_parked(tmp_path):
    """The Admin's own instance loads with no devices; it must not take the parked values away."""

    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {"WR1": {"identity": "A"}}, "parked_devices": {
        "identity:B": {"pv_priority_factor": 3.0},
    }}))

    RuntimeState(str(path), {"devices": {}}).load_or_create()

    assert json.loads(path.read_text())["parked_devices"] == {"identity:B": {"pv_priority_factor": 3.0}}


def _runtime_with(tmp_path, devices):
    path = tmp_path / "runtime-state.json"
    if not path.exists():
        path.write_text(json.dumps({"system": {}, "devices": devices}))
    state = RuntimeState(str(path), {"devices": {}})
    state.load_if_changed(force=True)
    return path, state


def test_an_entry_the_admin_created_is_the_one_the_ems_starts_with(tmp_path, monkeypatch):
    """Backlog 73: the next start keeps what the Admin wrote for a device the save added, value for value."""

    from ems.runtime_devices import new_runtime_entry

    added = {"name": "WR2", "ip": "192.0.2.6", "sn": "B", "max_power": 600, "pv_priority_factor": 2.0, "ac_discharge_enabled": False}
    path, state = _runtime_with(tmp_path, {"WR1": {"identity": "A", "max_power": 800}})
    created = new_runtime_entry(added, 800)
    saved = [{"name": "WR1", "ip": "192.0.2.5", "sn": "A"}, {"name": "WR2", "ip": "192.0.2.6", "sn": "B", "max_power": 600}]

    assert state.create_configured_device("WR2", "B", created) == "WR2"

    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", saved)
    built = [
        SimpleNamespace(name="WR1", sn="A", max_power=800, grid_off_mode=None, pv_priority_factor=1.0),
        SimpleNamespace(name="WR2", sn="B", max_power=600, grid_off_mode=None, pv_priority_factor=1.0),
    ]
    started = RuntimeState(str(path), build_runtime_defaults(built, saved)).load_or_create()

    assert started["devices"]["WR2"] == created
    assert (created["identity"], created["pv_priority_factor"], created["ac_discharge_enabled"]) == ("B", 2.0, False)


def test_creating_an_entry_never_takes_one_another_device_holds(tmp_path):
    """Review: a device removed since the EMS started still runs under its name until the next start, from that entry."""

    path, state = _runtime_with(tmp_path, {"WR2": {"identity": "B", "pv_priority_factor": 3.0}})

    with pytest.raises(ValueError):
        state.create_configured_device("WR2", "C", {"identity": "C"})

    assert json.loads(path.read_text())["devices"]["WR2"] == {"identity": "B", "pv_priority_factor": 3.0}


def test_creating_an_entry_leaves_one_its_device_owns_by_name_alone(tmp_path):
    """An entry without a serial goes to the device under its name at the next start; it is not this write's to replace."""

    path, state = _runtime_with(tmp_path, {"WR2": {"pv_priority_factor": 3.0}})

    assert state.create_configured_device("WR2", "C", {"identity": "C"}) is None

    assert json.loads(path.read_text())["devices"]["WR2"] == {"pv_priority_factor": 3.0}


def test_creating_an_entry_the_device_came_to_own_meanwhile_writes_it_in_place(tmp_path):
    path, state = _runtime_with(tmp_path, {"OLD": {"identity": "C", "pv_priority_factor": 3.0}})

    assert state.create_configured_device("WR3", "C", {"identity": "C", "pv_priority_factor": 1.0}) == "OLD"

    assert json.loads(path.read_text())["devices"] == {"OLD": {"identity": "C", "pv_priority_factor": 1.0}}


def test_a_start_that_read_the_config_before_an_admin_save_keeps_the_entry_the_saved_config_names(tmp_path, monkeypatch):
    """Review: the start prunes against the config it read; an entry the Admin wrote for a device the config on disk now names stays."""

    from ems.runtime_state import start_runtime_state

    started_with = [{"name": "WR1", "ip": "192.0.2.5", "sn": "A"}]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"devices": started_with + [{"name": "WR3", "ip": "192.0.2.7", "sn": "C"}]}))
    path = tmp_path / "runtime-state.json"
    path.write_text(json.dumps({"system": {}, "devices": {
        "WR1": {"identity": "A", "max_power": 800},
        "WR3": {"identity": "C", "max_power": 600, "ac_charge_enabled": False},
    }}))
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", started_with)
    built = [SimpleNamespace(name="WR1", sn="A", max_power=800, grid_off_mode=None, pv_priority_factor=1.0)]

    state = start_runtime_state(str(path), build_runtime_defaults(built, started_with), str(config_path))

    assert state.data["devices"]["WR3"]["ac_charge_enabled"] is False
    assert json.loads(path.read_text())["devices"]["WR3"]["ac_charge_enabled"] is False


def test_the_starts_move_never_saves_over_a_runtime_state_another_writer_changed(tmp_path):
    """Review: the move persisted the state the start built after the Admin had written an added device's entry."""

    path, state = _runtime_with(tmp_path, {"WR1": {"identity": "A"}})
    _other_path, other = _runtime_with(tmp_path, {"WR1": {"identity": "A"}})
    other.create_configured_device("WR3", "C", {"identity": "C"})

    with pytest.raises(OSError):
        state.save_unless_changed()

    assert json.loads(path.read_text())["devices"]["WR3"] == {"identity": "C"}
