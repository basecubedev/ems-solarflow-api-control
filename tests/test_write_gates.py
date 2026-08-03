# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems.controller import EMSController
from ems.models import DeviceState
from ems.runtime_intents import (
    DeviceRuntimeRole,
    ac_input_intent,
    ac_output_intent,
)

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]


class RuntimeStateStub:
    def __init__(self, system=None, devices=None):
        self.system = system or {}
        self.devices = devices or {}

    def load_if_changed(self):
        return None

    def get_system(self, key, default=None):
        return self.system.get(key, default)

    def get_device(self, device_name, key, default=None):
        return self.devices.get(device_name, {}).get(key, default)


class ShellyStub:
    def __init__(self, power):
        self.power = power

    def get_power(self):
        return self.power


class DashboardStoreStub:
    def __init__(self):
        self.records = []

    def record(self, snapshot):
        self.records.append(snapshot)


def device(name):
    return SimpleNamespace(
        name=name,
        ip="127.0.0.1",
        sn=f"{name}-SN",
        session=Mock(),
        max_power=800,
        pv_kwp=1.0,
        pv_priority_factor=1.0,
        battery_kwh=1.0,
        min_soc=15,
        max_soc=100,
        smart_mode=1,
        grid_off_mode=None
    )


def state(
    soc=80,
    min_soc=15,
    solar=500,
    output=0,
    output_limit=0,
    pack_in=0,
    pack_out=0,
    soc_limit=0,
    dc_status=1,
    ac_status=1,
    pack_state=2,
    ac_mode=2,
    input_limit_w=0
):
    return DeviceState(
        soc=soc,
        min_soc=min_soc,
        max_soc=100,
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
        ac_mode=ac_mode,
        ac_status=ac_status,
        dc_status=dc_status,
        grid_state=1,
        input_limit_w=input_limit_w,
    )


class WriteGateTest(unittest.TestCase):
    def run_controller_once(
        self,
        devices,
        states,
        runtime_state=None,
        load=300,
        dashboard_store=None
    ):
        controller = EMSController(
            devices=devices,
            shelly=ShellyStub(load),
            sleep_enabled=False,
            runtime_state=runtime_state,
            dashboard_store=dashboard_store
        )
        controller.run_startup_ac_mode_reconcile_once = Mock()
        controller.set_output_limit = Mock()

        with patch(
            "ems.controller.fetch_all_devices",
            return_value=states
        ), patch(
            "ems.controller.cfg.SYSTEM_ENABLED",
            True
        ), patch(
            "ems.controller.cfg.MAX_TOTAL_POWER",
            800
        ), patch(
            "ems.controller.cfg.MAX_DEVICE_POWER",
            800
        ), patch(
            "ems.controller.cfg.MIN_OUTPUT_LIMIT",
            0
        ), patch(
            "ems.controller.cfg.LOOP_INTERVAL",
            5
        ), patch(
            "ems.controller.cfg.DEADBAND",
            10
        ), patch(
            "ems.controller.cfg.SOC_RECONCILE_INTERVAL",
            0
        ), patch(
            "ems.controller.cfg.REDISTRIBUTE_CLAMPED_POWER",
            True
        ), patch(
            "ems.controller.cfg.PV_KWP_WEIGHTING",
            True
        ), patch(
            "ems.controller.cfg.BATTERY_KWH_WEIGHTING",
            True
        ):
            controller.run_once()

        return controller

    def test_offline_device_receives_no_write_but_online_device_continues(self):
        offline = device("WR1")
        online = device("WR2")
        controller = self.run_controller_once(
            [offline, online],
            [None, state()]
        )

        written_devices = [
            write.args[0].name
            for write in controller.set_output_limit.call_args_list
        ]

        self.assertNotIn("WR1", written_devices)
        self.assertIn("WR2", written_devices)

    def test_runtime_disabled_device_receives_no_write(self):
        disabled = device("WR1")
        enabled = device("WR2")
        runtime_state = RuntimeStateStub(
            devices={
                "WR1": {
                    "enabled": False
                }
            }
        )
        controller = self.run_controller_once(
            [disabled, enabled],
            [state(), state()],
            runtime_state=runtime_state
        )

        written_devices = [
            write.args[0].name
            for write in controller.set_output_limit.call_args_list
        ]

        self.assertNotIn("WR1", written_devices)
        self.assertIn("WR2", written_devices)

    def test_night_min_soc_idle_already_parked_device_receives_no_write(self):
        parked = device("WR1")
        runtime_state = RuntimeStateStub(
            system={
                "min_output_limit": 30
            }
        )
        controller = self.run_controller_once(
            [parked],
            [
                state(
                    soc=15,
                    min_soc=15,
                    solar=0,
                    output=0,
                    output_limit=30,
                    pack_in=0,
                    pack_out=0,
                    soc_limit=2,
                    dc_status=0,
                    ac_status=0,
                    pack_state=0
                )
            ],
            runtime_state=runtime_state,
            load=0
        )

        controller.set_output_limit.assert_not_called()

    def test_night_min_soc_idle_publishes_control_explanation_to_dashboard(self):
        wr1 = device("WR1")
        wr2 = device("WR2")
        dashboard_store = DashboardStoreStub()
        runtime_state = RuntimeStateStub(
            system={
                "min_output_limit": 30
            }
        )

        controller = self.run_controller_once(
            [wr1, wr2],
            [
                state(
                    soc=15,
                    min_soc=15,
                    solar=0,
                    output=0,
                    output_limit=30,
                    pack_in=0,
                    pack_out=0,
                    soc_limit=2,
                    dc_status=0,
                    ac_status=0,
                    pack_state=0
                ),
                state(
                    soc=14,
                    min_soc=15,
                    solar=0,
                    output=0,
                    output_limit=30,
                    pack_in=0,
                    pack_out=0,
                    soc_limit=2,
                    dc_status=0,
                    ac_status=0,
                    pack_state=0
                ),
            ],
            runtime_state=runtime_state,
            load=0,
            dashboard_store=dashboard_store
        )

        controller.set_output_limit.assert_not_called()
        self.assertIsNotNone(controller.last_control_explanation)
        self.assertEqual(
            controller.last_control_explanation.mode,
            "night_min_soc_idle"
        )
        self.assertEqual(
            set(controller.last_control_explanation.devices),
            {"WR1", "WR2"}
        )
        self.assertEqual(len(dashboard_store.records), 1)

        snapshot = dashboard_store.records[0]
        self.assertTrue(snapshot["controller"]["night_min_soc_idle"])
        self.assertEqual(snapshot["devices"]["WR1"]["target_w"], 30)
        self.assertEqual(snapshot["devices"]["WR2"]["target_w"], 30)
        self.assertEqual(snapshot["controller"]["allocated_target_total_w"], 60)
        self.assertEqual(snapshot["controller"]["effective_target_total_w"], 60)

        explanation = snapshot["control_explain"]
        self.assertIsNotNone(explanation)
        self.assertEqual(explanation["mode"], "night_min_soc_idle")
        self.assertEqual(explanation["requested_total_w"], 60)
        self.assertEqual(explanation["allocated_target_total_w"], 60)
        self.assertEqual(explanation["effective_target_total_w"], 60)
        self.assertEqual(explanation["min_output_limit_w"], 30)
        self.assertEqual(explanation["max_total_power_w"], 800)
        self.assertEqual(set(explanation["devices"]), {"WR1", "WR2"})
        self.assertEqual(
            explanation["devices"]["WR1"]["decision_reason"],
            "night_min_soc_idle"
        )
        self.assertEqual(
            explanation["devices"]["WR2"]["limiting_reason"],
            "below_min_soc"
        )
        self.assertEqual(
            explanation["devices"]["WR1"]["write_decision"],
            "skip"
        )
        self.assertEqual(
            explanation["devices"]["WR1"]["write_reason"],
            "already_at_min_output_limit"
        )

    def test_consecutive_eligible_writes_are_not_time_blocked(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(300),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub()
        )
        controller.run_startup_ac_mode_reconcile_once = Mock()
        controller.set_output_limit = Mock()

        with patch(
            "ems.controller.fetch_all_devices",
            side_effect=[
                [state()],
                [state()]
            ]
        ), patch(
            "ems.controller.cfg.SYSTEM_ENABLED",
            True
        ), patch(
            "ems.controller.cfg.MAX_TOTAL_POWER",
            800
        ), patch(
            "ems.controller.cfg.MAX_DEVICE_POWER",
            800
        ), patch(
            "ems.controller.cfg.MIN_OUTPUT_LIMIT",
            0
        ), patch(
            "ems.controller.cfg.LOOP_INTERVAL",
            5
        ), patch(
            "ems.controller.cfg.DEADBAND",
            10
        ), patch(
            "ems.controller.cfg.SOC_RECONCILE_INTERVAL",
            0
        ), patch(
            "ems.controller.cfg.REDISTRIBUTE_CLAMPED_POWER",
            True
        ), patch(
            "ems.controller.cfg.PV_KWP_WEIGHTING",
            True
        ), patch(
            "ems.controller.cfg.BATTERY_KWH_WEIGHTING",
            True
        ):
            controller.run_once()
            controller.run_once()

        self.assertEqual(controller.set_output_limit.call_count, 2)

    def test_default_runtime_intent_does_not_write_ac_mode_when_already_output(self):
        controlled = device("WR1")
        controller = self.run_controller_once(
            [controlled],
            [state(ac_mode=2)],
            load=300
        )

        controlled.session.post.assert_not_called()
        self.assertEqual(controller.set_output_limit.call_count, 1)

    def test_default_runtime_intent_restores_output_mode_once(self):
        controlled = device("WR1")
        controlled.session.post.return_value = SimpleNamespace(status_code=200)
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub()
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ):
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=1, solar=0, output=0, pack_in=0, pack_out=0),
                ac_output_intent("WR1")
            )
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=2, solar=0, output=0, pack_in=0, pack_out=0),
                ac_output_intent("WR1")
            )

        self.assertEqual(controlled.session.post.call_count, 1)
        self.assertEqual(
            controlled.session.post.call_args.kwargs["json"]["properties"],
            {"acMode": 2}
        )

    def test_default_runtime_intent_does_not_spam_ac_mode_writes(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub()
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ):
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=2),
                ac_output_intent("WR1")
            )
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=2),
                ac_output_intent("WR1")
            )

        controlled.session.post.assert_not_called()

    def test_unchanged_ac_mode_intent_logs_debug_not_info(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub()
        )

        with patch("ems.controller.log_event") as log_event:
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=2),
                ac_output_intent("WR1")
            )

        controlled.session.post.assert_not_called()
        self.assertTrue(
            any(
                call.args[0] == logging.DEBUG
                and call.args[1] == "ac_mode_intent_unchanged"
                for call in log_event.call_args_list
            )
        )

    def test_ac_input_runtime_intent_writes_ac_mode_1(self):
        controlled = device("WR1")
        controlled.session.post.return_value = SimpleNamespace(status_code=200)
        runtime_state = RuntimeStateStub(
            devices={
                "WR1": {
                    "runtime_role": "ac_input",
                    "runtime_role_reason": "test_charge"
                }
            }
        )
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=runtime_state
        )
        intent = controller.get_device_runtime_intent(controlled, state(ac_mode=2))

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ):
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=2),
                intent
            )

        controlled.session.post.assert_called_once()
        self.assertEqual(
            controlled.session.post.call_args.kwargs["json"]["properties"],
            {"acMode": 1}
        )

    def test_ac_input_runtime_intent_skips_when_already_correct(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub()
        )

        controller.reconcile_ac_mode_intent(
            controlled,
            state(ac_mode=1),
            ac_input_intent("WR1", "test_charge")
        )

        controlled.session.post.assert_not_called()

    def test_legacy_reserved_runtime_role_maps_to_ac_input_intent(self):
        controlled = device("WR1")
        runtime_state = RuntimeStateStub(
            devices={
                "WR1": {
                    "runtime_role": "reserved",
                    "runtime_role_reason": "manual_reservation"
                }
            }
        )
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=runtime_state
        )

        intent = controller.get_device_runtime_intent(controlled, state(ac_mode=2))

        self.assertEqual(intent.role, DeviceRuntimeRole.AC_INPUT)
        self.assertEqual(intent.reason, "manual_reservation")
        self.assertEqual(intent.desired_ac_mode, 1)
        self.assertFalse(intent.output_control_allowed)

    def test_ac_input_intent_does_not_write_when_already_correct(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub()
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ):
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=1),
                ac_input_intent("WR1", "manual_reservation")
            )

        controlled.session.post.assert_not_called()

    def test_ac_input_runtime_role_blocks_output_control(self):
        controlled = device("WR1")
        runtime_state = RuntimeStateStub(
            devices={
                "WR1": {
                    "runtime_role": "ac_input",
                    "runtime_role_reason": "manual_reservation"
                }
            }
        )
        controller = self.run_controller_once(
            [controlled],
            [state(ac_mode=2, solar=500, output=0, output_limit=0)],
            runtime_state=runtime_state,
            load=700
        )

        controller.set_output_limit.assert_not_called()
        self.assertIsNotNone(controller.last_control_explanation)
        device_explanation = controller.last_control_explanation.devices["WR1"]
        self.assertEqual(device_explanation.allocated_target_w, 0)
        self.assertEqual(device_explanation.effective_target_w, 0)
        self.assertEqual(device_explanation.write_decision, "blocked")
        self.assertEqual(device_explanation.write_reason, "runtime_role_ac_input")
        self.assertEqual(device_explanation.limiting_reason, "runtime_role_ac_input")
        self.assertFalse(controller._dashboard_capabilities[0].can_export)
        self.assertFalse(controller._dashboard_capabilities[0].can_discharge)

    def test_ac_input_legacy_role_receives_no_output_limit_write(self):
        controlled = device("WR1")
        runtime_state = RuntimeStateStub(
            devices={
                "WR1": {
                    "runtime_role": "ac_input_charge",
                    "runtime_role_reason": "test_charge"
                }
            }
        )
        controller = self.run_controller_once(
            [controlled],
            [state(ac_mode=1, solar=500, output=0, output_limit=0)],
            runtime_state=runtime_state,
            load=700
        )

        controller.set_output_limit.assert_not_called()
        self.assertIsNotNone(controller.last_control_explanation)
        device_explanation = controller.last_control_explanation.devices["WR1"]
        self.assertEqual(device_explanation.allocated_target_w, 0)
        self.assertEqual(device_explanation.effective_target_w, 0)
        self.assertEqual(device_explanation.write_decision, "blocked")
        self.assertEqual(
            device_explanation.write_reason,
            "runtime_role_ac_input"
        )
        self.assertEqual(device_explanation.limiting_reason, "runtime_role_ac_input")
        self.assertFalse(
            controller._dashboard_capabilities[0].can_export
        )
        self.assertFalse(
            controller._dashboard_capabilities[0].can_discharge
        )

    def test_ac_input_device_is_not_parked_by_night_min_soc_idle(self):
        controlled = device("WR1")
        runtime_state = RuntimeStateStub(
            system={
                "min_output_limit": 30
            },
            devices={
                "WR1": {
                    "runtime_role": "ac_input",
                    "runtime_role_reason": "test_charge"
                }
            }
        )
        controller = self.run_controller_once(
            [controlled],
            [
                state(
                    soc=15,
                    min_soc=15,
                    solar=0,
                    output=0,
                    output_limit=0,
                    pack_in=0,
                    pack_out=0,
                    soc_limit=2,
                    dc_status=0,
                    ac_status=0,
                    pack_state=0,
                    ac_mode=1
                )
            ],
            runtime_state=runtime_state,
            load=0
        )

        controller.set_output_limit.assert_not_called()
        self.assertFalse(controller.night_min_soc_idle_active)

    def test_safety_blocker_prevents_returning_to_output_mode(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub()
        )

        with patch("ems.controller.log_event") as log_event:
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=1, ac_status=2, solar=0, output=0),
                ac_output_intent("WR1", "startup_ac_mode_reconcile")
            )

        controlled.session.post.assert_not_called()
        self.assertTrue(
            any(
                call.args[1] == "ac_mode_intent_skip"
                and call.kwargs["reason"] == "ac_charge_active"
                for call in log_event.call_args_list
            )
        )

    def test_explicit_ac_output_runtime_intent_bypasses_startup_blocker(self):
        controlled = device("WR1")
        controlled.session.post.return_value = SimpleNamespace(status_code=200)
        runtime_state = RuntimeStateStub(
            devices={
                "WR1": {
                    "runtime_role": "ac_output",
                    "runtime_role_reason": "emsctl"
                }
            }
        )
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=runtime_state
        )
        intent = controller.get_device_runtime_intent(
            controlled,
            state(ac_mode=1, ac_status=2)
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ):
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=1, ac_status=2, solar=0, output=0),
                intent
            )

        controlled.session.post.assert_called_once()
        self.assertEqual(intent.role, DeviceRuntimeRole.AC_OUTPUT)
        self.assertEqual(intent.reason, "emsctl")
        self.assertTrue(intent.output_control_allowed)
        self.assertEqual(
            controlled.session.post.call_args.kwargs["json"]["properties"],
            {"acMode": 2}
        )

    def test_unknown_ac_mode_does_not_restore_output_mode_blindly(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub()
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ), patch("ems.controller.log_event") as log_event:
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=0),
                ac_output_intent("WR1", "startup_ac_mode_reconcile")
            )

        controlled.session.post.assert_not_called()
        self.assertTrue(
            any(
                call.args[0] == logging.WARNING
                and call.args[1] == "unknown_ac_mode"
                and call.kwargs["current_ac_mode"] == 0
                and call.kwargs["desired_ac_mode"] == 2
                for call in log_event.call_args_list
            )
        )

    def test_explicit_ac_output_runtime_intent_writes_from_unknown_ac_mode_zero(self):
        controlled = device("WR1")
        controlled.session.post.return_value = SimpleNamespace(status_code=200)
        runtime_state = RuntimeStateStub(
            devices={
                "WR1": {
                    "runtime_role": "ac_output",
                    "runtime_role_reason": "emsctl"
                }
            }
        )
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=runtime_state
        )
        intent = controller.get_device_runtime_intent(controlled, state(ac_mode=0))

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ):
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=0),
                intent
            )

        controlled.session.post.assert_called_once()
        self.assertEqual(
            controlled.session.post.call_args.kwargs["json"]["properties"],
            {"acMode": 2}
        )

    def test_ac_mode_intent_write_gates_respected(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub()
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=False
        ), patch("ems.controller.log_event") as log_event:
            controller.reconcile_ac_mode_intent(
                controlled,
                state(ac_mode=2),
                ac_input_intent("WR1", "test_charge")
            )

        controlled.session.post.assert_not_called()
        self.assertTrue(
            any(
                call.args[1] == "dry_run_ac_mode_intent_write"
                for call in log_event.call_args_list
            )
        )

    def test_ac_input_runtime_charge_power_writes_input_limit_on_next_loop(self):
        controlled = device("WR1")
        controlled.session.post.return_value = SimpleNamespace(status_code=200)
        runtime_state = RuntimeStateStub(
            devices={
                "WR1": {
                    "runtime_role": "ac_input",
                    "runtime_role_reason": "test_charge",
                    "ac_charge_power_w": 200,
                }
            }
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ):
            controller = self.run_controller_once(
                [controlled],
                [state(ac_mode=1, input_limit_w=0)],
                runtime_state=runtime_state,
                load=700
            )

        controlled.session.post.assert_called_once()
        self.assertEqual(
            controlled.session.post.call_args.kwargs["json"]["properties"],
            {"inputLimit": 200}
        )
        controller.set_output_limit.assert_not_called()

    def test_ac_input_runtime_charge_power_skips_when_unchanged(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub(
                devices={
                    "WR1": {
                        "runtime_role": "ac_input",
                        "ac_charge_power_w": 200,
                    }
                }
            )
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ), patch("ems.controller.log_event") as log_event:
            controller.reconcile_runtime_ac_charge_power(
                controlled,
                state(ac_mode=1, input_limit_w=200),
                ac_input_intent("WR1", "test_charge")
            )

        controlled.session.post.assert_not_called()
        self.assertTrue(
            any(
                call.args[0] == logging.DEBUG
                and call.args[1] == "runtime_ac_charge_power_unchanged"
                for call in log_event.call_args_list
            )
        )

    def test_ac_output_ignores_runtime_charge_power(self):
        controlled = device("WR1")
        runtime_state = RuntimeStateStub(
            devices={
                "WR1": {
                    "runtime_role": "ac_output",
                    "ac_charge_power_w": 200,
                }
            }
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ):
            self.run_controller_once(
                [controlled],
                [state(ac_mode=2, input_limit_w=0)],
                runtime_state=runtime_state,
                load=700
            )

        controlled.session.post.assert_not_called()

    def test_ac_input_without_charge_power_does_not_write_input_limit(self):
        controlled = device("WR1")
        runtime_state = RuntimeStateStub(
            devices={
                "WR1": {
                    "runtime_role": "ac_input",
                    "runtime_role_reason": "test_charge",
                }
            }
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ):
            self.run_controller_once(
                [controlled],
                [state(ac_mode=1, input_limit_w=0)],
                runtime_state=runtime_state,
                load=700
            )

        controlled.session.post.assert_not_called()

    def test_runtime_charge_power_write_gates_respected(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub(
                devices={
                    "WR1": {
                        "runtime_role": "ac_input",
                        "ac_charge_power_w": 200,
                    }
                }
            )
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=False
        ), patch("ems.controller.log_event") as log_event:
            controller.reconcile_runtime_ac_charge_power(
                controlled,
                state(ac_mode=1, input_limit_w=0),
                ac_input_intent("WR1", "test_charge")
            )

        controlled.session.post.assert_not_called()
        self.assertTrue(
            any(
                call.args[1] == "runtime_ac_charge_power_write_skipped"
                for call in log_event.call_args_list
            )
        )

    def test_invalid_runtime_charge_power_is_ignored(self):
        controlled = device("WR1")
        controller = EMSController(
            devices=[controlled],
            shelly=ShellyStub(0),
            sleep_enabled=False,
            runtime_state=RuntimeStateStub(
                devices={
                    "WR1": {
                        "runtime_role": "ac_input",
                        "ac_charge_power_w": "not-an-int",
                    }
                }
            )
        )

        with patch(
            "ems.controller.cfg.state_reconciliation_writes_allowed",
            return_value=True
        ), patch("ems.controller.log_event") as log_event:
            controller.reconcile_runtime_ac_charge_power(
                controlled,
                state(ac_mode=1, input_limit_w=0),
                ac_input_intent("WR1", "test_charge")
            )

        controlled.session.post.assert_not_called()
        self.assertTrue(
            any(
                call.args[1] == "runtime_ac_charge_power_invalid"
                for call in log_event.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
