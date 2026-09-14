# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from ems.runtime_intents import (
    PRIORITY_DEFAULT,
    PRIORITY_MAINTENANCE,
    PRIORITY_OPERATOR_PARK,
    DeviceRuntimeRole,
    ac_input_intent,
    ac_output_intent,
    resolve_device_intent,
    runtime_intent_from_role,
)

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]


def test_ac_output_intent_targets_ac_mode_2_and_allows_output():
    intent = ac_output_intent("WR1")

    assert intent.device == "WR1"
    assert intent.role is DeviceRuntimeRole.AC_OUTPUT
    assert intent.desired_ac_mode == 2
    assert intent.output_control_allowed is True


def test_ac_input_intent_targets_ac_mode_1_and_blocks_output():
    intent = ac_input_intent("WR1", "manual_test")

    assert intent.device == "WR1"
    assert intent.role is DeviceRuntimeRole.AC_INPUT
    assert intent.reason == "manual_test"
    assert intent.desired_ac_mode == 1
    assert intent.output_control_allowed is False


def test_runtime_intent_from_role_accepts_normalized_roles():
    output = runtime_intent_from_role("WR1", "ac_output")
    charge = runtime_intent_from_role("WR1", "ac_input", "manual_test")

    assert output.role is DeviceRuntimeRole.AC_OUTPUT
    assert output.desired_ac_mode == 2
    assert output.output_control_allowed is True
    assert charge.role is DeviceRuntimeRole.AC_INPUT
    assert charge.reason == "manual_test"
    assert charge.desired_ac_mode == 1
    assert charge.output_control_allowed is False


def test_runtime_intent_from_role_maps_legacy_roles_safely():
    normal = runtime_intent_from_role("WR1", "normal_output")
    charge = runtime_intent_from_role("WR1", "ac_input_charge")
    reserved = runtime_intent_from_role("WR1", "reserved")

    assert normal.role is DeviceRuntimeRole.AC_OUTPUT
    assert normal.output_control_allowed is True
    assert charge.role is DeviceRuntimeRole.AC_INPUT
    assert charge.output_control_allowed is False
    assert reserved.role is DeviceRuntimeRole.AC_INPUT
    assert reserved.output_control_allowed is False


def test_runtime_intent_from_role_unknown_returns_none():
    assert runtime_intent_from_role("WR1", "unsupported") is None


def test_an_intent_carries_its_own_setpoint():
    parked = ac_input_intent("WR1", "manual_test", setpoint_w=200)
    claimed_without_value = ac_input_intent("WR1", "firmware_owned")

    assert parked.setpoint_w == 200
    assert claimed_without_value.setpoint_w is None
    assert ac_output_intent("WR1").setpoint_w is None


def test_resolve_picks_the_highest_priority_regardless_of_order():
    default = ac_output_intent("WR1")
    parked = ac_input_intent("WR1", "operator", priority=PRIORITY_OPERATOR_PARK)
    maintenance = ac_input_intent("WR1", "assist", priority=PRIORITY_MAINTENANCE)

    assert resolve_device_intent([default, parked, maintenance]) is maintenance
    assert resolve_device_intent([maintenance, parked, default]) is maintenance
    assert resolve_device_intent([default, parked]) is parked


def test_resolve_ignores_absent_candidates():
    default = ac_output_intent("WR1")

    assert resolve_device_intent([None, default, None]) is default
    assert resolve_device_intent([None, None]) is None
    assert resolve_device_intent([]) is None


def test_resolve_keeps_the_earlier_candidate_on_a_tie():
    first = ac_output_intent("WR1", "first", priority=PRIORITY_DEFAULT)
    second = ac_output_intent("WR1", "second", priority=PRIORITY_DEFAULT)

    assert resolve_device_intent([first, second]) is first
