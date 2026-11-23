# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Without the load filter, the load the loop regulates on is still recorded.

The charge entry and the control snapshot read ``filtered_load_w``, which only
the median/EMA stage set. With ``filter_enabled`` false it stayed empty, the
entry judged a load of zero, and an exported surplus never started a charge.
"""

from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.config import OUTPUT_CONTROL_DEFAULTS
from ems.controller import EMSController
from tests.test_ac_charge_pv_less_device import ADMIN_STANDBY_FLOOR_W, Harness, ac_device, no_pv_state
from tests.test_write_gates import RuntimeStateStub, ShellyStub, device, state

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
    pytest.mark.simulation,
]


@pytest.mark.parametrize("output_control", [{"filter_enabled": False}, {}])
def test_without_the_filter_or_with_the_shipped_one_a_surplus_still_starts_a_charge(output_control):
    """The charge entry read the filter's output, and without the filter it read none."""

    with patch.dict(cfg.OUTPUT_CONTROL_CONFIG, output_control):
        harness = Harness([ac_device()], load=-900, min_output_limit=ADMIN_STANDBY_FLOOR_W)
        harness.run(cycles=15, states=[no_pv_state(soc=50)])

    assert harness.controller.charge_direction.charging is True
    assert harness.targets[-1] < 0, harness.targets


@pytest.mark.parametrize("output_control", [{"filter_enabled": False}, {"filter_enabled": True, "filter_method": "none"}])
def test_the_load_regulated_on_is_recorded_without_the_filter_too(output_control):
    controller = EMSController(
        devices=[device("WR1")],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
    )
    with patch.object(cfg, "OUTPUT_CONTROL_CONFIG", {**OUTPUT_CONTROL_DEFAULTS, **output_control}):
        controller.filter_output_control_load(-640.0)

    assert controller.filtered_load_w == -640.0


def test_the_night_idle_explains_the_reading_it_judged_not_one_from_before():
    """The idle regulates nothing; a value from the last regulated cycle stood there all night."""

    controller = EMSController(
        devices=[device("WR1")],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(),
    )
    controller.filtered_load_w = 640.0
    parked = state(soc=15, min_soc=15, solar=0, output=0, output_limit=30, soc_limit=2, dc_status=0, ac_status=0, pack_state=0)

    explanation = controller.build_night_min_soc_idle_explanation(120.0, [parked], [30], [30], True, 800, 30, [0])

    assert explanation.filtered_load_w == 120.0
