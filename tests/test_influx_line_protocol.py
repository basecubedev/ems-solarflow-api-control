# SPDX-License-Identifier: AGPL-3.0-or-later
"""One InfluxDB line-protocol record is always exactly one line."""

import math

import pytest

from scripts.influx_utils import build_line_protocol

pytestmark = [
    pytest.mark.unit,
]


@pytest.mark.parametrize("breaker", ["\n", "\r", "\r\n"])
def test_a_line_break_in_any_value_never_splits_the_record(breaker):
    line = build_line_protocol(
        f"ems{breaker}raw",
        {"device": f"WR1{breaker}evil,tag=x"},
        {"status": f"ok{breaker}injected value=1i", "power_w": 5},
        1,
    )
    assert "\n" not in line
    assert "\r" not in line
    assert line.endswith(" 1")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_non_finite_float_field_is_left_out(value):
    line = build_line_protocol("ems", {}, {"bad": value, "power_w": 5}, 1)
    assert line == "ems power_w=5i 1"


def test_a_record_with_only_non_finite_fields_is_not_written():
    assert build_line_protocol("ems", {}, {"bad": math.nan}, 1) is None
