import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from ems.models import DeviceCapabilities, DeviceState
from ems.target_control import calculate_targets, detect_capabilities


def device(
    name,
    max_power=800,
    pv_kwp=1.0,
    pv_priority_factor=1.0,
    battery_kwh=1.0,
    min_soc=15,
    max_soc=100
):
    return SimpleNamespace(
        name=name,
        max_power=max_power,
        pv_kwp=pv_kwp,
        pv_priority_factor=pv_priority_factor,
        battery_kwh=battery_kwh,
        min_soc=min_soc,
        max_soc=max_soc
    )


def state(
    soc=50,
    min_soc=15,
    max_soc=100,
    solar=0,
    output=0,
    output_limit=0,
    pack_in=0,
    pack_out=0,
    soc_limit=0,
    pack_state=2,
    ac_status=1,
    dc_status=1
):
    return DeviceState(
        soc=soc,
        min_soc=min_soc,
        max_soc=max_soc,
        solar=solar,
        output=output,
        pack_in=pack_in,
        pack_out=pack_out,
        temp=20,
        voltage=48,
        rssi=0,
        remain_minutes=0,
        solar1=0,
        solar2=0,
        solar3=0,
        solar4=0,
        output_limit=output_limit,
        soc_limit=soc_limit,
        pack_state=pack_state,
        fault_level=0,
        smart_mode=1,
        grid_off_mode=0,
        ac_mode=2,
        ac_status=ac_status,
        dc_status=dc_status,
        grid_state=1
    )


class EnergyAllocationTest(unittest.TestCase):
    def calculate(
        self,
        states,
        device_configs=None,
        requested_total=0,
        max_power=2000,
        capabilities=None,
        redistribute=True,
        pv_kwp_weighting=True,
        battery_kwh_weighting=True,
        pv_charge_balance_enabled=False,
        pv_charge_balance_deadband_percent=5.0,
        pv_charge_balance_full_bias_percent=15.0,
        pv_charge_balance_strength=1.0,
        explain=False
    ):
        if device_configs is None:
            device_configs = [
                device(f"WR{i + 1}")
                for i in range(len(states))
            ]

        patches = {
            "REDISTRIBUTE_CLAMPED_POWER": redistribute,
            "PV_KWP_WEIGHTING": pv_kwp_weighting,
            "BATTERY_KWH_WEIGHTING": battery_kwh_weighting,
            "PV_CHARGE_BALANCE_ENABLED": pv_charge_balance_enabled,
            "PV_CHARGE_BALANCE_DEADBAND_PERCENT": (
                pv_charge_balance_deadband_percent
            ),
            "PV_CHARGE_BALANCE_FULL_BIAS_PERCENT": (
                pv_charge_balance_full_bias_percent
            ),
            "PV_CHARGE_BALANCE_STRENGTH": pv_charge_balance_strength
        }

        with ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(
                    patch(f"ems.target_control.cfg.{name}", value)
                )

            return calculate_targets(
                load=0,
                devices=states,
                max_power=max_power,
                device_configs=device_configs,
                capabilities=capabilities,
                requested_total=requested_total,
                explain=explain
            )

    def assert_total(self, targets, expected, delta=1):
        self.assertAlmostEqual(sum(targets), expected, delta=delta)

    def assert_pv_only_limits(self, targets, states):
        for target, current in zip(targets, states):
            self.assertLessEqual(target, max(0, current.solar - current.pack_in))

    def assert_device_caps(self, targets, device_configs):
        for target, config in zip(targets, device_configs):
            self.assertGreaterEqual(target, 0)
            self.assertLessEqual(target, config.max_power)

    def test_pv_first_equal_devices_allocate_equally(self):
        states = [
            state(soc=50, solar=400),
            state(soc=50, solar=400)
        ]

        targets, current_total, new_total = self.calculate(
            states,
            requested_total=600
        )

        self.assertEqual(0, current_total)
        self.assertEqual(600, new_total)
        self.assertEqual([300, 300], targets)
        self.assert_pv_only_limits(targets, states)

    def test_pv_first_current_pv_difference_drives_larger_share(self):
        states = [
            state(soc=50, solar=600),
            state(soc=50, solar=200)
        ]

        targets, _, _ = self.calculate(states, requested_total=600)

        self.assertGreater(targets[0], targets[1])
        self.assert_pv_only_limits(targets, states)
        self.assert_total(targets, 600)

    def test_pv_kwp_weighting_changes_pv_first_share(self):
        states = [
            state(soc=50, solar=400),
            state(soc=50, solar=400)
        ]
        configs = [
            device("WR1", pv_kwp=2.0),
            device("WR2", pv_kwp=1.0)
        ]

        weighted_targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=600,
            pv_kwp_weighting=True
        )
        unweighted_targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=600,
            pv_kwp_weighting=False
        )

        self.assertGreater(weighted_targets[0], weighted_targets[1])
        self.assertEqual([300, 300], unweighted_targets)
        self.assert_pv_only_limits(weighted_targets, states)
        self.assert_total(weighted_targets, 600)

    def test_pv_priority_factor_amplifies_pv_first_share(self):
        states = [
            state(soc=50, solar=400),
            state(soc=50, solar=400)
        ]
        configs = [
            device("WR1", pv_priority_factor=1.5),
            device("WR2", pv_priority_factor=1.0)
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=600
        )

        self.assertGreater(targets[0], targets[1])
        self.assert_pv_only_limits(targets, states)
        self.assert_total(targets, 600)

    def test_control_explanation_contains_two_device_values(self):
        states = [
            state(soc=52, solar=600, output_limit=420),
            state(soc=50, solar=300, output_limit=210)
        ]
        configs = [
            device("WR1", pv_priority_factor=1.2, battery_kwh=2.0),
            device("WR2", pv_priority_factor=1.0, battery_kwh=1.0)
        ]

        expected_targets, expected_current, expected_new = self.calculate(
            states,
            configs,
            requested_total=600,
            pv_charge_balance_enabled=True
        )
        targets, current_total, new_total, explanation = self.calculate(
            states,
            configs,
            requested_total=600,
            pv_charge_balance_enabled=True,
            explain=True
        )

        self.assertEqual(expected_targets, targets)
        self.assertEqual(expected_current, current_total)
        self.assertEqual(expected_new, new_total)
        self.assertGreater(targets[0], 0)
        self.assertGreater(targets[1], 0)

        payload = explanation.to_dict()
        json.dumps(payload)

        self.assertEqual("pv_first", payload["mode"])
        self.assertEqual(600, payload["requested_total_w"])
        self.assertEqual(sum(targets), payload["allocated_target_total_w"])
        self.assertEqual(sum(targets), payload["effective_target_total_w"])
        self.assertEqual(600, payload["commanded_total_w"])
        self.assertIn("WR1", payload["devices"])
        self.assertIn("WR2", payload["devices"])

        wr1 = payload["devices"]["WR1"]
        wr2 = payload["devices"]["WR2"]
        self.assertEqual(600, wr1["pv_input_w"])
        self.assertEqual(300, wr2["pv_input_w"])
        self.assertEqual(52, wr1["soc"])
        self.assertEqual(50, wr2["soc"])
        self.assertEqual(1.2, wr1["pv_priority_factor"])
        self.assertIsNotNone(wr1["pv_weight"])
        self.assertIsNotNone(wr1["charge_balance_multiplier"])
        self.assertIsNotNone(wr1["soc_gap_percent"])
        self.assertEqual(targets[0], wr1["allocated_target_w"])
        self.assertEqual(targets[1], wr2["effective_target_w"])
        self.assertEqual(420, wr1["output_limit_w"])

    def test_pv_first_battery_topup_survives_final_constraints(self):
        states = [
            state(soc=40, min_soc=15, solar=724),
            state(soc=80, min_soc=15, solar=1000)
        ]
        configs = [
            device("WR1", max_power=800),
            device("WR2", max_power=800)
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=1600,
            max_power=1600,
            pv_charge_balance_enabled=True
        )

        self.assertEqual([800, 800], targets)
        self.assertGreater(targets[0], states[0].solar)
        self.assert_total(targets, 1600)

    def test_pv_first_keeps_pv_only_limits_when_no_topup_is_needed(self):
        states = [
            state(soc=40, min_soc=15, solar=724),
            state(soc=80, min_soc=15, solar=1000)
        ]
        configs = [
            device("WR1", max_power=800),
            device("WR2", max_power=800)
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=1200,
            max_power=1600,
            pv_charge_balance_enabled=False
        )

        self.assert_pv_only_limits(targets, states)
        self.assert_total(targets, 1200)

    def test_pv_first_battery_topup_respects_discharge_protection(self):
        states = [
            state(soc=20, min_soc=20, solar=800, pack_in=600),
            state(soc=70, min_soc=20, solar=900, pack_in=200)
        ]
        configs = [
            device("WR1", max_power=800),
            device("WR2", max_power=800)
        ]
        capabilities = [
            DeviceCapabilities(
                can_charge=True,
                can_discharge=False,
                can_export=True,
                can_ac_charge=False,
                reason="test_cannot_discharge"
            ),
            DeviceCapabilities(
                can_charge=True,
                can_discharge=True,
                can_export=True,
                can_ac_charge=False,
                reason="test_can_discharge"
            )
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=1000,
            max_power=1000,
            capabilities=capabilities
        )

        self.assertEqual([200, 800], targets)
        self.assertEqual(targets[0], states[0].solar - states[0].pack_in)
        self.assertEqual(
            targets[1],
            states[1].solar - states[1].pack_in + 100
        )
        self.assert_total(targets, 1000)

    def test_pv_charge_balance_biases_output_to_higher_soc_device(self):
        states = [
            state(soc=80, solar=600),
            state(soc=40, solar=400)
        ]

        enabled_targets, _, _ = self.calculate(
            states,
            requested_total=600,
            pv_charge_balance_enabled=True
        )
        disabled_targets, _, _ = self.calculate(
            states,
            requested_total=600,
            pv_charge_balance_enabled=False
        )

        self.assertEqual([600, 0], enabled_targets)
        self.assertEqual([360, 240], disabled_targets)
        self.assert_pv_only_limits(enabled_targets, states)
        self.assert_pv_only_limits(disabled_targets, states)

    def test_full_soc_device_gets_pv_export_priority(self):
        states = [
            state(
                soc=100,
                max_soc=100,
                solar=722,
                output=657,
                output_limit=657,
                soc_limit=1,
                pack_state=2
            ),
            state(
                soc=98,
                max_soc=100,
                solar=735,
                output=364,
                output_limit=364,
                soc_limit=0,
                pack_out=371,
                pack_state=1
            )
        ]

        targets, _, _ = self.calculate(
            states,
            requested_total=1000,
            max_power=1600,
            pv_charge_balance_enabled=True
        )

        self.assertEqual([722, 278], targets)
        self.assertGreater(targets[0], targets[1])
        self.assertLess(targets[1], states[1].solar)
        self.assert_pv_only_limits(targets, states)
        self.assert_total(targets, 1000)

    def test_no_full_device_preserves_charge_balance_allocation(self):
        states = [
            state(soc=80, solar=600),
            state(soc=40, solar=400)
        ]

        targets, _, _ = self.calculate(
            states,
            requested_total=600,
            pv_charge_balance_enabled=True
        )

        self.assertEqual([600, 0], targets)
        self.assert_pv_only_limits(targets, states)

    def test_full_soc_device_that_cannot_export_is_skipped(self):
        states = [
            state(soc=100, max_soc=100, solar=700, soc_limit=1),
            state(soc=80, max_soc=100, solar=700, soc_limit=0)
        ]
        capabilities = [
            DeviceCapabilities(
                can_charge=False,
                can_discharge=True,
                can_export=False,
                can_ac_charge=False,
                reason="test_cannot_export"
            ),
            DeviceCapabilities(
                can_charge=True,
                can_discharge=True,
                can_export=True,
                can_ac_charge=False,
                reason="test_can_export"
            )
        ]

        targets, _, _ = self.calculate(
            states,
            requested_total=600,
            capabilities=capabilities,
            pv_charge_balance_enabled=True
        )

        self.assertEqual([0, 600], targets)

    def test_full_soc_priority_respects_device_max_power(self):
        states = [
            state(soc=100, max_soc=100, solar=1200, soc_limit=1),
            state(soc=80, max_soc=100, solar=800, soc_limit=0)
        ]
        configs = [
            device("WR1", max_power=800),
            device("WR2", max_power=800)
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=1200,
            max_power=1600,
            pv_charge_balance_enabled=True
        )

        self.assertEqual([800, 400], targets)
        self.assert_device_caps(targets, configs)
        self.assert_total(targets, 1200)

    def test_pv_insufficient_balances_discharge_by_usable_soc(self):
        states = [
            state(soc=80, min_soc=20, solar=100),
            state(soc=40, min_soc=20, solar=100)
        ]

        targets, _, _ = self.calculate(states, requested_total=600)

        self.assertGreater(targets[0] - states[0].solar, 0)
        self.assertGreater(
            targets[0] - states[0].solar,
            targets[1] - states[1].solar
        )
        self.assert_total(targets, 600)

    def test_battery_kwh_weighting_changes_discharge_share(self):
        states = [
            state(soc=60, min_soc=20, solar=100),
            state(soc=60, min_soc=20, solar=100)
        ]
        configs = [
            device("WR1", battery_kwh=2.0),
            device("WR2", battery_kwh=1.0)
        ]

        weighted_targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=600,
            battery_kwh_weighting=True
        )
        unweighted_targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=600,
            battery_kwh_weighting=False
        )

        self.assertGreater(
            weighted_targets[0] - states[0].solar,
            weighted_targets[1] - states[1].solar
        )
        self.assertEqual([300, 300], unweighted_targets)
        self.assert_total(weighted_targets, 600)

    def test_min_soc_protected_device_does_not_discharge(self):
        states = [
            state(soc=20, min_soc=20, solar=100),
            state(soc=70, min_soc=20, solar=100)
        ]

        targets, _, _ = self.calculate(states, requested_total=500)

        self.assertEqual(0, targets[0] - states[0].solar)
        self.assertEqual(300, targets[1] - states[1].solar)
        self.assert_total(targets, 500)

    def test_soc_limit_2_device_does_not_discharge_when_capabilities_passed(self):
        states = [
            state(soc=70, min_soc=20, solar=100, soc_limit=2),
            state(soc=70, min_soc=20, solar=100, soc_limit=0)
        ]
        capabilities = [
            detect_capabilities(current)
            for current in states
        ]

        targets, _, _ = self.calculate(
            states,
            requested_total=500,
            capabilities=capabilities
        )

        self.assertEqual(0, targets[0] - states[0].solar)
        self.assertEqual(300, targets[1] - states[1].solar)
        self.assert_total(targets, 500)

    def test_device_max_power_caps_are_respected(self):
        states = [
            state(soc=80, min_soc=20, solar=100),
            state(soc=80, min_soc=20, solar=100)
        ]
        configs = [
            device("WR1", max_power=300),
            device("WR2", max_power=800)
        ]

        targets, _, new_total = self.calculate(
            states,
            configs,
            requested_total=900,
            max_power=1500
        )

        self.assertEqual(900, new_total)
        self.assert_device_caps(targets, configs)
        self.assert_total(targets, 900)

    def test_three_inverter_allocation_is_deterministic_and_ordered(self):
        states = [
            state(soc=80, min_soc=20, solar=500),
            state(soc=50, min_soc=20, solar=300),
            state(soc=30, min_soc=20, solar=100)
        ]
        configs = [
            device("WR1", battery_kwh=2.0),
            device("WR2", battery_kwh=1.0),
            device("WR3", battery_kwh=1.0)
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=700,
            pv_charge_balance_enabled=False
        )
        repeated_targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=700,
            pv_charge_balance_enabled=False
        )

        self.assertEqual(targets, repeated_targets)
        self.assertGreaterEqual(targets[0], targets[1])
        self.assertGreaterEqual(targets[1], targets[2])
        self.assert_pv_only_limits(targets, states)
        self.assert_device_caps(targets, configs)
        self.assert_total(targets, 700)

    def test_asymmetric_pv_first_allocation_prefers_larger_array(self):
        states = [
            state(soc=75, solar=1800),
            state(soc=55, solar=500)
        ]
        configs = [
            device("WR1", pv_kwp=5.0, battery_kwh=7.0),
            device("WR2", pv_kwp=2.0, battery_kwh=2.0)
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=1200,
            max_power=1500
        )

        self.assertGreater(targets[0], targets[1])
        self.assert_pv_only_limits(targets, states)
        self.assert_total(targets, 1200)

    def test_asymmetric_discharge_balancing_prefers_larger_battery(self):
        states = [
            state(soc=70, min_soc=20, solar=300),
            state(soc=70, min_soc=20, solar=100)
        ]
        configs = [
            device("WR1", pv_kwp=5.0, battery_kwh=7.0),
            device("WR2", pv_kwp=2.0, battery_kwh=2.0)
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=1200,
            max_power=1500
        )

        self.assertGreater(
            targets[0] - states[0].solar,
            targets[1] - states[1].solar
        )
        self.assert_total(targets, 1200)

    def test_smaller_low_soc_battery_is_protected_in_asymmetric_installation(self):
        states = [
            state(soc=70, min_soc=20, solar=300),
            state(soc=30, min_soc=20, solar=100)
        ]
        configs = [
            device("WR1", pv_kwp=5.0, battery_kwh=7.0),
            device("WR2", pv_kwp=2.0, battery_kwh=2.0)
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=1000,
            max_power=1500
        )

        self.assertGreater(
            targets[0] - states[0].solar,
            targets[1] - states[1].solar
        )
        self.assertLessEqual(targets[1] - states[1].solar, 100)
        self.assert_total(targets, 1000)

    def test_large_pv_full_battery_covers_output_while_low_soc_keeps_pv(self):
        states = [
            state(soc=95, solar=1000),
            state(soc=40, solar=600)
        ]
        configs = [
            device("WR1", pv_kwp=5.0, battery_kwh=7.0),
            device("WR2", pv_kwp=2.0, battery_kwh=2.0)
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=1000,
            max_power=1600,
            pv_charge_balance_enabled=True
        )

        self.assertGreater(targets[0], targets[1])
        self.assertLess(targets[1], states[1].solar)
        self.assert_pv_only_limits(targets, states)
        self.assert_total(targets, 1000)

    def test_device_caps_with_asymmetric_sizing_limit_total_to_achievable_cap(self):
        states = [
            state(soc=80, min_soc=20, solar=300),
            state(soc=80, min_soc=20, solar=100)
        ]
        configs = [
            device("WR1", max_power=800, pv_kwp=5.0, battery_kwh=7.0),
            device("WR2", max_power=400, pv_kwp=2.0, battery_kwh=2.0)
        ]

        targets, _, new_total = self.calculate(
            states,
            configs,
            requested_total=1500,
            max_power=1500
        )

        self.assertEqual(1500, new_total)
        self.assert_device_caps(targets, configs)
        self.assertLessEqual(sum(targets), 1200)

    def test_cannot_export_device_receives_no_allocation(self):
        states = [
            state(soc=80, solar=500),
            state(soc=80, solar=500)
        ]
        capabilities = [
            DeviceCapabilities(
                can_charge=True,
                can_discharge=False,
                can_export=False,
                can_ac_charge=False,
                reason="test_cannot_export"
            ),
            DeviceCapabilities(
                can_charge=True,
                can_discharge=True,
                can_export=True,
                can_ac_charge=False,
                reason="test_can_export"
            )
        ]

        targets, _, _ = self.calculate(
            states,
            requested_total=600,
            capabilities=capabilities
        )

        self.assertEqual(0, targets[0])
        self.assertEqual(600, targets[1])

    def test_regression_constraints_and_repeatability(self):
        states = [
            state(soc=80, min_soc=20, solar=500, pack_in=50),
            state(soc=45, min_soc=20, solar=350),
            state(soc=35, min_soc=20, solar=200)
        ]
        configs = [
            device("WR1", max_power=450, pv_kwp=3.0, battery_kwh=4.0),
            device("WR2", max_power=350, pv_kwp=2.0, battery_kwh=2.0),
            device("WR3", max_power=250, pv_kwp=1.0, battery_kwh=1.0)
        ]

        targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=800,
            max_power=1000,
            pv_charge_balance_enabled=True
        )
        repeated_targets, _, _ = self.calculate(
            states,
            configs,
            requested_total=800,
            max_power=1000,
            pv_charge_balance_enabled=True
        )

        self.assertEqual(targets, repeated_targets)
        self.assert_device_caps(targets, configs)
        self.assert_pv_only_limits(targets, states)
        self.assertLessEqual(sum(targets), 800)


if __name__ == "__main__":
    unittest.main()
