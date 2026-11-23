# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each battery pack's own power and state, recorded beside its cell voltages.

A SolarFlow 800 Pro 2 idle at SoC 41 % with minSoc 40 % reported
``packState`` 2 (discharging) with ``outputPackPower`` and ``packInputPower``
both 0, while its one pack said ``state`` 2 and ``power`` 4 (#385). The
device-level values cannot show such a standby drain; the pack's own can. The
EMS records what the pack reports and interprets nothing for control.
"""

import math
from types import SimpleNamespace

import pytest

from dashboard.telemetry import _state_telemetry_fields
from ems.clients import parse_device, zendure_pack_power_w, zendure_pack_state
from ems.history import schema
from ems.history.influx_writer import _PACK_FIELDS, build_telemetry_lines
from ems.history.provider import PACK_CELL_REDUCERS, PACK_RAW_FIELDS
from ems.models import PackCells
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator, observable_metrics, pack_witness

pytestmark = [
    pytest.mark.contract,
]

IDLE_AT_MIN_SOC = {
    "properties": {
        "electricLevel": 41,
        "packState": 2,
        "outputPackPower": 0,
        "packInputPower": 0,
        "outputHomePower": 0,
        "packNum": 1,
    },
    "packData": [
        {"sn": "PACK-A", "socLevel": 41, "state": 2, "power": 4, "totalVol": 4880, "batcur": 65535, "minVol": 325, "maxVol": 326}
    ],
}


def test_the_pack_reports_the_drain_the_device_level_values_hide():
    state = parse_device(IDLE_AT_MIN_SOC)

    assert (state.pack_in, state.pack_out) == (0, 0)
    assert state.battery_packs == (
        PackCells(pack=1, cell_min_v=3.25, cell_max_v=3.26, power_w=4.0, state=2),
    )


@pytest.mark.parametrize(
    "reported, watts",
    [(4, 4.0), (0, 0.0), ("12", 12.0), (2400, 2400.0), (-4, -4.0), (3.6, 4.0), (65535, -1.0), (65236, -300.0), (32768, None)],
)
def test_a_pack_power_is_whole_watts_and_a_16_bit_negative_reads_negative(reported, watts):
    assert zendure_pack_power_w(reported) == watts


@pytest.mark.parametrize("reported", [None, True, False, "", "abc", math.inf, math.nan, 10001, -10001, 65536, 20000, "9" * 400])
def test_no_power_or_one_no_pack_can_have_is_no_reading(reported):
    assert zendure_pack_power_w(reported) is None


@pytest.mark.parametrize("reported, state", [(0, 0), (1, 1), (2, 2), ("2", 2), (2.0, 2)])
def test_a_pack_state_is_idle_charging_or_discharging(reported, state):
    assert zendure_pack_state(reported) == state


@pytest.mark.parametrize("reported", [None, True, False, "", "x", 3, -1, 1.5, 65535, math.nan])
def test_a_state_of_unknown_meaning_is_no_reading(reported):
    assert zendure_pack_state(reported) is None


def test_a_pack_without_power_or_state_reads_none_for_them():
    report = {"properties": {"packNum": 1}, "packData": [{"sn": "PACK-A", "minVol": 325, "maxVol": 326}]}

    assert parse_device(report).battery_packs == (PackCells(pack=1, cell_min_v=3.25, cell_max_v=3.26),)


def test_the_live_data_carries_each_pack_s_power_and_state():
    fields = _state_telemetry_fields(parse_device(IDLE_AT_MIN_SOC))

    assert fields["battery_packs"] == [
        {"pack": 1, "cell_min_v": 3.25, "cell_max_v": 3.26, "power_w": 4.0, "state": 2, "temp_c": None}
    ]


def _lines(report):
    lines = build_telemetry_lines(
        [SimpleNamespace(name="Grill")], [parse_device(report)], {"Grill": True}, 0, timestamp_ns=1
    )
    return [line for line in lines if line.startswith("battery_pack")]


def test_influxdb_records_the_pack_power_and_state_beside_its_cells():
    assert _lines(IDLE_AT_MIN_SOC) == [
        "battery_pack,device=Grill,pack=1,source=zendure cell_max_v=3.26,cell_min_v=3.25,power_w=4.0,state=2.0 1"
    ]


def test_a_value_the_pack_does_not_report_is_left_out_and_a_pack_with_power_only_still_records():
    report = {
        "properties": {"packNum": 2},
        "packData": [{"sn": "PACK-A", "power": 30, "state": 1}, {"sn": "PACK-B", "minVol": 330}],
    }

    assert _lines(report) == [
        "battery_pack,device=Grill,pack=1,source=zendure power_w=30.0,state=1.0 1",
        "battery_pack,device=Grill,pack=2,source=zendure cell_min_v=3.3 1",
    ]


def test_every_pack_reading_reaches_influxdb():
    """The live snapshot takes every PackCells field through asdict; the
    history must name each too, or a new one is shown and never recorded."""

    from dataclasses import fields

    assert {field.name for field in fields(PackCells)} - {"pack"} == set(_PACK_FIELDS)


def test_the_raw_pack_fields_stay_out_of_the_downsampling_tasks():
    step = {"source": "raw", "target": "1m", "window": "1m"}
    influx = {"bucket_prefix": "ems", "org": "ems", "downsampling": [step]}

    flux = schema.build_cell_downsample_flux(influx, step) + schema.build_downsample_flux(influx, step)

    assert sorted(_PACK_FIELDS) == sorted([*PACK_CELL_REDUCERS, *PACK_RAW_FIELDS])
    assert set(PACK_RAW_FIELDS).isdisjoint(PACK_CELL_REDUCERS)
    for field_name in PACK_RAW_FIELDS:
        assert f'"{field_name}"' not in flux


def test_an_mqtt_report_reaches_the_same_pack_power_and_state():
    aggregator = ZendureMqttAggregator()
    aggregator.observe(
        "iot/pk/dev1/properties/report",
        '{"properties": {"electricLevel": 41, "packNum": 1},'
        ' "packData": [{"sn": "PACK-A", "minVol": 325, "maxVol": 326, "power": 4, "state": 2}]}',
    )
    aggregator.observe("iot/pk/dev1/properties/report", '{"packData": [{"sn": "PACK-A", "power": 6}]}')

    snapshot = aggregator.snapshots()[0]
    state = parse_device({"properties": observable_metrics(snapshot.metrics), "packData": pack_witness(snapshot)})

    assert state.battery_packs == (PackCells(pack=1, cell_min_v=3.25, cell_max_v=3.26, power_w=6.0, state=2),)
