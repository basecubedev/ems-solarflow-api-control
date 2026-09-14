# SPDX-License-Identifier: AGPL-3.0-or-later
"""The charge regulator wired into the control loop.

Covers the properties that only hold once the pieces are connected: that the
feature changes nothing while it is off, that a real surplus eventually reaches
the hardware as a negative target, that the way back is never blocked, and that
the regulator leaves no trace in operator state.
"""

from unittest.mock import Mock, patch

import pytest

from ems import config as cfg
from ems.config import AC_CHARGE_CONTROL_DEFAULTS
from ems.controller import EMSController
from tests.test_write_gates import RuntimeStateStub, ShellyStub, device, state

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def charging_device(name="WR1", **kwargs):
    dev = device(name)
    dev.hardware_profile = "solarflow_800_pro_2"
    dev.observed_product = None
    dev.ac_charge_enabled = True
    dev.max_charge_power_w = 0
    for key, value in kwargs.items():
        setattr(dev, key, value)
    return dev


def surplus_state(soc=50, pack_num=2):
    # PV is exporting and a real battery has room: a genuine surplus situation.
    # pack_num matters — a device with no pack must never be allocated a charge.
    return state(soc=soc, solar=900, output=0, soc_limit=0, pack_num=pack_num)


class Harness:
    """Runs the real control loop for N cycles against a fixed load."""

    def __init__(self, devices, load, runtime_state=None, feature=True):
        self.devices = devices
        self.controller = EMSController(
            devices=devices,
            shelly=ShellyStub(load),
            sleep_enabled=False,
            runtime_state=runtime_state,
        )
        self.controller.run_startup_ac_mode_reconcile_once = Mock()
        self.controller.set_output_limit = Mock()
        self.feature = {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": feature}

    def run(self, cycles=1, states=None, load=None):
        if load is not None:
            self.controller.shelly = ShellyStub(load)
        states = states or [surplus_state() for _ in self.devices]
        with patch("ems.controller.fetch_all_devices", return_value=states), patch(
            "ems.controller.cfg.SYSTEM_ENABLED", True
        ), patch("ems.controller.cfg.MAX_TOTAL_POWER", 800), patch(
            "ems.controller.cfg.MAX_DEVICE_POWER", 800
        ), patch("ems.controller.cfg.MIN_OUTPUT_LIMIT", 0), patch(
            "ems.controller.cfg.DEADBAND", 10
        ), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0), patch.object(
            cfg, "AC_CHARGE_CONTROL_CONFIG", self.feature
        ):
            for _ in range(cycles):
                self.controller.run_once()
        return self.controller

    @property
    def targets(self):
        return [call.args[1] for call in self.controller.set_output_limit.call_args_list]


def test_the_feature_off_produces_no_negative_target_ever():
    """The invariant the default rests on, exercised through the real loop."""

    harness = Harness([charging_device()], load=-900, feature=False)
    harness.run(cycles=20)

    assert all(target >= 0 for target in harness.targets)
    assert harness.controller.commanded_total_w >= 0
    assert harness.controller.charge_direction.charging is False
    assert harness.controller.commanded_total_floor_w() == 0


def test_a_sustained_surplus_eventually_charges():
    harness = Harness([charging_device()], load=-900)
    harness.run(cycles=12)

    assert any(target < 0 for target in harness.targets), harness.targets
    assert harness.controller.charge_direction.charging is True


def test_charging_never_starts_without_the_per_device_permission():
    harness = Harness([charging_device(ac_charge_enabled=False)], load=-900)
    harness.run(cycles=20)

    assert all(target >= 0 for target in harness.targets)
    assert harness.controller.charge_direction.charging is False


def test_charging_never_starts_on_a_model_without_a_charge_path():
    harness = Harness(
        [charging_device(hardware_profile="solarflow_800")], load=-900
    )
    harness.run(cycles=20)

    assert all(target >= 0 for target in harness.targets)
    assert harness.controller.charge_direction.charging is False


def test_switching_the_feature_off_mid_charge_returns_in_the_same_cycle():
    """The return path may never be gated by a threshold or a counter."""

    harness = Harness([charging_device()], load=-900)
    harness.run(cycles=12)
    assert harness.controller.charge_direction.charging is True

    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": False}
    before = len(harness.targets)
    harness.run(cycles=1)

    assert harness.controller.charge_direction.charging is False
    assert all(target >= 0 for target in harness.targets[before:])


def test_a_deficit_leaves_charging_immediately():
    harness = Harness([charging_device()], load=-900)
    harness.run(cycles=12)
    assert harness.controller.charge_direction.charging is True

    before = len(harness.targets)
    harness.run(cycles=1, load=1200)

    assert harness.controller.charge_direction.charging is False
    assert all(target >= 0 for target in harness.targets[before:])


def test_the_regulator_writes_nothing_into_operator_state():
    """Its decision is a property of the cycle, never durable intent."""

    runtime_state = RuntimeStateStub(devices={"WR1": {"enabled": True}})
    snapshot = {
        "system": dict(runtime_state.system),
        "devices": {name: dict(values) for name, values in runtime_state.devices.items()},
    }

    harness = Harness([charging_device()], load=-900, runtime_state=runtime_state)
    harness.run(cycles=12)

    assert harness.controller.charge_direction.charging is True
    assert runtime_state.system == snapshot["system"]
    assert runtime_state.devices == snapshot["devices"]


def test_simulation_dispatches_no_charge_to_hardware():
    """Mocking set_output_limit would skip the gate; let the real one run."""

    harness = Harness([charging_device()], load=-900)
    harness.controller.set_output_limit = EMSController.set_output_limit.__get__(
        harness.controller
    )

    with patch("ems.controller.cfg.SIMULATION_MODE", True), patch(
        "ems.controller.dispatch_device_write"
    ) as dispatch:
        harness.run(cycles=20)

    assert harness.controller.charge_direction.charging is True
    dispatch.assert_not_called()


def test_an_unreachable_device_is_not_allocated_a_charge():
    harness = Harness([charging_device()], load=-900)
    harness.run(cycles=20, states=[None])

    assert harness.targets == []
    assert harness.controller.charge_direction.charging is False


def test_the_regulator_does_not_read_its_own_charge_back_as_the_firmware_s():
    """Telemetry reflects the charge; the regulator must not mistake it.

    The firmware-owned claim fires on acMode=1 plus acStatus=2, which is exactly
    what a device the regulator is driving reports. Without excluding a charge
    the EMS asked for, the device would be marked uncommandable, drop out of the
    chargeable set and the regulator would shut itself down two cycles in.
    """

    harness = Harness([charging_device()], load=-900)
    harness.run(cycles=12)
    assert harness.controller.charge_direction.charging is True

    # From here the device reports what it is actually doing.
    charging_telemetry = state(
        soc=50, solar=900, output=0, soc_limit=0, ac_mode=1, ac_status=2,
        input_limit_w=600, pack_num=2,
    )
    harness.run(cycles=10, states=[charging_telemetry])

    assert harness.controller.charge_direction.charging is True


def test_a_leftover_charge_above_the_floor_is_taken_back():
    """A device found charging with a healthy battery is not the firmware's."""

    dev = charging_device(ac_charge_enabled=False)
    leftover = state(
        soc=60, solar=0, output=0, soc_limit=0, ac_mode=1, ac_status=2,
        input_limit_w=600,
    )

    harness = Harness([dev], load=100)
    harness.run(cycles=3, states=[leftover])

    intent = harness.controller.runtime_intents["WR1"]
    assert intent.reason != "firmware_owned_charge"
    assert intent.output_control_allowed is True


def test_a_charging_device_is_not_pushed_up_to_the_standby_output_floor():
    """min_output_limit describes output; a charging device produces none."""

    harness = Harness([charging_device()], load=-900)
    with patch("ems.controller.cfg.MIN_OUTPUT_LIMIT", 35):
        harness.run(cycles=12)

    negative = [target for target in harness.targets if target < 0]
    assert negative, harness.targets


def test_shutdown_returns_a_charging_device_and_forgets_the_direction():
    """A charge must not outlive the process that commanded it."""

    harness = Harness([charging_device()], load=-900)
    harness.run(cycles=12)
    assert harness.controller.charge_direction.charging is True
    assert harness.controller.commanded_device_targets["WR1"] < 0

    harness.controller.release_charging_devices()

    assert harness.controller.set_output_limit.call_args_list[-1].args[1] == 0
    assert harness.controller.commanded_device_targets["WR1"] == 0
    assert harness.controller.charge_direction.charging is False


def test_shutdown_leaves_a_discharging_device_alone():
    harness = Harness([charging_device()], load=600)
    harness.run(cycles=5)
    before = len(harness.controller.set_output_limit.call_args_list)

    harness.controller.release_charging_devices()

    assert len(harness.controller.set_output_limit.call_args_list) == before


def test_a_full_battery_does_not_put_the_system_into_charge_direction():
    """Permission without capacity would wind the integrator down for nothing.

    Entering charge direction opens the negative floor. With no device able to
    absorb anything, the allocation is empty and the integrator walks down to
    that floor against a charge that never happens — then has to climb back
    when the house needs power again.
    """

    harness = Harness([charging_device()], load=-900)
    # The helper's ceiling is 100, so a SoC of 100 leaves no headroom.
    full = state(soc=100, solar=900, output=0, soc_limit=0, pack_num=2)
    harness.run(cycles=20, states=[full])

    assert harness.controller.charge_direction.charging is False
    assert harness.controller.commanded_total_w >= 0


def test_a_device_without_a_battery_does_not_put_the_system_into_charge_direction():
    harness = Harness([charging_device()], load=-900)
    harness.run(cycles=20, states=[surplus_state(pack_num=0)])

    assert harness.controller.charge_direction.charging is False
    assert all(target >= 0 for target in harness.targets)


def test_the_integrator_never_commands_more_charge_than_devices_can_take():
    """An unreachable setpoint is windup with a delay attached.

    With a total cap well above what the devices accept, the integrator would
    walk down to that cap while only a fraction actually flows. The meter still
    reports the unabsorbed surplus, so it stays pinned there — and the exit,
    which reads commanded plus load, then has to climb the whole gap before it
    can fire. Measured at a 1200 W cap against a 400 W device, that turned a
    one-cycle exit into roughly a minute of drawing from the grid.
    """

    limited = charging_device(max_charge_power_w=400)
    harness = Harness([limited], load=-1500)
    harness.run(cycles=20)

    assert harness.controller.charge_direction.charging is True
    assert harness.controller.commanded_total_w >= -400
    assert harness.controller.commanded_total_floor_w() == -400


def test_the_floor_follows_the_devices_that_can_actually_take_it():
    two = [charging_device("WR1", max_charge_power_w=300),
           charging_device("WR2", max_charge_power_w=250)]
    harness = Harness(two, load=-1500)
    harness.run(cycles=20, states=[surplus_state(), surplus_state()])

    assert harness.controller.commanded_total_floor_w() == -550


def test_a_house_that_needs_power_ends_a_charge_even_with_no_export_capacity():
    """The no-export hold must never freeze a charge in place.

    The hold exists for the opposite situation: load is positive and nothing can
    serve it, so the discharge target must not ramp up against reality. It was
    written before the total could be negative, and it froze that too — at night
    with no PV a charging device reports no discharge capability, so the hold
    fired, the integrator stopped seeing the load, and the system charged from
    the grid while the house drew from it. Indefinitely.

    A charge is the first thing that should give way when the house needs power.
    """

    harness = Harness([charging_device()], load=-900)
    surplus = state(soc=50, solar=0, output=0, soc_limit=0, pack_num=2, dc_status=1)
    harness.run(cycles=10, states=[surplus])
    assert harness.controller.charge_direction.charging is True

    # Night, no PV, the device is charging and reports no way to discharge.
    charging_no_export = state(
        soc=50, solar=0, output=0, soc_limit=0, pack_num=2,
        dc_status=0, ac_mode=1, ac_status=2, pack_out=600,
    )
    before = len(harness.targets)
    harness.run(cycles=1, load=900, states=[charging_no_export])

    # The direction is out in the same cycle and no device is still told to
    # charge. The integrator itself walks back under its ramp, which is fine —
    # what must not happen is the charge continuing.
    assert harness.controller.charge_direction.charging is False
    assert harness.controller.commanded_device_targets["WR1"] >= 0
    assert all(target >= 0 for target in harness.targets[before:])
