# SPDX-License-Identifier: AGPL-3.0-or-later
"""The allocator only shares power out to devices that will be commanded.

Three separate conditions take a device out of the control loop: it is offline,
the operator disabled it, or something else owns its AC mode. Only the third was
ever told to the allocator. The first two were applied afterwards, when the
target was already assigned -- so the share went to a device that could not use
it and was then set to zero, and the rest of the plant never heard about it.

Measured before the change: two devices, 600 W requested, one of them offline.
The allocator reported 300 W each, `undistributed` stayed 0, and 300 W simply
did not happen. The loop recovers it over the next cycles through the meter,
which is why it went unnoticed; the explanation never mentions it at all.

One predicate answers "will this device be commanded", and everything that needs
the answer asks it.
"""

from unittest.mock import patch

import pytest

from ems.controller import EMSController
from ems.models import DeviceState
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


def controller_with(runtime_devices=None, online=None):
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
        item.name: __import__(
            "ems.runtime_intents", fromlist=["ac_output_intent"]
        ).ac_output_intent(item.name)
        for item in devices
    }
    return controller


def allocate(controller, states, requested_total=600):
    capabilities = controller.commandable_capabilities(
        [detect_capabilities(item) for item in states]
    )
    with patch.multiple(
        "ems.target_control.cfg",
        REDISTRIBUTE_CLAMPED_POWER=True,
        PV_KWP_WEIGHTING=True,
        BATTERY_KWH_WEIGHTING=True,
        PV_CHARGE_BALANCE_ENABLED=False,
    ):
        targets, _, _, explanation = calculate_targets(
            load=0,
            devices=states,
            max_power=1600,
            device_configs=controller.devices,
            capabilities=capabilities,
            requested_total=requested_total,
            explain=True,
            online_devices=controller.device_online,
        )
    effective = controller.effective_control_targets(list(targets), True, 0)
    return [round(value) for value in targets], effective, explanation


# --- the three ways out of the control loop ---------------------------------


def test_an_offline_device_receives_no_allocation():
    controller = controller_with(online={"WR1": True, "WR2": False})

    targets, effective, _ = allocate(controller, [state(), state()])

    assert targets[1] == 0
    assert effective[1] == 0


def test_a_disabled_device_receives_no_allocation():
    controller = controller_with(runtime_devices={"WR2": {"enabled": False}})

    targets, effective, _ = allocate(controller, [state(), state()])

    assert targets[1] == 0
    assert effective[1] == 0


def test_a_reserved_device_receives_no_allocation():
    """The one exclusion that was already reaching the allocator."""

    controller = controller_with(
        runtime_devices={"WR2": {"runtime_role": "ac_input"}}
    )
    controller.runtime_intents["WR2"] = __import__(
        "ems.runtime_intents", fromlist=["ac_input_intent"]
    ).ac_input_intent("WR2", "runtime_state")

    targets, effective, _ = allocate(controller, [state(), state()])

    assert targets[1] == 0
    assert effective[1] == 0


# --- and the power goes to a device that can deliver it ---------------------


def test_the_excluded_share_is_redistributed_rather_than_lost():
    controller = controller_with(online={"WR1": True, "WR2": False})

    targets, effective, _ = allocate(controller, [state(solar=800), state()])

    assert targets[0] == 600
    assert sum(effective) == 600


def test_what_is_allocated_is_what_is_commanded():
    """The invariant behind all of this, for every exclusion reason."""

    for description, kwargs in (
        ("offline", {"online": {"WR1": True, "WR2": False}}),
        ("disabled", {"runtime_devices": {"WR2": {"enabled": False}}}),
    ):
        controller = controller_with(**kwargs)
        targets, effective, _ = allocate(
            controller, [state(solar=800), state()]
        )

        assert sum(targets) == sum(effective), description


def test_an_unservable_request_is_reported_rather_than_silently_dropped():
    """What the plant cannot deliver has to show up as undistributed.

    The remaining device sits at its discharge floor, so its 200 W of PV is all
    there is; the battery top-up has nothing to add. Before the change the
    missing 400 W were attributed to the offline device and vanished from the
    explanation entirely.
    """

    controller = controller_with(online={"WR1": True, "WR2": False})

    targets, effective, explanation = allocate(
        controller, [state(solar=200, soc=15), state()], requested_total=600
    )

    assert sum(effective) == sum(targets) == 200

    shortfall = [
        limit
        for limit in explanation.limits
        if limit.name == "pv_only_target_limit" and limit.active
    ]
    assert shortfall and shortfall[0].value == 400


# --- nothing changes when every device is commandable -----------------------


def test_a_fully_available_plant_is_unchanged():
    controller = controller_with()

    targets, effective, _ = allocate(controller, [state(), state()])

    assert targets == [300, 300]
    assert effective == [300, 300]


def test_the_predicate_agrees_with_the_index_helpers():
    """One answer, whoever asks for it."""

    controller = controller_with(
        online={"WR1": True, "WR2": False},
        runtime_devices={"WR1": {"enabled": True}},
    )

    commandable = [
        index
        for index, dev in enumerate(controller.devices)
        if controller.device_commandable(dev)
    ]

    assert controller.active_online_device_indexes() == commandable
    assert controller.night_min_soc_controllable_indices() == commandable
