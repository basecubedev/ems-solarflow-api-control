# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who the EMS commands, and who it still counts on.

Three conditions take a device out of the *write* path: it is offline, the
operator disabled it, or something else owns its AC mode. Only the third also
takes it out of the *allocation*, and the difference is not an oversight.

A reserved device has been commanded into AC input; it is not exporting, so the
allocator must not count on it. An offline or disabled device was simply never
written to this cycle -- it keeps the last `outputLimit` it was given and goes
on delivering roughly that much. Dropping it from the allocation hands its share
to a device that is still running, and the plant then delivers both until the
meter feedback catches up. Under-delivering costs an import for a few cycles;
over-delivering exports, which is the worse half of the trade.

So one predicate answers "will this device be commanded" for the write path, the
night-idle set and the explanation, while the allocator keeps asking the
narrower question it has always asked.
"""

from unittest.mock import patch

import pytest

from ems.controller import EMSController
from ems.models import DeviceState
from ems.runtime_intents import ac_input_intent, ac_output_intent
from ems.target_control import calculate_targets, detect_capabilities
from tests.test_write_gates import RuntimeStateStub, ShellyStub, device

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]


def state(solar=400, soc=50):
    return DeviceState(
        soc=soc,
        min_soc=15,
        max_soc=100,
        solar=solar,
        output=0,
        pack_in=0,
        pack_out=0,
        temp=20,
        voltage=48,
        rssi=0,
        remain_minutes=0,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
        output_limit=0,
        soc_limit=0,
        pack_state=2,
        fault_level=0,
        smart_mode=1,
        grid_off_mode=0,
        ac_mode=2,
        ac_status=1,
        dc_status=1,
        grid_state=1,
        pack_num=1,
    )


def controller_with(runtime_devices=None, online=None, reserved=()):
    devices = [device("WR1"), device("WR2")]
    for item in devices:
        item.battery_kwh = 1.0
        item.pv_kwp = 1.0
        item.pv_priority_factor = 1.0

    controller = EMSController(
        devices=devices,
        shelly=ShellyStub(0),
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(devices=runtime_devices or {}),
    )
    controller.device_online = online or {"WR1": True, "WR2": True}
    controller.runtime_intents = {
        item.name: (
            ac_input_intent(item.name, "runtime_state")
            if item.name in reserved
            else ac_output_intent(item.name)
        )
        for item in devices
    }
    return controller


def allocate(controller, states, requested_total=600):
    capabilities = controller.intent_filtered_capabilities(
        [detect_capabilities(item) for item in states]
    )
    with patch.multiple(
        "ems.target_control.cfg",
        REDISTRIBUTE_CLAMPED_POWER=True,
        PV_KWP_WEIGHTING=True,
        BATTERY_KWH_WEIGHTING=True,
        PV_CHARGE_BALANCE_ENABLED=False,
    ):
        targets, _, _ = calculate_targets(
            load=0,
            devices=states,
            max_power=1600,
            device_configs=controller.devices,
            capabilities=capabilities,
            requested_total=requested_total,
        )
    effective = controller.effective_control_targets(list(targets), True, 0)
    return [round(value) for value in targets], effective


# --- a reserved device is not exporting, so nothing is expected of it -------


def test_a_reserved_device_receives_no_allocation():
    controller = controller_with(reserved={"WR2"})

    targets, effective = allocate(controller, [state(solar=800), state()])

    assert targets[1] == 0
    assert effective[1] == 0
    assert targets[0] == 600


# --- an uncommanded device keeps delivering, so it keeps its share ----------


def test_an_offline_device_keeps_its_share_of_the_allocation():
    """It is not written to, and it has not stopped either."""

    controller = controller_with(online={"WR1": True, "WR2": False})

    targets, effective = allocate(controller, [state(), state()])

    assert targets == [300, 300]
    assert effective[1] == 0


def test_a_disabled_device_keeps_its_share_of_the_allocation():
    controller = controller_with(runtime_devices={"WR2": {"enabled": False}})

    targets, effective = allocate(controller, [state(), state()])

    assert targets == [300, 300]
    assert effective[1] == 0


def test_the_survivor_is_not_pushed_up_to_cover_an_offline_device():
    """The regression this file exists to prevent.

    Two devices sharing 600 W, one goes offline. If its share were handed to
    the other, the plant would command 600 W from a device already delivering
    300 W beside one that never stopped -- 900 W into a 600 W load.
    """

    controller = controller_with(online={"WR1": True, "WR2": False})

    targets, _ = allocate(controller, [state(solar=800), state(solar=800)])

    assert targets[0] == 300


# --- the command predicate governs the write path, and agrees with itself ---


def test_all_three_conditions_block_the_command():
    for description, kwargs in (
        ("offline", {"online": {"WR1": True, "WR2": False}}),
        ("disabled", {"runtime_devices": {"WR2": {"enabled": False}}}),
        ("reserved", {"reserved": {"WR2"}}),
    ):
        controller = controller_with(**kwargs)

        assert controller.device_commandable(controller.devices[0]), description
        assert not controller.device_commandable(controller.devices[1]), description


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"online": {"WR1": True, "WR2": False}}, "offline"),
        ({"runtime_devices": {"WR2": {"enabled": False}}}, "device_disabled"),
        ({"reserved": {"WR2"}}, "runtime_role_ac_input"),
    ],
)
def test_the_block_reason_names_the_condition(kwargs, expected):
    controller = controller_with(**kwargs)

    assert controller.device_command_block_reason(controller.devices[1]) == expected


def test_the_predicate_agrees_with_the_index_helpers():
    """Two call sites, one answer. They were byte-identical copies before."""

    controller = controller_with(online={"WR1": True, "WR2": False})

    commandable = [
        index
        for index, dev in enumerate(controller.devices)
        if controller.device_commandable(dev)
    ]

    assert controller.active_online_device_indexes() == commandable
    assert controller.night_min_soc_controllable_indices() == commandable


def test_a_fully_available_plant_is_unchanged():
    controller = controller_with()

    targets, effective = allocate(controller, [state(), state()])

    assert targets == [300, 300]
    assert effective == [300, 300]
