# SPDX-License-Identifier: AGPL-3.0-or-later
"""The charge regulator wired into the control loop.

Covers the properties that only hold once the pieces are connected: that the
feature changes nothing while it is off, that a real surplus eventually reaches
the hardware as a negative target, that the way back is never blocked, and that
the regulator leaves no trace in operator state.
"""

import logging
import time
from unittest.mock import Mock, patch

import pytest

from ems import config as cfg
from ems.config import AC_CHARGE_CONTROL_DEFAULTS
from ems.controller import EMSController
from ems.health import CommHealth
from ems.runtime_intents import REGULATOR_CHARGE_REASON
from ems.target_control import detect_capabilities
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


def test_the_shipped_standby_floor_does_not_block_entry():
    """Entry needs `commanded_total_w <= 0`, and the floor is not on the total.

    The harness patches `min_output_limit` to 0, so nothing else here would
    notice if the shipped default of 35 W held the total above zero and made
    entry unreachable on every real installation. It does not: the floor applies
    per device at the output stage, and the total still reaches zero.
    """

    harness = Harness([charging_device("WR1"), charging_device("WR2")], load=-900)
    with patch("ems.controller.cfg.MIN_OUTPUT_LIMIT", 35):
        harness.run(cycles=20)

    assert harness.controller.charge_direction.charging is True
    assert all(target < 0 for target in harness.targets[-2:])


def test_a_commanded_charge_reaches_the_transport_as_a_charge_operation():
    """End to end across the seam the other tests mock away.

    Everything above stops at `set_output_limit`; the write-path tests start
    after it. This runs the real loop with that seam intact and asserts what the
    transport was actually asked to do -- so a surplus really does become an AC
    charge command and not just a negative number in a dict.
    """

    from ems.power_command import build_zensdk_power_operation
    from ems.power_direction import AC_MODE_INPUT, OPERATION_CHARGE

    harness = Harness([charging_device("WR1")], load=-900)
    dispatched = []
    harness.controller.set_output_limit = lambda dev, value: dispatched.append(
        (dev.name, int(value))
    )
    harness.run(cycles=20)

    assert harness.controller.charge_direction.charging is True
    assert dispatched, "the loop never wrote anything"

    name, value = dispatched[-1]
    assert name == "WR1"
    assert value < 0, dispatched[-3:]

    operation = build_zensdk_power_operation(value)
    assert operation.operation == OPERATION_CHARGE
    assert operation.properties["acMode"] == AC_MODE_INPUT
    assert operation.properties["inputLimit"] == abs(value)
    assert operation.properties["outputLimit"] == 0


class DyingMeter:
    """A meter that starts failing and then holds its last reading forever.

    That is the real client's behaviour: on a read error it returns
    ``last_value`` and marks the health ``stale_used``, with nothing capping how
    long it may keep doing so.
    """

    def __init__(self, power):
        self.power = power
        self.health = CommHealth("stub", kind="read")
        self.health.record_success(latency_ms=1.0)
        self.failing = False

    def get_power(self):
        if self.failing:
            self.health.record_failure(error=OSError("unreachable"), stale_used=True)
        else:
            self.health.record_success(latency_ms=1.0)
        return self.power


def test_a_charge_does_not_outlive_the_meter_that_justifies_it():
    """A held reading of "still exporting" is indistinguishable from a surplus.

    Discharging on a stale meter places energy the operator already owns;
    charging *spends*, so it is the direction that has to fail closed. Without
    this the EMS keeps drawing from the grid on a number that may be hours old.
    """

    harness = Harness([charging_device()], load=-900)
    meter = DyingMeter(-900)
    harness.controller.shelly = meter
    harness.run(cycles=20)
    assert harness.controller.charge_direction.charging is True

    # The meter starts failing. The held reading is stale but still young, which
    # a transient miss looks like, and a charge must survive that: ending it
    # costs a full re-entry window.
    meter.failing = True
    harness.run(cycles=2)
    assert harness.controller.charge_direction.charging is True, (
        "a single missed read must not end a charge -- re-entry costs a full window"
    )

    # Now let the last good reading age past the tolerance. Moving the clock
    # backwards on the health record rather than sleeping keeps this
    # deterministic (rule 7).
    meter.health.last_success_monotonic = time.monotonic() - 3600
    harness.run(cycles=1)
    assert harness.controller.charge_direction.charging is False


def test_a_meter_without_health_tracking_still_charges():
    """Refusing to charge because a transport reports no health would disable
    the feature for it entirely, which is not what fail-closed means here."""

    harness = Harness([charging_device()], load=-900)
    assert not hasattr(harness.controller.shelly, "health")

    harness.run(cycles=20)

    assert harness.controller.charge_direction.charging is True


def charging_hardware_state(soc=60):
    """A device already drawing from the grid, as found after a restart."""

    item = state(soc=soc, solar=0, output=0, soc_limit=0, pack_num=2)
    item.ac_mode = 1
    item.ac_status = 2
    item.output_limit = 0
    item.input_limit_w = 600
    item.grid_input = 600
    return item


def test_a_running_charge_is_stopped_even_when_the_target_is_zero():
    """The write deadband must see a charging device as negative, not as idle.

    A charging device reports outputLimit 0 and output 0 while drawing 600 W. A
    discharge-only reference reads that as idle, so an idle target looks like no
    change and the write is skipped -- leaving the hardware charging from the
    grid with the EMS running and content. Local-HTTP devices are rescued by the
    startup acMode reconcile; MQTT control devices have no such path, which is
    how this would have reached hardware.
    """

    device_config = charging_device()
    device_config.supports_state_reconciliation = False
    harness = Harness([device_config], load=0)
    harness.controller.run_startup_ac_mode_reconcile_once = Mock()
    writes = []
    harness.controller.set_output_limit = lambda dev, value: writes.append(int(value))

    harness.run(cycles=3, states=[charging_hardware_state()])

    assert harness.controller.charge_direction.charging is False
    assert writes, "the EMS left the device charging without writing anything"
    assert writes[0] == 0

    from ems.power_command import build_zensdk_power_operation
    from ems.power_direction import AC_MODE_OUTPUT, OPERATION_IDLE

    operation = build_zensdk_power_operation(writes[0])
    assert operation.operation == OPERATION_IDLE
    assert operation.properties["acMode"] == AC_MODE_OUTPUT
    assert operation.properties["inputLimit"] == 0


def test_a_charge_the_ems_itself_commanded_is_not_rewritten_every_cycle():
    """The same reference must still suppress a no-op, or every cycle writes."""

    device_config = charging_device()
    harness = Harness([device_config], load=-900)
    writes = []
    harness.controller.set_output_limit = lambda dev, value: writes.append(int(value))
    harness.run(cycles=20)
    assert harness.controller.charge_direction.charging is True

    # Telemetry now agrees with what was commanded: nothing left to say.
    settled = charging_hardware_state()
    settled.grid_input = abs(harness.controller.commanded_device_targets["WR1"])
    settled.input_limit_w = settled.grid_input
    before = len(writes)
    harness.run(cycles=3, states=[settled], load=0)

    assert len(writes) == before, writes[before:]


class FollowingHardware:
    """Telemetry that follows the command, the way a device does.

    Every other test here holds the state fixed, which hides anything that only
    goes wrong once the device actually reports what it was told.
    """

    def __init__(self, item):
        self.state = item

    def __call__(self, dev, value):
        target = int(value)
        if target < 0:
            self.state.ac_mode = 1
            self.state.ac_status = 2
            self.state.input_limit_w = -target
            self.state.grid_input = -target
            self.state.output_limit = 0
        else:
            self.state.ac_mode = 2
            self.state.ac_status = 1
            self.state.input_limit_w = 0
            self.state.grid_input = 0
            self.state.output_limit = target


def test_the_state_reconciler_does_not_fight_the_regulator_over_ac_mode():
    """Two writers of acMode, once per loop, on the maintainer's own hardware.

    The per-cycle default claim is `ac_output`, whose desired mode is
    AC_MODE_OUTPUT. While the regulator holds a device in acMode 1 the
    reconciler sees a mismatch and writes acMode 2 -- against the power command
    writing acMode 1 -- so the relay is commanded back and forth every cycle.
    Local-API installations have state reconciliation on by default, so this was
    live; it stayed invisible because the tests' state-reconciliation gate is
    shut and their telemetry never follows the command.
    """

    item = surplus_state()
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0

    harness = Harness([charging_device()], load=-900)
    harness.controller.device_state_writes_allowed = lambda dev: True
    harness.controller.set_output_limit = FollowingHardware(item)

    written = []
    with patch(
        "ems.controller.write_device_properties",
        side_effect=lambda dev, properties, **kw: written.append(properties) or True,
    ):
        harness.run(cycles=25, states=[item])

    assert harness.controller.charge_direction.charging is True
    assert item.ac_mode == 1, "the device never reached charge mode"
    assert [p for p in written if "acMode" in p] == []

    intent = harness.controller.runtime_intents["WR1"]
    assert intent.reason == REGULATOR_CHARGE_REASON
    # None means "the power command owns this direction", not "output".
    assert intent.desired_ac_mode is None
    # True, or the regulator reads its own claim back as someone else's and
    # refuses to keep charging the device it is charging.
    assert intent.output_control_allowed is True


def test_an_operator_park_still_takes_a_device_away_mid_charge():
    """The regulator's claim must lose to a park, or the park does nothing."""

    from ems.runtime_intents import (
        PRIORITY_OPERATOR_PARK,
        PRIORITY_REGULATOR,
        ac_input_intent,
        regulator_charge_intent,
        resolve_device_intent,
    )

    assert PRIORITY_REGULATOR < PRIORITY_OPERATOR_PARK

    winner = resolve_device_intent([
        regulator_charge_intent("WR1", ems_commanded_charge=True),
        ac_input_intent("WR1", "operator_park"),
    ])

    assert winner.reason == "operator_park"
    assert winner.output_control_allowed is False


def test_both_directions_ask_the_same_question_about_eligibility():
    """Online, switched on, and not claimed by someone else are one rule.

    They were written out twice, once per direction, and two copies of an
    eligibility rule is how the two sides drift apart. They already had: the
    discharge flag is read from the device config only, while the charge flag
    reads runtime-state first -- nobody decided that, it is an artefact of two
    code paths.
    """

    from ems.runtime_intents import ac_input_intent

    harness = Harness([charging_device()], load=-900)
    controller = harness.controller
    dev = controller.devices[0]
    item = surplus_state()
    capability = detect_capabilities(item)

    controller.runtime_intents = {}
    assert controller.device_active(dev) is True
    assert controller.device_intent_allows_command(dev) is True

    # Offline takes the device away from both directions.
    controller.device_online[dev.name] = False
    assert controller.device_active(dev) is False
    assert controller.device_charge_allowed(dev, item, capability) is False
    controller.device_online[dev.name] = True

    # So does a claim by someone else, and through the same predicate.
    controller.runtime_intents = {dev.name: ac_input_intent(dev.name, "operator_park")}
    assert controller.device_intent_allows_command(dev) is False
    assert controller.device_charge_allowed(dev, item, capability) is False
    assert controller.device_output_control_allowed(dev) is False


def test_a_device_that_drifts_out_of_charge_mode_is_put_back():
    """Standing the reconciler down is not "nobody watches".

    The reconciler writes a bare acMode, and a direction change needs the atomic
    set -- a bare `acMode: 1` would leave inputLimit at whatever it held, so the
    device would sit in the charge direction drawing nothing. The power command
    is the one that can write a complete direction change, and it notices the
    drift because the write deadband measures the target against the *measured*
    AC input rather than against an output the device does not produce.
    """

    item = surplus_state()
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0

    harness = Harness([charging_device()], load=-900)
    harness.controller.device_state_writes_allowed = lambda dev: True
    hardware = FollowingHardware(item)
    written = []

    def record(dev, value):
        written.append(int(value))
        hardware(dev, value)

    harness.controller.set_output_limit = record
    with patch("ems.controller.write_device_properties", return_value=True):
        harness.run(cycles=20, states=[item])
        assert harness.controller.charge_direction.charging is True
        assert item.ac_mode == 1

        # The device falls out of charge mode on its own: a vendor-app touch, a
        # reset, a firmware decision. Nothing the EMS did.
        item.ac_mode = 2
        item.ac_status = 1
        item.input_limit_w = 0
        item.grid_input = 0
        item.output_limit = 0
        before = len(written)
        harness.run(cycles=1, states=[item])

    assert written[before:], "the drift went uncorrected"
    assert written[-1] < 0
    assert item.ac_mode == 1, "the device was not put back into charge mode"
    assert item.input_limit_w == abs(written[-1]), "put back without a charge power"


def collapsed_band_harness(hysteresis):
    """A closed loop whose meter responds to the charge, as a real one does."""

    class RespondingMeter:
        def __init__(self, surplus):
            self.surplus = surplus
            self.charge = 0

        def get_power(self):
            return self.surplus + self.charge

    item = surplus_state()
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0

    harness = Harness([charging_device()], load=-200)
    meter = RespondingMeter(-200)
    harness.controller.shelly = meter
    hardware = FollowingHardware(item)

    def respond(dev, value):
        meter.charge = -int(value) if int(value) < 0 else 0
        hardware(dev, value)

    harness.controller.set_output_limit = respond
    harness.feature = {
        **AC_CHARGE_CONTROL_DEFAULTS,
        "enabled": True,
        "charge_start_w": 150,
        "charge_hysteresis_w": hysteresis,
    }
    return harness, item


def count_direction_changes(harness, item, cycles):
    changes = 0
    previous = harness.controller.charge_direction.charging
    for _ in range(cycles):
        harness.run(cycles=1, states=[item])
        now = harness.controller.charge_direction.charging
        if now != previous:
            changes += 1
        previous = now
    return changes


def test_the_shipped_band_does_not_flutter_against_a_steady_surplus():
    harness, item = collapsed_band_harness(50)

    assert count_direction_changes(harness, item, 200) <= 2


def test_a_collapsed_band_flutters_and_the_rate_cap_is_what_bounds_it():
    """Entry and exit at the same threshold defeats the asymmetry entirely.

    Kept as a test rather than only as a config warning: it records what the
    hourly cap is actually for, and that it holds.
    """

    harness, item = collapsed_band_harness(0)

    changes = count_direction_changes(harness, item, 200)

    assert changes > 10, changes
    assert len(harness.controller.charge_direction.entries) <= 12


def test_the_rate_limit_is_said_once_per_episode_not_once_per_cycle():
    """Sixty identical lines bury every other event instead of surfacing this one."""

    harness, item = collapsed_band_harness(0)

    with patch("ems.controller.log_event") as log:
        for _ in range(200):
            harness.run(cycles=1, states=[item])

    warnings = [
        call
        for call in log.call_args_list
        if call.args[1:2] == ("ac_charge_entry_rate_limited",)
        and call.args[0] == logging.WARNING
    ]

    assert 0 < len(warnings) <= 3, len(warnings)


def test_a_device_that_stops_answering_hands_its_share_to_the_others():
    """Offline is a signal the allocator acts on, unlike "draws nothing".

    Its capacity leaves the total, its target collapses to zero, the remaining
    device takes up the slack, and the write it can no longer receive is skipped
    rather than attempted. The direction itself holds -- the surplus is still
    there and something can still absorb it.
    """

    items = [surplus_state(), surplus_state()]
    for item in items:
        item.ac_mode = 2
        item.ac_status = 1
        item.output_limit = 0
        item.input_limit_w = 0
        item.grid_input = 0

    harness = Harness(
        [charging_device("A"), charging_device("B")], load=-1200
    )
    hardware = [FollowingHardware(items[0]), FollowingHardware(items[1])]
    harness.controller.set_output_limit = lambda dev, value: hardware[
        0 if dev.name == "A" else 1
    ](dev, value)

    harness.run(cycles=20, states=items)
    assert harness.controller.charge_direction.charging is True
    assert harness.controller.commanded_device_targets["B"] < 0
    shared_capacity = harness.controller.charge_capacity_w

    # B stops answering: no telemetry at all, which is how the real fetch
    # reports an unreachable device.
    harness.run(cycles=4, states=[items[0], None])

    assert harness.controller.device_online["B"] is False
    assert harness.controller.charge_capacity_w < shared_capacity
    assert harness.controller.commanded_device_targets["B"] == 0
    assert harness.controller.commanded_device_targets["A"] < 0
    assert harness.controller.charge_direction.charging is True


def test_the_full_charge_assist_takes_a_device_away_mid_charge():
    """Two features want the same AC direction; the ladder decides, once.

    The assist outranks the regulator, and its claim forbids output control, so
    the regulator stops commanding the device in the same cycle rather than both
    writing a direction at it.
    """

    from ems.runtime_intents import ac_input_intent

    item = surplus_state()
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0

    harness = Harness([charging_device()], load=-900)
    harness.controller.set_output_limit = FollowingHardware(item)
    harness.run(cycles=20, states=[item])

    assert harness.controller.charge_direction.charging is True
    assert harness.controller.runtime_intents["WR1"].reason == REGULATOR_CHARGE_REASON

    harness.controller.full_charge_assist_intent = lambda dev: ac_input_intent(
        dev.name, "full_charge_assist", setpoint_w=600
    )
    harness.run(cycles=3, states=[item])

    assert harness.controller.runtime_intents["WR1"].reason == "full_charge_assist"
    assert harness.controller.commanded_device_targets["WR1"] == 0
    assert harness.controller.charge_direction.charging is False


def test_the_per_device_runtime_switch_stops_a_charge_in_one_cycle():
    """The switch the Control tab writes, end to end.

    Turning a device out of charging is the one action that must never wait for
    a threshold, a counter or a restart, and it is now reachable from a browser
    -- so the path from runtime-state to a device back in output mode is worth
    holding still. The feature-level switch already had a test; the per-device
    one did not, and it is the one an operator reaches for when a single device
    misbehaves.
    """

    item = surplus_state()
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0

    runtime = RuntimeStateStub(devices={})
    harness = Harness([charging_device()], load=-900, runtime_state=runtime)
    harness.controller.set_output_limit = FollowingHardware(item)
    harness.run(cycles=20, states=[item])

    assert harness.controller.charge_direction.charging is True
    assert item.ac_mode == 1

    runtime.devices["WR1"] = {"ac_charge_enabled": False}
    harness.run(cycles=1, states=[item])

    assert harness.controller.commanded_device_targets["WR1"] == 0
    assert harness.controller.charge_direction.charging is False
    assert item.ac_mode == 2
    assert item.grid_input == 0


def test_an_mqtt_control_device_charges_through_the_same_loop():
    """A whole transport that had no loop-level coverage.

    Everything else here builds a local-HTTP device. The MQTT control client is
    a different class with a different constructor, no `resolved_hardware_profile`
    (it carries a pinned one instead) and no state reconciliation -- and that
    last difference already hid one defect, the charge that was not stopped
    after a restart. So the path is walked once end to end.

    It also happens to be the test user's configuration: a 2400 AC on MQTT,
    whose own ceiling is 2400 W and which the installation limit holds to 1200.
    """

    from ems.mqtt_control.zendure_profiles import WRITE_PROFILE_ZENSDK_PROPERTIES
    from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient

    class ServiceStub:
        def publish(self, *args, **kwargs):
            return True

        def snapshot(self, *args, **kwargs):
            return None

    dev = ZendureMqttDeviceClient(
        name="WR1",
        service=ServiceStub(),
        device_id="ABC123",
        topic_family="zensdk_ha_scalar",
        source="zendure_cloud_mqtt",
        hardware_profile="solarflow_2400_ac",
        power_write_profile=WRITE_PROFILE_ZENSDK_PROPERTIES,
        ac_charge_enabled=True,
        max_charge_power_w=0,
        max_power=800,
        battery_kwh=2.0,
        min_soc=15,
        max_soc=100,
        smart_mode=1,
    )
    assert dev.supports_state_reconciliation is False
    assert not hasattr(dev, "resolved_hardware_profile")

    item = surplus_state()
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0
    item.charge_max_limit_w = 2400

    harness = Harness([dev], load=-900)
    harness.controller.set_output_limit = FollowingHardware(item)
    harness.run(cycles=20, states=[item])

    assert harness.controller.charge_direction.charging is True
    # The pinned profile is what resolves the model, and it charges.
    assert harness.controller.device_model_supports_charge(dev) is True
    # Its own ceiling is read from telemetry, then held to the installation limit.
    assert harness.controller.device_charge_limits["WR1"] == 2400
    assert harness.controller.commanded_device_targets["WR1"] == -1200
    assert item.ac_mode == 1


def test_the_regulator_state_stays_bounded_over_a_long_run():
    """Nothing here may grow with uptime.

    The entry window, the hourly entry list and the per-device maps are the only
    state the regulator keeps between cycles, and a surplus that comes and goes
    exercises all three. Four thousand cycles is about five and a half hours at
    the shipped loop interval.
    """

    class OscillatingMeter:
        def __init__(self):
            self.cycle = 0
            self.charge = 0

        def get_power(self):
            self.cycle += 1
            surplus = -400 if (self.cycle // 40) % 2 == 0 else 500
            return surplus + self.charge

    item = surplus_state()
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0

    harness = Harness([charging_device()], load=0)
    meter = OscillatingMeter()
    harness.controller.shelly = meter
    hardware = FollowingHardware(item)

    def respond(dev, value):
        meter.charge = -int(value) if int(value) < 0 else 0
        hardware(dev, value)

    harness.controller.set_output_limit = respond
    harness.run(cycles=4000, states=[item])

    direction = harness.controller.charge_direction
    assert len(direction.entry_window) <= 7
    assert len(direction.entries) <= 12
    assert len(harness.controller.silent_charge_cycles) == 1
    assert len(harness.controller.commanded_device_targets) == 1
    assert len(harness.controller.device_charge_limits) == 1


def test_the_control_view_explains_a_charge_as_a_charge():
    """"Why is this device at -1000 W?" was answered with "pv_first_allocation".

    The explanation is built by the discharge allocator, which runs first and
    against a requested total of zero while charging. Its numbers are replaced
    downstream; its reasons were not, so the Control tab named the strategy that
    did not decide it, for a direction it does not describe.
    """

    items = [surplus_state(), surplus_state()]
    for item in items:
        item.ac_mode = 2
        item.ac_status = 1
        item.output_limit = 0
        item.input_limit_w = 0
        item.grid_input = 0

    charging = charging_device("CHARGES")
    refused = charging_device("REFUSED")
    refused.ac_charge_enabled = False

    harness = Harness([charging, refused], load=-900)
    harness.run(cycles=20, states=items)

    assert harness.controller.charge_direction.charging is True
    explanation = harness.controller.last_control_explanation.to_dict()

    assert explanation["mode"] == "ac_charge"
    devices = explanation["devices"]
    assert devices["CHARGES"]["effective_target_w"] < 0
    assert devices["CHARGES"]["decision_reason"] == "ac_charge_allocation"
    # A device that may not charge says so, rather than inheriting a discharge
    # reason for a target of zero.
    assert devices["REFUSED"]["decision_reason"] == "ac_charge_not_permitted"


def test_explaining_a_charge_changes_no_target():
    """It only retells the decision. A wording pass that moved a watt would be
    a second place deciding what the devices do."""

    import inspect

    from ems.controller import EMSController

    source = inspect.getsource(EMSController.explain_charge_allocation)
    for forbidden in ("targets[", "commanded_device_targets", "device_charge_limits"):
        assert f"{forbidden} =" not in source, forbidden
    assert "return" in source
