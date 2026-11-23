# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shipped load filter passes every reading unchanged; it smooths only when set to.

Measured on a SolarFlow 800 Pro 2 on 2026-11-23 (issue #341): a write shows at
the grid after about two seconds, and battery output reaches the target before
the next five-second cycle. The median/EMA stage then counted a load step's
correction twice: a median over two readings is their mean, and the older one is
the error the last write already corrected.

The filter ships on, with a window of 1 and an EMA of 1.0, rather than off: a
release before main switched it off reads a filter switched off as no surplus
charge, so a rollback after the refresh keeps charging. A window of 2 and an EMA
of 0.85 are the light filter for a meter whose readings jump.
"""

from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.config import OUTPUT_CONTROL_DEFAULTS
from ems.controller import EMSController
from tests.test_write_dead_time import STEP_AT_S, Loop, step
from tests.test_write_gates import RuntimeStateStub, ShellyStub, device

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
    pytest.mark.simulation,
]

MEASURED_DEAD_TIME_S = 2.0
INTERVAL_S = 5.0
FIRST_CYCLE_AFTER_STEP = int(STEP_AT_S / INTERVAL_S)
UNBOUNDED_RAMPS = {
    "ramp_up_w_per_cycle": 5000,
    "ramp_down_w_per_cycle": 5000,
    "device_ramp_up_w_per_cycle": 5000,
    "device_ramp_down_w_per_cycle": 5000,
}
LIGHT_FILTER = {"filter_enabled": True, "median_window": 2, "ema_alpha": 0.85}


def run_house(house, cycles=24, **output_control):
    loop = Loop(
        house,
        interval=INTERVAL_S,
        write_delay=0.3,
        lag=MEASURED_DEAD_TIME_S,
        max_power=1600,
        output_control=output_control,
    )
    return loop.run(cycles)


def run_step(before, after, **output_control):
    return run_house(step(before, after, STEP_AT_S), **{**UNBOUNDED_RAMPS, **output_control})


def filtered(readings, **output_control):
    controller = EMSController(
        devices=[device("WR1")],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
    )
    config = {**OUTPUT_CONTROL_DEFAULTS, **output_control}
    with patch.object(cfg, "OUTPUT_CONTROL_CONFIG", config):
        return [round(controller.filter_output_control_load(value), 1) for value in readings]


def spike(moment):
    return 2300 if STEP_AT_S <= moment < STEP_AT_S + 1.0 else 300


def swinging(moment):
    return 600 + (137 if int(moment) % 3 == 0 else -91 if int(moment) % 3 == 1 else 0)


def test_the_shipped_filter_is_on_and_passes_every_reading_unchanged():
    readings = [0.0, 900.0, -300.0, 600.0, -49.0, 51.0, 1200.0, -1200.0]

    assert OUTPUT_CONTROL_DEFAULTS["filter_enabled"] is True
    assert OUTPUT_CONTROL_DEFAULTS["filter_method"] == "median_ema"
    assert OUTPUT_CONTROL_DEFAULTS["median_window"] == 1
    assert OUTPUT_CONTROL_DEFAULTS["ema_alpha"] == 1.0
    assert filtered(readings) == readings
    assert cfg.load_filter_smooths(OUTPUT_CONTROL_DEFAULTS) is False


@pytest.mark.parametrize(
    "house, ramps",
    [
        (step(200, 1200, STEP_AT_S), UNBOUNDED_RAMPS),
        (step(1200, 200, STEP_AT_S), UNBOUNDED_RAMPS),
        (step(200, 1200, STEP_AT_S), {}),
        (step(1200, 200, STEP_AT_S), {}),
        (spike, {}),
        (swinging, {}),
    ],
)
def test_the_shipped_filter_regulates_exactly_as_no_filter(house, ramps):
    assert run_house(house, **ramps) == run_house(house, **ramps, filter_enabled=False)


@pytest.mark.parametrize("before, after", [(200, 1200), (1200, 200)])
def test_a_load_step_is_counted_once_by_the_shipped_defaults(before, after):
    outputs = run_step(before, after)

    settled = outputs[FIRST_CYCLE_AFTER_STEP:]
    assert settled == [after] * len(settled)


def test_the_light_filter_overshoots_a_rise_and_swings_as_the_docs_say():
    """docs/technical/configuration.md quotes these figures: about 180 W over, about 40 s to stay within 25 W."""

    after = run_step(200, 1200, **LIGHT_FILTER)[FIRST_CYCLE_AFTER_STEP:]

    assert 170 <= max(after) - 1200 <= 190
    outside = [index for index, output in enumerate(after) if abs(output - 1200) > 25]
    assert 35 <= (outside[-1] + 1) * INTERVAL_S <= 45


@pytest.mark.parametrize("before, after", [(200, 1200), (1200, 200)])
def test_the_light_filter_still_smooths(before, after):
    outputs = run_step(before, after, **LIGHT_FILTER)

    first = outputs[FIRST_CYCLE_AFTER_STEP]
    assert min(before, after) < first < max(before, after)
    assert cfg.load_filter_smooths(LIGHT_FILTER) is True


@pytest.mark.parametrize("stored", ["maybe", None, ""])
def test_an_unreadable_filter_switch_falls_back_to_the_shipped_one(stored):
    readings = [0.0, 900.0, 300.0]
    light = {"median_window": 2, "ema_alpha": 0.85}

    assert filtered(readings, filter_enabled=stored, **light) == filtered(readings, filter_enabled=True, **light)
    assert filtered(readings, filter_enabled=stored, **light) != filtered(readings, filter_enabled=False, **light)


def test_the_filter_s_own_settings_fall_back_to_the_shipped_ones():
    readings = [0.0, 900.0, 300.0, 600.0]

    unreadable = filtered(readings, filter_enabled=True, median_window="x", ema_alpha="y")

    assert unreadable == readings
    assert unreadable != filtered(readings, **LIGHT_FILTER)


@pytest.mark.parametrize(
    "output_control, smooths",
    [
        ({}, False),
        ({"filter_enabled": False, "median_window": 2, "ema_alpha": 0.85}, False),
        ({"filter_method": "none", "median_window": 2}, False),
        ({"median_window": 2}, True),
        ({"ema_alpha": 0.85}, True),
        ({"median_window": "x", "ema_alpha": "y"}, False),
        ({"median_window": 0, "ema_alpha": 3}, False),
    ],
)
def test_the_filter_smooths_only_on_with_a_window_above_one_or_an_ema_below_one(output_control, smooths):
    assert cfg.load_filter_smooths(output_control) is smooths
