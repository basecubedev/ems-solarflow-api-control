# SPDX-License-Identifier: AGPL-3.0-or-later
"""ZenSDK power-operation contracts (atomic mode+power property sets)."""

import pytest

from ems.mqtt_control.zensdk_operations import (
    ZenSdkOperationError,
    build_zensdk_power_operation,
)

pytestmark = [pytest.mark.simulation, pytest.mark.power_control]


def test_discharge_contract_is_the_atomic_source_backed_set():
    op = build_zensdk_power_operation(300)
    assert op.operation == "discharge"
    assert op.properties == {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 300,
        "inputLimit": 0,
    }
    assert op.expected_properties == op.properties


def test_idle_contract_stays_in_smart_output_regulation():
    # Deliberate deviation from Zendure-HA power_off (smartMode 0): the EMS
    # five-second loop crosses 0 W routinely and must not toggle a
    # flash-persistent operating mode on every crossing.
    op = build_zensdk_power_operation(0)
    assert op.operation == "idle"
    assert op.properties == {
        "smartMode": 1,
        "acMode": 2,
        "outputLimit": 0,
        "inputLimit": 0,
    }


def test_charge_has_no_verified_contract_and_fails_closed():
    with pytest.raises(ZenSdkOperationError):
        build_zensdk_power_operation(-300)


@pytest.mark.parametrize("bad", [True, 300.0, "300", None])
def test_non_integer_targets_are_rejected(bad):
    with pytest.raises(ZenSdkOperationError):
        build_zensdk_power_operation(bad)


def test_expected_properties_are_a_copy_not_an_alias():
    op = build_zensdk_power_operation(200)
    assert op.expected_properties is not op.properties
