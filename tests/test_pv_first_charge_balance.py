import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ems.models import DeviceState
from ems.target_control import calculate_targets


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
