import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ems.controller import EMSController
from ems.models import DeviceState


class ShellyStub:
    def get_power(self):
        return 0


def device_state():
    return DeviceState(
        soc=50,
        min_soc=15,
        max_soc=100,
        solar=0,
        output=0,
        pack_in=0,
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
        pack_state=0,
        fault_level=0,
        smart_mode=0,
        grid_off_mode=0,
        ac_mode=0,
        ac_status=0,
        dc_status=0,
        grid_state=1,
    )


class HASyncLoopHardeningTest(unittest.TestCase):
    def test_run_once_continues_when_ha_sync_fails(self):
        dev = SimpleNamespace(
            name="WR1",
            max_power=800,
            pv_kwp=1.0,
            pv_priority_factor=1.0,
            battery_kwh=1.0,
            min_soc=15,
            max_soc=100
        )
        controller = EMSController(
            devices=[dev],
            shelly=ShellyStub(),
            sleep_enabled=False
        )
        controller.sync_ha_runtime_state = Mock(
            side_effect=RuntimeError("ha unavailable")
        )
        controller.set_output_limit = Mock()

        with patch(
            "ems.controller.fetch_all_devices",
            return_value=[device_state()]
        ) as fetch_all_devices, patch(
            "ems.controller.log_event"
        ) as log_event, patch(
            "ems.controller.cfg.SYSTEM_ENABLED",
            False
        ), patch(
            "ems.controller.cfg.SOC_RECONCILE_INTERVAL",
            0
        ):
            controller.run_once()

        controller.sync_ha_runtime_state.assert_called_once_with()
        fetch_all_devices.assert_called_once_with([dev])

        self.assertTrue(
            any(
                call.args[:2] == (
                    logging.WARNING,
                    "ha_runtime_sync_failed"
                )
                for call in log_event.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
