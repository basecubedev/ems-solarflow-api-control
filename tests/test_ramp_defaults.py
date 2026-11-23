# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The total is ramped; a device is not, unless an installation switches it on.

Measured on a SolarFlow 800 Pro 2 on 2026-11-23 (issue #341): a device lowers
its output about two seconds after a write, in one jump, and raises it within
the next five-second cycle. A per-device ramp below the total's held a single
device back while the total counted it as delivered, and the total wound up:
it overshot a rise and fell through a drop to nothing. The total's ramps stay:
the up-ramp keeps one wrong reading or a load of a few seconds from being
answered with the device's full power, and the down-ramp limits how fast an AC
charge grows, which the device follows at only about 85 W/s.
"""

import pytest

from ems.config import OUTPUT_CONTROL_DEFAULTS
from tests.test_write_dead_time import STEP_AT_S, Loop, step

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
    pytest.mark.simulation,
]

MEASURED_DEAD_TIME_S = 2.0
INTERVAL_S = 5.0
FIRST_CYCLE_AFTER_STEP = int(STEP_AT_S / INTERVAL_S)
BYPASS = OUTPUT_CONTROL_DEFAULTS["bypass_ramp_multiplier"]


def run(house, cycles=12, **output_control):
    loop = Loop(
        house,
        interval=INTERVAL_S,
        write_delay=0.3,
        lag=MEASURED_DEAD_TIME_S,
        max_power=1600,
        output_control=output_control,
    )
    return loop.run(cycles)


def one_reading(base, extra):
    return lambda moment: base + extra if STEP_AT_S <= moment < STEP_AT_S + 1.0 else base


def test_the_shipped_defaults_ramp_the_total_and_no_single_device():
    assert OUTPUT_CONTROL_DEFAULTS["ramp_enabled"] is True
    assert OUTPUT_CONTROL_DEFAULTS["ramp_up_w_per_cycle"] == 500
    assert OUTPUT_CONTROL_DEFAULTS["ramp_down_w_per_cycle"] == 300
    assert OUTPUT_CONTROL_DEFAULTS["device_ramp_enabled"] is False
    assert OUTPUT_CONTROL_DEFAULTS["device_ramp_up_w_per_cycle"] == 400
    assert OUTPUT_CONTROL_DEFAULTS["device_ramp_down_w_per_cycle"] == 200


def test_one_device_follows_a_falling_load_at_the_total_s_pace_without_falling_through():

    outputs = run(step(1200, 200, STEP_AT_S))

    after = outputs[FIRST_CYCLE_AFTER_STEP:]
    assert after[:3] == [1200 - 300 * BYPASS, 1200 - 300 * BYPASS - 300, 200]
    assert after[3:] == [200] * len(after[3:])


def test_one_device_follows_a_rising_load_as_fast_as_the_total():

    outputs = run(step(200, 700, STEP_AT_S))

    settled = outputs[FIRST_CYCLE_AFTER_STEP:]
    assert settled == [700] * len(settled)


def test_with_the_per_device_ramp_on_one_device_winds_the_total_up():
    """The device ramp below the total's held the device back while the total counted it delivered.

    docs/technical/configuration.md quotes these figures.
    """

    fall = run(step(1200, 200, STEP_AT_S), device_ramp_enabled=True)[FIRST_CYCLE_AFTER_STEP:]
    rise = run(step(200, 700, STEP_AT_S), device_ramp_enabled=True)[FIRST_CYCLE_AFTER_STEP:]

    assert fall[:5] == [900, 600, 400, 200, 0]
    assert rise[:3] == [600, 800, 700]


def test_one_high_reading_moves_the_output_no_further_than_the_up_ramp():
    outputs = run(one_reading(300, 2000))

    before = outputs[FIRST_CYCLE_AFTER_STEP - 1]
    assert before == 300
    assert outputs[FIRST_CYCLE_AFTER_STEP] == before + 500 * BYPASS
    after = outputs[FIRST_CYCLE_AFTER_STEP + 1:]
    assert min(after) == 300
    assert after[-1] == 300


@pytest.mark.parametrize(
    "unreadable, house, expected",
    [
        ({"device_ramp_enabled": "maybe"}, step(200, 700, STEP_AT_S), 700),
        ({"ramp_down_w_per_cycle": "x"}, step(1200, 200, STEP_AT_S), 1200 - 300 * BYPASS),
        ({"ramp_up_w_per_cycle": "x"}, step(200, 1500, STEP_AT_S), 200 + 500 * BYPASS),
        ({"bypass_ramp_multiplier": "x"}, step(200, 1500, STEP_AT_S), 200 + 500 * BYPASS),
        ({"large_import_bypass_w": "x"}, step(200, 750, STEP_AT_S), 700),
        ({"large_export_bypass_w": "x"}, step(1000, 450, STEP_AT_S), 700),
        ({"target_deadband_w": "x"}, step(200, 207, STEP_AT_S), 207),
    ],
)
def test_an_unreadable_ramp_or_deadband_setting_falls_back_to_the_shipped_one(unreadable, house, expected):
    outputs = run(house, cycles=FIRST_CYCLE_AFTER_STEP + 1, **unreadable)

    assert outputs[FIRST_CYCLE_AFTER_STEP] == expected


def test_a_device_ramp_switched_on_still_limits_each_device():
    outputs = run(step(200, 1000, STEP_AT_S), device_ramp_enabled=True)

    assert outputs[FIRST_CYCLE_AFTER_STEP] == 200 + 400 * BYPASS
