# SPDX-License-Identifier: AGPL-3.0-or-later
from ems.runtime_intents import (
    DeviceRuntimeRole,
    ac_input_intent,
    ac_output_intent,
    runtime_intent_from_role,
)


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
