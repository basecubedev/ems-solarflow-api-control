# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each battery pack's temperature, read beside its power (#415).

A pack reports ``packData[].maxTemp`` in tenths of a kelvin, as the inverter
reports ``hyperTmp``: on a SolarFlow 800 Pro 2 outdoors both said 2861, 13.0 °C.
The same reader takes it, so a missing or impossible reading is no reading,
never 0 °C and never -273 °C. It goes to the live data and to InfluxDB's raw
bucket, like the pack's power.
"""

import pytest

from dashboard.telemetry import _state_telemetry_fields
from ems.clients import parse_device
from ems.history import schema
from ems.history.influx_writer import build_telemetry_lines
from ems.history.provider import PACK_RAW_FIELDS
from ems.models import PackCells
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator, observable_metrics, pack_witness

pytestmark = [
    pytest.mark.contract,
]

TWO_PACKS = {
    "properties": {"electricLevel": 60, "packNum": 2, "hyperTmp": 2861},
    "packData": [
        {"sn": "PACK-B", "minVol": 330, "maxVol": 331, "maxTemp": 2981},
        {"sn": "PACK-A", "minVol": 325, "maxVol": 326, "maxTemp": 2861},
    ],
}


def test_each_pack_reads_its_temperature_in_celsius():
    state = parse_device(TWO_PACKS)

    assert state.temp == 13.0
    assert [(pack.pack, pack.temp_c) for pack in state.battery_packs] == [(1, 13.0), (2, 25.0)]


@pytest.mark.parametrize("reported", [None, 0, 1, 2130, 3932, "", "abc", True])
def test_a_missing_or_impossible_pack_temperature_is_no_reading(reported):
    pack = {"sn": "PACK-A", "minVol": 325, "maxVol": 326}
    if reported is not None:
        pack["maxTemp"] = reported

    state = parse_device({"properties": {"packNum": 1}, "packData": [pack]})

    assert state.battery_packs == (PackCells(pack=1, cell_min_v=3.25, cell_max_v=3.26),)


def test_the_live_data_carries_each_pack_s_temperature():
    packs = _state_telemetry_fields(parse_device(TWO_PACKS))["battery_packs"]

    assert [(entry["pack"], entry["temp_c"]) for entry in packs] == [(1, 13.0), (2, 25.0)]


def _pack_lines(report):
    lines = build_telemetry_lines(
        [type("Device", (), {"name": "Shed"})()], [parse_device(report)], {"Shed": True}, 0, timestamp_ns=1
    )
    return [line for line in lines if line.startswith("battery_pack")]


def test_influxdb_records_the_pack_temperature_and_downsamples_it_in_the_battery_task():
    assert _pack_lines(TWO_PACKS) == [
        "battery_pack,device=Shed,pack=1,source=zendure cell_max_v=3.26,cell_min_v=3.25,temp_c=13.0 1",
        "battery_pack,device=Shed,pack=2,source=zendure cell_max_v=3.31,cell_min_v=3.3,temp_c=25.0 1",
    ]
    step = {"source": "raw", "target": "1m", "window": "1m"}
    influx = {"bucket_prefix": "ems", "org": "ems", "downsampling": [step]}
    flux = schema.build_cell_downsample_flux(influx, step) + schema.build_downsample_flux(influx, step)
    assert "temp_c" in PACK_RAW_FIELDS
    assert '"temp_c"' not in flux
    battery = schema.build_battery_downsample_flux(influx, step)
    assert '(r._measurement == "battery_pack" and (r._field == "temp_c"))' in battery.split("fn: mean")[0].rsplit("from(bucket:", 1)[1]


def test_a_pack_that_reports_only_its_temperature_is_still_recorded():
    report = {"properties": {"packNum": 1}, "packData": [{"sn": "PACK-A", "maxTemp": 2861}]}

    assert _pack_lines(report) == ["battery_pack,device=Shed,pack=1,source=zendure temp_c=13.0 1"]


def test_an_mqtt_report_reaches_the_same_pack_temperature():
    aggregator = ZendureMqttAggregator()
    aggregator.observe(
        "iot/pk/dev1/properties/report",
        '{"properties": {"electricLevel": 41, "packNum": 1, "hyperTmp": 2861},'
        ' "packData": [{"sn": "PACK-A", "minVol": 325, "maxVol": 326, "maxTemp": 2861}]}',
    )
    aggregator.observe("iot/pk/dev1/properties/report", '{"packData": [{"sn": "PACK-A", "maxTemp": 2881}]}')

    snapshot = aggregator.snapshots()[0]
    state = parse_device({"properties": observable_metrics(snapshot.metrics), "packData": pack_witness(snapshot)})

    assert state.temp == 13.0
    assert state.battery_packs == (PackCells(pack=1, cell_min_v=3.25, cell_max_v=3.26, temp_c=15.0),)
