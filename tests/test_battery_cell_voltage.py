# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each battery pack's lowest and highest cell voltage, recorded per pack.

A SolarFlow 800 Pro 2 reported ``packData[].minVol`` 317 and ``maxVol`` 318
(hundredths of a volt, Zendure's property reference) beside ``totalVol`` 4760
for its one pack at SoC 40 on a November evening; the unit beside it showed
3.28 V at the same SoC. The cell voltage says where a battery really stands
when two state-of-charge estimates agree. The packs are numbered from 1 in
serial order and the serial itself goes nowhere: not into the live data, not
into InfluxDB.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from dashboard.sqlite_store import DashboardStore
from dashboard.telemetry import _state_telemetry_fields
from ems.clients import battery_pack_cells, parse_device, zendure_cell_voltage_v, zero_device_state
from ems.history import schema
from ems.history.influx_provider import PACK_CELL_INFLUX_SERIES, InfluxHistoryProvider
from ems.history.influx_writer import _PACK_CELL_FIELDS, build_telemetry_lines
from ems.history.provider import (
    PACK_CELL_REDUCERS,
    PACK_CELL_SERIES,
    SqliteHistoryProvider,
    normalize_series,
    pack_cells_of_series,
)
from ems.models import MAX_PACK_NUMBER, PackCells
from ems.diagnostics import diagnose_reported_property_names
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator, merge_packs, observable_metrics, pack_witness

pytestmark = [
    pytest.mark.contract,
]


def _pack(serial, low, high, **extra):
    return {"sn": serial, "packType": 300, "socLevel": 40, "minVol": low, "maxVol": high, **extra}


def _report(*packs, pack_num=None):
    properties = {"electricLevel": 40, "BatVolt": 4788}
    properties["packNum"] = len(packs) if pack_num is None else pack_num
    return {"properties": properties, "packData": list(packs)}


@pytest.mark.parametrize(
    "reported, volts",
    [(317, 3.17), (328, 3.28), ("324", 3.24), (365, 3.65), (420, 4.2), (150, 1.5), (500, 5.0), (317.4, 3.17)],
)
def test_hundredths_of_a_volt_become_volts(reported, volts):
    assert zendure_cell_voltage_v(reported) == volts


@pytest.mark.parametrize(
    "reported",
    [None, 0, -317, 149, 501, 65535, True, False, "", "abc", math.inf, math.nan, 10**400, "9" * 400],
)
def test_no_reading_or_an_impossible_one_is_no_cell_voltage(reported):
    assert zendure_cell_voltage_v(reported) is None


def test_the_report_gives_each_pack_its_cell_voltages():
    state = parse_device(_report(_pack("PACK-A", 317, 318)))

    assert state.battery_packs == (PackCells(pack=1, cell_min_v=3.17, cell_max_v=3.18),)


def test_packs_are_numbered_in_serial_order_whatever_order_the_report_lists():
    forward = parse_device(_report(_pack("PACK-A", 320, 322), _pack("PACK-B", 330, 333)))
    backward = parse_device(_report(_pack("PACK-B", 330, 333), _pack("PACK-A", 320, 322)))

    assert forward.battery_packs == backward.battery_packs == (
        PackCells(pack=1, cell_min_v=3.2, cell_max_v=3.22),
        PackCells(pack=2, cell_min_v=3.3, cell_max_v=3.33),
    )


@pytest.mark.parametrize(
    "packs",
    [
        [{"minVol": 330, "maxVol": 333}, {"minVol": 320, "maxVol": 322}],
        [{"sn": "PACK-B", "minVol": 330, "maxVol": 333}, {"minVol": 320, "maxVol": 322}],
        [{"sn": "", "minVol": 330, "maxVol": 333}],
        [{"sn": "PACK-A", "minVol": 330, "maxVol": 333}, {"sn": "PACK-A", "minVol": 320, "maxVol": 322}],
    ],
)
def test_a_report_with_a_pack_it_does_not_name_by_serial_numbers_none_of_them(packs):
    assert parse_device(_report(*packs)).battery_packs == ()


def test_a_report_with_fewer_packs_than_it_counts_numbers_none_of_them():
    partial = _report(_pack("PACK-B", 330, 333), pack_num=2)

    assert parse_device(partial).battery_packs == ()


def test_a_report_that_does_not_count_its_packs_numbers_none_of_them():
    report = {"properties": {"electricLevel": 40}, "packData": [_pack("PACK-B", 317, 318)]}

    assert parse_device(report).battery_packs == ()


def test_a_pack_without_cell_readings_keeps_its_number():
    state = parse_device(_report(_pack("PACK-A", 320, 322), {"sn": "PACK-B", "power": 4}))

    assert state.battery_packs == (
        PackCells(pack=1, cell_min_v=3.2, cell_max_v=3.22),
        PackCells(pack=2, cell_min_v=None, cell_max_v=None, power_w=4.0),
    )


def test_a_lowest_cell_above_the_highest_is_no_reading():
    assert parse_device(_report(_pack("PACK-A", 330, 320))).battery_packs == (
        PackCells(pack=1, cell_min_v=None, cell_max_v=None),
    )


def test_pack_data_inside_the_properties_reads_the_same():
    report = {"properties": {"packNum": 1, "packData": [_pack("PACK-A", 317, 318)]}}

    assert battery_pack_cells(report, report["properties"]) == (
        PackCells(pack=1, cell_min_v=3.17, cell_max_v=3.18),
    )


def test_a_report_counting_more_packs_than_the_history_numbers_numbers_none():
    packs = [_pack(f"PACK-{index:02d}", 320, 322) for index in range(MAX_PACK_NUMBER + 1)]

    assert parse_device(_report(*packs)).battery_packs == ()
    assert len(parse_device(_report(*packs[:MAX_PACK_NUMBER])).battery_packs) == MAX_PACK_NUMBER


def test_the_dashboard_knows_the_same_highest_pack_number():
    import re
    from pathlib import Path

    source = Path("frontend/dashboard-modern/src/lib/cells.js").read_text(encoding="utf-8")

    assert int(re.search(r"export const MAX_PACK_NUMBER = (\d+);", source).group(1)) == MAX_PACK_NUMBER


def test_a_device_without_packs_has_no_cell_voltages():
    assert parse_device({"properties": {"packNum": 0}}).battery_packs == ()
    assert parse_device({"properties": {}}).battery_packs == ()
    assert zero_device_state().battery_packs == ()


def test_an_mqtt_report_reaches_the_same_cell_voltages():
    aggregator = ZendureMqttAggregator()
    aggregator.observe(
        "iot/pk/dev1/properties/report",
        '{"properties": {"electricLevel": 40, "packNum": 1}, "packData": [{"sn": "PACK-A", "minVol": 317, "maxVol": 318}]}',
    )
    snapshot = aggregator.snapshots()[0]

    state = parse_device({"properties": observable_metrics(snapshot.metrics), "packData": pack_witness(snapshot)})

    assert state.battery_packs == (PackCells(pack=1, cell_min_v=3.17, cell_max_v=3.18),)


def _mqtt_state(aggregator):
    snapshot = aggregator.snapshots()[0]
    return parse_device({"properties": observable_metrics(snapshot.metrics), "packData": pack_witness(snapshot)})


def test_an_mqtt_report_of_one_pack_of_two_keeps_the_other_pack():
    aggregator = ZendureMqttAggregator()
    aggregator.observe(
        "iot/pk/dev1/properties/report",
        '{"properties": {"packNum": 2}, "packData": [{"sn": "PACK-A", "minVol": 320, "maxVol": 322},'
        ' {"sn": "PACK-B", "minVol": 330, "maxVol": 333}]}',
    )
    aggregator.observe("iot/pk/dev1/properties/report", '{"packData": [{"sn": "PACK-B", "minVol": 331}]}')

    assert _mqtt_state(aggregator).battery_packs == (
        PackCells(pack=1, cell_min_v=3.2, cell_max_v=3.22),
        PackCells(pack=2, cell_min_v=3.31, cell_max_v=3.33),
    )


def test_an_mqtt_device_numbers_no_pack_before_it_has_heard_of_all_of_them():
    aggregator = ZendureMqttAggregator()
    aggregator.observe(
        "iot/pk/dev1/properties/report",
        '{"properties": {"packNum": 2}, "packData": [{"sn": "PACK-B", "minVol": 330, "maxVol": 333}]}',
    )

    assert _mqtt_state(aggregator).battery_packs == ()


def test_a_change_report_updates_the_packs_it_names_and_leaves_the_rest():
    known = [{"sn": "PACK-A", "minVol": 320}, {"sn": "PACK-B", "minVol": 330}]

    assert merge_packs(known, [{"sn": "PACK-A", "maxVol": 322}], 2) == [
        {"sn": "PACK-A", "minVol": 320, "maxVol": 322},
        {"sn": "PACK-B", "minVol": 330},
    ]
    assert merge_packs(known, [{"minVol": 316}], 2) == [{"minVol": 316}]
    assert known == [{"sn": "PACK-A", "minVol": 320}, {"sn": "PACK-B", "minVol": 330}]


def test_a_change_report_naming_an_old_pack_keeps_a_pack_just_added():
    aggregator = ZendureMqttAggregator()
    topic = "iot/pk/dev1/properties/report"
    aggregator.observe(topic, '{"properties": {"packNum": 1}, "packData": [{"sn": "PACK-A", "minVol": 320, "maxVol": 322}]}')
    aggregator.observe(topic, '{"packData": [{"sn": "PACK-B", "minVol": 330, "maxVol": 333}]}')
    aggregator.observe(topic, '{"packData": [{"sn": "PACK-A", "socLevel": 41}]}')
    aggregator.observe(topic, '{"properties": {"packNum": 2}}')

    assert [cells.pack for cells in _mqtt_state(aggregator).battery_packs] == [1, 2]


def test_a_pack_added_in_a_change_report_drops_no_installed_pack():
    aggregator = ZendureMqttAggregator()
    topic = "iot/pk/dev1/properties/report"
    aggregator.observe(topic, '{"properties": {"packNum": 1}, "packData": [{"sn": "PACK-A", "minVol": 320, "maxVol": 322}]}')
    aggregator.observe(topic, '{"packData": [{"sn": "PACK-B", "minVol": 330, "maxVol": 333}]}')
    aggregator.observe(topic, '{"properties": {"packNum": 2}}')

    assert _mqtt_state(aggregator).battery_packs == (
        PackCells(pack=1, cell_min_v=3.2, cell_max_v=3.22),
        PackCells(pack=2, cell_min_v=3.3, cell_max_v=3.33),
    )


def test_a_list_of_every_pack_drops_the_packs_it_leaves_out_and_keeps_the_fields_it_does_not_repeat():
    known = [{"sn": "PACK-A", "minVol": 320}, {"sn": "PACK-B", "minVol": 330}]

    assert merge_packs(known, [{"sn": "PACK-A", "socLevel": 55}, {"sn": "PACK-C"}], 2) == [
        {"sn": "PACK-A", "minVol": 320, "socLevel": 55},
        {"sn": "PACK-C"},
    ]
    assert merge_packs([], [{"minVol": 316}]) == [{"minVol": 316}]


def test_a_change_report_of_a_single_pack_keeps_its_cells():
    aggregator = ZendureMqttAggregator()
    topic = "iot/pk/dev1/properties/report"
    aggregator.observe(topic, '{"properties": {"packNum": 1}, "packData": [{"sn": "PACK-A", "minVol": 317, "maxVol": 318}]}')
    aggregator.observe(topic, '{"packData": [{"sn": "PACK-A", "socLevel": 41}]}')

    assert _mqtt_state(aggregator).battery_packs == (PackCells(pack=1, cell_min_v=3.17, cell_max_v=3.18),)


def test_a_swapped_pack_is_forgotten_at_the_next_complete_report():
    aggregator = ZendureMqttAggregator()
    topic = "iot/pk/dev1/properties/report"
    aggregator.observe(topic, '{"properties": {"packNum": 2}, "packData": [{"sn": "PACK-A", "minVol": 320, "maxVol": 322},'
                              ' {"sn": "PACK-B", "minVol": 330, "maxVol": 333}]}')
    aggregator.observe(topic, '{"packData": [{"sn": "PACK-C", "minVol": 340, "maxVol": 344}]}')
    assert _mqtt_state(aggregator).battery_packs == ()

    aggregator.observe(topic, '{"properties": {"packNum": 2}, "packData": [{"sn": "PACK-A"}, {"sn": "PACK-C"}]}')
    assert _mqtt_state(aggregator).battery_packs == (
        PackCells(pack=1, cell_min_v=3.2, cell_max_v=3.22),
        PackCells(pack=2, cell_min_v=3.4, cell_max_v=3.44),
    )


def test_the_property_inventory_still_finds_an_unread_property_beside_pack_data():
    report = _report(_pack("PACK-A", 317, 318))
    report["properties"]["fooBar"] = 7

    assert "fooBar" in diagnose_reported_property_names(report)["unmapped"]
    assert "packNum" not in diagnose_reported_property_names(report)["unmapped"]


def test_the_live_data_names_each_pack_by_number_and_no_serial():
    state = parse_device(_report(_pack("PACK-A", 320, 322), _pack("PACK-B", 330, None)))

    fields = _state_telemetry_fields(state)

    assert fields["battery_packs"] == [
        {"pack": 1, "cell_min_v": 3.2, "cell_max_v": 3.22, "power_w": None, "state": None, "temp_c": None},
        {"pack": 2, "cell_min_v": 3.3, "cell_max_v": None, "power_w": None, "state": None, "temp_c": None},
    ]
    assert "PACK-A" not in repr(fields)
    assert _state_telemetry_fields(zero_device_state())["battery_packs"] == []


def _device(name):
    from types import SimpleNamespace

    return SimpleNamespace(name=name)


def test_influxdb_records_one_point_per_pack_and_no_serial():
    state = parse_device(_report(_pack("PACK-A", 320, 322), _pack("PACK-B", 330, None), {"sn": "PACK-C"}))

    lines = build_telemetry_lines([_device("Grill")], [state], {"Grill": True}, 0, timestamp_ns=1)

    packs = [line for line in lines if line.startswith("battery_pack")]
    assert packs == [
        "battery_pack,device=Grill,pack=1,source=zendure cell_max_v=3.22,cell_min_v=3.2 1",
        "battery_pack,device=Grill,pack=2,source=zendure cell_min_v=3.3 1",
    ]
    assert not any("PACK-" in line for line in lines)


def test_influxdb_and_the_dashboard_judge_a_reading_by_one_rule():
    import inspect
    from types import SimpleNamespace

    from dashboard.telemetry import device_telemetry_is_measured
    from ems.controller import EMSController

    def broken(name):
        raise ValueError("bad telemetry_max_age_seconds")

    assert "device_telemetry_is_measured(self, name)" in inspect.getsource(EMSController.publish_to_influx)
    assert device_telemetry_is_measured(SimpleNamespace(telemetry_fresh=broken), "Grill") is True
    assert device_telemetry_is_measured(SimpleNamespace(telemetry_fresh=lambda name: False), "Grill") is False


def test_the_developer_cell_tasks_are_what_the_schema_generates():
    from pathlib import Path

    for window in ("1m", "15m"):
        path = Path(f"develop/influxdb/tasks/downsample_{window}_cells.flux")
        written = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("//"))
        generated = schema.build_cell_downsample_flux(
            {"bucket_prefix": "zendure", "org": "zendure-dev"}, {"source": "raw", "target": window, "window": window}
        )
        assert written.strip() == generated.strip()


def test_a_device_held_past_its_last_read_records_no_pack_point():
    state = parse_device(_report(_pack("PACK-A", 320, 322)))

    held = build_telemetry_lines([_device("Grill")], [state], {"Grill": True}, 0, timestamp_ns=1, measured=lambda name: False)
    fresh = build_telemetry_lines([_device("Grill")], [state], {"Grill": True}, 0, timestamp_ns=1, measured=lambda name: True)

    assert not any(line.startswith("battery_pack") for line in held)
    assert any(line.startswith("zendure_device") for line in held)
    assert any(line.startswith("battery_pack") for line in fresh)


def test_an_offline_device_records_no_pack_point():
    state = parse_device(_report(_pack("PACK-A", 320, 322)))

    lines = build_telemetry_lines([_device("Grill")], [state], {"Grill": False}, 0, timestamp_ns=1)

    assert not any(line.startswith("battery_pack") for line in lines)


_STEP = {"source": "raw", "target": "1m", "window": "1m"}
_INFLUX = {"bucket_prefix": "ems", "org": "ems", "downsampling": [_STEP]}


def test_each_step_carries_every_pack_field_in_a_task_of_its_own_keeping_its_extreme():
    flux = schema.build_cell_downsample_flux(_INFLUX, _STEP)

    assert sorted(PACK_CELL_REDUCERS) == sorted(_PACK_CELL_FIELDS)
    assert 'option task = {name: "ems-downsample-1m:cells", every: 1m}' in flux
    for field_name, reducer in PACK_CELL_REDUCERS.items():
        pipeline = flux.split(f'r._field == "{field_name}")')[1].split("\n")
        assert 'r._measurement == "battery_pack"' in flux
        assert pipeline[1].strip() == f"|> aggregateWindow(every: 1m, fn: {reducer}, createEmpty: false)"
        assert pipeline[2].strip() == '|> to(bucket: "ems_1m", org: "ems")'
    main = schema.build_downsample_flux(_INFLUX, _STEP)
    assert "battery_pack" not in main and "cell_" not in main


def test_the_safe_sync_adds_the_cell_tasks_beside_main_tasks_it_keeps():
    from tests.test_history_schema import FakeInfluxClient

    client = FakeInfluxClient()
    client.buckets.update({"ems_raw": 0, "ems_1m": 0})
    client.create_task(schema.build_downsample_flux(_INFLUX, _STEP))

    report = schema.sync(client, _INFLUX, safe=True)

    assert schema.planned_task_names(_INFLUX) == ["ems-downsample-1m", "ems-downsample-1m:cells"]
    assert {task["name"]: task["action"] for task in report["tasks"]} == {
        "ems-downsample-1m": "unchanged",
        "ems-downsample-1m:cells": "created",
    }


def test_every_pack_series_reads_a_field_the_writer_records_in_both_histories():
    assert sorted(PACK_CELL_SERIES.values()) == sorted(_PACK_CELL_FIELDS) == sorted(PACK_CELL_INFLUX_SERIES)
    for field_name, spec in PACK_CELL_INFLUX_SERIES.items():
        assert spec["measurement"] == schema.CELL_MEASUREMENT == "battery_pack"
        assert spec["field"] == field_name
        assert spec["window"] == spec["collapse"] == PACK_CELL_REDUCERS[field_name]


@pytest.mark.parametrize("series_id", ["cell_min:1", "cell_max:1", f"cell_max:{MAX_PACK_NUMBER}"])
def test_a_pack_series_is_a_valid_series(series_id):
    assert normalize_series([series_id]) == [series_id]


@pytest.mark.parametrize(
    "series_id",
    ["cell_min:0", f"cell_min:{MAX_PACK_NUMBER + 1}", "cell_min:01", "cell_min:", "cell_min", "cell_min:x",
     "cell_min:²", "cell_min:-1", "cell_avg:1", "pack:1", None],
)
def test_anything_else_is_no_pack_series(series_id):
    assert pack_cells_of_series(series_id) is None


def _sample(timestamp, devices):
    return {
        "timestamp": timestamp,
        "devices": {
            name: {"soc": 40, "battery_packs": packs} for name, packs in devices.items()
        },
        "grid_power_w": 0,
        "home_load_w": 0,
        "pv_total_w": 0,
        "inverter_output_w": 0,
        "inverter_charge_w": 0,
        "battery_power_w": 0,
        "average_soc": 40,
        "controller": {"commanded_total_w": 0},
        "rules": {},
        "control_explain": None,
    }


def _store_with_packs(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path)
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)
    readings = (
        {"Grill": [{"pack": 1, "cell_min_v": 3.17, "cell_max_v": 3.18}],
         "Terasse": [{"pack": 1, "cell_min_v": 3.28, "cell_max_v": 3.29}]},
        {"Grill": [{"pack": 1, "cell_min_v": 3.21, "cell_max_v": None}],
         "Terasse": [{"pack": 1, "cell_min_v": 3.3, "cell_max_v": 3.31}]},
        {"Grill": [], "Terasse": []},
    )
    for minute, devices in enumerate(readings):
        store.record(_sample((start + timedelta(minutes=minute)).isoformat(), devices))
    return SqliteHistoryProvider(str(path)), start


def test_the_local_history_reads_one_device_pack_by_pack(tmp_path):
    provider, start = _store_with_packs(tmp_path)

    result = provider.query(
        start, start + timedelta(minutes=5), devices=["Grill"], series=["cell_min:1", "cell_max:1", "cell_min:2"]
    )

    assert result.series["cell_min:1"] == [3.17, 3.21, None]
    assert result.series["cell_max:1"] == [3.18, None, None]
    assert result.series["cell_min:2"] == [None, None, None]


def _snapshot_of(states, online, fresh=lambda name: True):
    from types import SimpleNamespace

    from dashboard.telemetry import build_dashboard_snapshot

    names = list(states)
    controller = SimpleNamespace(
        devices=[SimpleNamespace(name=name) for name in names],
        runtime_state=None,
        device_online=online,
        telemetry_fresh=fresh,
        _dashboard_capabilities=[],
        commanded_total_w=0,
        filtered_load_w=0,
        last_control_explanation=None,
    )
    zeros = [0] * len(names)
    return build_dashboard_snapshot(
        controller, 0, list(states.values()), zeros, zeros, 0, 0,
        enabled=True, max_total_power=1600, min_output_limit=35,
    )


def test_a_device_offline_or_out_of_date_publishes_no_cells():
    state = parse_device(_report(_pack("PACK-A", 317, 318)))
    states = {"Live": state, "Offline": state, "Stale": state}

    snapshot = _snapshot_of(states, {"Live": True, "Offline": False, "Stale": True}, fresh=lambda name: name != "Stale")

    assert snapshot["devices"]["Live"]["battery_packs"] == [
        {"pack": 1, "cell_min_v": 3.17, "cell_max_v": 3.18, "power_w": None, "state": None, "temp_c": None}
    ]
    assert snapshot["devices"]["Offline"]["battery_packs"] == []
    assert snapshot["devices"]["Stale"]["battery_packs"] == []


def test_thinning_a_long_answer_keeps_each_stretch_s_extreme_of_a_cell_series():
    from datetime import datetime as dt

    from ems.history.provider import HistoryResult, decimate_history_result

    lows = [3.2] * 10
    lows[4] = 2.95
    result = HistoryResult(
        source="influxdb", start=dt(2026, 11, 15), end=dt(2026, 11, 16), window="1m",
        time=list(range(10)),
        series={"cell_min:1": lows, "cell_max:1": [3.3, 3.31, 3.45, 3.3, 3.3, 3.3, 3.3, 3.3, 3.3, 3.3], "pv": list(range(10))},
    )

    decimate_history_result(result, max_points=4)

    assert result.time == [0, 3, 6, 9]
    assert result.series["cell_min:1"] == [3.2, 3.2, 2.95, 3.2]
    gappy = HistoryResult(
        source="influxdb", start=dt(2026, 11, 15), end=dt(2026, 11, 16), window="1m",
        time=list(range(10)), series={"cell_min:1": [3.2, 2.9, None, None, None, None, None, None, None, 3.2]},
    )
    decimate_history_result(gappy, max_points=4)
    assert gappy.series["cell_min:1"] == [3.2, 2.9, None, 3.2]
    assert result.series["cell_max:1"] == [3.3, 3.45, 3.3, 3.3]
    assert result.series["pv"] == [0, 3, 6, 9]


def test_a_pack_series_beside_a_system_series_reads_both(tmp_path):
    provider, start = _store_with_packs(tmp_path)

    result = provider.query(start, start + timedelta(minutes=5), series=["cell_min:1", "target", "pv"])

    assert result.series["cell_min:1"] == [3.17, 3.21, None]
    assert result.series["target"] == [0, 0, 0]
    assert result.series["pv"] == [0, 0, 0]


def test_without_a_choice_a_device_the_ems_only_reads_counts_no_cells(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    store = DashboardStore(path)
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)
    sample = _sample(start.isoformat(), {
        "Grill": [{"pack": 1, "cell_min_v": 3.17, "cell_max_v": 3.18}],
        "Shed": [{"pack": 1, "cell_min_v": 3.01, "cell_max_v": 3.4}],
    })
    sample["devices"]["Shed"]["read_only"] = True
    store.record(sample)
    provider = SqliteHistoryProvider(str(path))

    everyone = provider.query(start, start + timedelta(minutes=5), series=["cell_min:1"])
    chosen = provider.query(start, start + timedelta(minutes=5), devices=["Shed"], series=["cell_min:1"])

    assert everyone.series["cell_min:1"] == [3.17]
    assert chosen.series["cell_min:1"] == [3.01]


def test_across_devices_the_lowest_and_the_highest_cell_count(tmp_path):
    provider, start = _store_with_packs(tmp_path)

    result = provider.query(start, start + timedelta(minutes=5), series=["cell_min:1", "cell_max:1"])

    assert result.series["cell_min:1"] == [3.17, 3.21, None]
    assert result.series["cell_max:1"] == [3.29, 3.31, None]


class _Client:
    def __init__(self):
        self.queries = []

    def query_raw(self, flux):
        self.queries.append(flux)
        return ""


def test_influxdb_reads_one_pack_by_its_tag_and_keeps_the_extreme_across_devices():
    client = _Client()
    provider = InfluxHistoryProvider({"enabled": True, "bucket_prefix": "ems", "query_profiles": []}, client=client)
    start = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)

    provider.query(start, start + timedelta(hours=1), devices=["Grill"], series=["cell_min:2", "cell_max:2"])

    low, high = client.queries
    for flux in (low, high):
        assert 'r._measurement == "battery_pack"' in flux
        assert 'r.device == "Grill"' in flux
        assert 'r.pack == "2"' in flux
    assert 'r._field == "cell_min_v"' in low and "fn: min, createEmpty" in low and "|> min()" in low
    assert 'r._field == "cell_max_v"' in high and "fn: max, createEmpty" in high and "|> max()" in high
