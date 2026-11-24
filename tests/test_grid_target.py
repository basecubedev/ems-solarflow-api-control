# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Grid targets: the meter reading the EMS settles on while discharging and while charging (#381).

At 0 the EMS aims for 0 W at the meter, as it always has. Each target is
signed, import positive and export negative, from -200 to 200 W:
``system.grid_target_discharge_w`` applies while the battery discharges and
``system.grid_target_charge_w`` while a surplus charge runs. 35 W of house load
without PV and a discharge target of 10 W leave about 25 W to the battery and
10 W to the grid; 80 W of export and a charge target of -10 W charge about
70 W. Whether a charge starts or ends is still judged on the surplus itself,
before either target: no target starts a charge from the grid or keeps one
going once the surplus is gone.
"""

import dataclasses
import json
import random
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from ems.controller import EMSController
from ems.grid_reading import GridReading
from tests.test_ac_charge_regulator import Harness, charging_device, surplus_state
from tests.test_write_gates import RuntimeStateStub, device, state

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


class LoopMeter:
    """The grid meter of a closed loop: the house's net load, less what is fed out, plus what is drawn in."""

    def __init__(self, net_load):
        self.net_load = net_load
        self.written = {}

    def get_power(self):
        return self.net_load - sum(self.written.values())

    def write(self, dev, target, *args, **kwargs):
        self.written[dev.name] = target
        return True


UNSET = object()


def discharge_loop(grid_target=UNSET, house=35, cycles=20):
    meter = LoopMeter(house)
    system = {} if grid_target is UNSET else {"grid_target_discharge_w": grid_target}
    ems = EMSController(
        devices=[device("WR1")],
        shelly=meter,
        sleep_enabled=False,
        runtime_state=RuntimeStateStub(system=system),
    )
    ems.set_output_limit = Mock(side_effect=meter.write)
    plant = [state(60, solar=0)]
    with patch("ems.controller.fetch_all_devices", return_value=plant), patch(
        "ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0
    ), patch("ems.controller.cfg.MAX_TOTAL_POWER", 800), patch(
        "ems.controller.cfg.MIN_OUTPUT_LIMIT", 0
    ):
        for _cycle in range(cycles):
            ems.run_once()
    return meter, ems


def test_without_a_target_the_battery_covers_the_house_to_zero():
    meter, _ems = discharge_loop()

    assert abs(meter.get_power()) <= 5
    assert meter.written["WR1"] >= 30


def test_a_target_leaves_that_much_import_to_the_grid():
    meter, ems = discharge_loop(grid_target=10)

    assert 5 <= meter.get_power() <= 15, meter.get_power()
    assert 20 <= meter.written["WR1"] <= 30, meter.written
    assert ems.last_control_explanation.grid_target_w == 10
    assert ems.last_control_explanation.filtered_load_w == pytest.approx(meter.get_power(), abs=6)


def test_an_export_target_while_discharging_feeds_that_much_to_the_grid():
    meter, ems = discharge_loop(grid_target=-10)

    assert -15 <= meter.get_power() <= -5, meter.get_power()
    assert 40 <= meter.written["WR1"] <= 50, meter.written
    assert ems.last_control_explanation.grid_target_w == -10


@pytest.mark.parametrize("stored", [-201, "x", None, 201, True])
def test_a_target_beyond_200_watts_or_not_a_number_reads_as_zero(stored):
    meter, _ems = discharge_loop(grid_target=stored)

    assert abs(meter.get_power()) <= 5


def charge_harness(charge_target, net_load, *, start_w, hysteresis_w, discharge_target=0):
    harness = Harness(
        [charging_device()],
        load=net_load,
        runtime_state=RuntimeStateStub(
            system={"grid_target_charge_w": charge_target, "grid_target_discharge_w": discharge_target}
        ),
    )
    harness.feature.update(charge_start_w=start_w, charge_hysteresis_w=hysteresis_w)
    meter = LoopMeter(net_load)
    harness.controller.shelly = meter
    harness.controller.set_output_limit = Mock(side_effect=meter.write)
    return harness, meter


def test_a_target_alone_never_starts_a_charge_from_the_grid():
    """A balanced meter is 10 W short of a 10 W target; that is no surplus to charge."""

    harness, meter = charge_harness(10, 0, start_w=5, hysteresis_w=0, discharge_target=10)

    harness.run(cycles=30, states=[surplus_state()])

    assert harness.controller.charge_direction.entries == ()
    assert harness.controller.charge_direction.charging is False


def test_an_export_target_while_discharging_is_no_surplus_to_charge():
    """The battery feeding 50 W into the grid shows as export at the meter, but no surplus."""

    harness, meter = charge_harness(0, 0, start_w=5, hysteresis_w=0, discharge_target=-50)

    harness.run(cycles=30, states=[surplus_state()])

    assert harness.controller.charge_direction.entries == ()
    assert -55 <= meter.get_power() <= -45, meter.get_power()


def test_an_import_target_while_charging_charges_the_export_and_the_target():
    harness, meter = charge_harness(10, -80, start_w=50, hysteresis_w=20)

    harness.run(cycles=30, states=[surplus_state()])

    assert harness.controller.charge_direction.charging is True
    assert -95 <= meter.written["WR1"] <= -85, meter.written
    assert 5 <= meter.get_power() <= 15, meter.get_power()


def test_an_export_target_while_charging_leaves_that_much_of_the_surplus_to_the_grid():
    harness, meter = charge_harness(-10, -80, start_w=50, hysteresis_w=20)

    harness.run(cycles=30, states=[surplus_state()])

    assert harness.controller.charge_direction.charging is True
    assert -75 <= meter.written["WR1"] <= -65, meter.written
    assert -15 <= meter.get_power() <= -5, meter.get_power()
    assert harness.controller.last_control_explanation.grid_target_w == -10


def test_each_direction_settles_on_its_own_target():
    harness, meter = charge_harness(-10, 35, start_w=50, hysteresis_w=20, discharge_target=10)

    harness.run(cycles=20, states=[surplus_state()])
    assert harness.controller.charge_direction.charging is False
    assert 5 <= meter.get_power() <= 15, meter.get_power()

    meter.net_load = -120
    harness.run(cycles=30, states=[surplus_state()])
    assert harness.controller.charge_direction.charging is True
    assert -15 <= meter.get_power() <= -5, meter.get_power()

    meter.net_load = 35
    harness.run(cycles=5, states=[surplus_state()])
    assert harness.controller.charge_direction.charging is False
    assert harness.controller.last_control_explanation.grid_target_w == 10


def test_a_charge_whose_surplus_is_gone_ends_although_the_target_asks_for_import():
    """Judged after the target, a 10 W target held a 10 W charge from the grid for good."""

    harness, meter = charge_harness(10, -80, start_w=50, hysteresis_w=45)
    harness.run(cycles=30, states=[surplus_state()])
    assert harness.controller.charge_direction.charging is True

    meter.net_load = 0
    harness.run(cycles=10, states=[surplus_state()])

    assert harness.controller.charge_direction.charging is False
    assert meter.written["WR1"] >= 0, meter.written


@pytest.mark.parametrize(
    "charge_target, discharge_target, sized",
    [(0, 0, 100), (-200, 0, 100), (-200, 200, 0), (100, 0, 100), (-10, 10, 90)],
)
def test_the_discharge_after_a_charge_is_sized_on_the_discharge_target(charge_target, discharge_target, sized):
    """The cycle that ends a charge sizes the discharge after it; on the charge target, -200 W fed 200 W of battery out."""

    harness, meter = charge_harness(charge_target, -600, start_w=150, hysteresis_w=50, discharge_target=discharge_target)
    harness.run(cycles=30, states=[surplus_state()])
    assert harness.controller.charge_direction.charging is True
    draw = -meter.written["WR1"]
    meter.net_load = 100 + draw
    meter.written = {}
    leaving = dataclasses.replace(surplus_state(), solar=0, grid_input=draw, ac_mode=1)

    harness.run(cycles=1, states=[leaving])

    assert harness.controller.charge_direction.charging is False
    assert harness.controller.commanded_total_w == pytest.approx(sized, abs=1)
    assert harness.controller.last_control_explanation.grid_target_w == charge_target


@pytest.mark.parametrize("charge_target", [0, -200, 200])
def test_a_discharge_held_on_a_corrected_reading_after_a_charge_moves_to_the_discharge_target(charge_target):
    """A corrected reading sizes nothing: the integrator's total carries over, and on the charge target -200 W fed 200 W out."""

    harness, meter = charge_harness(charge_target, -600, start_w=150, hysteresis_w=50)
    harness.run(cycles=30, states=[surplus_state()])
    assert harness.controller.charge_direction.charging is True
    draw = -meter.written["WR1"]
    meter.net_load = 100 + draw
    meter.written = {}
    leaving = dataclasses.replace(surplus_state(), solar=0, grid_input=draw, ac_mode=1)

    with patch.object(harness.controller.write_dead_time, "unseen_w", return_value=5):
        harness.run(cycles=1, states=[leaving])

    assert harness.controller.charge_direction.charging is False
    assert harness.controller.commanded_total_w == pytest.approx(95, abs=2)
    assert meter.written["WR1"] == pytest.approx(95, abs=2)


@pytest.mark.parametrize("charge_target, discharge_target", [(0, -200), (100, -100), (0, 0), (-200, -200)])
def test_a_charge_ended_at_its_limit_on_a_corrected_reading_discharges_nothing_into_the_export(charge_target, discharge_target):
    """At its limit the total sat on the charge floor; moved from there by the target change, a full battery fed 200 W into 600 W of export."""

    harness, meter = charge_harness(charge_target, -600, start_w=150, hysteresis_w=50, discharge_target=discharge_target)
    harness.feature.update(max_total_charge_power_w=150)
    harness.run(cycles=40, states=[surplus_state()])
    assert harness.controller.charge_direction.charging is True
    written = Mock(side_effect=meter.write)
    harness.controller.set_output_limit = written
    full = dataclasses.replace(surplus_state(), soc=100, soc_limit=1, grid_input=-int(meter.written["WR1"]), ac_mode=1)

    with patch.object(harness.controller.write_dead_time, "unseen_w", return_value=5):
        harness.run(cycles=1, states=[full])
    harness.run(cycles=1, states=[full])

    assert harness.controller.charge_direction.charging is False
    assert harness.controller.commanded_total_w <= 0
    assert all(call.args[1] <= 0 for call in written.call_args_list), written.call_args_list


@pytest.mark.parametrize("charge_target, discharge_target", [(0, -200), (100, -100), (0, 0), (-200, -200)])
def test_a_charge_ended_at_its_limit_on_a_repeated_reading_discharges_nothing_into_the_export(charge_target, discharge_target):
    """A repeated reading's goal is held at the charge floor; moved from there, a full battery fed 50 W into the export."""

    harness, meter = charge_harness(charge_target, -600, start_w=150, hysteresis_w=50, discharge_target=discharge_target)
    harness.feature.update(max_total_charge_power_w=150)
    sequence = [0]
    meter.get_reading = lambda: GridReading(meter.get_power(), sequence=sequence[0])
    for _cycle in range(40):
        sequence[0] += 1
        harness.run(cycles=1, states=[surplus_state()])
    assert harness.controller.charge_direction.charging is True
    written = Mock(side_effect=meter.write)
    harness.controller.set_output_limit = written
    full = dataclasses.replace(surplus_state(), soc=100, soc_limit=1, grid_input=-int(meter.written["WR1"]), ac_mode=1)

    harness.run(cycles=2, states=[full])

    assert harness.controller.charge_direction.charging is False
    assert harness.controller.commanded_total_w <= 0
    assert all(call.args[1] <= 0 for call in written.call_args_list), written.call_args_list


class StaleMeterHealth:
    attempted = True
    consecutive_failures = 0
    stale_used = False

    def age_seconds(self):
        return 999 if self.stale_used else 0


def test_a_charge_ended_by_a_held_meter_holds_its_total_whatever_the_targets():
    """A held reading sizes nothing: on a -200 W discharge target the exit wrote 40 W of discharge blind."""

    harness, meter = charge_harness(0, -400, start_w=150, hysteresis_w=50, discharge_target=-200)
    meter.health = StaleMeterHealth()
    harness.run(cycles=30, states=[surplus_state()])
    meter.net_load = -160
    harness.run(cycles=10, states=[surplus_state()])
    assert harness.controller.charge_direction.charging is True
    before = harness.controller.commanded_total_w
    meter.health.stale_used = True
    written = Mock(side_effect=meter.write)
    harness.controller.set_output_limit = written
    leaving = dataclasses.replace(surplus_state(), grid_input=-int(meter.written["WR1"]), ac_mode=1)

    harness.run(cycles=1, states=[leaving])

    assert harness.controller.charge_direction.charging is False
    assert harness.controller.commanded_total_w == before
    assert all(call.args[1] <= 0 for call in written.call_args_list), written.call_args_list


def test_a_discharge_target_changed_within_a_cycle_counts_from_the_next_reading():
    """Only the cycle that ends a charge moves to the other target; a runtime write mid-cycle skipped the ramp."""

    meter, ems = discharge_loop(grid_target=0, house=300, cycles=10)

    def fetch(devices):
        ems.runtime_state.system["grid_target_discharge_w"] = -200
        return [state(60, solar=0)]

    with patch("ems.controller.fetch_all_devices", side_effect=fetch), patch(
        "ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0
    ), patch("ems.controller.cfg.MAX_TOTAL_POWER", 800), patch("ems.controller.cfg.MIN_OUTPUT_LIMIT", 0):
        ems.run_once()
        assert ems.commanded_total_w == pytest.approx(300, abs=1)
        ems.run_once()

    assert ems.commanded_total_w == pytest.approx(500, abs=1)


def test_moving_the_total_to_another_target_keeps_the_reading_goal_and_the_limits():
    ems = EMSController(devices=[device("WR1")], shelly=LoopMeter(0), sleep_enabled=False)
    ems.commanded_total_w = 250
    ems._reading_goal = (250, 750)
    ems._desired_before_limits_w = 750

    assert ems.move_total_to_target(-200, 2400, has_export_capacity=True, standby_total_w=0) == 50
    assert ems._reading_goal == (50, 550)
    assert ems.reading_goal_w() == 550

    ems.commanded_total_w, ems._reading_goal = 100, None
    assert ems.move_total_to_target(-200, 2400, has_export_capacity=True, standby_total_w=0) == 0
    ems.commanded_total_w = 100
    assert ems.move_total_to_target(200, 150, has_export_capacity=True, standby_total_w=0) == 150
    ems.commanded_total_w = 0
    assert ems.move_total_to_target(200, 2400, has_export_capacity=False, standby_total_w=35) == 35
    ems.commanded_total_w = 100
    assert ems.move_total_to_target(200, 2400, has_export_capacity=False, standby_total_w=35) == 100

    ems.commanded_total_w, ems._reading_goal, ems._desired_before_limits_w = 100, (100, 600), 600
    assert ems.move_total_to_target(200, 150, has_export_capacity=True, standby_total_w=0) == 150
    assert ems._reading_goal == (150, 800)


def test_a_total_held_up_by_the_charge_floor_moves_from_what_the_reading_asked():
    """The reading asked for -450 W; the floor held the total at -150 W. On a 200 W lower target that is -250 W, so 0 W."""

    ems = EMSController(devices=[device("WR1")], shelly=LoopMeter(0), sleep_enabled=False)
    ems.commanded_total_w = -150
    ems._reading_goal = (-150, -150)
    ems._desired_before_limits_w = -450

    assert ems.move_total_to_target(200, 2400, has_export_capacity=True, standby_total_w=0) == 0
    assert ems._reading_goal == (0, -250)
    assert ems.reading_goal_w() == -250


@pytest.mark.parametrize("charge_target", [-150, -200])
def test_a_charge_ends_once_an_export_charge_target_leaves_it_nothing_to_charge(charge_target):
    """120 W of surplus against a -200 W target asked for +80 W: the battery fed the grid while charging."""

    harness, meter = charge_harness(charge_target, -600, start_w=150, hysteresis_w=50)
    harness.run(cycles=30, states=[surplus_state()])
    assert harness.controller.charge_direction.charging is True
    draw = -meter.written["WR1"]
    written = Mock(side_effect=meter.write)
    harness.controller.set_output_limit = written
    meter.net_load = -120 + draw
    meter.written = {}
    leaving = dataclasses.replace(surplus_state(), grid_input=draw, ac_mode=1)

    harness.run(cycles=1, states=[leaving])

    assert harness.controller.charge_direction.charging is False
    assert all(call.args[1] <= 0 for call in written.call_args_list), written.call_args_list


def test_a_surplus_swinging_below_an_export_charge_target_starts_no_charge():
    """Against a -200 W target, 170 to 240 W of surplus swung between charging 40 W and discharging 30 W."""

    harness, meter = charge_harness(-200, -170, start_w=150, hysteresis_w=50)
    written = Mock(side_effect=meter.write)
    harness.controller.set_output_limit = written

    for surplus in (170, 240) * 15:
        meter.net_load = -surplus
        harness.run(cycles=1, states=[surplus_state()])

    assert harness.controller.charge_direction.entries == ()
    assert all(call.args[1] >= 0 for call in written.call_args_list), written.call_args_list


def test_an_export_charge_target_raises_the_thresholds_to_its_export():
    harness, _meter = charge_harness(-200, 0, start_w=150, hysteresis_w=50)

    settings = harness.controller.charge_settings()

    assert (settings.start_w, settings.stop_w) == (250, 200)


@pytest.mark.parametrize("charge_target, thresholds", [(-100, (150, 100)), (-50, (150, 100)), (0, (150, 100)), (50, (150, 100)), (-120, (170, 120))])
def test_a_charge_target_within_the_stop_threshold_keeps_the_thresholds(charge_target, thresholds):
    harness, _meter = charge_harness(charge_target, 0, start_w=150, hysteresis_w=50)

    settings = harness.controller.charge_settings()

    assert (settings.start_w, settings.stop_w) == thresholds


def test_a_surplus_beyond_the_raised_start_charges_what_the_export_target_leaves():
    harness, meter = charge_harness(-200, -300, start_w=150, hysteresis_w=50)

    harness.run(cycles=30, states=[surplus_state()])

    assert harness.controller.charge_direction.charging is True
    assert meter.written["WR1"] == pytest.approx(-100, abs=5), meter.written
    assert meter.get_power() == pytest.approx(-200, abs=5)


def test_devices_that_take_less_than_a_raised_stop_threshold_still_charge():
    """The capacity check keeps the configured stop: 150 W of capacity against a -200 W target is a charge of 150 W."""

    harness, meter = charge_harness(-200, -600, start_w=150, hysteresis_w=50)
    harness.feature.update(max_total_charge_power_w=150)

    harness.run(cycles=40, states=[surplus_state()])

    assert harness.controller.charge_direction.charging is True
    assert len(harness.controller.charge_direction.entries) == 1
    assert meter.written["WR1"] == pytest.approx(-150, abs=1)


KEYS = ("grid_target_discharge_w", "grid_target_charge_w")


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", [-200, -10, 0, 10, 200])
def test_the_runtime_writer_takes_a_target_from_minus_to_plus_200_watts(key, value):
    from ems import runtime_commands

    assert runtime_commands.validate_system_values({key: value}) == {key: value}


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", [-201, 201, "ten", True, None, 2.5])
def test_the_runtime_writer_refuses_a_target_beyond_200_watts_or_not_whole_watts(key, value):
    from ems import runtime_commands

    with pytest.raises(runtime_commands.RuntimeWriteError):
        runtime_commands.validate_system_values({key: value})


@pytest.mark.parametrize(
    "command, action_id, key",
    [
        ("grid-target-discharge", "grid_target_discharge", "grid_target_discharge_w"),
        ("grid-target-charge", "grid_target_charge", "grid_target_charge_w"),
    ],
)
def test_emsctl_and_a_schedule_set_each_target_through_the_one_command_path(command, action_id, key):
    from ems import runtime_commands, schedule_actions, schedule_cli, schedule_text

    assert runtime_commands.validate_command("system", command, "-10") == {key: -10}
    action = schedule_actions.schedule_action(action_id)
    assert action.keys == (key,)
    assert schedule_actions.action_commands(action, -10) == ((command, -10),)
    assert schedule_cli.parse_value(action, "-10") == -10
    assert schedule_text.describe_value(action, -10) == "10 W export"
    assert schedule_text.describe_value(action, 10) == "10 W import"
    assert schedule_text.describe_value(action, 0) == "0 W"


@pytest.mark.parametrize(
    "stored, read",
    [(0, 0), (10, 10), ("15", 15), (200, 200), (12.7, 12), (-5, -5), (-200, -200), (-201, 0), (201, 0), ("x", 0), (None, 0), (True, 0)],
)
def test_a_stored_target_reads_as_whole_watts_and_as_zero_outside_its_range(stored, read):
    from ems import config

    assert config.grid_target_w(stored) == read


def test_the_configured_target_is_the_default_without_a_runtime_value():
    with patch("ems.controller.cfg.GRID_TARGET_DISCHARGE_W", 10):
        meter, _ems = discharge_loop()

    assert 5 <= meter.get_power() <= 15, meter.get_power()


def test_the_configuration_names_the_default_target(tmp_path):
    from types import SimpleNamespace

    from ems import config

    path = tmp_path / "config.json"
    template = json.loads((Path(__file__).resolve().parents[1] / "config" / "config.template.json").read_text())
    template["system"]["grid_target_discharge_w"] = 15
    template["system"]["grid_target_charge_w"] = -15
    path.write_text(json.dumps(template), encoding="utf-8")
    args = SimpleNamespace(config=str(path), dry_run=False, simulate=False, replay=None, self_test=False)
    snapshot = {name: getattr(config, name) for name in dir(config) if name.isupper()}
    try:
        config.initialize(args, str(tmp_path))
        assert (config.GRID_TARGET_DISCHARGE_W, config.GRID_TARGET_CHARGE_W) == (15, -15)
    finally:
        for name, value in snapshot.items():
            setattr(config, name, value)


def test_a_target_lowered_at_runtime_starts_no_charge_on_a_surplus_below_the_threshold():
    """Filtered with the old target subtracted, a cut from 200 W to 0 read as 200 W more surplus."""

    runtime = RuntimeStateStub(system={"grid_target_discharge_w": 200})
    harness = Harness([charging_device()], load=-100, runtime_state=runtime)
    harness.feature.update(charge_start_w=150, charge_hysteresis_w=50, entry_confirm_cycles=1, entry_window_cycles=1)
    meter = LoopMeter(-100)
    harness.controller.shelly = meter
    harness.controller.set_output_limit = Mock(side_effect=meter.write)
    with patch.dict("ems.controller.cfg.OUTPUT_CONTROL_CONFIG", {"filter_enabled": True, "ema_alpha": 0.2}):
        harness.run(cycles=10, states=[surplus_state()])
        runtime.system["grid_target_discharge_w"] = 0
        harness.run(cycles=15, states=[surplus_state()])

    assert harness.controller.charge_direction.entries == ()
    assert all(target >= 0 for target in meter.written.values()), meter.written


@pytest.mark.parametrize(
    "control, active, target",
    [
        ({"filtered_load_w": 12.0, "grid_target_w": 10.0}, True, 10.0),
        ({"filtered_load_w": 12.0, "grid_target_w": 0.0}, False, 0.0),
        ({"filtered_load_w": 12.0}, False, None),
        ({}, None, None),
    ],
)
def test_diagnose_judges_the_deadband_from_the_target_and_names_it(control, active, target):
    from ems import diagnostics

    config = {"system": {"output_control": {"load_deadband_w": 5}}}
    snapshot = diagnostics.diagnose_control_snapshot(config, {}, {"control": control}, "runtime-state.json")

    assert snapshot["deadband_active"] is active
    assert snapshot["grid_target_w"] == target


@pytest.mark.parametrize("grid_target", [-200, -100, 0, 100, 160, 200])
def test_a_charge_held_at_its_limit_keeps_charging_whatever_the_target(grid_target):
    """At its limit the total sits at the floor; read as floor plus target, a 250 W cap looked like 50 W of surplus."""

    harness, meter = charge_harness(grid_target, -600, start_w=150, hysteresis_w=50)
    harness.feature.update(max_total_charge_power_w=250)

    harness.run(cycles=60, states=[surplus_state()])

    assert harness.controller.charge_direction.charging is True
    assert len(harness.controller.charge_direction.entries) == 1
    assert meter.written["WR1"] == pytest.approx(-250, abs=1)


def test_the_no_export_hold_measures_from_the_target():
    """150 W of import is 50 W below a 200 W target: no load to hold the standby total against."""

    ems = EMSController(devices=[device("WR1")], shelly=LoopMeter(0), sleep_enabled=False)
    ems.commanded_total_w = 35
    with patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0):
        total = ems.stabilized_total_target(
            150, [state(60, solar=0)], 800, has_export_capacity=False, standby_total_w=35, grid_target_w=200
        )

    assert total < 35


def test_the_large_import_bypass_measures_from_the_target():
    """650 W of import is 550 W above a 100 W target: under the 600 W bypass, so the ramp is not widened."""

    ems = EMSController(devices=[device("WR1")], shelly=LoopMeter(0), sleep_enabled=False)
    ems.commanded_total_w = 0
    with patch.dict("ems.controller.cfg.OUTPUT_CONTROL_CONFIG", {"ramp_up_w_per_cycle": 300}):
        total = ems.stabilized_total_target(650, [state(60, solar=0)], 2400, grid_target_w=100)

    assert total == 300


def test_a_limit_raised_a_few_watts_while_held_there_does_not_end_the_charge():
    """Within the target deadband the total stays at the old limit, above the new floor."""

    harness, meter = charge_harness(200, -600, start_w=150, hysteresis_w=50)
    harness.feature.update(max_total_charge_power_w=250)
    harness.run(cycles=30, states=[surplus_state()])
    harness.feature.update(max_total_charge_power_w=253)

    harness.run(cycles=15, states=[surplus_state()])

    assert harness.controller.charge_direction.charging is True
    assert len(harness.controller.charge_direction.entries) == 1


def test_control_quality_measures_export_from_the_target_in_effect():
    """A -50 W target held within a few watts is good regulation, not 100 % export."""

    from ems import diagnostics

    held = [{"grid_power_w": -50 + d, "grid_target_w": -50.0} for d in (2, -3, 1, 0)]
    report = diagnostics.diagnose_export_import_quality(held)

    assert report["status"] == "ok"
    assert report["below_target_duration_percent"] == 0
    assert report["near_target_duration_percent"] == 100
    assert report["export_duration_percent"] == 100
    assert report["max_export_peak_w"] == -53
    assert report["measured_from_grid_target"] is True
    assert report["min_grid_power_w"] == -53
    assert report["average_grid_power_w"] == -50
    assert report["average_deviation_w"] == 0
    assert diagnostics.diagnose_quality_score(report)["classification"] == "excellent"
    assert diagnostics.diagnose_quality_score(report)["near_zero_percent"] == 100


def test_control_quality_names_the_time_below_the_target():
    from ems import diagnostics

    report = diagnostics.diagnose_export_import_quality(
        [{"grid_power_w": value, "grid_target_w": 10.0} for value in (-40, -45, -35, 10)]
    )

    assert report["below_target_duration_percent"] == 75
    assert report["max_below_target_w"] == -55
    assert report["max_export_peak_w"] == -45
    assert "Time below the grid target is 75.0%." in report["warnings"]


def test_samples_without_a_target_are_measured_from_0_w_as_before():
    from ems import diagnostics

    samples = diagnostics.diagnose_live_grid_samples(
        {"grid_samples": [{"grid_power_w": value, "cycle_at": str(value)} for value in (-40, -45, -35, 10)]}
    )
    report = diagnostics.diagnose_export_import_quality(samples)

    assert [sample["grid_target_w"] for sample in samples] == [0.0] * 4
    assert report["export_duration_percent"] == 75
    assert report["max_export_peak_w"] == -45
    assert report["measured_from_grid_target"] is False
    assert all("grid target" not in warning for warning in report["warnings"])


def test_the_meter_swinging_round_its_target_counts_as_sign_changes():
    from ems import diagnostics

    swing = [{"grid_power_w": value, "grid_target_w": -50.0} for value in (-45, -55, -45, -55)]

    assert diagnostics.diagnose_meter_quality(swing)["sign_changes"] == 3
    assert diagnostics.diagnose_meter_quality([{"grid_power_w": s["grid_power_w"]} for s in swing])["sign_changes"] == 0


def test_a_recorded_grid_sample_carries_the_target_in_effect(tmp_path):
    from ems.control_status import ControlStatusWriter

    meter, ems = discharge_loop(grid_target=-10, cycles=3)
    writer = ControlStatusWriter(tmp_path / "control-status.json")
    writer.write(ems)

    snapshot = json.loads((tmp_path / "control-status.json").read_text(encoding="utf-8"))
    assert snapshot["grid_samples"][-1]["grid_target_w"] == -10


def test_the_dashboard_snapshot_shows_the_target_in_effect():
    from dashboard.telemetry import build_dashboard_snapshot

    meter, ems = discharge_loop(grid_target=-10, cycles=3)
    written = [meter.written["WR1"]]
    snapshot = build_dashboard_snapshot(
        ems,
        meter.get_power(),
        [state(60, solar=0)],
        written,
        written,
        written[0],
        written[0],
        enabled=True,
        max_total_power=800,
        min_output_limit=0,
    )

    assert snapshot["controller"]["grid_target_w"] == -10


def test_control_quality_reads_the_target_from_the_recorded_snapshot(tmp_path):
    """The target goes from the snapshot's grid samples to the quality: a held -10 W target is no export."""

    from ems import diagnostics
    from ems.control_status import ControlStatusWriter

    meter, ems = discharge_loop(grid_target=-10, cycles=20)
    writer = ControlStatusWriter(tmp_path / "control-status.json")
    writer.write(ems)
    snapshot = json.loads((tmp_path / "control-status.json").read_text(encoding="utf-8"))

    report = diagnostics.diagnose_export_import_quality(diagnostics.diagnose_live_grid_samples(snapshot))

    assert report["min_grid_power_w"] == pytest.approx(-10, abs=1)
    assert report["measured_from_grid_target"] is True
    assert report["average_deviation_w"] == pytest.approx(0, abs=1)


def test_control_quality_measures_import_peaks_from_the_target():
    from ems import diagnostics

    report = diagnostics.diagnose_export_import_quality(
        [{"grid_power_w": value, "grid_target_w": 10.0} for value in (40, 20, 10)]
    )

    assert report["max_above_target_w"] == 30
    assert report["max_import_peak_w"] == 40


def test_the_quality_text_names_values_measured_from_the_target():
    from ems import diagnostics

    held = diagnostics.diagnose_export_import_quality([{"grid_power_w": -50, "grid_target_w": -50.0}] * 3)
    plain = diagnostics.diagnose_export_import_quality([{"grid_power_w": 5}] * 3)

    def text(export_import):
        lines = diagnostics.diagnose_control_quality_text(
            {
                "export_import": export_import,
                "quality_score": diagnostics.diagnose_quality_score(export_import),
                "pv_diagnostics": {},
                "soc_balancing": {},
                "root_causes": [],
            }
        )
        return lines if isinstance(lines, str) else "\n".join(lines)

    assert "Max Below Target:" in text(held) and "Max Export:" in text(held)
    assert "Max Below Target:" not in text(plain)
    assert "the grid target in effect" in text(held) and "0 W grid exchange" not in text(held)
    assert "Max Export:" in text(plain) and "Below Target" not in text(plain)
    assert "0 W grid exchange" in text(plain)
    assert "Near-Target Share:" in text(held) and "Near-Zero Share:" in text(plain)


@pytest.mark.parametrize(
    "runtime_target, config_target, band, raised",
    [(None, -200, (250, 200), True), (-200, 0, (250, 200), True), (-50, -200, (150, 100), False), (None, None, (150, 100), False)],
)
def test_diagnose_reports_the_charge_band_the_loop_uses(runtime_target, config_target, band, raised):
    from ems import diagnostics

    config = {
        "system": {} if config_target is None else {"grid_target_charge_w": config_target},
        "ac_charge_control": {"enabled": True, "charge_start_w": 150, "charge_hysteresis_w": 50},
        "devices": [],
    }
    runtime = {} if runtime_target is None else {"system": {"grid_target_charge_w": runtime_target}}

    snapshot = diagnostics.diagnose_ac_charging_snapshot(config, runtime, {})

    assert (snapshot["ac_charge_start_w"], snapshot["ac_charge_stop_w"]) == band
    assert snapshot["ac_charge_band_raised_by_grid_target"] is raised
    assert ("raised by the charge grid target" in "\n".join(diagnostics.diagnose_ac_charging_text(snapshot))) is raised


@pytest.mark.parametrize(
    "start, stop, target, band",
    [(150, 100, 0, (150, 100)), (150, 100, -100, (150, 100)), (150, 100, -101, (151, 101)), (150, 100, -200, (250, 200)), (40, 0, -100, (140, 100)), (150, 100, 200, (150, 100))],
)
def test_the_charge_band_moves_only_for_an_export_beyond_the_stop_threshold(start, stop, target, band):
    from ems.ac_charge_control import charge_thresholds_w

    assert charge_thresholds_w(start, stop, target) == band


def test_samples_without_a_target_report_the_meter_fields_as_before():
    """At 0 W the fields named for the target are the meter's own, and the status is judged as before."""

    from ems import diagnostics

    report = diagnostics.diagnose_export_import_quality([{"grid_power_w": value} for value in (10, -300, -260, 20)])

    assert report["status"] == "error"
    assert report["warnings"][0] == "Export peaks up to -300 W detected."
    assert (report["max_export_peak_w"], report["max_below_target_w"]) == (-300, -300)
    assert report["export_duration_percent"] == report["below_target_duration_percent"] == 50
    assert report["near_zero_duration_percent"] == report["near_target_duration_percent"] == 50


def test_the_quality_root_cause_names_the_target(tmp_path):
    from ems import diagnostics
    from ems.control_status import ControlStatusWriter

    _meter, ems = discharge_loop(grid_target=10, house=-300, cycles=5)
    ControlStatusWriter(tmp_path / "control-status.json").write(ems)
    (tmp_path / "runtime-state.json").write_text("{}", encoding="utf-8")

    report = diagnostics.diagnose_control_quality_report({"system": {}}, str(tmp_path / "runtime-state.json"))

    cause = next(cause for cause in report["root_causes"] if cause["code"] == "export_peaks_detected")
    assert cause["message"] == "The grid meter went below the grid target during the sample window."


@pytest.mark.parametrize("filtered, seeds", [(False, range(0, 20)), (True, range(40, 60))])
def test_random_targets_never_discharge_while_charging_or_past_the_discharge_target_on_leaving(filtered, seeds):
    """Closed loops with random targets, thresholds, caps, loads and a meter slower than the loop.

    Telemetry follows the writes. No cycle that goes on charging writes a
    discharge, and no cycle that ends a charge discharges more than the house
    draws above the discharge target. A charge entered on a new reading and
    left on its repetition was moved by both targets' difference: 251 W
    where the house drew 173 W above the target (filtered, seed 50).
    """

    output_control = (
        {"filter_enabled": True, "ema_alpha": 0.3, "median_window": 3, "ramp_up_w_per_cycle": 120, "ramp_down_w_per_cycle": 90}
        if filtered
        else {}
    )
    problems = []
    for seed in seeds:
        rng = random.Random(seed)
        meter_rng = random.Random(seed + 7)
        discharge_target, charge_target = rng.randint(-200, 200), rng.randint(-200, 200)
        runtime = RuntimeStateStub(
            system={"grid_target_discharge_w": discharge_target, "grid_target_charge_w": charge_target}
        )
        harness = Harness([charging_device()], load=0, runtime_state=runtime)
        harness.feature.update(
            charge_start_w=rng.choice([50, 100, 150, 200]),
            charge_hysteresis_w=rng.choice([20, 50, 100]),
            entry_confirm_cycles=rng.choice([1, 3, 5]),
        )
        if rng.random() < 0.5:
            harness.feature.update(max_total_charge_power_w=rng.choice([150, 250, 400, 800]))
        net = rng.randint(-800, 400)
        meter = LoopMeter(net)
        new_reading_odds = rng.choice([0.3, 0.6, 1.0])
        held = {"sequence": 0, "power": None}

        def reading(meter=meter, meter_rng=meter_rng, odds=new_reading_odds, held=held):
            if held["power"] is None or meter_rng.random() < odds:
                held["sequence"] += 1
                held["power"] = meter.get_power()
            return GridReading(held["power"], sequence=held["sequence"])

        meter.get_reading = reading
        harness.controller.shelly = meter
        written = Mock(side_effect=meter.write)
        harness.controller.set_output_limit = written
        for cycle in range(300):
            if rng.random() < 0.15:
                net = max(-900, min(600, net + rng.randint(-250, 250)))
                meter.net_load = net
            if rng.random() < 0.03:
                discharge_target, charge_target = rng.randint(-200, 200), rng.randint(-200, 200)
                runtime.system.update(grid_target_discharge_w=discharge_target, grid_target_charge_w=charge_target)
            was_charging = harness.controller.charge_direction.charging
            written.reset_mock()
            target = meter.written.get("WR1", 0)
            telemetry = dataclasses.replace(
                surplus_state(), output=max(0, target), grid_input=max(0, -target), ac_mode=1 if target < 0 else 2
            )
            with patch.dict("ems.controller.cfg.OUTPUT_CONTROL_CONFIG", output_control):
                harness.run(cycles=1, states=[telemetry])
            charging = harness.controller.charge_direction.charging
            writes = [call.args[1] for call in written.call_args_list]
            if was_charging and charging and any(value > 0 for value in writes):
                problems.append(("discharge while charging", seed, cycle, writes))
            if was_charging and not charging and writes and max(writes) > max(0, net - discharge_target) + 15:
                problems.append(("discharge past the target on leaving", seed, cycle, writes, net, discharge_target))

    assert problems == []
