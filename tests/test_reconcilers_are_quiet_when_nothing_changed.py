# SPDX-License-Identifier: AGPL-3.0-or-later
"""A reconciler that writes unconditionally is invisible until it wears something out.

Every state reconciler is supposed to write only when telemetry disagrees with
the intended state. Nothing enforced that, and the cost of getting it wrong is
not an error but a device commanded the same value once per loop -- which for
`acMode` means a relay. That exact fault was live: while the regulator charged a
device, the per-cycle `ac_output` claim made the acMode reconciler write
`acMode: 2` every cycle against the power command's `acMode: 1`.

So this pins both halves: silence when the device already matches, and a write
of *only* the field that drifted when one does.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems import config as cfg
from ems.config import AC_CHARGE_CONTROL_DEFAULTS
from ems.controller import EMSController
from tests.test_write_gates import ShellyStub, device, state

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def settled_device():
    dev = device("WR1")
    dev.hardware_profile = "solarflow_800_pro_2"
    dev.observed_product = None
    dev.ac_charge_enabled = True
    dev.max_charge_power_w = 0
    dev.min_soc = 15
    dev.max_soc = 100
    dev.smart_mode = 1
    dev.grid_off_mode = None
    return dev


def settled_state():
    """Telemetry that already matches every value the EMS would reconcile."""

    item = state(soc=50, solar=0, output=0, soc_limit=0, pack_num=2)
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0
    item.smart_mode = 1
    item.min_soc = 15
    item.max_soc = 100
    return item


def run_cycles(item, *, cycles=1, load=0):
    """Run the real loop and return (property writes, outputLimit writes)."""

    controller = EMSController(
        devices=[settled_device()],
        shelly=ShellyStub(load),
        sleep_enabled=False,
        runtime_state=None,
    )
    controller.device_state_writes_allowed = lambda dev: True
    properties = []
    outputs = []
    controller.set_output_limit = lambda dev, value: outputs.append(int(value))

    with patch("ems.controller.fetch_all_devices", return_value=[item]), patch(
        "ems.controller.write_device_properties",
        side_effect=lambda dev, props, **kw: properties.append(props) or True,
    ), patch(
        "ems.controller.cfg.ARGS", SimpleNamespace(replay=None, simulate=False)
    ), patch("ems.controller.cfg.SYSTEM_ENABLED", True), patch(
        "ems.controller.cfg.MAX_TOTAL_POWER", 800
    ), patch("ems.controller.cfg.MAX_DEVICE_POWER", 800), patch(
        "ems.controller.cfg.MIN_OUTPUT_LIMIT", 0
    ), patch("ems.controller.cfg.DEADBAND", 2), patch(
        "ems.controller.cfg.RECONCILE_SMART_MODE", True
    ), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 1), patch.object(
        cfg, "AC_CHARGE_CONTROL_CONFIG", {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": True}
    ):
        for _ in range(cycles):
            controller.run_once()

    return sorted({key for props in properties for key in props}), outputs


def test_a_settled_device_is_written_to_at_all():
    """Thirty loops against a device that already agrees: not one write."""

    written, outputs = run_cycles(settled_state(), cycles=30)

    assert written == []
    assert outputs == []


@pytest.mark.parametrize(
    "field,value,expected",
    [
        # Each reconciler speaks only for its own field.
        ("ac_mode", 1, ["acMode"]),
        ("smart_mode", 0, ["smartMode"]),
        # The SoC window is one setting with two bounds, written as one property
        # set -- still behind a single "unchanged" check, as the first test shows.
        ("min_soc", 5, ["minSoc", "socSet"]),
        ("max_soc", 80, ["minSoc", "socSet"]),
    ],
)
def test_only_the_field_that_drifted_is_written(field, value, expected):
    item = settled_state()
    setattr(item, field, value)

    written, outputs = run_cycles(item)

    assert written == expected
    assert outputs == []


def test_a_load_moves_the_output_limit_and_nothing_else():
    written, outputs = run_cycles(settled_state(), load=400)

    assert written == []
    assert outputs == [400]
