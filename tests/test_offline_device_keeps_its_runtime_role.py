# SPDX-License-Identifier: AGPL-3.0-or-later
"""A device that does not answer keeps the role its claims give it (K-low).

Every cycle began with the default ``ac_output`` claim for every device, and
only a device that answered had its claims resolved over it. A device the
operator had parked in AC input, or one the full-charge assist held, was
therefore an ``ac_output`` device for every cycle it did not answer: the
allocator, which keeps an offline device in the allocation on its cached
state, gave it a share of the output it would never deliver, and the
dashboard showed the default role. Its claims are now resolved as for a device
that answered; the one that needs a report, the firmware's own charge, is
carried from the last cycle, and nothing is reconciled without a report.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems.controller import EMSController
from ems.runtime_intents import (
    FIRMWARE_CHARGE_REASON,
    DeviceRuntimeIntent,
    DeviceRuntimeRole,
)
from test_write_gates import RuntimeStateStub, ShellyStub, device, state

pytestmark = [
    pytest.mark.unit,
    pytest.mark.power_control,
]


def _controller(runtime_devices=None):
    devices = [device("WR1"), device("WR2")]
    for dev in devices:
        dev.session.post.return_value = SimpleNamespace(status_code=200)
    controller = EMSController(
        devices=devices,
        shelly=ShellyStub(300),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(devices=runtime_devices or {}),
    )
    controller.set_output_limit = Mock(return_value=True)
    return controller, devices


def _cycle(controller, states):
    with patch("ems.controller.fetch_all_devices", return_value=states), patch.multiple(
        "ems.controller.cfg",
        SYSTEM_ENABLED=True,
        MAX_TOTAL_POWER=800,
        MAX_DEVICE_POWER=800,
        MIN_OUTPUT_LIMIT=0,
        LOOP_INTERVAL=5,
        DEADBAND=10,
        SOC_RECONCILE_INTERVAL=0,
        DRY_RUN=False,
        SIMULATION_MODE=False,
        ARGS=SimpleNamespace(replay=False),
        ALLOW_HARDWARE_WRITES=True,
        ALLOW_STATE_RECONCILIATION_WRITES=True,
    ):
        controller.run_once()


def _parked():
    return {"WR2": {"runtime_role": "ac_input", "runtime_role_reason": "emsctl"}}


def test_a_parked_device_stays_parked_while_it_does_not_answer():
    controller, (_, parked) = _controller(_parked())

    _cycle(controller, [state(), state(ac_mode=1)])
    assert controller.runtime_intents["WR2"].role is DeviceRuntimeRole.AC_INPUT
    parked.session.post.reset_mock()

    _cycle(controller, [state(), None])

    intent = controller.runtime_intents["WR2"]
    assert intent.role is DeviceRuntimeRole.AC_INPUT
    assert intent.operator_role is True
    assert controller.device_output_control_allowed(parked) is False
    assert parked.session.post.call_count == 0


def test_a_parked_device_gets_no_share_of_the_output_while_it_does_not_answer():
    """The allocator keeps an offline device on its cached state; a parked one gets nothing."""

    controller, _ = _controller(_parked())

    _cycle(controller, [state(), state(ac_mode=1)])
    _cycle(controller, [state(), None])

    devices = controller.last_control_explanation.devices
    assert devices["WR2"].allocated_target_w == 0
    assert devices["WR1"].allocated_target_w > 0


def test_a_device_parked_before_it_ever_answered_is_parked():
    controller, (_, parked) = _controller(_parked())

    _cycle(controller, [state(), None])

    assert controller.runtime_intents["WR2"].role is DeviceRuntimeRole.AC_INPUT
    assert controller.device_output_control_allowed(parked) is False


def test_the_firmware_s_own_charge_is_carried_while_the_device_does_not_answer():
    controller, (_, charging) = _controller()
    firmware = DeviceRuntimeIntent(
        device="WR2",
        role=DeviceRuntimeRole.AC_INPUT,
        reason=FIRMWARE_CHARGE_REASON,
        desired_ac_mode=None,
        output_control_allowed=False,
        priority=50,
    )

    _cycle(controller, [state(), state()])
    controller.runtime_intents["WR2"] = firmware
    _cycle(controller, [state(), None])

    assert controller.runtime_intents["WR2"] == firmware
    assert controller.device_output_control_allowed(charging) is False


def test_an_unparked_device_keeps_the_default_role_while_it_does_not_answer():
    controller, (_, quiet) = _controller()

    _cycle(controller, [state(), state()])
    _cycle(controller, [state(), None])

    intent = controller.runtime_intents["WR2"]
    assert intent.role is DeviceRuntimeRole.AC_OUTPUT
    assert intent.operator_role is False
