# SPDX-License-Identifier: AGPL-3.0-or-later
"""An EMS switched off, or a device out of its control, is written nothing.

The dashboard promises it ("It stops writing to every inverter"), but the
state reconcilers kept writing acMode, smartMode and the SoC window while
control was off (OFF-1 in docs/developer/review-coverage.md). The owner's
answer of 2026-10-10: off is off. The one exception stays the owner's decision
of 2026-10-04, the single command that ends a charge the EMS itself started.
"""

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems import config as cfg
from ems.ac_charge_control import ChargeDirectionState
from ems.config import AC_CHARGE_CONTROL_DEFAULTS
from ems.controller import EMSController
from tests.test_reconcilers_are_quiet_when_nothing_changed import (
    WinterMorning,
    settled_device,
    settled_state,
)
from tests.test_write_gates import RuntimeStateStub, ShellyStub

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class Runtime(RuntimeStateStub):
    """Operator state with the switches the dashboard sets, and nothing else."""

    def __init__(self, **switches):
        super().__init__(**switches)
        self.data = {}


def run_cycle(item, runtime_state, *, drift):
    """One settled cycle, then ``drift`` and one more; return what was written.
    """

    controller = EMSController(
        devices=[settled_device()],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=runtime_state,
    )
    properties = []
    outputs = []
    controller.set_output_limit = lambda dev, value, **_kwargs: (
        outputs.append(int(value)) or True
    )

    with (
        patch("ems.controller.datetime", WinterMorning),
        patch("ems.controller.fetch_all_devices", return_value=[item]),
        patch(
            "ems.controller.write_device_properties",
            side_effect=lambda dev, props, **kw: (
                properties.append(props) or True
            ),
        ),
        patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True,
        ),
        patch(
            "ems.controller.cfg.ARGS",
            SimpleNamespace(replay=None, simulate=False),
        ),
        patch("ems.controller.cfg.SYSTEM_ENABLED", True),
        patch("ems.controller.cfg.MAX_TOTAL_POWER", 800),
        patch("ems.controller.cfg.MAX_DEVICE_POWER", 800),
        patch("ems.controller.cfg.MIN_OUTPUT_LIMIT", 0),
        patch("ems.controller.cfg.DEADBAND", 2),
        patch("ems.controller.cfg.RECONCILE_SMART_MODE", True),
        patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 1),
        patch.object(
            cfg,
            "AC_CHARGE_CONTROL_CONFIG",
            {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": True},
        ),
    ):
        controller.run_once()
        drift()
        controller.run_once()

    return sorted({key for props in properties for key in props}), outputs


DRIFTS = [
    ("ac_mode", 1, ["acMode"]),
    ("smart_mode", 0, ["smartMode"]),
    ("min_soc", 5, ["minSoc", "socSet"]),
]

OFF = {
    "EMS switched off": Runtime(system={"enabled": False}),
    "device taken out of control": Runtime(devices={"WR1": {"enabled": False}}),
}


@pytest.mark.parametrize("field,value,expected", DRIFTS)
def test_a_drifted_field_is_written_while_the_ems_is_on(field, value, expected):
    item = settled_state()

    written, _ = run_cycle(
        item, Runtime(), drift=lambda: setattr(item, field, value)
    )

    assert written == expected


@pytest.mark.parametrize("off", OFF.values(), ids=OFF.keys())
@pytest.mark.parametrize("field,value,expected", DRIFTS)
def test_nothing_is_written_while_it_is_off(off, field, value, expected):
    item = settled_state()

    written, outputs = run_cycle(
        item, off, drift=lambda: setattr(item, field, value)
    )

    assert written == []
    assert outputs == []


@pytest.mark.parametrize(
    "off,reason",
    [
        (OFF["EMS switched off"], "control_disabled"),
        (OFF["device taken out of control"], "device_disabled"),
    ],
    ids=OFF.keys(),
)
def test_the_withheld_write_names_why(off, reason, caplog):
    item = settled_state()

    with caplog.at_level(logging.INFO):
        run_cycle(item, off, drift=lambda: setattr(item, "smart_mode", 0))

    withheld = [
        r.getMessage()
        for r in caplog.records
        if "dry_run_device_modes" in r.getMessage()
    ]
    assert withheld, "the withheld smartMode write is logged"
    assert all(reason in message for message in withheld)


def regulator_with_a_target_never_written(off):
    """The regulator steers on while control is off; no one gets its target."""

    controller = EMSController(
        devices=[settled_device()],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=off,
    )
    controller.set_output_limit = Mock(return_value=True)
    controller.control_enabled = off.get_system("enabled", True)
    controller.commanded_device_targets["WR1"] = -600
    return controller


@pytest.mark.parametrize("off", OFF.values(), ids=OFF.keys())
def test_a_charge_the_regulator_only_computed_while_off_is_no_charge(off):
    controller = regulator_with_a_target_never_written(off)

    controller.release_charging_devices()

    assert controller.set_output_limit.call_count == 0
    assert controller.charges_held_by_ems() == {}


def test_the_regulator_s_target_is_a_charge_while_control_is_on():
    controller = regulator_with_a_target_never_written(Runtime())

    controller.release_charging_devices()

    assert controller.set_output_limit.call_count == 1


def test_switching_control_back_on_starts_the_charge_direction_afresh():
    controller = regulator_with_a_target_never_written(
        Runtime(system={"enabled": False})
    )
    controller.charge_direction = ChargeDirectionState(
        charging=True, entry_window=(1.0, 2.0), entries=(10.0,)
    )
    controller.charge_capacity_w = 900
    controller.device_charge_limits = {"WR1": 900}

    controller.note_control_enabled(True)

    assert controller.charge_direction.charging is False
    assert controller.charge_direction.entry_window == ()
    assert controller.charge_direction.entries == (10.0,)
    assert controller.charge_capacity_w == 0
    assert controller.device_charge_limits == {}


@pytest.mark.parametrize(
    "switched_off,kept",
    [(False, True), (True, False)],
    ids=[
        "on record on a device in control",
        "on record on a device out of control",
    ],
)
def test_only_a_charge_the_ems_still_supervises_keeps_the_direction(
    switched_off, kept
):
    runtime = Runtime(
        system={"enabled": False},
        devices={"WR2": {"enabled": False}} if switched_off else {},
    )
    second = settled_device()
    second.name = "WR2"
    second.charge_commanded = True
    controller = EMSController(
        devices=[settled_device(), second],
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=runtime,
    )
    controller.control_enabled = False
    controller.charge_direction = ChargeDirectionState(charging=True)

    controller.note_control_enabled(True)

    assert controller.charge_direction.charging is kept
