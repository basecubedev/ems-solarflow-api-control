# SPDX-License-Identifier: AGPL-3.0-or-later
"""ZenSDK power-operation contracts (atomic mode+power property sets)."""

import pytest

from ems.power_command import (
    ZenSdkOperationError,
    build_zensdk_power_operation,
)

pytestmark = [
    pytest.mark.mqtt,
    pytest.mark.unit,
    pytest.mark.simulation,
    pytest.mark.power_control,
]


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


def test_charge_contract_is_the_measured_atomic_set():
    """The charge shape, as observed on a SolarFlow 800 Pro 2 on 2026-09-13.

    Writing this set produced gridInputPower 150 and outputPackPower 210 within
    4.5 s; acMode echoed at ~2.4 s and acStatus reached 2 at ~4.5 s. The negative
    EMS target becomes a positive charging watt value, which is the sign
    convention the write adapter owns.
    """

    op = build_zensdk_power_operation(-150)

    assert op.operation == "charge"
    assert op.properties == {
        "smartMode": 1,
        "acMode": 1,
        "outputLimit": 0,
        "inputLimit": 150,
    }
    assert op.expected_properties == op.properties
    assert op.expected_properties is not op.properties


@pytest.mark.parametrize("bad", [True, 300.0, "300", None])
def test_non_integer_targets_are_rejected(bad):
    with pytest.raises(ZenSdkOperationError):
        build_zensdk_power_operation(bad)


def test_expected_properties_are_a_copy_not_an_alias():
    op = build_zensdk_power_operation(200)
    assert op.expected_properties is not op.properties
