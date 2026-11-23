# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The load filter is off unless an installation switches it on.

Measured on a SolarFlow 800 Pro 2 on 2026-11-23 (issue #341): a write shows at
the grid after about two seconds, and battery output reaches the target before
the next five-second cycle. The median/EMA stage then counted a load step's
correction twice: a median over two readings is their mean, and the older one is
the error the last write already corrected. The filter stays, with its light
settings, for a meter whose readings jump; whoever switches it on gets them.
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


def run_step(before, after, **output_control):
    loop = Loop(
        step(before, after, STEP_AT_S),
        interval=INTERVAL_S,
        write_delay=0.3,
        lag=MEASURED_DEAD_TIME_S,
        max_power=1600,
        output_control={**UNBOUNDED_RAMPS, **output_control},
    )
    return loop.run(24)


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


def test_the_shipped_defaults_leave_the_filter_off_with_its_light_settings_ready():
    assert OUTPUT_CONTROL_DEFAULTS["filter_enabled"] is False
    assert OUTPUT_CONTROL_DEFAULTS["filter_method"] == "median_ema"
    assert OUTPUT_CONTROL_DEFAULTS["median_window"] == 2
    assert OUTPUT_CONTROL_DEFAULTS["ema_alpha"] == 0.85


@pytest.mark.parametrize("before, after", [(200, 1200), (1200, 200)])
def test_a_load_step_is_counted_once_by_the_shipped_defaults(before, after):
    outputs = run_step(before, after)

    settled = outputs[FIRST_CYCLE_AFTER_STEP:]
    assert settled == [after] * len(settled)


def test_the_filter_switched_on_overshoots_a_rise_and_swings_as_the_docs_say():
    """docs/technical/configuration.md quotes these figures: about 180 W over, about 40 s to stay within 25 W."""

    after = run_step(200, 1200, filter_enabled=True)[FIRST_CYCLE_AFTER_STEP:]

    assert 170 <= max(after) - 1200 <= 190
    outside = [index for index, output in enumerate(after) if abs(output - 1200) > 25]
    assert 35 <= (outside[-1] + 1) * INTERVAL_S <= 45


@pytest.mark.parametrize("before, after", [(200, 1200), (1200, 200)])
def test_the_filter_switched_on_still_smooths_with_its_light_settings(before, after):
    outputs = run_step(before, after, filter_enabled=True)

    first = outputs[FIRST_CYCLE_AFTER_STEP]
    assert min(before, after) < first < max(before, after)


@pytest.mark.parametrize("stored", ["maybe", None, ""])
def test_an_unreadable_filter_switch_counts_as_off(stored):
    assert filtered([0.0, 900.0], filter_enabled=stored) == [0.0, 900.0]


def test_the_filter_s_own_settings_fall_back_to_the_shipped_ones():
    readings = [0.0, 900.0, 300.0, 600.0]

    unreadable = filtered(readings, filter_enabled=True, median_window="x", ema_alpha="y")

    assert unreadable == filtered(readings, filter_enabled=True)
    assert unreadable != filtered(readings, filter_enabled=True, median_window=3, ema_alpha=0.65)
