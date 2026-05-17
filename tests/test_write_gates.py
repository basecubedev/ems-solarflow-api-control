import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ems.controller import EMSController
from ems.models import DeviceState


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


def device(name):
    return SimpleNamespace(
        name=name,
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
    pack_state=2
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
        ac_mode=2,
        ac_status=ac_status,
        dc_status=dc_status,
        grid_state=1,
    )


class WriteGateTest(unittest.TestCase):
    def run_controller_once(
        self,
        devices,
        states,
        runtime_state=None,
        load=300
    ):
        controller = EMSController(
            devices=devices,
            shelly=ShellyStub(load),
            sleep_enabled=False,
            runtime_state=runtime_state
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


if __name__ == "__main__":
    unittest.main()
