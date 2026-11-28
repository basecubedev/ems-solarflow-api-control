# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each battery pack's own state of charge, from the report to the history.

A device with several packs reports one charge level (``electricLevel``) for
all of them; each pack names its own in ``packData[].socLevel``. A device at
50 % may hold one pack at 30 % and one at 70 %. The EMS reads the pack's value,
shows it with the pack, records it in InfluxDB and serves it as
``pack_soc:<n>``.
"""

import math
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard.telemetry import _state_telemetry_fields
from ems.clients import parse_device, zendure_pack_soc
from ems.config import normalize_influxdb_config
from ems.history import schema
from ems.history.influx_provider import BATTERY_INFLUX_SERIES, PACK_INFLUX_SERIES, InfluxHistoryProvider
from ems.history.influx_writer import build_telemetry_lines
from ems.history.provider import (
    PACK_CELL_REDUCERS,
    PACK_RAW_FIELDS,
    PACK_SERIES,
    PACK_SERIES_REDUCERS,
    SERIES_CATALOG,
    _pack_series_value,
    normalize_series,
    pack_cells_of_series,
)
from ems.zendure_mqtt.snapshot import ZendureMqttAggregator, observable_metrics, pack_witness

pytestmark = [pytest.mark.contract]

TWO_PACKS = {
    "properties": {"electricLevel": 50, "packNum": 2},
    "packData": [
        {"sn": "PACK-B", "socLevel": 70, "minVol": 331, "maxVol": 333},
        {"sn": "PACK-A", "socLevel": 30, "minVol": 320, "maxVol": 322},
    ],
}


@pytest.mark.parametrize(
    "raw,expected",
    [(0, 0.0), (41, 41.0), (100, 100.0), (101, None), (-1, None), (None, None), ("41", 41.0), ("n/a", None), (True, None),
     ("-0", 0.0), (-0.4, 0.0)],
)
def test_a_pack_s_charge_is_read_as_whole_percent_and_an_impossible_one_as_none(raw, expected):
    read = zendure_pack_soc(raw)

    assert read == expected
    assert read is None or math.copysign(1.0, read) == 1.0, "a pack at zero is 0 %, never -0 %"


def test_each_pack_keeps_its_own_charge_in_serial_order():
    state = parse_device(TWO_PACKS)

    assert [(pack.pack, pack.soc) for pack in state.battery_packs] == [(1, 30.0), (2, 70.0)]
    assert state.soc == 50


def test_the_live_data_carries_each_pack_s_charge():
    packs = _state_telemetry_fields(parse_device(TWO_PACKS))["battery_packs"]

    assert [(pack["pack"], pack["soc"]) for pack in packs] == [(1, 30.0), (2, 70.0)]


def test_an_mqtt_report_reaches_the_same_pack_charge():
    aggregator = ZendureMqttAggregator()
    topic = "iot/pk/dev1/properties/report"
    aggregator.observe(topic, '{"properties": {"electricLevel": 50, "packNum": 2}, "packData": '
                              '[{"sn": "PACK-B", "socLevel": 70}, {"sn": "PACK-A", "socLevel": 30}]}')
    aggregator.observe(topic, '{"packData": [{"sn": "PACK-A", "socLevel": 31}]}')

    snapshot = aggregator.snapshots()[0]
    state = parse_device({"properties": observable_metrics(snapshot.metrics), "packData": pack_witness(snapshot)})

    assert [(pack.pack, pack.soc) for pack in state.battery_packs] == [(1, 31.0), (2, 70.0)]


def test_influxdb_records_each_pack_s_charge_and_downsamples_it_with_its_mean():
    lines = build_telemetry_lines([SimpleNamespace(name="Shed")], [parse_device(TWO_PACKS)], {"Shed": True}, 0, timestamp_ns=1)

    assert [line for line in lines if line.startswith("battery_pack")] == [
        "battery_pack,device=Shed,pack=1,source=zendure cell_max_v=3.22,cell_min_v=3.2,cell_spread_v=0.02,soc=30.0 1",
        "battery_pack,device=Shed,pack=2,source=zendure cell_max_v=3.33,cell_min_v=3.31,cell_spread_v=0.02,soc=70.0 1",
    ]
    assert ("battery_pack", "soc", "mean") in schema.BATTERY_DOWNSAMPLED


@pytest.mark.parametrize(
    "series,parsed",
    [("pack_soc:2", ("soc", 2)), ("pack_soc:0", None), ("pack_soc:17", None), ("pack_soc:02", None), ("pack_soc", None)],
)
def test_a_pack_s_charge_is_a_series_of_its_own(series, parsed):
    assert pack_cells_of_series(series) == parsed
    assert normalize_series([series]) == ([series] if parsed else ["pv", "output", "battery"])


class _Client:
    def __init__(self):
        self.queries = []

    def query_raw(self, flux, accept="application/csv"):
        self.queries.append(flux)
        return ",result,table,_time,_value\r\n,_result,0,2026-11-27T06:00:00Z,64,\r\n"


def test_influxdb_answers_a_pack_s_charge_from_its_pack_and_device():
    client = _Client()
    provider = InfluxHistoryProvider(normalize_influxdb_config({"enabled": True, "bucket_prefix": "ems"}), client=client)

    result = provider.query(
        datetime(2026, 11, 27, 5, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 27, 7, 0, tzinfo=timezone.utc),
        series=["pack_soc:2"],
        devices=["WR1"],
    )

    assert result.series["pack_soc:2"] == [64.0]
    query = client.queries[0]
    assert 'r._measurement == "battery_pack"' in query and 'r._field == "soc"' in query
    assert 'r.pack == "2"' in query and 'r.device == "WR1"' in query


def test_sqlite_answers_a_pack_s_charge_as_the_mean_across_devices():
    payload = {
        "devices": {
            "WR1": {"battery_packs": [{"pack": 1, "soc": 30.0}]},
            "WR2": {"battery_packs": [{"pack": 1, "soc": 50.0}]},
            "Meter": {"read_only": True, "battery_packs": [{"pack": 1, "soc": 99.0}]},
        }
    }

    assert _pack_series_value(payload, "soc", 1, None) == 40.0
    assert _pack_series_value(payload, "soc", 1, ["WR2"]) == 50.0


ROOT = Path(__file__).resolve().parents[1]


def test_every_pack_series_is_written_read_kept_and_drawn_by_the_same_name():
    """The pack series live in five lists that must agree: a field written but
    not downsampled empties a long range, one served but not queried answers
    nothing, and a measure the dashboard draws or reads for a card, and the
    device's own line beside its packs, must name a series the history
    serves."""

    downsampled = {field: reducer for measurement, field, reducer in schema.BATTERY_DOWNSAMPLED if measurement == "battery_pack"}
    for kind, field in PACK_SERIES.items():
        reducer = PACK_SERIES_REDUCERS[field]
        assert field in PACK_INFLUX_SERIES, kind
        if field in PACK_CELL_REDUCERS:
            assert PACK_CELL_REDUCERS[field] == reducer, kind
        else:
            assert field in PACK_RAW_FIELDS and downsampled.get(field) == reducer, kind

    battery_js = (ROOT / "frontend" / "dashboard-modern" / "src" / "lib" / "battery.js").read_text(encoding="utf-8")
    drawn = set(re.findall(r'(?:perPack|series): "(\w+)"', battery_js))
    assert {"pack_temp", "cell_spread"} <= drawn <= set(PACK_SERIES)
    beside = set(re.findall(r'device: "(\w+)"', battery_js))
    assert beside and beside <= set(BATTERY_INFLUX_SERIES) & set(SERIES_CATALOG)

    preview = (ROOT / "scripts" / "dashboard_preview_data.py").read_text(encoding="utf-8")
    listed = re.search(r"_PREVIEW_PACK_SERIES = \(([^)]*)\)", preview).group(1)
    assert set(re.findall(r'"(\w+)"', listed)) == set(PACK_SERIES)
