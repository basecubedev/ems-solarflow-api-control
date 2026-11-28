# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The battery's condition in the history: what the device reports, what the EMS decided.

A battery in the cold was only visible in the logs: whether it heated, whether
the BMS let it charge, whether the EMS held its PV back. These are recorded per
cycle, kept by a downsampling task of their own and served as series, so the
Analytics batteries view can show them over a winter.
"""

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dashboard.telemetry import BATTERY_CONDITION_FIELDS, _state_telemetry_fields
from ems.clients import parse_device
from ems.config import normalize_influxdb_config
from ems.controller import history_decisions
from ems.history import schema
from ems.history.influx_provider import BATTERY_INFLUX_SERIES, InfluxHistoryProvider
from ems.history.influx_writer import _DEVICE_FIELDS, build_telemetry_lines
from ems.history.provider import (
    SERIES_CATALOG,
    STATE_SERIES,
    HistoryResult,
    _pack_series_value,
    decimate_history_result,
    normalize_series,
    pack_cells_of_series,
)
from ems.runtime_intents import DeviceRuntimeRole

pytestmark = [pytest.mark.contract]

REPORT = {
    "properties": {
        "electricLevel": 39,
        "packNum": 1,
        "heatState": 1,
        "lowTemperature": 1,
        "is_error": 0,
        "chargeAvailPower": 0,
        "dischargeAvailPower": 800,
        "derateFlag": 1,
        "inputLimit": 300,
    },
    "packData": [{"sn": "PACK-A", "maxTemp": 2741}],
}


def _fields(line):
    body = line.split(" ", 1)[1].rsplit(" ", 1)[0]
    return dict(pair.split("=", 1) for pair in body.split(","))


def _device_line(lines):
    return next(line for line in lines if line.startswith("zendure_device,"))


def test_the_report_s_battery_condition_reaches_the_device_point():
    state = parse_device(REPORT)

    fields = _fields(_device_line(build_telemetry_lines([SimpleNamespace(name="WR1")], [state], {"WR1": True}, 0, timestamp_ns=1)))

    assert (fields["heat_state"], fields["low_temp"], fields["error_flag"], fields["derate"]) == ("1.0", "1.0", "0.0", "1.0")
    assert (fields["charge_avail_w"], fields["discharge_avail_w"], fields["input_limit_w"]) == ("0.0", "800.0", "300.0")


def test_a_report_without_them_writes_none_of_them():
    state = parse_device({"properties": {"electricLevel": 50}})

    fields = _fields(_device_line(build_telemetry_lines([SimpleNamespace(name="WR1")], [state], {"WR1": True}, 0, timestamp_ns=1)))

    assert not {"heat_state", "low_temp", "error_flag", "derate", "charge_avail_w", "discharge_avail_w", "input_limit_w"} & set(fields)


def test_an_input_limit_the_device_does_not_report_is_left_out_not_written_as_zero():
    reported = parse_device({"properties": {"electricLevel": 50, "inputLimit": 0}})
    silent = parse_device({"properties": {"electricLevel": 50}})

    lines = build_telemetry_lines(
        [SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")], [reported, silent], {}, 0, timestamp_ns=1,
    )
    first, second = (_fields(line) for line in lines if line.startswith("zendure_device,"))

    assert first["input_limit_w"] == "0.0"
    assert "input_limit_w" not in second


def test_the_ems_s_decisions_are_written_as_numbers_the_history_can_serve(monkeypatch):
    monkeypatch.setattr("ems.controller.cfg.winter_feature_enabled", lambda runtime_state=None: True)
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1"), SimpleNamespace(name="WR2")],
        winter=SimpleNamespace(devices={"WR1": SimpleNamespace(target=34, holding=True)}),
        runtime_intents={"WR1": SimpleNamespace(role=DeviceRuntimeRole.AC_INPUT)},
    )

    decided = history_decisions(controller)
    lines = build_telemetry_lines(
        controller.devices, [parse_device(REPORT), parse_device(REPORT)], {}, 0, timestamp_ns=1, decisions=decided,
    )
    first, second = (_fields(line) for line in lines if line.startswith("zendure_device,"))

    assert (first["winter_target"], first["winter_hold"], first["ac_input"]) == ("34.0", "1.0", "1.0")
    assert not {"winter_target", "winter_hold", "ac_input"} & set(second)


def test_a_winter_target_is_recorded_only_while_winter_mode_is_on(monkeypatch):
    switched = {"on": True}
    monkeypatch.setattr("ems.controller.cfg.winter_feature_enabled", lambda runtime_state=None: switched["on"])
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name="WR1")],
        winter=SimpleNamespace(devices={"WR1": SimpleNamespace(target=34, holding=False)}),
        runtime_intents={},
        runtime_state=None,
    )

    assert history_decisions(controller)["WR1"]["winter_target"] == 34
    switched["on"] = False
    assert history_decisions(controller)["WR1"] == {"winter_target": None, "winter_hold": False, "ac_input": None}


def test_each_condition_keeps_its_own_reducer_in_the_downsampled_buckets():
    kept = {(measurement, field): reducer for measurement, field, reducer in schema.BATTERY_DOWNSAMPLED}

    assert kept == {
        ("battery_pack", "temp_c"): "mean",
        ("battery_pack", "soc"): "mean",
        ("battery_pack", "cell_spread_v"): "max",
        ("battery_pack", "voltage_v"): "mean",
        ("battery_pack", "current_a"): "mean",
        ("battery_pack", "flow_w"): "mean",
        **{("zendure_device", state): "max" for state in STATE_SERIES},
        ("zendure_device", "charge_avail_w"): "mean",
        ("zendure_device", "discharge_avail_w"): "mean",
        ("zendure_device", "input_limit_w"): "mean",
        ("zendure_device", "winter_target"): "last",
    }


def test_the_battery_task_carries_every_condition_field_the_writer_records_in_one_pipeline_per_reducer():
    written = set(_DEVICE_FIELDS) | {"input_limit_w", "winter_target", "winter_hold", "ac_input"}
    step = {"source": "raw", "target": "1m", "window": "1m"}
    flux = schema.build_battery_downsample_flux({"bucket_prefix": "ems", "org": "ems"}, step)
    pipelines = flux.split("from(bucket:")[1:]
    carried = set()
    for pipeline in pipelines:
        reducer = re.search(r"aggregateWindow\(every: 1m, fn: (\w+), createEmpty: false\)", pipeline).group(1)
        for measurement, fields in re.findall(r'r\._measurement == "(\w+)" and \(([^)]*)\)', pipeline):
            carried |= {(measurement, field, reducer) for field in re.findall(r'r\._field == "(\w+)"', fields)}

    assert len(pipelines) == len({reducer for _measurement, _field, reducer in schema.BATTERY_DOWNSAMPLED})
    assert carried == set(schema.BATTERY_DOWNSAMPLED)
    assert all(measurement == "battery_pack" or field in written for measurement, field, _reducer in carried)


def test_an_installed_system_gets_the_battery_task_by_itself():
    names = schema.planned_task_names(normalize_influxdb_config({"enabled": True, "bucket_prefix": "ems"}))

    assert {name for name in names if name.endswith(schema.BATTERY_TASK_SUFFIX)} == {
        "ems-downsample-1m:battery", "ems-downsample-5m:battery", "ems-downsample-1h:battery",
    }


def test_every_battery_series_is_catalogued_and_reads_a_field_a_downsampling_task_keeps():
    kept = {(measurement, field) for measurement, field, _reducer in schema.BATTERY_DOWNSAMPLED}

    for name, spec in BATTERY_INFLUX_SERIES.items():
        assert name in SERIES_CATALOG, name
        assert normalize_series([name]) == [name]
        assert (spec["measurement"], spec["field"]) in kept or spec["field"] in schema.NUMERIC_FIELDS, name


@pytest.mark.parametrize(
    "series,parsed",
    [("pack_temp:2", ("temp_c", 2)), ("pack_temp:0", None), ("pack_temp:17", None), ("pack_temp:x", None)],
)
def test_a_pack_temperature_is_read_per_pack(series, parsed):
    assert pack_cells_of_series(series) == parsed


class _Client:
    def __init__(self):
        self.queries = []

    def query_raw(self, flux, accept="application/csv"):
        self.queries.append(flux)
        return ",result,table,_time,_value\r\n,_result,0,2026-11-27T06:00:00Z,1,\r\n"


def _query(series):
    client = _Client()
    provider = InfluxHistoryProvider(normalize_influxdb_config({"enabled": True, "bucket_prefix": "ems"}), client=client)
    result = provider.query(
        datetime(2026, 11, 27, 5, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 27, 7, 0, tzinfo=timezone.utc),
        series=series,
        devices=["WR1"],
    )
    return result, client.queries


def test_the_pack_temperature_is_queried_from_its_pack_and_device():
    result, queries = _query(["pack_temp:2"])

    assert result.series["pack_temp:2"] == [1.0]
    assert 'r._field == "temp_c"' in queries[0] and 'r.pack == "2"' in queries[0] and 'r.device == "WR1"' in queries[0]


def test_a_state_keeps_its_window_s_maximum_so_a_short_heating_shows():
    _result, queries = _query(["heat_state"])

    assert 'r._field == "heat_state"' in queries[0]
    assert "fn: max" in queries[0]


def _long_range(values):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return HistoryResult(
        source="influxdb", start=start, end=start, window="5m",
        time=[index * 300 for index in range(len(values[next(iter(values))]))], series=dict(values),
    )


def test_a_long_range_thins_the_pack_temperature_by_its_mean():
    temps = [float(index % 10) for index in range(8640)]

    result = decimate_history_result(_long_range({"pack_temp:1": temps}), max_points=2000)

    assert len(result.series["pack_temp:1"]) == len(result.time) <= 2000
    assert 3.0 < sum(result.series["pack_temp:1"]) / len(result.series["pack_temp:1"]) < 6.0
    assert max(result.series["pack_temp:1"]) < 9.0


@pytest.mark.parametrize("state", STATE_SERIES)
def test_a_short_state_survives_a_long_range_being_thinned(state):
    misses = 0
    for at in range(0, 8640, 7):
        values = [0] * 8640
        values[at] = 1
        thinned = decimate_history_result(_long_range({state: values, "pv": [1.0] * 8640}), max_points=2000)
        misses += sum(thinned.series[state]) != 1

    assert misses == 0


def test_sqlite_reads_the_pack_temperature_as_the_mean_across_devices():
    payload = {
        "devices": {
            "WR1": {"battery_packs": [{"pack": 1, "temp_c": 2.0}]},
            "WR2": {"battery_packs": [{"pack": 1, "temp_c": 6.0}]},
            "Meter": {"read_only": True, "battery_packs": [{"pack": 1, "temp_c": 40.0}]},
        }
    }

    assert _pack_series_value(payload, "temp_c", 1, None) == 4.0
    assert _pack_series_value(payload, "temp_c", 1, ["WR2"]) == 6.0


def _keeps(predicate, measurement, field):
    record = {"r._measurement": f'"{measurement}"', "r._field": f'"{field}"'}
    text = predicate
    for name, value in record.items():
        text = text.replace(name, value)
    return eval(text, {"__builtins__": {}}, {})


def test_each_battery_pipeline_keeps_exactly_its_reducer_s_fields_and_no_other():
    candidates = {(measurement, field) for measurement, field, _reducer in schema.BATTERY_DOWNSAMPLED} | {
        ("zendure_device", "soc"), ("zendure_device", "temp_c"), ("battery_pack", "heat_state"),
        ("battery_pack", "cell_min_v"), ("grid", "power"),
    }
    for reducer in {reducer for _measurement, _field, reducer in schema.BATTERY_DOWNSAMPLED}:
        predicate = schema._battery_predicate(reducer)
        kept = {pair for pair in candidates if _keeps(predicate, *pair)}

        assert kept == {(measurement, field) for measurement, field, kept_by in schema.BATTERY_DOWNSAMPLED if kept_by == reducer}


def test_the_sqlite_summary_reads_no_field_a_series_does_not_have():
    from ems.history import provider

    assert "$.None" not in provider._SUMMARY_SELECT
    assert None not in provider._SUMMARY_FIELDS


def test_the_live_data_carries_the_battery_s_states_and_what_it_accepts_none_when_not_reported():
    """The device card reads them now as the history keeps them; a state the
    report does not carry stays missing rather than reading as off."""

    reported = _state_telemetry_fields(parse_device({"properties": {
        "electricLevel": 40, "heatState": 1, "lowTemperature": 0, "derateFlag": 1, "is_error": 0,
        "chargeAvailPower": 0, "dischargeAvailPower": 1200,
    }}))
    silent = _state_telemetry_fields(parse_device({"properties": {"electricLevel": 40}}))

    assert {name: reported[name] for name in BATTERY_CONDITION_FIELDS} == {
        "heat_state": 1, "low_temp": 0, "derate": 1, "error_flag": 0, "charge_avail_w": 0, "discharge_avail_w": 1200,
    }
    assert {name: silent[name] for name in BATTERY_CONDITION_FIELDS} == dict.fromkeys(BATTERY_CONDITION_FIELDS)
    assert set(BATTERY_CONDITION_FIELDS) <= set(_DEVICE_FIELDS)
