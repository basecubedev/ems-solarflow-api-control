# SPDX-License-Identifier: AGPL-3.0-or-later
from types import SimpleNamespace

import pytest

from ems.clients import zero_device_state
from ems.runtime_intents import (
    FIRMWARE_CHARGE_REASON,
    PRIORITY_DEFAULT,
    PRIORITY_FIRMWARE_OBSERVED,
    PRIORITY_MAINTENANCE,
    PRIORITY_OPERATOR_PARK,
    DeviceRuntimeRole,
    ac_input_intent,
    ac_output_intent,
    firmware_charge_intent,
    resolve_device_intent,
    runtime_intent_from_role,
)


def telemetry(ac_mode=2, ac_status=1, soc_limit=2, soc=9, min_soc=15, max_soc=100):
    """Telemetry for a device at its discharge floor unless told otherwise."""

    return SimpleNamespace(
        ac_mode=ac_mode,
        ac_status=ac_status,
        soc_limit=soc_limit,
        soc=soc,
        min_soc=min_soc,
        max_soc=max_soc,
    )


def healthy(ac_mode=1, ac_status=2):
    """A charging device whose battery is nowhere near its floor."""

    return telemetry(ac_mode, ac_status, soc_limit=0, soc=60, min_soc=15)

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


def test_firmware_charge_claims_the_device_and_commands_nothing():
    intent = firmware_charge_intent("WR1", telemetry(ac_mode=1, ac_status=2))

    assert intent.reason == FIRMWARE_CHARGE_REASON
    assert intent.priority == PRIORITY_FIRMWARE_OBSERVED
    assert intent.role is DeviceRuntimeRole.AC_INPUT
    assert intent.desired_ac_mode is None
    assert intent.setpoint_w is None
    assert intent.output_control_allowed is False


def test_a_charge_the_ems_asked_for_is_not_firmware_owned():
    """Without this the regulator reads its own charge back as the firmware's.

    The device would be marked uncommandable, drop out of the chargeable set,
    and the regulator would shut itself down two cycles after starting.
    """

    assert firmware_charge_intent("WR1", telemetry(1, 2)) is not None
    assert (
        firmware_charge_intent("WR1", telemetry(1, 2), ems_commanded_charge=True)
        is None
    )


def test_a_charge_above_the_floor_is_a_leftover_not_a_firmware_action():
    """The firmware acts at the floor; anything higher is ours to take back.

    An EMS that died mid-charge, or a charge started from the vendor app, would
    otherwise be protected forever by a claim meant for emergency recovery.
    """

    assert firmware_charge_intent("WR1", healthy()) is None


def test_a_deliberate_claim_outranks_the_firmware_observation():
    """The observation only overrides the loop's reflexive default.

    It must never block a claim the EMS made on purpose — an operator parking
    the device, or the full-charge assist restoring it to output. Getting this
    wrong deadlocked the assist restore, which retries acMode=2 against a device
    that is still reporting a charge.
    """

    firmware = firmware_charge_intent("WR1", telemetry(1, 2))
    parked = ac_input_intent("WR1", "operator", priority=PRIORITY_OPERATOR_PARK)
    restoring = ac_output_intent("WR1", "assist_restore", priority=PRIORITY_MAINTENANCE)

    assert resolve_device_intent([ac_output_intent("WR1"), firmware]) is firmware
    assert resolve_device_intent([parked, firmware]) is parked
    assert resolve_device_intent([restoring, firmware]) is restoring


def test_mode_and_status_must_agree():
    # acMode written but the device has not followed yet (~2 s settling window).
    assert firmware_charge_intent("WR1", telemetry(ac_mode=1, ac_status=1)) is None
    # Mode already restored while the status still lags behind.
    assert firmware_charge_intent("WR1", telemetry(ac_mode=2, ac_status=2)) is None


def test_an_unreachable_device_never_claims_a_firmware_charge():
    assert firmware_charge_intent("WR1", zero_device_state()) is None


def test_unreadable_telemetry_fails_closed():
    assert firmware_charge_intent("WR1", telemetry(ac_mode="?", ac_status="?")) is None
    assert firmware_charge_intent("WR1", telemetry(ac_mode=None, ac_status=None)) is None
    assert firmware_charge_intent("WR1", SimpleNamespace()) is None


def test_the_firmware_observation_sits_just_above_the_default():
    assert PRIORITY_DEFAULT < PRIORITY_FIRMWARE_OBSERVED < PRIORITY_OPERATOR_PARK
    assert firmware_charge_intent("WR1", telemetry(1, 2)).priority == (
        PRIORITY_FIRMWARE_OBSERVED
    )
