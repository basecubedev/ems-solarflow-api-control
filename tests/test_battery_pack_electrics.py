# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A battery's voltage, and each pack's voltage, current, power and cell spread, from the report to the history.

The device reports its battery's voltage (``BatVolt``); each pack its own
voltage across its cells (``packData[].totalVol``), its current
(``batcur``) and its power, whose direction its ``state`` names. The EMS
signs a pack's current and power the way the history keeps the battery,
positive while it charges, so a window's mean across both directions still
means something; it keeps the gap between a pack's highest and lowest cell
of each reading, whose widest a window keeps. It records them in InfluxDB
and serves them as ``pack_voltage:<n>``, ``pack_current:<n>``,
``pack_power:<n>``, ``cell_spread:<n>`` and ``device_voltage``.
"""

import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dashboard.telemetry import _state_telemetry_fields
from ems.clients import (
    pack_flow,
    parse_device,
    zendure_battery_voltage_v,
    zendure_pack_current_a,
    zero_device_state,
)
from ems.config import normalize_influxdb_config
from ems.e3dc_runtime import e3dc_device_state
from ems.history import schema
from ems.history.influx_provider import InfluxHistoryProvider
from ems.history.influx_writer import build_telemetry_lines
from ems.history.provider import (
    HistoryResult,
    _pack_series_value,
    decimate_history_result,
    normalize_series,
    pack_cells_of_series,
)
from ems.models import PackCells
from ems.opendtu_http import opendtu_device_state
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator, observable_metrics, pack_witness

pytestmark = [pytest.mark.contract]

TWO_PACKS = {
    "properties": {"electricLevel": 50, "packNum": 2, "BatVolt": 4788},
    "packData": [
        {"sn": "PACK-B", "state": 1, "power": 120, "batcur": 25, "totalVol": 4795},
        {"sn": "PACK-A", "state": 2, "power": 4, "batcur": 65535, "totalVol": 4760},
    ],
}


@pytest.mark.parametrize(
    "raw,expected",
    [(4788, 47.88), ("4760", 47.6), (None, None), (0, None), (20000, None), ("n/a", None), (True, None)],
)
def test_a_battery_voltage_is_read_in_volts_and_a_missing_one_is_none_not_zero(raw, expected):
    assert zendure_battery_voltage_v(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [(65535, 0.1), (25, 2.5), (65511, 2.5), (0, 0.0), (None, None), (6000, None), ("x", None), (True, None)],
)
def test_a_pack_current_is_its_size_in_amps_from_a_signed_16_bit_tenth(raw, expected):
    read = zendure_pack_current_a(raw)

    assert read == expected
    assert read is None or math.copysign(1.0, read) == 1.0


@pytest.mark.parametrize(
    "size,state,expected",
    [(4.0, 1, 4.0), (4.0, 2, -4.0), (-4.0, 1, 4.0), (0.0, 2, 0.0), (0.0, 0, 0.0), (4.0, 0, None), (4.0, None, None), (None, 1, None)],
)
def test_a_pack_reading_takes_the_direction_its_state_names(size, state, expected):
    flow = pack_flow(size, state)

    assert flow == expected
    assert flow is None or flow != 0 or math.copysign(1.0, flow) == 1.0, "no reading is -0"


def test_each_pack_keeps_its_own_voltage_current_and_power_in_serial_order():
    state = parse_device(TWO_PACKS)

    assert state.voltage == 47.88
    assert [(pack.pack, pack.voltage_v, pack.current_a, pack.flow_w) for pack in state.battery_packs] == [
        (1, 47.6, -0.1, -4.0),
        (2, 47.95, 2.5, 120.0),
    ]


@pytest.mark.parametrize(
    "state",
    [parse_device({"properties": {"electricLevel": 50}}), zero_device_state(), e3dc_device_state(None), opendtu_device_state(None)],
)
def test_a_device_without_a_battery_voltage_records_none_rather_than_zero_volts(state):
    lines = build_telemetry_lines([SimpleNamespace(name="Shed")], [state], {"Shed": True}, 0, timestamp_ns=1)

    assert state.voltage is None
    assert _state_telemetry_fields(state)["voltage_v"] is None
    assert "voltage=" not in next(line for line in lines if line.startswith("zendure_device"))


@pytest.mark.parametrize(
    "low,high,spread",
    [(3.17, 3.19, 0.02), (3.2, 3.2, 0.0), (3.2, None, None), (None, 3.2, None), (3.3, 3.2, None)],
)
def test_a_pack_keeps_the_gap_between_its_cells_of_one_reading(low, high, spread):
    assert PackCells(pack=1, cell_min_v=low, cell_max_v=high).cell_spread_v == spread
    assert PackCells(pack=1, cell_min_v=low, cell_max_v=high, cells_paired=False).cell_spread_v is None


def test_an_mqtt_report_of_one_cell_alone_gives_no_spread_until_a_report_gives_both():
    """A change report may carry the highest cell alone: the lowest one is then
    an earlier report's, and their gap no reading's."""

    aggregator = ZendureMqttAggregator()
    topic = "iot/pk/dev1/properties/report"

    def spread():
        snapshot = aggregator.snapshots()[0]
        state = parse_device({"properties": observable_metrics(snapshot.metrics), "packData": pack_witness(snapshot)})
        return [(pack.cell_min_v, pack.cell_max_v, pack.cell_spread_v) for pack in state.battery_packs]

    aggregator.observe(topic, '{"properties": {"packNum": 1}, "packData": [{"sn": "PACK-A", "minVol": 320, "maxVol": 321}]}')
    assert spread() == [(3.2, 3.21, 0.01)]
    aggregator.observe(topic, '{"packData": [{"sn": "PACK-A", "maxVol": 345}]}')
    assert spread() == [(3.2, 3.45, None)]
    aggregator.observe(topic, '{"packData": [{"sn": "PACK-A", "socLevel": 40}]}')
    assert spread() == [(3.2, 3.45, None)]
    aggregator.observe(topic, '{"packData": [{"sn": "PACK-A", "minVol": 343, "maxVol": 345}]}')
    assert spread() == [(3.43, 3.45, 0.02)]


def test_a_thinned_range_keeps_the_widest_spread_of_one_reading_not_the_gap_between_two():
    """A charge lifts every cell 0.1 V within one stretch while they stay 10 to
    30 mV apart: the stretch's lowest and highest cell lie more than 100 mV
    apart, the widest spread of any one reading stays 30 mV."""

    low = [3.20, 3.20, 3.30, 3.30]
    result = HistoryResult(
        source="sqlite",
        start=None,
        end=None,
        window="raw",
        time=[0, 10, 20, 30],
        series={
            "cell_min:1": low,
            "cell_max:1": [value + 0.01 for value in low],
            "cell_spread:1": [0.01, 0.03, 0.02, 0.01],
        },
    )

    thinned = decimate_history_result(result, max_points=2)

    assert max(thinned.series["cell_max:1"]) - min(thinned.series["cell_min:1"]) > 0.1
    assert thinned.series["cell_spread:1"] == [0.01, 0.03]


def test_the_live_data_carries_each_pack_s_voltage_current_and_power():
    packs = _state_telemetry_fields(parse_device(TWO_PACKS))["battery_packs"]

    assert [(pack["voltage_v"], pack["current_a"], pack["flow_w"]) for pack in packs] == [(47.6, -0.1, -4.0), (47.95, 2.5, 120.0)]


def test_an_mqtt_report_reaches_the_same_pack_voltage_current_and_power():
    aggregator = ZendureMqttAggregator()
    topic = "iot/pk/dev1/properties/report"
    aggregator.observe(topic, '{"properties": {"electricLevel": 50, "packNum": 1}, "packData": '
                              '[{"sn": "PACK-A", "state": 2, "power": 4, "batcur": 65535, "totalVol": 4760}]}')
    aggregator.observe(topic, '{"packData": [{"sn": "PACK-A", "state": 1, "power": 300, "batcur": 63}]}')

    snapshot = aggregator.snapshots()[0]
    state = parse_device({"properties": observable_metrics(snapshot.metrics), "packData": pack_witness(snapshot)})

    assert [(pack.voltage_v, pack.current_a, pack.flow_w) for pack in state.battery_packs] == [(47.6, 6.3, 300.0)]


def test_influxdb_records_each_pack_s_voltage_current_and_power_and_downsamples_their_mean():
    lines = build_telemetry_lines([SimpleNamespace(name="Shed")], [parse_device(TWO_PACKS)], {"Shed": True}, 0, timestamp_ns=1)

    assert [line for line in lines if line.startswith("battery_pack")] == [
        "battery_pack,device=Shed,pack=1,source=zendure current_a=-0.1,flow_w=-4.0,power_w=4.0,state=2.0,voltage_v=47.6 1",
        "battery_pack,device=Shed,pack=2,source=zendure current_a=2.5,flow_w=120.0,power_w=120.0,state=1.0,voltage_v=47.95 1",
    ]
    assert "voltage=47.88" in next(line for line in lines if line.startswith("zendure_device"))
    for field_name in ("voltage_v", "current_a", "flow_w"):
        assert ("battery_pack", field_name, "mean") in schema.BATTERY_DOWNSAMPLED
    assert ("battery_pack", "cell_spread_v", "max") in schema.BATTERY_DOWNSAMPLED
    assert "voltage" in schema.NUMERIC_FIELDS


@pytest.mark.parametrize(
    "series,parsed",
    [
        ("pack_voltage:1", ("voltage_v", 1)),
        ("pack_current:2", ("current_a", 2)),
        ("pack_power:16", ("flow_w", 16)),
        ("cell_spread:3", ("cell_spread_v", 3)),
        ("pack_power:0", None),
        ("pack_power:17", None),
        ("pack_current", None),
    ],
)
def test_a_pack_s_voltage_current_and_power_are_series_of_their_own(series, parsed):
    assert pack_cells_of_series(series) == parsed
    assert normalize_series([series]) == ([series] if parsed else ["pv", "output", "battery"])


def test_the_battery_voltage_is_a_series_of_its_own():
    assert normalize_series(["device_voltage"]) == ["device_voltage"]


class _Client:
    def __init__(self):
        self.queries = []

    def query_raw(self, flux, accept="application/csv"):
        self.queries.append(flux)
        return ",result,table,_time,_value\r\n,_result,0,2026-11-28T06:00:00Z,-42.5,\r\n"


@pytest.mark.parametrize(
    "series,measurement,field_name,pack",
    [
        ("pack_power:2", "battery_pack", "flow_w", "2"),
        ("pack_current:1", "battery_pack", "current_a", "1"),
        ("pack_voltage:1", "battery_pack", "voltage_v", "1"),
        ("cell_spread:2", "battery_pack", "cell_spread_v", "2"),
        ("device_voltage", "zendure_device", "voltage", None),
    ],
)
def test_influxdb_answers_each_series_from_its_own_field_and_device(series, measurement, field_name, pack):
    client = _Client()
    provider = InfluxHistoryProvider(normalize_influxdb_config({"enabled": True, "bucket_prefix": "ems"}), client=client)

    result = provider.query(
        datetime(2026, 11, 28, 5, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 28, 7, 0, tzinfo=timezone.utc),
        series=[series],
        devices=["WR1"],
    )

    assert result.series[series] == [-42.5]
    query = client.queries[0]
    assert f'r._measurement == "{measurement}"' in query and f'r._field == "{field_name}"' in query
    assert 'r.device == "WR1"' in query
    assert pack is None or f'r.pack == "{pack}"' in query


def test_sqlite_answers_a_pack_s_power_as_the_mean_across_devices():
    payload = {
        "devices": {
            "WR1": {"battery_packs": [{"pack": 1, "flow_w": -100.0, "voltage_v": 47.5}]},
            "WR2": {"battery_packs": [{"pack": 1, "flow_w": 300.0, "voltage_v": 48.5}]},
        }
    }

    assert _pack_series_value(payload, "flow_w", 1, None) == 100.0
    assert _pack_series_value(payload, "voltage_v", 1, ["WR1"]) == 47.5
