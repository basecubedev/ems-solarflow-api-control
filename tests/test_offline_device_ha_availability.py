# SPDX-License-Identifier: AGPL-3.0-or-later
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems.controller import EMSController
from ems.models import DeviceState

pytestmark = [
    pytest.mark.power_control,
    pytest.mark.unit,
]


class HARecorder:
    def __init__(self):
        self.states = {}

    def set_state(
        self,
        entity_id,
        state,
        unit=None,
        device_class=None,
        state_class=None,
        icon=None,
        extra_attributes=None
    ):
        self.states[entity_id] = {
            "state": state,
            "unit": unit,
            "device_class": device_class,
            "state_class": state_class,
            "icon": icon,
            "attributes": extra_attributes or {}
        }


class ShellyStub:
    def get_power(self):
        return 0


def device_config():
    return SimpleNamespace(
        name="WR1",
        max_power=800,
        pv_kwp=1.0,
        pv_priority_factor=1.0,
        battery_kwh=1.0,
        min_soc=15,
        max_soc=100
    )


def device_state(soc=50):
    return DeviceState(
        soc=soc,
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


class OfflineDeviceHAAvailabilityTest(unittest.TestCase):
    def test_cached_device_state_is_marked_unavailable_then_live_again(self):
        dev = device_config()
        ha = HARecorder()
        controller = EMSController(
            devices=[dev],
            shelly=ShellyStub(),
            ha=ha,
            sleep_enabled=False
        )
        controller.runtime_ha_enabled = Mock(return_value=True)
        controller.run_startup_ac_mode_reconcile_once = Mock()
        controller.set_output_limit = Mock()

        with patch(
            "ems.controller.fetch_all_devices",
            side_effect=[
                [device_state(soc=50)],
                [None],
                [device_state(soc=55)]
            ]
        ), patch(
            "ems.controller.cfg.SYSTEM_ENABLED",
            False
        ), patch(
            "ems.controller.cfg.SOC_RECONCILE_INTERVAL",
            0
        ):
            controller.run_once()
            self.assertEqual(
                ha.states["binary_sensor.wr1_available"]["state"],
                "on"
            )
            self.assertTrue(
                ha.states[
                    "sensor.ems_solarflow_wr1_soc"
                ]["attributes"]["available"]
            )
            self.assertEqual(
                ha.states[
                    "sensor.ems_solarflow_wr1_soc"
                ]["attributes"]["telemetry_source"],
                "live"
            )

            controller.run_once()
            self.assertEqual(
                ha.states["binary_sensor.wr1_available"]["state"],
                "off"
            )
            self.assertFalse(
                ha.states[
                    "sensor.ems_solarflow_wr1_soc"
                ]["attributes"]["available"]
            )
            self.assertEqual(
                ha.states[
                    "sensor.ems_solarflow_wr1_soc"
                ]["attributes"]["telemetry_source"],
                "cached"
            )

            controller.run_once()
            self.assertEqual(
                ha.states["binary_sensor.wr1_available"]["state"],
                "on"
            )
            self.assertTrue(
                ha.states[
                    "sensor.ems_solarflow_wr1_soc"
                ]["attributes"]["available"]
            )
            self.assertEqual(
                ha.states[
                    "sensor.ems_solarflow_wr1_soc"
                ]["attributes"]["telemetry_source"],
                "live"
            )
            self.assertEqual(
                ha.states["sensor.ems_solarflow_wr1_soc"]["state"],
                55
            )


if __name__ == "__main__":
    unittest.main()
