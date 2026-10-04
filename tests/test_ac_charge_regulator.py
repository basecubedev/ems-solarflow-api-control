# SPDX-License-Identifier: AGPL-3.0-or-later
"""The charge regulator wired into the control loop.

Covers the properties that only hold once the pieces are connected: that the
feature changes nothing while it is off, that a real surplus eventually reaches
the hardware as a negative target, that the way back is never blocked, and that
the regulator leaves no trace in operator state.
"""

import logging
import time
from contextlib import nullcontext
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


class SteppedClock:
    """A monotonic clock that moves one loop interval per cycle and no further."""

    def __init__(self, start=10_000.0):
        self.now = start

    def __call__(self):
        return self.now


class Harness:
    """Runs the real control loop for N cycles against a fixed load.

    A device whose transport is a real client over a hardware double is polled
    through that client every cycle, as the live loop fetches it, so what the
    client remembers follows what the device reports. ``seconds_per_cycle``
    runs the loop on a stepped clock instead of the wall clock, for whatever is
    bounded by time rather than by cycles.
    """

    def __init__(
        self,
        devices,
        load,
        runtime_state=None,
        feature=True,
        min_output_limit=0,
        seconds_per_cycle=None,
    ):
        self.devices = devices
        self.min_output_limit = min_output_limit
        self.controller = EMSController(
            devices=devices,
            shelly=ShellyStub(load),
            sleep_enabled=False,
            runtime_state=runtime_state,
        )
        self.controller.run_startup_ac_mode_reconcile_once = Mock()
        self.controller.set_output_limit = Mock()
        self.feature = {**AC_CHARGE_CONTROL_DEFAULTS, "enabled": feature}
        self.seconds_per_cycle = seconds_per_cycle
        self.clock = SteppedClock()

    def fetch(self, states):
        for dev, item in zip(self.devices, states):
            if item is not None and polled_through_its_client(dev):
                dev.fetch()
        return states

    def run(self, cycles=1, states=None, load=None):
        if load is not None:
            self.controller.shelly = ShellyStub(load)
        states = states or [surplus_state() for _ in self.devices]
        clock = (
            patch("time.monotonic", self.clock)
            if self.seconds_per_cycle
            else nullcontext()
        )
        with patch(
            "ems.controller.fetch_all_devices",
            side_effect=lambda _devices: self.fetch(states),
        ), patch(
            "ems.controller.cfg.SYSTEM_ENABLED", True
        ), patch("ems.controller.cfg.MAX_TOTAL_POWER", 800), patch(
            "ems.controller.cfg.MAX_DEVICE_POWER", 800
        ), patch("ems.controller.cfg.MIN_OUTPUT_LIMIT", self.min_output_limit), patch(
            "ems.controller.cfg.DEADBAND", 10
        ), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0), patch.object(
            cfg, "AC_CHARGE_CONTROL_CONFIG", self.feature
        ), clock:
            for _ in range(cycles):
                self.controller.run_once()
                self.clock.now += self.seconds_per_cycle or 0
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

    harness = Harness([charging_device()], load=-900, min_output_limit=35)
    harness.run(cycles=12)

    negative = [target for target in harness.targets if target < 0]
    assert negative, harness.targets
    assert harness.targets[-1] < 0, harness.targets


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


class _HttpReply:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class FollowingHardware:
    """Telemetry that follows the command, the way a device does.

    Every other test here holds the state fixed, which hides anything that only
    goes wrong once the device actually reports what it was told.

    The device is told property writes, not targets. A target travels through
    the device's own client -- the local-API client a config device is in the
    live loop, or an MQTT control client -- which decides what goes on the wire
    and keeps the record of the EMS's own charge the controller reads, and the
    device honours the contract that wire format is written against: a
    write carrying ``acMode`` changes direction together with its setpoints,
    and without it an ``outputLimit`` is ignored in ``acMode = 1`` and an
    ``inputLimit`` in ``acMode = 2``. The first version switched direction on
    any target, and so hid an exit from a charge that was a bare
    ``outputLimit``: on hardware only the state reconciler, behind its own
    gate, ever ended that charge.

    It reports ``smartMode`` and keeps the contract a device in ``smartMode =
    0`` keeps: a setpoint written without the mode is ignored. Three more
    things a device does can be switched on, because each hid a defect while
    the double could not do it:

    ``switch_polls``
        A change of direction is reported as written at once, but that many
        polls still show the current of the direction it left -- the ~2 s
        settling window.
    ``ignore(predicate)``
        The next write the predicate matches is accepted and not applied, the
        way a device answers 200 to a command it then does not carry out.
    ``firmware_charge_w``
        At the battery floor the firmware puts the device back into its own
        protection charge at that power, ``firmware_reentry_polls`` polls after
        the device left one.
    ``apply_after_polls``
        A write lands that many polls after it was sent, the way a command
        travels through a broker to the device; until then the device reports
        what it did before.
    ``unreported``
        Properties the device leaves out of its report, the way a model that
        has no ``inputLimit`` or ``acStatus`` field does.
    ``input_ceiling_w``
        The device clamps a written ``inputLimit`` to this and reports the
        clamped value, the way a device bounds a charge by its own limit.

    A poll is one telemetry period: one fetch of the device through its client.
    """

    def __init__(
        self,
        item,
        *,
        switch_polls=0,
        firmware_charge_w=None,
        firmware_reentry_polls=1,
        apply_after_polls=0,
        unreported=(),
        input_ceiling_w=None,
    ):
        self.state = item
        self.writes = []
        self.unapplied = []
        self._ignored = []
        self.switch_polls = switch_polls
        self._settling = None
        self.firmware_charge_w = firmware_charge_w
        self.firmware_reentry_polls = firmware_reentry_polls
        self._polls_out_of_charge = 0
        self.apply_after_polls = apply_after_polls
        self._in_transit = []
        self.unreported = frozenset(unreported)
        self.input_ceiling_w = input_ceiling_w

    def __call__(self, dev, value, charge_exit=None):
        if charge_exit is None:
            return bool(dev.dispatch_output_limit(int(value)))
        return bool(dev.dispatch_output_limit(int(value), charge_exit=charge_exit))

    def ignore(self, predicate, times=1):
        self._ignored.append([predicate, times])

    def report(self):
        item = self.state
        report = {
            "acMode": item.ac_mode,
            "acStatus": item.ac_status,
            "smartMode": item.smart_mode,
            "outputLimit": item.output_limit,
            "inputLimit": item.input_limit_w,
            "gridInputPower": item.grid_input,
        }
        return {key: value for key, value in report.items() if key not in self.unreported}

    def get(self, url, **_kwargs):
        self.poll()
        return _HttpReply({"properties": self.report()})

    def post(self, url, json=None, **_kwargs):
        self.receive(json["properties"])
        return _HttpReply({"success": True})

    def receive(self, properties):
        if not self.apply_after_polls:
            self.apply(properties)
            return
        self._in_transit.append([self.apply_after_polls, dict(properties)])

    def _deliver(self):
        for entry in self._in_transit:
            entry[0] -= 1
        while self._in_transit and self._in_transit[0][0] <= 0:
            self.apply(self._in_transit.pop(0)[1])

    def poll(self):
        self._deliver()
        if self._settling == 0:
            self._settling = None
            self._settle()
        elif self._settling is not None:
            self._settling -= 1
        self._firmware_poll()

    def apply(self, properties):
        self.writes.append(dict(properties))
        for entry in self._ignored:
            predicate, times = entry
            if times and predicate(properties):
                entry[1] -= 1
                self.unapplied.append(dict(properties))
                return
        item = self.state
        if "smartMode" in properties:
            item.smart_mode = int(properties["smartMode"])
        if item.smart_mode != 1:
            return
        switches = "acMode" in properties
        direction_before = item.ac_mode
        if switches:
            item.ac_mode = int(properties["acMode"])
        charging = item.ac_mode == 1
        if "inputLimit" in properties and (switches or charging):
            item.input_limit_w = int(properties["inputLimit"])
            if self.input_ceiling_w is not None:
                item.input_limit_w = min(item.input_limit_w, self.input_ceiling_w)
        if "outputLimit" in properties and (switches or not charging):
            item.output_limit = int(properties["outputLimit"])
        self._follow(direction_before)

    def _follow(self, direction_before):
        if self.switch_polls and self.state.ac_mode != direction_before:
            self._settling = self.switch_polls
            return
        self._settle()

    def _settle(self):
        item = self.state
        drawing = item.ac_mode == 1 and item.input_limit_w > 0
        item.ac_status = 2 if drawing else 1
        item.grid_input = item.input_limit_w if drawing else 0

    def _firmware_poll(self):
        item = self.state
        if self.firmware_charge_w is None or item.soc > item.min_soc:
            return
        if item.ac_status == 2 or self._settling is not None:
            self._polls_out_of_charge = 0
            return
        if self._polls_out_of_charge < self.firmware_reentry_polls:
            self._polls_out_of_charge += 1
            return
        self._polls_out_of_charge = 0
        direction_before = item.ac_mode
        item.ac_mode = 1
        item.input_limit_w = self.firmware_charge_w
        self._follow(direction_before)


class FollowingBroker:
    """The MQTT service of a device that is a hardware double.

    Publishes land on the double, at once or as many polls later as the
    double's ``apply_after_polls`` says; ``published`` keeps every payload in
    the order it went out. A snapshot is one poll of the double, stamped with
    the clock the client reads, and carries only what the double reports.
    """

    def __init__(self, hardware):
        self.hardware = hardware
        self.published = []

    def publish_message(self, message):
        import json

        properties = json.loads(message.payload)["properties"]
        self.published.append(properties)
        self.hardware.receive(properties)
        return True

    def snapshot_status(self, device_id, **_kwargs):
        from types import SimpleNamespace

        from ems.zendure_mqtt.service import classify_snapshot

        self.hardware.poll()
        now = time.monotonic()
        metrics = self.hardware.report()
        snapshot = SimpleNamespace(
            metrics=metrics,
            last_seen_monotonic=now,
            metric_monotonic={key: now for key in metrics},
            battery_packs=None,
        )
        return classify_snapshot(snapshot, 60.0, now_monotonic=now)


def polled_through_its_client(dev):
    """Whether the harness reads this device through its own client each cycle."""

    if isinstance(getattr(dev, "session", None), FollowingHardware):
        return True
    return isinstance(getattr(dev, "_service", None), FollowingBroker)


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

    dev, hardware = following_device(item)
    harness = Harness([dev], load=-900)
    harness.controller.device_state_writes_allowed = lambda dev: True
    harness.controller.set_output_limit = hardware

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

    dev, hardware = following_device(item)
    harness = Harness([dev], load=-900)
    harness.controller.device_state_writes_allowed = lambda dev: True
    written = []

    def record(dev, value, **kwargs):
        written.append(int(value))
        hardware(dev, value, **kwargs)

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

    dev, hardware = following_device(item)
    harness = Harness([dev], load=-200)
    meter = RespondingMeter(-200)
    harness.controller.shelly = meter

    def respond(dev, value, **kwargs):
        meter.charge = -int(value) if int(value) < 0 else 0
        hardware(dev, value, **kwargs)

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

    a, hardware_a = following_device(items[0], name="A")
    b, hardware_b = following_device(items[1], name="B")
    harness = Harness([a, b], load=-1200)
    harness.controller.set_output_limit = lambda dev, value, **kwargs: (
        hardware_a if dev.name == "A" else hardware_b
    )(dev, value, **kwargs)

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

    dev, hardware = following_device(item)
    harness = Harness([dev], load=-900)
    harness.controller.set_output_limit = hardware
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
    dev, hardware = following_device(item)
    harness = Harness([dev], load=-900, runtime_state=runtime)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])

    assert harness.controller.charge_direction.charging is True
    assert item.ac_mode == 1

    runtime.devices["WR1"] = {"ac_charge_enabled": False}
    harness.run(cycles=1, states=[item])

    assert harness.controller.commanded_device_targets["WR1"] == 0
    assert harness.controller.charge_direction.charging is False
    assert item.ac_mode == 2
    assert item.grid_input == 0


def idle_output_state():
    item = surplus_state()
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0
    return item


def charge_on_following_hardware(**harness_kwargs):
    item = idle_output_state()
    dev, hardware = following_device(item)
    harness = Harness([dev], load=-900, **harness_kwargs)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])
    assert harness.controller.charge_direction.charging is True
    assert item.ac_mode == 1
    return harness, hardware, item


def test_the_way_back_needs_no_state_reconciliation():
    """Leaving a charge is the power command's job, on the power command's gate.

    The local-API exit was a bare outputLimit, which a device in acMode 1
    ignores. The charge then ended only when the state reconciler wrote acMode
    back -- and with allow_state_reconciliation_writes off it never ended: the
    EMS wrote outputLimit 800 into a device that went on drawing from the grid.
    """

    harness, hardware, item = charge_on_following_hardware()
    harness.controller.device_state_writes_allowed = lambda dev: False

    harness.run(cycles=1, states=[item], load=1200)

    assert harness.controller.charge_direction.charging is False
    assert item.ac_mode == 2
    assert item.grid_input == 0
    assert hardware.writes[-1]["acMode"] == 2
    assert hardware.writes[-1]["inputLimit"] == 0


def test_a_shutdown_release_actually_releases():
    """The release writes once and the process exits; nothing comes after it.

    `ac_charge_released_on_shutdown` was logged over a bare outputLimit 0, so a
    device left by --once, --max-cycles or an unhandled error kept charging.
    """

    harness, hardware, item = charge_on_following_hardware()

    harness.controller.release_charging_devices()

    assert item.ac_mode == 2
    assert item.grid_input == 0
    assert hardware.writes[-1] == {
        "smartMode": 1, "acMode": 2, "outputLimit": 0, "inputLimit": 0,
    }


IDLE_EXIT = {"smartMode": 1, "acMode": 2, "outputLimit": 0, "inputLimit": 0}


def pv_less_state(soc):
    """A device with no PV of its own, idle in the output direction."""

    item = state(soc=soc, min_soc=15, solar=0, output=0, soc_limit=0, pack_num=2)
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0
    return item


def start_charging(item):
    item.ac_mode = 1
    item.ac_status = 2
    item.input_limit_w = 800
    item.grid_input = 800


def following_http_device(
    item, name="WR1", hardware_profile="solarflow_2400_ac", **hardware_options
):
    """A real local-API client whose device is the hardware double.

    What the client last put on the wire stays with the client, as on a live
    system, so a test sees the same record the controller reads.
    """

    from ems.clients import ZendureClient

    hardware = FollowingHardware(item, **hardware_options)
    dev = ZendureClient(
        name, "192.0.2.10", f"{name}-SN", hardware, 15, 100, 1, None, 800,
        hardware_profile=hardware_profile,
    )
    return dev, hardware


def following_device(item, name="WR1", **hardware_options):
    """``charging_device`` as the live loop builds it: its own local-API client."""

    return following_http_device(
        item, name=name, hardware_profile="solarflow_800_pro_2", **hardware_options
    )


def floor_harness(item, load, **harness_options):
    dev, hardware = following_http_device(item)
    harness = Harness([dev], load=load, **harness_options)
    harness.controller.set_output_limit = hardware
    return harness, hardware


def test_a_running_charge_moves_its_power_with_one_value_in_the_closed_loop():
    """The bare ``inputLimit`` path, reached through the loop rather than a stub.

    The local-API client sends only the power inside a charge it started once
    its own report shows the charge mode with ``smartMode = 1``. The hardware
    double reported no ``smartMode``, so no closed-loop test ever reached that
    branch: every step of the ramp went out as the whole set. Here the mode
    also takes a poll to settle, and the ramp still moves only the power.
    """

    item = pv_less_state(soc=50)
    dev, hardware = following_http_device(item, switch_polls=1)
    harness = Harness([dev], load=-300)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])

    entry, *ramp = hardware.writes
    assert entry == {"smartMode": 1, "acMode": 1, "outputLimit": 0, "inputLimit": 200}
    assert ramp, "the charge power never moved"
    assert all(set(write) == {"inputLimit"} for write in ramp), ramp
    assert item.ac_mode == 1
    assert item.grid_input == ramp[-1]["inputLimit"]


@pytest.mark.parametrize("standby_floor", [0, 35], ids=["regulated", "night_idle"])
def test_a_charge_found_at_the_floor_after_a_restart_is_ended_once(standby_floor):
    """Owner decision 2026-10-04, the restart case.

    A process that starts and finds a device charging at its floor cannot tell
    the firmware's protection charge from its own predecessor's charge, left
    drawing from the grid. Claiming it for the firmware left the second running
    unsupervised. The exit is written once; a device that charges on after it is
    the firmware's, and nothing more is written until that charge ends. The
    night idle's park write is that exit too.
    """

    item = pv_less_state(soc=15)
    start_charging(item)
    harness, hardware = floor_harness(item, load=300, min_output_limit=standby_floor)

    harness.run(cycles=1, states=[item])

    assert hardware.writes == [{**IDLE_EXIT, "outputLimit": standby_floor}]
    assert item.ac_mode == 2

    start_charging(item)
    harness.run(cycles=5, states=[item])

    assert hardware.writes == [{**IDLE_EXIT, "outputLimit": standby_floor}]
    assert harness.controller.runtime_intents["WR1"].reason == "firmware_owned_charge"


@pytest.mark.parametrize("transport", ["http", "mqtt"])
@pytest.mark.parametrize("unreadable", ["every_sixth_read", "offline_spell"])
def test_a_failed_read_does_not_forget_whose_charge_runs_at_the_floor(
    transport, unreadable
):
    """Only a restart makes a floor charge unproven.

    One failed read dropped the device from the set of devices the EMS had
    watched, so the firmware's protection charge read as unattributable on the
    next good read and got the exit again: seven full exits in forty cycles
    with one read in six lost. The EMS's own record survives an outage on the
    transport, so nothing the EMS charged can start in the gap unseen.
    """

    build = following_http_device if transport == "http" else following_mqtt_device
    item = pv_less_state(soc=15)
    start_charging(item)
    dev, hardware = build(item, firmware_charge_w=300, firmware_reentry_polls=1)
    harness = Harness([dev], load=300, seconds_per_cycle=5)
    harness.controller.set_output_limit = hardware

    def readable(cycle):
        if unreadable == "every_sixth_read":
            return cycle % 6 != 5
        return not 10 <= cycle < 16

    for cycle in range(40):
        harness.run(cycles=1, states=[item] if readable(cycle) else [None])

    assert [write for write in hardware.writes if leaves_the_charge(write)] == [
        IDLE_EXIT
    ]
    assert item.grid_input == 300, "the firmware's charge was not running"
    assert harness.controller.runtime_intents["WR1"].reason == "firmware_owned_charge"


@pytest.mark.parametrize("refused_by", ["feature", "device_switch", "model"])
def test_a_floor_charge_the_ems_could_not_have_started_is_the_firmware_s(refused_by):
    """The one exit is for a charge that may be the EMS's own.

    A device the EMS can never charge -- the feature off, its own switch off, a
    model with no AC charge path -- cannot be left charging by an EMS before a
    restart. Its charge at the floor is the firmware's from the first cycle,
    and writing the exit into it was the fight the owner's decision rules out.
    """

    item = pv_less_state(soc=15)
    start_charging(item)
    harness, hardware = floor_harness(
        item, load=300, feature=refused_by != "feature"
    )
    dev = harness.devices[0]
    if refused_by == "device_switch":
        dev.ac_charge_enabled = False
    if refused_by == "model":
        dev.hardware_profile = "solarflow_800"

    harness.run(cycles=4, states=[item])

    assert hardware.writes == []
    assert harness.controller.runtime_intents["WR1"].reason == "firmware_owned_charge"


def test_a_device_someone_else_holds_in_ac_input_gets_no_mode_writes():
    """A fleet the EMS never charged keeps main's writes.

    The vendor app holds a device in AC input above the floor, the EMS has never
    charged it, and state reconciliation is off. Every non-negative target used
    to go out as the whole set -- smartMode and acMode under the power gate
    alone, the writes the operator had switched off. Only ``outputLimit`` is the
    power command's to write there; taking the device back is the reconciler's.
    """

    item = pv_less_state(soc=50)
    item.ac_mode = 1
    item.input_limit_w = 400
    item.grid_input = 400
    item.ac_status = 2
    harness, hardware = floor_harness(item, load=300)
    harness.controller.device_state_writes_allowed = lambda _dev: False

    harness.run(cycles=6, states=[item])

    assert hardware.writes, "the power command wrote nothing at all"
    assert all(set(write) == {"outputLimit"} for write in hardware.writes), hardware.writes


class KeepsIgnoredSettings(FollowingHardware):
    """A device that stores a setpoint it does not act on, and reports it."""

    def apply(self, properties):
        ignored_before = len(self.unapplied)
        super().apply(properties)
        taken = len(self.unapplied) == ignored_before and self.state.smart_mode == 1
        if taken and "outputLimit" in properties:
            self.state.output_limit = int(properties["outputLimit"])


def test_a_bare_write_is_not_repeated_against_a_charge_it_cannot_end():
    """The deadband measures a target against what the write can change.

    A charging device was compared by the AC input it draws, so that "switch
    this device off" is not read as no change. Against a charge the local API
    cannot end with a bare ``outputLimit`` that comparison never settles: the
    same value went out every cycle to a device the vendor app holds in AC
    input, where the code before charging wrote it once.
    """

    item = pv_less_state(soc=50)
    item.ac_mode = 1
    item.input_limit_w = 400
    item.grid_input = 400
    item.ac_status = 2
    dev, _ = following_http_device(item)
    hardware = KeepsIgnoredSettings(item)
    dev.session = hardware
    harness = Harness([dev], load=300)
    harness.controller.set_output_limit = hardware
    harness.controller.device_state_writes_allowed = lambda _dev: False

    harness.run(cycles=8, states=[item])

    values = [write["outputLimit"] for write in hardware.writes]
    assert values and len(values) == len(set(values)), values


def test_a_floor_charge_the_ems_saw_begin_is_left_to_the_firmware():
    """Where the EMS can tell, it does not touch the firmware's charge at all."""

    item = pv_less_state(soc=15)
    harness, hardware = floor_harness(item, load=300)
    harness.run(cycles=2, states=[item])
    before = list(hardware.writes)

    start_charging(item)
    harness.run(cycles=5, states=[item])

    assert hardware.writes == before
    assert harness.controller.runtime_intents["WR1"].reason == "firmware_owned_charge"


def test_a_firmware_charge_settling_in_is_not_fought_by_the_reconciler():
    """The firmware writes its mode first; current follows about two seconds on.

    A poll inside that window sees ``acMode = 1`` with a setpoint and nothing
    flowing yet. The firmware claim waited for the status, so the state
    reconciler took the device for an ordinary one in the wrong mode and wrote
    a bare ``acMode = 2`` into the firmware's protection charge -- the fight the
    owner's decision rules out, and a relay move each time the poll landed
    there.
    """

    item = pv_less_state(soc=15)
    harness, hardware = floor_harness(item, load=300)
    harness.controller.device_state_writes_allowed = lambda dev: True
    reconciled = []
    with patch(
        "ems.controller.write_device_properties",
        side_effect=lambda dev, properties, **kw: reconciled.append(properties) or True,
    ):
        harness.run(cycles=2, states=[item])
        before = list(hardware.writes)

        item.ac_mode = 1
        item.input_limit_w = 800
        harness.run(cycles=1, states=[item])
        assert [p for p in reconciled if "acMode" in p] == []

        start_charging(item)
        harness.run(cycles=3, states=[item])

    assert [p for p in reconciled if "acMode" in p] == []
    assert hardware.writes == before
    assert harness.controller.runtime_intents["WR1"].reason == "firmware_owned_charge"


def test_the_ems_own_charge_at_the_floor_is_still_its_own_after_an_absence():
    """A device back from offline mid-charge is not the firmware's to keep.

    While it was unreachable the regulator's target for it fell to zero, which
    was the only record of the charge: on its return the charge read as the
    firmware's and the EMS left it drawing with nobody watching. What the
    transport last put on the wire survives an absence.
    """

    item = pv_less_state(soc=15)
    harness, hardware = floor_harness(item, load=-900)
    harness.run(cycles=20, states=[item])
    assert item.ac_mode == 1, "the regulator never charged the device"

    harness.run(cycles=4, states=[None], load=300)
    harness.run(cycles=1, states=[item], load=300)

    assert item.ac_mode == 2
    assert item.grid_input == 0
    assert hardware.writes[-1]["acMode"] == 2


@pytest.mark.parametrize("soc", [50, 15])
def test_switching_control_back_on_does_not_end_a_running_charge(soc):
    """Re-enabling control resets the output memory, not the charge.

    The reset cleared the regulator's only record of its own charge and seeded
    the total from the devices' output, which is zero for a charging device.
    The next cycle read the balanced meter as no surplus and ended the charge
    -- a relay round trip above min SoC -- and at min SoC handed it to the
    firmware claim, drawing unsupervised.
    """

    item = pv_less_state(soc=soc)
    harness, hardware = floor_harness(item, load=-900)
    harness.run(cycles=20, states=[item])
    assert item.ac_mode == 1, "the regulator never charged the device"
    controller = harness.controller
    controller.device_state_writes_allowed = lambda dev: True
    before = len(hardware.writes)

    controller.note_control_enabled(False)
    controller.note_control_enabled(True)
    reconciled = []
    with patch(
        "ems.controller.write_device_properties",
        side_effect=lambda dev, properties, **kw: reconciled.append(properties) or True,
    ):
        harness.run(cycles=2, states=[item], load=0)

    assert controller.charge_direction.charging is True
    assert controller.runtime_intents["WR1"].reason == REGULATOR_CHARGE_REASON
    assert item.ac_mode == 1
    assert all(write["acMode"] == 1 for write in hardware.writes[before:])
    assert [p for p in reconciled if "acMode" in p] == []


def following_mqtt_device(item, name="WR1", **hardware_options):
    """A real MQTT control client whose broker delivers to the hardware double."""

    from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
    from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

    hardware = FollowingHardware(item, **hardware_options)

    dev = ZendureMqttDeviceClient(
        name=name,
        service=FollowingBroker(hardware),
        device_id="DEV1",
        topic_family=FAMILY_LEGACY_JSON,
        product_key="PK",
        source="local_mqtt",
        hardware_profile="solarflow_2400_ac",
        max_power=800,
        min_soc=15,
        max_soc=100,
        smart_mode=1,
    )
    return dev, hardware


@pytest.mark.parametrize("transport", ["http", "mqtt"])
@pytest.mark.parametrize("switch", ["system", "device"])
def test_disabling_a_charging_device_ends_its_charge_once(transport, switch):
    """Owner decision 2026-10-04: one final command, then nothing.

    Disabling means the EMS writes nothing more, and a device it had put into
    charge then went on drawing from the grid: an MQTT device indefinitely, a
    local-API device only until the state reconciler happened to write acMode
    back, and on `system.enabled = false` nothing was written at all. The one
    command that ends the charge is the exit to idle; after it, nothing.
    """

    build = following_http_device if transport == "http" else following_mqtt_device
    item = pv_less_state(soc=50)
    dev, hardware = build(item)
    runtime = RuntimeStateStub(devices={})
    harness = Harness([dev], load=-900, runtime_state=runtime)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])
    assert item.ac_mode == 1, "the regulator never charged the device"
    before = len(hardware.writes)

    if switch == "system":
        runtime.system["enabled"] = False
    else:
        runtime.devices["WR1"] = {"enabled": False}
    harness.run(cycles=6, states=[item])

    assert hardware.writes[before:] == [IDLE_EXIT]
    assert item.ac_mode == 2
    assert item.grid_input == 0


def leaves_the_charge(properties):
    return properties.get("acMode") == 2


@pytest.mark.parametrize("transport", ["http", "mqtt"])
@pytest.mark.parametrize("switch", ["system", "device"])
def test_a_final_command_the_device_did_not_apply_is_sent_again(transport, switch):
    """The one final command counts when the device leaves the charge.

    It counted when the transport accepted it: a device that answered and did
    not apply it went on drawing 800-1200 W with nobody watching, the MQTT
    client logging the acMode mismatch it would never act on. The exit goes out
    again once the resend window has passed -- not every cycle -- and once the
    device reports it out of the charge, nothing more is written.
    """

    build = following_http_device if transport == "http" else following_mqtt_device
    item = pv_less_state(soc=50)
    dev, hardware = build(item, switch_polls=1)
    runtime = RuntimeStateStub(devices={})
    harness = Harness([dev], load=-900, runtime_state=runtime, seconds_per_cycle=5)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])
    assert item.ac_mode == 1, "the regulator never charged the device"
    before = len(hardware.writes)
    hardware.ignore(leaves_the_charge)

    if switch == "system":
        runtime.system["enabled"] = False
    else:
        runtime.devices["WR1"] = {"enabled": False}
    harness.run(cycles=6, states=[item])

    assert hardware.writes[before:] == [IDLE_EXIT]
    assert item.grid_input > 0
    assert dev.charge_commanded is True

    harness.run(cycles=1, states=[item])

    assert hardware.writes[before:] == [IDLE_EXIT, IDLE_EXIT]

    harness.run(cycles=12, states=[item])

    assert hardware.writes[before:] == [IDLE_EXIT, IDLE_EXIT]
    assert item.ac_mode == 2
    assert item.grid_input == 0
    assert dev.charge_commanded is False


@pytest.mark.parametrize("transport", ["http", "mqtt"])
@pytest.mark.parametrize("standby_floor", [0, 35], ids=["regulated", "night_idle"])
@pytest.mark.parametrize("switch_polls", [0, 1], ids=["instant", "settling"])
def test_the_ems_s_own_charge_at_the_floor_is_not_the_firmware_s_while_it_runs_on(
    transport, standby_floor, switch_polls
):
    """A lost exit at the battery floor is not a firmware protection charge.

    The exit counted as delivered when the transport took it, so a device still
    charging at the EMS's own setpoint read as the firmware's charge one cycle
    later, and the EMS's 600 W ran on unsupervised. While the device has not
    been seen out of it, it is the EMS's charge, and the exit is asked again --
    also by the night idle, which parks a device once and then writes nothing.
    """

    build = following_http_device if transport == "http" else following_mqtt_device
    item = pv_less_state(soc=15)
    dev, hardware = build(item, switch_polls=switch_polls)
    harness = Harness(
        [dev], load=-900, seconds_per_cycle=5, min_output_limit=standby_floor
    )
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])
    assert item.ac_mode == 1, "the regulator never charged the device"
    before = len(hardware.writes)
    hardware.ignore(leaves_the_charge)

    reasons = []
    idle = []
    for _ in range(12):
        harness.run(cycles=1, states=[item], load=300)
        reasons.append(harness.controller.runtime_intents["WR1"].reason)
        idle.append(harness.controller.night_min_soc_idle_active)

    assert any(idle) is bool(standby_floor)
    assert "firmware_owned_charge" not in reasons, reasons
    assert len(hardware.unapplied) == 1
    exits = [write for write in hardware.writes[before:] if leaves_the_charge(write)]
    assert len(exits) == 2
    assert item.ac_mode == 2
    assert item.grid_input == 0
    assert dev.charge_commanded is False


@pytest.mark.parametrize("transport", ["http", "mqtt"])
def test_a_device_settling_out_of_a_charge_it_left_is_not_written_again(transport):
    """The current of a charge the device already left is not a charge.

    For about two seconds after it takes the exit a device reports the output
    direction written and ``inputLimit`` 0 while current still flows. The write
    deadband measured the target against that current, read a running charge,
    and sent the exit again -- over MQTT the whole set, smartMode included,
    whenever a poll landed in that window.
    """

    build = following_http_device if transport == "http" else following_mqtt_device
    item = pv_less_state(soc=50)
    dev, hardware = build(item, switch_polls=1)
    runtime = RuntimeStateStub(devices={})
    harness = Harness([dev], load=-900, runtime_state=runtime)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])
    assert item.ac_mode == 1, "the regulator never charged the device"
    before = len(hardware.writes)

    runtime.devices["WR1"] = {"ac_charge_enabled": False}
    harness.run(cycles=1, states=[item])
    assert hardware.writes[before:] == [IDLE_EXIT]

    harness.run(cycles=1, states=[item])
    assert hardware.writes[before:] == [IDLE_EXIT]
    harness.run(cycles=3, states=[item])
    assert hardware.writes[before:] == [IDLE_EXIT]
    assert item.ac_status == 1 and item.grid_input == 0


def maintenance_claim_without_a_charge(dev):
    from ems.runtime_intents import (
        PRIORITY_MAINTENANCE,
        DeviceRuntimeIntent,
        DeviceRuntimeRole,
    )

    return DeviceRuntimeIntent(
        device=dev.name,
        role=DeviceRuntimeRole.AC_OUTPUT,
        reason="battery_full_charge_assist",
        desired_ac_mode=None,
        output_control_allowed=False,
        priority=PRIORITY_MAINTENANCE,
    )


@pytest.mark.parametrize("transport", ["http", "mqtt"])
@pytest.mark.parametrize("claim", ["operator_park", "maintenance"])
def test_a_claim_on_a_charging_device_ends_the_ems_s_charge_once(transport, claim):
    """Taking a device from the regulator is no licence to leave it drawing.

    A park or a maintenance claim stops every power command to the device, and
    one that commands no charge of its own left the regulator's last
    ``inputLimit`` in place: the device drew from the grid, unsupervised, until
    its SoC ceiling. The owner's decision for disabling applies: one final
    command ends the EMS's own charge, then nothing. A park holds the device in
    AC input, so on the local API, where that role is the reconciler's to keep,
    the command ends the charge by its setpoint and leaves the mode alone.
    """

    build = following_http_device if transport == "http" else following_mqtt_device
    item = pv_less_state(soc=50)
    dev, hardware = build(item)
    runtime = RuntimeStateStub(devices={})
    harness = Harness([dev], load=-900, runtime_state=runtime)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])
    assert item.ac_mode == 1, "the regulator never charged the device"
    before = len(hardware.writes)

    if claim == "operator_park":
        runtime.devices["WR1"] = {
            "runtime_role": "ac_input",
            "runtime_role_reason": "emsctl",
        }
    else:
        harness.controller.full_charge_assist_intent = maintenance_claim_without_a_charge
    harness.run(cycles=6, states=[item])

    in_place = claim == "operator_park" and transport == "http"
    assert hardware.writes[before:] == [{"inputLimit": 0} if in_place else IDLE_EXIT]
    assert item.grid_input == 0
    assert dev.charge_commanded is False


@pytest.mark.parametrize(
    "transport, state_writes, owned",
    [("http", True, True), ("http", False, False), ("mqtt", True, False)],
)
def test_a_claim_owns_the_charge_only_where_it_can_write_its_own_power(
    transport, state_writes, owned
):
    """An AC-input role with its own power owns ``inputLimit`` from then on.

    Only where that power reaches the device: over MQTT there is no state
    reconciliation, and with its gate shut nothing writes it, so the device
    would go on at the regulator's last value. There the charge is ended once,
    like any other claim.
    """

    build = following_http_device if transport == "http" else following_mqtt_device
    item = pv_less_state(soc=50)
    dev, hardware = build(item)
    runtime = RuntimeStateStub(devices={})
    harness = Harness([dev], load=-900, runtime_state=runtime)
    harness.controller.set_output_limit = hardware
    harness.controller.device_state_writes_allowed = lambda _dev: state_writes
    harness.run(cycles=20, states=[item])
    assert item.ac_mode == 1, "the regulator never charged the device"
    before = len(hardware.writes)

    runtime.devices["WR1"] = {
        "runtime_role": "ac_input",
        "runtime_role_reason": "emsctl",
        "ac_charge_power_w": 400,
    }
    harness.run(cycles=6, states=[item])

    if owned:
        assert hardware.writes[before:] == [{"inputLimit": 400}]
        assert item.grid_input == 400
    elif transport == "http":
        assert hardware.writes[before:] == [{"inputLimit": 0}]
        assert item.grid_input == 0
    else:
        assert hardware.writes[before:] == [IDLE_EXIT]
        assert item.grid_input == 0


def charge_then_claim(claim, state_writes=True):
    item = pv_less_state(soc=50)
    dev, hardware = following_http_device(item)
    runtime = RuntimeStateStub(devices={})
    harness = Harness([dev], load=-900, runtime_state=runtime)
    harness.controller.set_output_limit = hardware
    harness.controller.device_state_writes_allowed = lambda _dev: state_writes
    harness.run(cycles=20, states=[item])
    assert item.ac_mode == 1, "the regulator never charged the device"
    before = len(hardware.writes)
    runtime.devices["WR1"] = claim
    harness.run(cycles=3, states=[item])
    return harness, hardware, item, runtime, before


@pytest.mark.parametrize("then", ["system_disabled", "device_disabled", "ems_stops"])
def test_a_claim_that_charges_on_its_own_power_keeps_it(then):
    """Taking the charge over is taking its ownership.

    An AC-input park with its own charge power owns ``inputLimit`` from the
    cycle it takes the device. The EMS's record of its own charge stayed set,
    so switching control off ended the operator's charge -- the exit, then the
    reconciler putting acMode 1 and the power back a cycle later: two relay
    moves and a gap -- and a stop of the EMS would have ended it for good.
    """

    claim = {
        "runtime_role": "ac_input",
        "runtime_role_reason": "dashboard",
        "ac_charge_power_w": 500,
    }
    harness, hardware, item, runtime, before = charge_then_claim(claim)
    dev = harness.devices[0]
    assert hardware.writes[before:] == [{"inputLimit": 500}]
    assert dev.charge_commanded is False

    if then == "system_disabled":
        runtime.system["enabled"] = False
        harness.run(cycles=3, states=[item])
    elif then == "device_disabled":
        runtime.devices["WR1"] = {**claim, "enabled": False}
        harness.run(cycles=3, states=[item])
    else:
        harness.controller.release_charging_devices()

    assert hardware.writes[before:] == [{"inputLimit": 500}]
    assert item.ac_mode == 1
    assert item.grid_input == 500
    assert harness.controller.charges_held_by_ems() == {}


def test_a_park_as_ac_input_ends_the_ems_s_charge_without_moving_the_relay():
    """The park's role is AC input; ending the EMS's charge keeps it there.

    The exit to idle wrote acMode 2, and the state reconciler wrote acMode 1
    back for the role one cycle later: two relay moves for a device that was
    in the right mode all along. Ending the charge by its setpoint is enough.
    """

    claim = {"runtime_role": "ac_input", "runtime_role_reason": "emsctl"}
    harness, hardware, item, _runtime, before = charge_then_claim(claim)
    harness.run(cycles=3, states=[item])

    assert hardware.writes[before:] == [{"inputLimit": 0}]
    assert item.ac_mode == 1
    assert item.grid_input == 0
    assert harness.devices[0].charge_commanded is False


def test_a_disabled_device_s_charge_ends_while_the_rest_idles_at_night():
    """The night idle ends the cycle before any write path is reached.

    A disabled device no longer counts among the controllable ones. When it was
    unreachable as it was disabled, the others enter the idle meanwhile, and the
    final command it is still owed falls into a cycle the idle ends early.
    """

    floor = state(
        soc=15, min_soc=15, solar=0, output=0, output_limit=35, pack_in=0,
        pack_out=0, soc_limit=2, dc_status=0, ac_status=0, pack_state=0,
    )
    item = pv_less_state(soc=50)
    charging, hardware = following_http_device(item, name="B")
    parked = charging_device("A", ac_charge_enabled=False)
    runtime = RuntimeStateStub(devices={})
    harness = Harness(
        [parked, charging], load=-900, runtime_state=runtime, min_output_limit=35
    )
    harness.controller.set_output_limit = (
        lambda dev, value: hardware(dev, value) if dev.name == "B" else True
    )
    harness.run(cycles=20, states=[floor, item])
    assert item.ac_mode == 1, "the regulator never charged the device"
    before = len(hardware.writes)

    runtime.devices["B"] = {"enabled": False}
    harness.run(cycles=3, states=[floor, None], load=300)
    assert harness.controller.night_min_soc_idle_active is True
    assert hardware.writes[before:] == []

    harness.run(cycles=2, states=[floor, item], load=300)

    assert harness.controller.night_min_soc_idle_active is True
    assert hardware.writes[before:] == [IDLE_EXIT]


def test_disabling_a_device_that_is_not_charging_writes_nothing():
    item = pv_less_state(soc=50)
    dev, hardware = following_http_device(item)
    runtime = RuntimeStateStub(devices={})
    harness = Harness([dev], load=300, runtime_state=runtime)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=3, states=[item])
    before = len(hardware.writes)

    runtime.system["enabled"] = False
    harness.run(cycles=3, states=[item])

    assert hardware.writes[before:] == []


def discharge_forbidden(item, name="WR1"):
    dev, hardware = following_http_device(item, name=name)
    dev.ac_discharge_enabled = False
    return dev, hardware


def test_a_device_forbidden_to_discharge_still_charges():
    """`ac_discharge_enabled` forbids one direction, not both.

    It blocked every power write while the device still counted as chargeable:
    the direction entered, the total wound down to the charge floor, and nothing
    was ever written -- with `ac_charge_not_delivered` silent, because the ramp
    had zeroed the very target it watches.
    """

    item = pv_less_state(soc=50)
    dev, hardware = discharge_forbidden(item)
    harness = Harness([dev], load=-900, min_output_limit=35)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])

    assert harness.controller.charge_direction.charging is True
    assert item.ac_mode == 1
    assert all(write.get("outputLimit", 0) <= 0 for write in hardware.writes)


def test_forbidding_discharge_ends_a_running_discharge():
    """A positive target becomes zero, and zero is written.

    Blocking the write instead left a discharge that was running when the flag
    was set running for good.
    """

    item = pv_less_state(soc=50)
    item.output_limit = 300
    item.output = 300
    dev, hardware = discharge_forbidden(item)
    harness = Harness([dev], load=300, min_output_limit=35)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=3, states=[item])

    assert hardware.writes, "the running discharge was never ended"
    assert all(write == {"outputLimit": 0} for write in hardware.writes)
    assert item.output_limit == 0


def test_a_fleet_keeps_the_charge_share_of_a_device_forbidden_to_discharge():
    items = [pv_less_state(soc=50), pv_less_state(soc=50)]
    forbidden, forbidden_hardware = discharge_forbidden(items[0], name="A")
    allowed, allowed_hardware = following_http_device(items[1], name="B")
    harness = Harness([forbidden, allowed], load=-1200)
    harness.controller.set_output_limit = lambda dev, value: (
        forbidden_hardware if dev.name == "A" else allowed_hardware
    )(dev, value)
    harness.run(cycles=20, states=items)

    assert items[0].ac_mode == 1, "the forbidden device's share was lost"
    assert items[1].ac_mode == 1


def test_a_device_forbidden_to_discharge_is_never_parked_at_the_standby_floor():
    """The night idle parks devices at min_output_limit -- an output."""

    floor = state(
        soc=15, min_soc=15, solar=0, output=0, output_limit=0, pack_in=0,
        pack_out=0, soc_limit=2, dc_status=0, ac_status=0, pack_state=0,
    )
    dev, hardware = discharge_forbidden(floor)
    harness = Harness([dev], load=300, min_output_limit=35)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=5, states=[floor])

    assert all(write.get("outputLimit", 0) <= 0 for write in hardware.writes)


def test_an_mqtt_control_device_charges_through_the_same_loop():
    """A whole transport that had no loop-level coverage.

    Everything else here builds a local-HTTP device. The MQTT control client is
    a different class with a different constructor, no `resolved_hardware_profile`
    (it carries a pinned one instead) and no state reconciliation -- and that
    last difference already hid one defect, the charge that was not stopped
    after a restart. So the path is walked once end to end.

    It also happens to be the test user's configuration: a 2400 AC on MQTT,
    whose own ceiling is 2400 W. The MQTT client refuses any charge above the
    device's ``max_power`` before it publishes, so the regulator is held to
    that bound too. This test used to replace the write and assert -1200 W: a
    target the real precheck refuses with ``target_above_maximum``, which is
    the charge the test user never saw.
    """

    import json

    from ems.mqtt_control.zendure_profiles import WRITE_PROFILE_ZENSDK_PROPERTIES
    from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient
    from ems.zendure_mqtt.topics import FAMILY_LEGACY_JSON

    item = surplus_state()
    item.ac_mode = 2
    item.ac_status = 1
    item.output_limit = 0
    item.input_limit_w = 0
    item.grid_input = 0
    item.charge_max_limit_w = 2400
    hardware = FollowingHardware(item)

    class ServiceStub:
        def publish_message(self, message):
            hardware.apply(json.loads(message.payload)["properties"])
            return True

        def snapshot_status(self, *args, **kwargs):
            return None

    dev = ZendureMqttDeviceClient(
        name="WR1",
        service=ServiceStub(),
        device_id="ABC123",
        topic_family=FAMILY_LEGACY_JSON,
        product_key="PK",
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

    harness = Harness([dev], load=-900)
    harness.controller.set_output_limit = hardware
    harness.run(cycles=20, states=[item])

    assert harness.controller.charge_direction.charging is True
    # The pinned profile is what resolves the model, and it charges.
    assert harness.controller.device_model_supports_charge(dev) is True
    # Its own ceiling is read from telemetry, then held to what the transport
    # accepts, which is below the installation limit here.
    assert harness.controller.device_charge_limits["WR1"] == 800
    commanded = harness.controller.commanded_device_targets["WR1"]
    assert commanded == -800
    assert dev._precheck_target(int(commanded)) == "charge"
    assert item.ac_mode == 1


def test_the_mqtt_charge_bound_and_its_enforcement_agree():
    """The flag the regulator reads and the check the client runs are a pair."""

    from ems.mqtt_control.zendure_profiles import WRITE_PROFILE_ZENSDK_PROPERTIES
    from ems.zendure_mqtt.device_client import ZendureMqttDeviceClient, _WriteBlocked

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
        max_power=800,
        min_soc=15,
        max_soc=100,
        smart_mode=1,
    )

    assert dev.charge_bounded_by_max_power is True
    assert dev._precheck_target(-800) == "charge"
    with pytest.raises(_WriteBlocked):
        dev._precheck_target(-801)


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

    dev, hardware = following_device(item)
    harness = Harness([dev], load=0)
    meter = OscillatingMeter()
    harness.controller.shelly = meter

    def respond(dev, value, **kwargs):
        meter.charge = -int(value) if int(value) < 0 else 0
        hardware(dev, value, **kwargs)

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
