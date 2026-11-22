# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A PV input the operator marks empty is neither shown nor recorded (#382 follow-up).

A SolarFlow 800 Pro 2 with three modules on four inputs reports the empty one
as ``solarPower1: 0`` around the clock, and nothing in its report tells an
empty input from a dark one. ``devices[].unused_pv_inputs`` names the empty
ones: the device tile and the InfluxDB history leave them out, the PV total
stays what the device reports, and the control path reads the report as it is.
"""

from types import SimpleNamespace

import pytest

from dashboard.telemetry import _state_telemetry_fields, build_dashboard_snapshot
from ems import config as cfg
from ems.clients import parse_device
from ems.config_catalog import config_field_index
from ems.config_mutation import ConfigChange, INVALID, SET, resolve_change
from ems.history.influx_writer import build_telemetry_lines
from ems.pv_inputs import pv_input_powers, pv_inputs_kept, unused_pv_inputs

pytestmark = [
    pytest.mark.contract,
    pytest.mark.config,
]

THREE_OF_FOUR = {"properties": {"electricLevel": 40, "solarInputPower": 148, "solarPower1": 0, "solarPower2": 23, "solarPower3": 59, "solarPower4": 66}}


def _fields(line):
    return dict(item.split("=", 1) for item in line.split(" ")[1].split(","))


def _device_line(lines, name="WR1"):
    return next(line for line in lines if line.startswith("zendure_device") and f"device={name}," in line)


@pytest.mark.parametrize(
    "value,expected",
    [
        ([1], {1}),
        ([4, 1], {1, 4}),
        ([], set()),
        (None, set()),
        ("1", set()),
        ([0, 5, True, 2.0, "3", 2], {2}),
    ],
)
def test_the_config_names_the_empty_inputs_and_nothing_it_cannot_mean(value, expected):
    assert unused_pv_inputs({"name": "WR1", "unused_pv_inputs": value}) == frozenset(expected)
    assert unused_pv_inputs(None) == frozenset()


def test_an_input_counts_when_the_report_carried_it_and_the_config_does_not_mark_it_empty():
    state = parse_device(THREE_OF_FOUR)

    assert pv_inputs_kept(state) == (True, True, True, True)
    assert pv_inputs_kept(state, frozenset({1})) == (False, True, True, True)
    assert pv_inputs_kept(SimpleNamespace(pv_inputs_reported=None), frozenset({3})) == (True, True, False, True)


def test_the_watch_reads_every_reported_input_even_one_marked_empty():
    assert pv_input_powers(parse_device(THREE_OF_FOUR)) == [0.0, 23.0, 59.0, 66.0]
    assert pv_input_powers(parse_device({"properties": {"solarPower2": 40}})) == [None, 40.0, None, None]


def test_the_tile_leaves_an_empty_input_out_and_keeps_the_total():
    fields = _state_telemetry_fields(parse_device(THREE_OF_FOUR), frozenset({1}))

    assert fields["pv_inputs_w"] == [None, 23.0, 59.0, 66.0]
    assert fields["pv_input_w"] == 148.0


def test_the_history_leaves_an_empty_input_out_and_keeps_the_total():
    lines = build_telemetry_lines(
        [SimpleNamespace(name="WR1")], [parse_device(THREE_OF_FOUR)], {"WR1": True}, 0,
        timestamp_ns=1, unused_inputs=lambda name: frozenset({1}),
    )
    fields = _fields(_device_line(lines))

    assert "solar1" not in fields
    assert (fields["solar2"], fields["solar3"], fields["solar4"], fields["solar"]) == ("23.0", "59.0", "66.0", "148.0")


def test_a_read_only_device_s_empty_input_is_left_out_of_the_history_too():
    state = parse_device({"properties": {"solarPower1": 0, "solarPower2": 210}})
    lines = build_telemetry_lines(
        [], [], {}, 0, timestamp_ns=1,
        read_only_tiles=[{"name": "DTU", "source": "read_only", "online": True, "state": state}],
        unused_inputs=lambda name: frozenset({1}) if name == "DTU" else frozenset(),
    )

    fields = _fields(_device_line(lines, "DTU"))
    assert "solar1" not in fields and fields["solar2"] == "210.0"


def _config(monkeypatch, *entries):
    monkeypatch.setattr(cfg, "ZENDURE_CONFIG", list(entries))


def test_the_ems_writes_the_history_by_the_saved_device_entry(monkeypatch):
    from ems.controller import EMSController

    _config(monkeypatch, {"name": "WR1", "ip": "192.0.2.10", "sn": "SN", "unused_pv_inputs": [1]})
    captured = []
    fake = SimpleNamespace(
        influx_writer=SimpleNamespace(enqueue=captured.append),
        devices=[SimpleNamespace(name="WR1")],
        device_online={"WR1": True},
        _last_influx_publish=0,
    )

    EMSController.publish_to_influx(fake, 0.0, [parse_device(THREE_OF_FOUR)])

    fields = _fields(_device_line(captured[0]))
    assert "solar1" not in fields and fields["solar4"] == "66.0"


def test_the_dashboard_shows_the_device_by_the_saved_device_entry(monkeypatch):
    _config(monkeypatch, {"name": "WR1", "ip": "192.0.2.10", "sn": "SN", "unused_pv_inputs": [1]})
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        runtime_state=None,
        device_online={"WR1": True},
        commanded_total_w=0,
        filtered_load_w=0,
        _dashboard_capabilities=[],
        zendure_mqtt_runtime=None,
    )

    snapshot = build_dashboard_snapshot(
        controller, load_w=0, states=[parse_device(THREE_OF_FOUR)], targets=[0], effective_targets=[0],
        allocated_total_w=0, effective_total_w=0, enabled=True, max_total_power=800, min_output_limit=35,
    )

    device = snapshot["devices"]["WR1"]
    assert device["pv_inputs_w"] == [None, 23.0, 59.0, 66.0]
    assert device["pv_input_w"] == 148.0


def test_a_marked_input_leaves_the_state_the_control_path_reads_as_reported(monkeypatch):
    _config(monkeypatch, {"name": "WR1", "ip": "192.0.2.10", "sn": "SN", "unused_pv_inputs": [2]})
    state = parse_device(THREE_OF_FOUR)

    _state_telemetry_fields(state, frozenset({2}))
    build_telemetry_lines([SimpleNamespace(name="WR1")], [state], {"WR1": True}, 0, timestamp_ns=1, unused_inputs=lambda name: frozenset({2}))

    assert (state.solar2, state.pv_inputs_reported) == (23, (True, True, True, True))


def _field():
    return config_field_index(scope="maintenance", prefix="devices[].")["unused_pv_inputs"]


def test_the_catalogue_declares_the_key_as_input_numbers_one_to_four():
    field = _field()

    assert field["type"] == "integer_list"
    assert (field["min"], field["max"]) == (1, 4)


@pytest.mark.parametrize("value", [[0], [5], [1, 9], ["x"], [1.5]])
def test_an_input_number_outside_one_to_four_is_refused(value):
    operation, _ = resolve_change(_field(), ConfigChange("unused_pv_inputs", value))

    assert operation == INVALID


def test_a_valid_list_is_written():
    assert resolve_change(_field(), ConfigChange("unused_pv_inputs", [3, 1])) == (SET, [3, 1])


def test_months_keep_their_own_range_check():
    months = config_field_index(scope="maintenance")["winter.months"]

    assert resolve_change(months, ConfigChange("winter.months", [13]))[0] == INVALID
    assert resolve_change(months, ConfigChange("winter.months", [11, 12]))[0] == SET


def test_the_capture_script_leaves_an_empty_input_out_as_the_ems_does():
    from tests.test_influx_runtime_state_read import load_capture_module

    fields = load_capture_module().build_device_fields(parse_device(THREE_OF_FOUR), frozenset({1}))

    assert "solar1" not in fields and fields["solar2"] == 23.0


@pytest.mark.parametrize(
    "entry,warned",
    [
        ({"name": "WR1", "ip": "192.0.2.10", "sn": "SN", "unused_pv_inputs": [1, 5]}, True),
        ({"name": "DTU", "type": "opendtu_http", "ip": "192.0.2.20", "unused_pv_inputs": "1"}, True),
        ({"name": "WR1", "ip": "192.0.2.10", "sn": "SN", "unused_pv_inputs": [1]}, False),
        ({"name": "WR1", "ip": "192.0.2.10", "sn": "SN"}, False),
    ],
)
def test_diagnose_warns_about_an_entry_it_cannot_read_fully(entry, warned):
    from ems.diagnostics import diagnose_config_plausibility

    checks = []
    diagnose_config_plausibility(checks, None, {"system": {}, "devices": [entry]})

    found = [check for check in checks if check["code"] == "device_unused_pv_inputs_invalid"]
    assert bool(found) is warned
    assert all(check["level"] == "warning" for check in found)


def test_an_opendtu_tile_leaves_an_empty_input_out_of_its_own_list(monkeypatch):
    from tests.test_opendtu_http_read_only_device import _answer, _inverter, _Response, _runtime, _snapshot

    _config(
        monkeypatch,
        {"name": "WR1", "ip": "192.0.2.10", "sn": "SN"},
        {"name": "TSUN OpenDTU", "type": "opendtu_http", "ip": "192.0.2.70", "unused_pv_inputs": [1]},
    )

    tile = _snapshot(_runtime(_Response(_answer(_inverter()))))["devices"]["TSUN OpenDTU"]

    assert tile["pv_inputs_w"] == [None, 205.1]
    assert tile["pv_input_w"] == 425.5
