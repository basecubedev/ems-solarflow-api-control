# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Zendure device temperature is reported in tenths of a kelvin.

``hyperTmp`` 2861 is 13.0 °C, the value Zendure's own Home Assistant
integration shows (``(value - 2731) / 10``) and the one the pack beside it
reported (``packData[].maxTemp`` 2861) on a SolarFlow 800 Pro 2 at 22:25 on a
late-October evening outdoors. The EMS divided by 100 and published 28.6 °C,
to the dashboard's live data and to InfluxDB; at 0 °C it would have said
27.3 °C. A device that reports no temperature must say so, not 0 °C, and
certainly not -273 °C.
"""

import math

import pytest

from dashboard.telemetry import _state_telemetry_fields
from ems.clients import parse_device, zero_device_state
from ems.e3dc_runtime import e3dc_device_state
from ems.history.influx_writer import _device_field_values

pytestmark = [
    pytest.mark.unit,
]


def _celsius(value):
    from ems.clients import zendure_temperature_c

    return zendure_temperature_c(value)


@pytest.mark.parametrize(
    "reported, celsius",
    [
        (2861, 13.0),
        (2981, 25.0),
        (2731, 0.0),
        (2631, -10.0),
        (3111, 38.0),
        ("2861", 13.0),
        (2866, 13.5),
        (2131, -60.0),
        (3931, 120.0),
    ],
)
def test_tenths_of_a_kelvin_become_degrees_celsius(reported, celsius):
    assert _celsius(reported) == celsius


@pytest.mark.parametrize(
    "reported",
    [None, 0, -5, 1, 2130, 3932, 2**53 - 1, 10**400, "", "abc", True, math.inf, math.nan, "9" * 400],
)
def test_no_reading_or_an_impossible_one_is_no_temperature(reported):
    assert _celsius(reported) is None


def test_the_report_gives_the_device_temperature_in_celsius():
    assert parse_device({"properties": {"hyperTmp": 2861}}).temp == 13.0
    assert parse_device({"properties": {}}).temp is None


def test_a_device_without_a_reading_has_no_temperature():
    assert zero_device_state().temp is None
    assert e3dc_device_state(None).temp is None


def test_the_dashboard_publishes_celsius_and_a_missing_reading_as_missing():
    assert _state_telemetry_fields(parse_device({"properties": {"hyperTmp": 2861}}))["temperature_c"] == 13.0
    assert _state_telemetry_fields(zero_device_state())["temperature_c"] is None


def test_influxdb_records_celsius_and_skips_a_missing_reading():
    assert _device_field_values(parse_device({"properties": {"hyperTmp": 2861}}))["temp"] == 13.0
    assert "temp" not in _device_field_values(zero_device_state())
    assert "temp" not in _device_field_values(e3dc_device_state(None))
