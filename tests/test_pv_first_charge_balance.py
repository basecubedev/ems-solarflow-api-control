# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ems.models import DeviceState
from ems.target_control import calculate_targets

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]


def device(name):
    return SimpleNamespace(
        name=name,
        max_power=800,
        pv_kwp=1.0,
        pv_priority_factor=1.0,
        battery_kwh=1.0,
        min_soc=15,
        max_soc=100
    )


def state(soc, solar, pack_in=0, min_soc=15, max_soc=100):
    return DeviceState(
        soc=soc,
        min_soc=min_soc,
        max_soc=max_soc,
        solar=solar,
        output=0,
        pack_in=pack_in,
        pack_out=0,
        temp=20,
        voltage=48,
        rssi=0,
        remain_minutes=0,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
        output_limit=0,
        soc_limit=0,
        pack_state=2,
        fault_level=0,
        smart_mode=1,
        grid_off_mode=0,
        ac_mode=2,
        ac_status=1,
        dc_status=1,
        grid_state=1
    )


class PvFirstChargeBalanceTest(unittest.TestCase):
    def calculate(self, states, requested_total, enabled=True):
        devices = [device(f"WR{i + 1}") for i in range(len(states))]

        with patch(
            "ems.target_control.cfg.REDISTRIBUTE_CLAMPED_POWER",
            True
        ), patch(
            "ems.target_control.cfg.PV_KWP_WEIGHTING",
            True
        ), patch(
            "ems.target_control.cfg.BATTERY_KWH_WEIGHTING",
            True
        ), patch(
            "ems.target_control.cfg.PV_CHARGE_BALANCE_ENABLED",
            enabled
        ), patch(
            "ems.target_control.cfg.PV_CHARGE_BALANCE_DEADBAND_PERCENT",
            5.0
        ), patch(
            "ems.target_control.cfg.PV_CHARGE_BALANCE_FULL_BIAS_PERCENT",
            15.0
        ), patch(
            "ems.target_control.cfg.PV_CHARGE_BALANCE_STRENGTH",
            1.0
        ):
            targets, _, _ = calculate_targets(
                load=0,
                devices=states,
                max_power=800,
                device_configs=devices,
                requested_total=requested_total
            )

        return targets

    def test_pv_first_without_soc_gap_stays_pv_weighted(self):
        targets = self.calculate(
            [
                state(soc=50, solar=400),
                state(soc=52, solar=400)
            ],
            requested_total=600
        )

        self.assertEqual([300, 300], targets)

    def test_lower_soc_device_keeps_local_pv_in_pv_first(self):
        states = [
            state(soc=80, solar=600),
            state(soc=40, solar=400)
        ]

        targets = self.calculate(states, requested_total=600)

        self.assertEqual([600, 0], targets)
        self.assertLessEqual(targets[0], states[0].solar - states[0].pack_in)
        self.assertLessEqual(targets[1], states[1].solar - states[1].pack_in)

    def test_pv_first_topup_can_fill_after_device_cap_clamp(self):
        devices = [
            SimpleNamespace(
                name="WR1",
                max_power=600,
                pv_kwp=1.0,
                pv_priority_factor=1.0,
                battery_kwh=1.0,
                min_soc=15,
                max_soc=100
            ),
            device("WR2")
        ]
        states = [
            state(soc=80, solar=800),
            state(soc=40, solar=100)
        ]

        with patch(
            "ems.target_control.cfg.REDISTRIBUTE_CLAMPED_POWER",
            True
        ), patch(
            "ems.target_control.cfg.PV_KWP_WEIGHTING",
            True
        ), patch(
            "ems.target_control.cfg.PV_CHARGE_BALANCE_ENABLED",
            False
        ):
            targets, _, _ = calculate_targets(
                load=0,
                devices=states,
                max_power=800,
                device_configs=devices,
                requested_total=800
            )

        self.assertEqual([600, 200], targets)
        self.assertEqual(800, sum(targets))

    def test_disabling_feature_restores_previous_pv_first_allocation(self):
        targets = self.calculate(
            [
                state(soc=80, solar=600),
                state(soc=40, solar=400)
            ],
            requested_total=600,
            enabled=False
        )

        self.assertEqual([360, 240], targets)

    def test_discharge_branch_still_balances_by_usable_soc(self):
        states = [
            state(soc=80, solar=100),
            state(soc=40, solar=100)
        ]

        enabled_targets = self.calculate(
            states,
            requested_total=600,
            enabled=True
        )
        disabled_targets = self.calculate(
            states,
            requested_total=600,
            enabled=False
        )

        self.assertEqual(disabled_targets, enabled_targets)
        self.assertGreater(enabled_targets[0], enabled_targets[1])


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("requested", [500, 800, 850, 900, 1000, 1100])
def test_the_balance_gives_back_the_pv_it_kept_when_no_battery_can_cover(requested):
    """A preference, not a reason to draw from the grid.

    The emptier batteries at their minSoc keep their PV, and the fuller one tops
    the house up to its rating. Past that nothing covered the rest: the split
    stayed at 800 W up to the summed PV while the kept PV went into the emptier
    batteries, then jumped to all of it.
    """

    states = [state(80, 100), state(15, 500), state(15, 500)]

    targets, _current, _new = calculate_targets(
        0,
        states,
        2400,
        device_configs=[device("WR1"), device("WR2"), device("WR3")],
        requested_total=requested,
    )

    assert sum(targets) == requested
    assert targets[0] == min(requested, 800)


def test_pv_the_balance_gives_back_is_no_shortfall_and_says_so(caplog):
    states = [state(60, 300, min_soc=60), state(15, 400)]

    with caplog.at_level(logging.DEBUG):
        targets, _current, _new, explanation = calculate_targets(
            0,
            states,
            1600,
            device_configs=[device("WR1"), device("WR2")],
            requested_total=500,
            explain=True,
        )

    assert targets == [300, 200]
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "pv_first_battery_topup_unmet" in record.getMessage()
    ]
    assert "PV the charge balance kept back covered what no battery could" in explanation.notes
    pv_only = next(
        limit for limit in explanation.limits if limit.name == "pv_only_target_limit"
    )
    assert (pv_only.active, pv_only.value) == (False, 0)
    assert explanation.devices["WR2"].decision_reason != "pv_first_with_battery_topup"


@pytest.mark.parametrize(
    "emptier_pv,unmet",
    [(150, 150), (400, 0)],
    ids=["still short", "covered"],
)
def test_the_pv_only_limit_counts_what_a_device_can_deliver(emptier_pv, unmet):
    """PV above a device's rating is no PV-first share it can deliver."""

    states = [state(60, 1000, min_soc=60), state(15, emptier_pv)]

    targets, _current, _new, explanation = calculate_targets(
        0,
        states,
        1600,
        device_configs=[device("WR1"), device("WR2")],
        requested_total=1100,
        explain=True,
    )

    pv_only = next(
        limit for limit in explanation.limits if limit.name == "pv_only_target_limit"
    )
    assert 1100 - sum(targets) == unmet
    assert (pv_only.active, pv_only.value) == (unmet > 0, unmet)
