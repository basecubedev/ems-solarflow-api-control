# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The control loop hands a full device's PV to another device's AC charge.

Runs the real loop (``tests.test_ac_charge_regulator.Harness``). A device in
bypass is simulated the way the firmware behaves: it reports more output than
the limit it was given, whatever the EMS writes.
"""

from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from ems import surplus_export as se
from ems.config import AC_CHARGE_CONTROL_DEFAULTS
from tests.test_ac_charge_regulator import Harness, charging_device
from tests.test_write_gates import RuntimeStateStub, state

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
    pytest.mark.simulation,
]


def managed(name):
    dev = charging_device(name)
    dev.resolved_hardware_profile = lambda: "solarflow_800_pro_2"
    dev.write_properties = Mock(return_value=True)
    return dev


def reported(item, grid_reverse):
    return replace(item, grid_reverse=grid_reverse, grid_reverse_reported=True)


def full(grid_reverse=se.EXPORT_REFUSED, output=0, output_limit=0, solar=700):
    return reported(
        state(soc=100, solar=solar, output=output, output_limit=output_limit, soc_limit=1, pack_num=2),
        grid_reverse,
    )


def room(grid_reverse=se.EXPORT_REFUSED, soc=50):
    return reported(state(soc=soc, solar=0, output=0, soc_limit=0, pack_num=2), grid_reverse)


def export_writes(dev):
    return [
        call.args[0]
        for call in dev.write_properties.call_args_list
        if se.GRID_REVERSE_PROPERTY in call.args[0]
    ]


def limits(harness, dev):
    return [call.args[1] for call in harness.controller.set_output_limit.call_args_list if call.args[0] is dev]


def harness_for(devices, load, **feature):
    harness = Harness(devices, load=load, min_output_limit=35)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS, **feature}
    harness.state_writes = True
    return harness


def test_a_full_device_is_allowed_to_export_while_another_can_charge():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)

    harness.run(cycles=1, states=[full(), room()])

    assert export_writes(source) == [{"gridReverse": se.EXPORT_ALLOWED}]
    assert export_writes(taker) == []


def test_export_is_refused_when_every_device_is_full():
    first, second = managed("A"), managed("B")
    harness = harness_for([first, second], load=0)

    harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), full(se.EXPORT_ALLOWED)])

    assert export_writes(first) == [{"gridReverse": se.EXPORT_REFUSED}]
    assert export_writes(second) == [{"gridReverse": se.EXPORT_REFUSED}]


def test_a_single_full_device_has_its_export_refused():
    only = managed("A")
    harness = harness_for([only], load=0)

    harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED)])

    assert export_writes(only) == [{"gridReverse": se.EXPORT_REFUSED}]


def test_with_ac_charging_off_nothing_can_take_the_pv_in_and_export_is_refused():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0, enabled=False)

    harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), room()])

    assert export_writes(source) == [{"gridReverse": se.EXPORT_REFUSED}]


def test_with_no_devices_set_the_export_switch_is_left_to_the_app():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0, surplus_export_devices=0)

    harness.run(cycles=3, states=[full(se.EXPORT_ALLOWED), full(se.EXPORT_ALLOWED)])

    assert export_writes(source) == []
    assert export_writes(taker) == []


def test_no_export_switch_is_written_behind_a_shut_state_gate():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    harness.state_writes = False

    harness.run(cycles=1, states=[full(), room()])

    assert export_writes(source) == []


def test_a_device_in_bypass_is_told_its_share_of_the_house_and_nothing_else_is_allocated_to_it():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)

    controller = harness.run(cycles=1, states=[bypassing, room()])

    assert limits(harness, source)[-1] == 300 - 35
    assert controller.surplus_export_plan.exporting == frozenset({"A"})
    explanation = controller.last_control_explanation.devices["A"]
    assert explanation.limiting_reason == "surplus_export"
    assert explanation.allocated_target_w == 300 - 35


def test_a_device_in_bypass_is_never_told_less_than_the_standby_floor():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-700)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)

    harness.run(cycles=1, states=[bypassing, room()])

    assert limits(harness, source)[-1] == 35


def test_the_bypass_surplus_charges_the_other_device_and_never_the_source():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)

    controller = harness.run(cycles=15, states=[bypassing, room()])

    assert controller.charge_direction.charging is True
    assert controller.charge_commanded_by_ems(taker)
    assert not controller.charge_commanded_by_ems(source)


def test_a_source_that_leaves_full_during_the_charge_it_fed_is_kept_out_of_that_charge():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    controller = harness.run(cycles=15, states=[bypassing, room()])
    assert controller.charge_direction.charging is True

    left_full = reported(state(soc=97, solar=700, output=300, output_limit=300, soc_limit=0, pack_num=2), 1)
    controller = harness.run(cycles=3, states=[left_full, room()], load=-200)

    assert controller.charge_direction.charging is True
    assert "A" in controller.surplus_export_charge_sources
    assert not controller.charge_commanded_by_ems(source)


def test_stopping_the_ems_refuses_the_export_it_allowed():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    harness.run(cycles=1, states=[full(), room()])
    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), room()])
    source.write_properties.reset_mock()

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True):
        controller.restore_grid_export()

    assert export_writes(source) == [{"gridReverse": se.EXPORT_REFUSED}]
    assert export_writes(taker) == []


def test_stopping_behind_a_shut_state_gate_writes_nothing():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    harness.run(cycles=1, states=[full(), room()])
    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), room()])
    source.write_properties.reset_mock()

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=False):
        controller.restore_grid_export()

    assert export_writes(source) == []


def test_the_total_sized_on_leaving_a_charge_leaves_out_what_a_device_in_bypass_delivers():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    controller = harness.controller
    controller.surplus_export_plan = se.SurplusExportPlan(
        state=se.SurplusExportState(), exporting=frozenset({"A"})
    )
    states = [full(se.EXPORT_ALLOWED, output=700, output_limit=300), room()]
    controller.devices_leaving_a_charge = Mock(return_value=([states[1]], []))
    controller.grid_meter_holding = False

    total = controller.discharge_total_leaving_a_charge(100.0, states, 0.0, 800)

    assert total == 100.0


def test_a_device_in_bypass_takes_no_part_of_what_the_other_devices_must_deliver():
    source, other = managed("A"), managed("B")
    harness = harness_for([source, other], load=500)
    bypassing = full(se.EXPORT_ALLOWED, output=300, output_limit=200)
    discharging = reported(state(soc=60, solar=0, output=0, soc_limit=0, pack_num=2), se.EXPORT_REFUSED)

    harness.run(cycles=3, states=[bypassing, discharging])

    assert limits(harness, other)[-1] >= 500


def test_a_device_in_bypass_is_never_told_more_than_its_own_maximum():
    source, taker = managed("A"), managed("B")
    source.max_power = 200
    harness = harness_for([source, taker], load=-400)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=150)

    harness.run(cycles=1, states=[bypassing, room()])

    assert limits(harness, source)[-1] == 200


def test_the_others_share_only_what_the_installation_limit_leaves_beside_a_bypass():
    source, other = managed("A"), managed("B")
    harness = harness_for([source, other], load=1000)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    discharging = reported(state(soc=60, solar=0, output=0, soc_limit=0, pack_num=2), se.EXPORT_REFUSED)

    controller = harness.run(cycles=3, states=[bypassing, discharging])

    assert limits(harness, other)[-1] <= 100
    assert controller.commanded_total_w <= 100


def test_a_device_that_may_not_supply_the_house_is_never_allowed_to_export_nor_told_a_share():
    source, taker = managed("A"), managed("B")
    source.ac_discharge_enabled = False
    harness = harness_for([source, taker], load=-400)

    harness.run(cycles=1, states=[full(), room()])
    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED, output=700, output_limit=0), room()])

    assert {"gridReverse": se.EXPORT_ALLOWED} not in export_writes(source)
    assert controller.surplus_export_plan.grid_export["A"] == "refuse"
    assert limits(harness, source)[-1] == 0


def test_a_device_switched_off_has_its_export_switch_left_alone():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(devices={"A": {"enabled": False}})
    harness = Harness([source, taker], load=0, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True

    harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), room()])

    assert export_writes(source) == []


def test_the_charge_capacity_leaves_out_a_source_that_left_full_during_the_charge():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    harness.run(cycles=15, states=[bypassing, room()])

    left_full = reported(state(soc=97, solar=700, output=300, output_limit=300, soc_limit=0, pack_num=2), 1)
    controller = harness.run(cycles=1, states=[left_full, room()], load=-200)

    assert controller.charge_direction.charging is True
    assert controller.charge_capacity_w == controller.device_charge_limits["B"]
    assert controller.device_charge_limits["A"] == 0


def test_a_source_that_left_full_keeps_its_share_while_the_charge_it_fed_runs():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    harness.run(cycles=15, states=[bypassing, room()])
    told = limits(harness, source)[-1]

    left_full = reported(state(soc=97, solar=700, output=told, output_limit=told, soc_limit=0, pack_num=2), 1)
    controller = harness.run(cycles=2, states=[left_full, room()], load=-200)

    assert told == 300
    assert controller.charge_direction.charging is True
    assert limits(harness, source)[-1] == told


def test_a_device_allowed_to_export_stays_out_of_the_charge_once_it_leaves_full():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    allowed_not_yet_passing = full(se.EXPORT_ALLOWED, output=0, output_limit=35)
    controller = harness.run(cycles=15, states=[allowed_not_yet_passing, room()])
    assert controller.charge_direction.charging is True
    assert controller.surplus_export_plan.exporting == frozenset()

    left_full = reported(state(soc=97, solar=0, output=0, soc_limit=0, pack_num=2), 1)
    controller = harness.run(cycles=2, states=[left_full, room()])

    assert "A" in controller.surplus_export_charge_sources
    assert not controller.charge_commanded_by_ems(source)


def test_the_export_is_refused_on_stop_from_the_last_report_after_a_missed_poll():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    harness.run(cycles=1, states=[full(), room()])
    harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), room()])
    controller = harness.run(cycles=1, states=[None, room()])
    assert controller.surplus_export_plan.state.allowed == ()
    source.write_properties.reset_mock()

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True):
        controller.restore_grid_export()

    assert export_writes(source) == [{"gridReverse": se.EXPORT_REFUSED}]


def test_a_switched_off_device_gives_its_place_to_a_full_device_that_is_on():
    off, on, taker = managed("A"), managed("C"), managed("B")
    runtime = RuntimeStateStub(devices={"A": {"enabled": False}})
    harness = Harness([off, on, taker], load=0, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True

    controller = harness.run(cycles=1, states=[full(solar=900), full(solar=300), room()])

    assert controller.surplus_export_plan.state.allowed == ("C",)
    assert export_writes(on) == [{"gridReverse": se.EXPORT_ALLOWED}]


def test_with_control_switched_off_a_device_in_bypass_keeps_the_target_control_off_gives_it():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(system={"enabled": False})
    harness = Harness([source, taker], load=-400, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True

    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED, output=700, output_limit=200), room()])

    assert controller.surplus_export_plan.exporting == frozenset({"A"})
    assert controller.last_control_explanation.devices["A"].effective_target_w == 0


def test_the_discharge_ceiling_leaves_out_a_device_in_bypass():
    source, other = managed("A"), managed("B")
    controller = harness_for([source, other], load=0).controller
    controller.surplus_export_plan = se.SurplusExportPlan(
        state=se.SurplusExportState(), exporting=frozenset({"A"})
    )
    states = [
        full(se.EXPORT_ALLOWED, output=700, output_limit=200),
        state(soc=60, min_soc=15, solar=0, output=0, pack_num=2),
    ]

    ceiling = controller.discharge_ceiling_w(3000, [0, 100], states, None)

    assert ceiling == 100


def test_a_switched_off_device_in_bypass_keeps_the_target_a_switched_off_device_gets():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(devices={"A": {"enabled": False}})
    harness = Harness([source, taker], load=-400, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True

    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED, output=700, output_limit=200), room()])

    assert controller.surplus_export_plan.exporting == frozenset({"A"})
    assert controller.last_control_explanation.devices["A"].effective_target_w == 0


def allow_then_report(harness, source_state=None, taker_state=None):
    harness.run(cycles=1, states=[source_state or full(), taker_state or room()])
    return harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), taker_state or room()])


def test_switching_control_off_refuses_the_export_it_allowed_once_more():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(system={})
    harness = Harness([source, taker], load=0, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    allow_then_report(harness)
    source.write_properties.reset_mock()

    runtime.system["enabled"] = False
    controller = harness.run(cycles=3, states=[full(se.EXPORT_ALLOWED), room()])

    assert export_writes(source) == [{"gridReverse": se.EXPORT_REFUSED}]
    assert controller.grid_export_allowed_by_ems() == []


def test_switching_a_device_off_refuses_the_export_it_was_allowed():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(devices={})
    harness = Harness([source, taker], load=0, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    allow_then_report(harness)
    source.write_properties.reset_mock()

    runtime.devices["A"] = {"enabled": False}
    harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), room()])

    assert export_writes(source) == [{"gridReverse": se.EXPORT_REFUSED}]


def test_failed_cycles_refuse_the_export_as_they_return_the_charges():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    controller = allow_then_report(harness)
    source.write_properties.reset_mock()

    with patch.object(controller, "run_once", side_effect=RuntimeError("boom")), patch(
        "ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True
    ):
        for _ in range(4):
            controller.run_guarded()

    assert {"gridReverse": se.EXPORT_REFUSED} in export_writes(source)


def test_an_export_the_gates_withheld_is_not_one_the_ems_holds():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    harness.state_writes = False

    controller = harness.run(cycles=2, states=[full(), room()])

    assert controller.surplus_export_plan.grid_export["A"] == "allow"
    assert controller.grid_export_allowed_by_ems() == []


def test_a_bypass_that_ends_outside_a_charge_hands_its_share_to_the_total():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED, output=700, output_limit=200), room()])
    assert limits(harness, source)[-1] == 300 - 35
    assert controller.charge_direction.charging is False
    before = controller.commanded_total_w

    left_full = reported(state(soc=97, solar=700, output=300, output_limit=300, soc_limit=0, pack_num=2), 1)
    controller = harness.run(cycles=1, states=[left_full, room()], load=0)

    assert controller.commanded_total_w >= before + 300 - 35
    assert limits(harness, source)[-1] >= 250


def test_an_export_no_charge_follows_is_refused_through_the_control_loop():
    source, taker = managed("A"), managed("B")
    harness = Harness([source, taker], load=-100, min_output_limit=35, seconds_per_cycle=30)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    harness.run(cycles=1, states=[full(), room()])

    controller = harness.run(cycles=6, states=[full(se.EXPORT_ALLOWED), room()])

    assert controller.charge_direction.charging is False
    assert controller.surplus_export_plan.reason == se.REASON_NO_CHARGE
    assert export_writes(source)[-1] == {"gridReverse": se.EXPORT_REFUSED}


def test_the_export_limit_reads_the_meter_through_the_control_loop():
    source, taker = managed("A"), managed("B")
    harness = harness_for(
        [source, taker], load=-400, surplus_export_limit_w=100, surplus_export_limit_seconds=0
    )
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)

    controller = harness.run(cycles=2, states=[bypassing, room()])

    assert controller.surplus_export_plan.reason == se.REASON_HELD
    assert export_writes(source)[-1] == {"gridReverse": se.EXPORT_REFUSED}


def test_restoring_on_stop_leaves_a_device_the_ems_never_allowed():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    allow_then_report(harness, taker_state=room(se.EXPORT_REFUSED))
    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), room(se.EXPORT_ALLOWED)])
    controller.grid_export.forget("B")
    controller.last_states["B"] = room(se.EXPORT_ALLOWED)
    taker.write_properties.reset_mock()

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True):
        controller.restore_grid_export()

    assert export_writes(taker) == []


def test_a_device_that_starts_passing_its_pv_as_the_charge_starts_is_kept_out_at_once():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    controller = harness.controller
    for _ in range(20):
        controller = harness.run(cycles=1, states=[bypassing, room()])
        if controller.charge_direction.charging:
            break
    assert controller.charge_direction.charging is True

    left_full = reported(state(soc=97, solar=700, output=300, output_limit=300, soc_limit=0, pack_num=2), 1)
    controller = harness.run(cycles=1, states=[left_full, room()], load=-200)

    assert controller.device_charge_limits["A"] == 0


def test_the_share_told_is_where_the_device_ramp_starts():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)

    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED, output=700, output_limit=200), room()])

    assert controller.commanded_device_targets["A"] == 300 - 35


def test_a_told_share_is_forgotten_once_no_charge_and_no_bypass_needs_it():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED, output=700, output_limit=200), room()])
    left_full = reported(state(soc=97, solar=700, output=300, output_limit=300, soc_limit=0, pack_num=2), 1)

    controller = harness.run(cycles=2, states=[left_full, room()], load=0)

    assert "A" not in controller.surplus_export_told


def test_a_device_passing_its_pv_by_the_app_s_switch_is_kept_out_of_the_charge_it_fed():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400, surplus_export_devices=0)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    controller = harness.run(cycles=15, states=[bypassing, room()])
    assert controller.charge_direction.charging is True

    left_full = reported(state(soc=97, solar=700, output=300, output_limit=300, soc_limit=0, pack_num=2), 1)
    controller = harness.run(cycles=1, states=[left_full, room()], load=-200)

    assert controller.device_charge_limits["A"] == 0


def test_a_device_in_bypass_alone_does_not_put_the_ceiling_on_the_held_path():
    source, other = managed("A"), managed("B")
    controller = harness_for([source, other], load=0).controller
    controller.surplus_export_plan = se.SurplusExportPlan(
        state=se.SurplusExportState(), exporting=frozenset({"A"})
    )
    states = [
        full(se.EXPORT_ALLOWED, output=700, output_limit=200),
        state(soc=60, min_soc=15, solar=0, output=0, pack_num=2),
    ]

    assert controller.discharge_ceiling_w(3000, [0, None], states, None) == 3000


def test_the_entry_script_refuses_the_export_on_every_stop():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "ems-solarflow-api-control.py").read_text(encoding="utf-8")
    finally_block = text[text.index('if stopped_by["signal"]:') :]
    after_branches = finally_block[finally_block.index("            ems.release_charging_devices()\n") :][:120]

    assert "\n        ems.restore_grid_export()\n" in after_branches
    assert "grid_export_kept_across_stop" not in text


def bypass_controller():
    source, other = managed("A"), managed("B")
    controller = harness_for([source, other], load=0).controller
    controller.surplus_export_plan = se.SurplusExportPlan(
        state=se.SurplusExportState(), exporting=frozenset({"A"})
    )
    return controller, source, other


def test_the_write_dead_time_forgets_a_device_in_bypass():
    from types import SimpleNamespace

    from ems.mqtt_control.dispatch import WriteDispatchStatus

    controller, source, _other = bypass_controller()
    exporting = controller.surplus_export_plan
    controller.surplus_export_plan = se.SurplusExportPlan(state=se.SurplusExportState())
    with patch("ems.controller.time.monotonic", return_value=50.0):
        controller.note_power_sent(source, SimpleNamespace(status=WriteDispatchStatus.PUBLISHED, target_w=100))
    controller.surplus_export_plan = exporting

    with patch("ems.controller.time.monotonic", return_value=100.0):
        controller.note_power_sent(source, SimpleNamespace(status=WriteDispatchStatus.PUBLISHED, target_w=300))

    assert controller.write_dead_time.unseen_w(75.0, 100.0) == 0


def test_the_first_total_leaves_out_what_a_device_in_bypass_delivers():
    controller, _source, _other = bypass_controller()
    controller.commanded_total_w = None
    states = [
        full(se.EXPORT_ALLOWED, output=700, output_limit=200),
        state(soc=60, solar=0, output=100, output_limit=100, pack_num=2),
    ]

    controller.initialize_commanded_total(states, 3000)

    assert controller.commanded_total_w == 100


def test_a_stop_right_after_an_allow_refuses_it_though_the_report_still_shows_the_refusal():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    controller = harness.run(cycles=1, states=[full(), room()])
    assert export_writes(source) == [{"gridReverse": se.EXPORT_ALLOWED}]
    assert controller.grid_export_allowed_by_ems() == ["A"]
    source.write_properties.reset_mock()

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True):
        controller.restore_grid_export()

    assert export_writes(source) == [{"gridReverse": se.EXPORT_REFUSED}]


def test_control_switched_off_right_after_an_allow_refuses_it_though_the_report_lags():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(system={})
    harness = Harness([source, taker], load=0, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    harness.run(cycles=1, states=[full(), room()])
    source.write_properties.reset_mock()

    runtime.system["enabled"] = False
    harness.run(cycles=1, states=[full(), room()])

    assert export_writes(source) == [{"gridReverse": se.EXPORT_REFUSED}]


def test_stopping_after_control_was_switched_off_still_refuses_the_export_behind_the_write_gates():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(system={})
    harness = Harness([source, taker], load=0, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    allow_then_report(harness)
    runtime.system["enabled"] = False
    harness.state_writes = False
    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), room()])
    assert controller.control_switched_off(source) == ["control_disabled"]
    source.write_properties.reset_mock()

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True):
        controller.restore_grid_export()

    assert export_writes(source) == [{"gridReverse": se.EXPORT_REFUSED}]


def test_cycles_that_keep_failing_after_the_export_write_do_not_switch_it_back_and_forth():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    controller = allow_then_report(harness)
    source.write_properties.reset_mock()

    def following():
        writes = export_writes(source)
        shown = writes[-1]["gridReverse"] if writes else se.EXPORT_ALLOWED
        return [full(shown), room()]

    with patch.object(controller, "grid_meter_reading_is_fresh", side_effect=RuntimeError("boom")), patch(
        "ems.controller.fetch_all_devices", side_effect=lambda _devices: following()
    ), patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True), patch(
        "ems.controller.cfg.SYSTEM_ENABLED", True
    ), patch("ems.controller.cfg.SOC_RECONCILE_INTERVAL", 0), patch.object(
        se_cfg(), "AC_CHARGE_CONTROL_CONFIG", harness.feature
    ):
        for _ in range(8):
            controller.run_guarded()
        reached = controller.grid_meter_reading_is_fresh.call_count

    writes = export_writes(source)
    assert reached == 8
    assert {"gridReverse": se.EXPORT_ALLOWED} not in writes
    assert writes.count({"gridReverse": se.EXPORT_REFUSED}) == 1


def se_cfg():
    from ems import config as cfg

    return cfg


def test_a_device_allowed_but_no_longer_full_takes_no_charge_that_is_about_to_start():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    harness.run(cycles=1, states=[full(), room()])
    near_full = reported(state(soc=99, solar=700, output=35, output_limit=35, soc_limit=0, pack_num=2), 1)

    controller = harness.run(cycles=1, states=[near_full, room()])

    assert controller.charge_direction.charging is False
    assert controller.surplus_export_plan.state.allowed == ("A",)
    assert controller.device_charge_limits["A"] == 0


def test_a_device_still_taken_for_bypass_after_its_pv_is_gone_is_told_the_whole_house_share():
    source = managed("A")
    harness = harness_for([source], load=-400, surplus_export_devices=0)
    harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED, output=700, output_limit=200)])
    night = full(se.EXPORT_ALLOWED, output=300, output_limit=300, solar=0)

    controller = harness.run(cycles=1, states=[night], load=300)

    assert controller.surplus_export_plan.exporting == frozenset({"A"})
    assert limits(harness, source)[-1] == 600


def test_the_grace_covers_three_charge_entry_windows_at_a_slow_loop():
    source, taker = managed("A"), managed("B")
    harness = Harness([source, taker], load=-100, min_output_limit=35, seconds_per_cycle=30)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    with patch("ems.controller.cfg.LOOP_INTERVAL", 30):
        harness.run(cycles=1, states=[full(), room()])
        controller = harness.run(cycles=10, states=[full(se.EXPORT_ALLOWED), room()])

    assert controller.charge_direction.charging is False
    assert controller.surplus_export_plan.grid_export["A"] == "allow"


def test_a_long_charge_fed_by_the_bypass_keeps_the_export_allowed():
    source, taker = managed("A"), managed("B")
    harness = Harness([source, taker], load=-400, min_output_limit=35, seconds_per_cycle=30)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    reasons = []

    for _ in range(40):
        controller = harness.run(cycles=1, states=[bypassing, room()])
        reasons.append(controller.surplus_export_plan.reason)

    assert controller.charge_direction.charging is True
    assert set(reasons) == {se.REASON_PASSING}


def test_an_allow_the_device_already_shows_is_refused_on_stop():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=0)
    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), room()])
    assert export_writes(source) == []
    assert controller.grid_export_allowed_by_ems() == ["A"]

    with patch("ems.controller.cfg.state_reconciliation_writes_allowed", return_value=True):
        controller.restore_grid_export()

    assert export_writes(source) == [{"gridReverse": se.EXPORT_REFUSED}]


def test_a_device_in_an_operator_ac_input_role_is_neither_allowed_nor_told_a_share():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(devices={"A": {"runtime_role": "ac_input"}})
    harness = Harness([source, taker], load=-400, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True

    controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED, output=700, output_limit=200), room()])

    assert controller.surplus_export_plan.grid_export["A"] == "refuse"
    assert "A" not in controller.surplus_export_told


def test_a_device_allowed_during_a_running_charge_stays_out_of_it_when_it_leaves_full():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    dark_full = full(se.EXPORT_REFUSED, output=35, output_limit=35, solar=0)
    controller = harness.run(cycles=15, states=[dark_full, room()])
    assert controller.charge_direction.charging is True
    assert controller.surplus_export_plan.state.allowed == ()

    controller = harness.run(cycles=1, states=[full(se.EXPORT_REFUSED, output=35, output_limit=35), room()])
    assert controller.surplus_export_plan.state.allowed == ("A",)
    left_full = reported(state(soc=97, solar=700, output=35, output_limit=35, soc_limit=0, pack_num=2), 1)
    controller = harness.run(cycles=1, states=[left_full, room()])

    assert controller.charge_direction.charging is True
    assert controller.device_charge_limits["A"] == 0


def test_a_bypass_that_ends_during_a_charge_hands_nothing_to_the_total():
    controller, _source, _other = bypass_controller()
    controller.charge_direction = replace(controller.charge_direction, charging=True)
    controller.commanded_total_w = -500.0
    controller.surplus_export_left = frozenset({"A"})
    controller.surplus_export_told = {"A": 300}

    controller.take_over_left_bypass()

    assert controller.commanded_total_w == -500.0


def test_the_first_total_while_charging_leaves_out_what_a_device_in_bypass_delivers():
    controller, _source, _other = bypass_controller()
    controller.commanded_total_w = None
    controller.charge_direction = replace(controller.charge_direction, charging=True)
    states = [
        full(se.EXPORT_ALLOWED, output=700, output_limit=200),
        state(soc=60, solar=0, output=100, output_limit=100, pack_num=2),
    ]

    controller.initialize_commanded_total(states, 3000)

    assert controller.commanded_total_w == 100


def test_the_first_total_from_outputs_leaves_out_what_a_device_in_bypass_delivers():
    controller, _source, _other = bypass_controller()
    controller.commanded_total_w = None
    states = [
        full(se.EXPORT_ALLOWED, output=700, output_limit=0),
        state(soc=60, solar=0, output=100, output_limit=0, pack_num=2),
    ]

    controller.initialize_commanded_total(states, 3000)

    assert controller.commanded_total_w == 100


def test_the_limits_never_add_up_to_more_than_the_installation_beside_a_bypass():
    source, other = managed("A"), managed("B")
    harness = harness_for([source, other], load=600)
    bypassing = full(se.EXPORT_ALLOWED, output=300, output_limit=200, solar=300)
    discharging = reported(state(soc=60, solar=0, output=0, soc_limit=0, pack_num=2), se.EXPORT_REFUSED)

    for _ in range(4):
        harness.run(cycles=1, states=[bypassing, discharging])
        assert limits(harness, source)[-1] + limits(harness, other)[-1] <= 800


def test_the_share_is_what_the_house_draws_beyond_the_other_devices():
    source, other = managed("A"), managed("B")
    harness = harness_for([source, other], load=300)
    bypassing = full(se.EXPORT_ALLOWED, output=300, output_limit=200, solar=300)
    discharging = reported(state(soc=60, solar=0, output=0, soc_limit=0, pack_num=2), se.EXPORT_REFUSED)

    harness.run(cycles=3, states=[bypassing, discharging])

    assert limits(harness, source)[-1] + limits(harness, other)[-1] <= 600


def test_switching_ac_charging_off_refuses_the_export_at_once():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    harness.run(cycles=15, states=[bypassing, room()])
    source.write_properties.reset_mock()

    harness.feature = {**harness.feature, "enabled": False}
    controller = harness.run(cycles=1, states=[bypassing, room()])

    assert controller.surplus_export_plan.reason == se.REASON_NO_TAKER
    assert export_writes(source) == [{"gridReverse": se.EXPORT_REFUSED}]


def test_the_grace_is_at_least_120_s_at_the_default_loop():
    source, taker = managed("A"), managed("B")
    harness = Harness([source, taker], load=-100, min_output_limit=35, seconds_per_cycle=10)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    allowed_at = harness.clock.now
    harness.run(cycles=1, states=[full(), room()])
    refused_at = None
    for _ in range(20):
        now = harness.clock.now
        controller = harness.run(cycles=1, states=[full(se.EXPORT_ALLOWED), room()])
        if controller.surplus_export_plan.reason == se.REASON_NO_CHARGE:
            refused_at = now
            break

    assert refused_at is not None
    assert refused_at - allowed_at >= 10 + se.START_GRACE_SECONDS


def test_switching_the_taker_off_refuses_the_export_at_once():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(devices={})
    harness = Harness([source, taker], load=-400, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    harness.run(cycles=15, states=[bypassing, room()])

    runtime.devices["B"] = {"enabled": False}
    controller = harness.run(cycles=1, states=[bypassing, room()])

    assert controller.surplus_export_plan.reason == se.REASON_NO_TAKER


def test_a_taker_an_operator_parks_in_ac_input_ends_the_export_at_once():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(devices={})
    harness = Harness([source, taker], load=-400, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    harness.run(cycles=15, states=[bypassing, room()])

    runtime.devices["B"] = {"runtime_role": "ac_input"}
    controller = harness.run(cycles=1, states=[bypassing, room()])

    assert controller.surplus_export_plan.reason == se.REASON_NO_TAKER


def test_a_device_the_ems_does_not_command_counts_at_what_it_was_last_given():
    source, off, taker = managed("A"), managed("B"), managed("C")
    runtime = RuntimeStateStub(devices={"B": {"enabled": False}})
    harness = Harness([source, off, taker], load=200, min_output_limit=35, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    bypassing = full(se.EXPORT_ALLOWED, output=300, output_limit=200, solar=300)
    feeding = reported(state(soc=60, solar=0, output=400, output_limit=400, pack_num=2), se.EXPORT_REFUSED)

    controller = harness.run(cycles=1, states=[bypassing, feeding, room()])

    house, installation = 300 + 400 + 200, 800
    others = 400 + max(0, controller.last_control_explanation.devices["C"].effective_target_w)
    assert limits(harness, source)[-1] == min(house - others, installation - others)


def test_several_devices_in_bypass_share_what_the_others_leave():
    first, second, other = managed("A"), managed("C"), managed("B")
    harness = harness_for([first, second, other], load=300)
    harness.feature = {**harness.feature, "surplus_export_devices": 2}
    discharging = reported(state(soc=60, solar=0, output=0, soc_limit=0, pack_num=2), se.EXPORT_REFUSED)

    controller = harness.run(
        cycles=1,
        states=[
            full(se.EXPORT_ALLOWED, output=400, output_limit=100, solar=400),
            full(se.EXPORT_ALLOWED, output=200, output_limit=100, solar=200),
            discharging,
        ],
    )

    assert controller.surplus_export_plan.exporting == frozenset({"A", "C"})
    beside = max(0, controller.last_control_explanation.devices["B"].effective_target_w)
    needed = min(900 - beside, 800 - beside)
    told = limits(harness, first)[-1] + limits(harness, second)[-1]
    assert needed - 1 <= told <= needed
    assert limits(harness, first)[-1] == 2 * limits(harness, second)[-1]


def test_a_device_held_at_its_share_counts_at_that_share_beside_another_bypass():
    first, second, taker = managed("A"), managed("C"), managed("B")
    harness = harness_for([first, second, taker], load=-600, surplus_export_devices=2)
    passing_a = full(se.EXPORT_ALLOWED, output=500, output_limit=100, solar=500)
    passing_c = full(se.EXPORT_ALLOWED, output=400, output_limit=100, solar=400)
    controller = harness.run(cycles=15, states=[passing_a, passing_c, room()])
    assert controller.charge_direction.charging is True
    held = controller.surplus_export_told["A"]

    left_full = reported(state(soc=97, solar=500, output=held, output_limit=held, soc_limit=0, pack_num=2), 1)
    controller = harness.run(cycles=1, states=[left_full, passing_c, room()], load=-300)

    house = held + 400 - 300
    told = controller.last_control_explanation.devices
    assert told["A"].effective_target_w == held
    assert told["C"].effective_target_w == max(35, min(house - held, 800 - held))


def test_a_taker_the_full_charge_assist_holds_ends_the_export_at_once():
    source, taker = managed("A"), managed("B")
    harness = harness_for([source, taker], load=-400)
    bypassing = full(se.EXPORT_ALLOWED, output=700, output_limit=200)
    controller = harness.run(cycles=15, states=[bypassing, room()])

    with patch.object(controller, "full_charge_assist_holds_direction", lambda dev: dev.name == "B"):
        controller = harness.run(cycles=1, states=[bypassing, room()])

    assert controller.surplus_export_plan.reason == se.REASON_NO_TAKER


def test_the_grace_reads_the_meter_against_the_discharge_grid_target():
    source, taker = managed("A"), managed("B")
    runtime = RuntimeStateStub(system={"grid_target_discharge_w": -100})
    harness = Harness([source, taker], load=-100, min_output_limit=35, seconds_per_cycle=30, runtime_state=runtime)
    harness.feature = {**AC_CHARGE_CONTROL_DEFAULTS}
    harness.state_writes = True
    harness.run(cycles=1, states=[full(), room()])

    controller = harness.run(cycles=10, states=[full(se.EXPORT_ALLOWED), room()])

    assert controller.grid_target_w == -100
    assert controller.surplus_export_plan.grid_export["A"] == "allow"
