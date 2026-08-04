# SPDX-License-Identifier: AGPL-3.0-or-later
import importlib.util
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ems.models import DeviceState

pytestmark = [
    pytest.mark.contract,
]


def load_capture_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "capture_runtime_to_influx.py"
    )
    spec = importlib.util.spec_from_file_location(
        "capture_runtime_to_influx",
        path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def device_state(output=100):
    return DeviceState(
        soc=80,
        min_soc=15,
        max_soc=100,
        solar=250,
        output=output,
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
        pack_state=2,
        fault_level=0,
        smart_mode=1,
        grid_off_mode=0,
        ac_mode=2,
        ac_status=1,
        dc_status=1,
        grid_state=1,
    )


class InfluxRuntimeStateReadTest(unittest.TestCase):
    def test_read_runtime_state_returns_none_for_unreadable_inputs(self):
        capture = load_capture_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            missing_path = temp_path / "missing-runtime-state.json"
            empty_path = temp_path / "empty-runtime-state.json"
            empty_path.write_text("")

            with patch.object(capture, "log_event") as log_event:
                self.assertIsNone(
                    capture.read_runtime_state(str(missing_path))
                )
                self.assertIsNone(
                    capture.read_runtime_state(str(empty_path))
                )

            warning_events = [
                call
                for call in log_event.call_args_list
                if call.args[:2] == (
                    logging.WARNING,
                    "influx_capture_runtime_state_read_error"
                )
            ]
            self.assertEqual(len(warning_events), 2)

        with patch.object(capture.os.path, "exists", return_value=True), \
            patch("builtins.open", side_effect=OSError("permission denied")), \
            patch.object(capture, "log_event") as log_event:
            self.assertIsNone(
                capture.read_runtime_state("/tmp/runtime-state-denied.json")
            )

        log_event.assert_called_once()
        self.assertEqual(
            log_event.call_args.args[:2],
            (
                logging.WARNING,
                "influx_capture_runtime_state_read_error"
            )
        )

    def test_invalid_runtime_state_json_is_skipped(self):
        capture = load_capture_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "config.json"
            env_path = temp_path / ".env"
            runtime_state_path = temp_path / "runtime-state.json"

            config_path.write_text(json.dumps({
                "devices": [
                    {
                        "name": "WR1",
                        "ip": "127.0.0.1",
                        "sn": "sn",
                        "max_power": 800
                    }
                ],
                "shelly": {
                    "ip": ""
                },
                "system": {
                    "runtime_state_path": str(runtime_state_path)
                }
            }))
            env_path.write_text(
                "INFLUXDB_URL=http://127.0.0.1:8086\n"
                "INFLUXDB_ORG=test-org\n"
                "INFLUXDB_TOKEN=test-token\n"
                "INFLUXDB_BUCKET_RAW=test-bucket\n"
            )
            runtime_state_path.write_text("{invalid json")

            args = SimpleNamespace(
                config=str(config_path),
                env=str(env_path),
                interval=5,
                duration=1,
                run_id="runtime-state-read-test",
                bucket="",
                runtime_state_path=str(runtime_state_path),
                include_runtime_state=True,
                skip_shelly=True,
                log_level="info"
            )
            influx = Mock()

            with patch.object(capture, "parse_args", return_value=args), \
                patch.object(capture, "setup_logging"), \
                patch.object(capture, "create_session", return_value=Mock()), \
                patch.object(capture, "InfluxHTTPClient", return_value=influx), \
                patch.object(capture, "fetch_all_devices",
                             return_value=[device_state()]), \
                patch.object(capture.time, "time", side_effect=[0, 2, 2]), \
                patch.object(capture, "log_event") as log_event:
                capture.main()

        influx.write_lines.assert_called_once()
        written_lines = influx.write_lines.call_args.args[1]
        self.assertTrue(
            any(line.startswith("zendure_device,") for line in written_lines)
        )
        self.assertFalse(
            any(line.startswith("ems_runtime,") for line in written_lines)
        )
        self.assertTrue(
            any(
                call.args[:2] == (
                    logging.WARNING,
                    "influx_capture_runtime_state_read_error"
                )
                for call in log_event.call_args_list
            )
        )


class CollectorShellyMeterSchemaTest(unittest.TestCase):
    """The standalone collector must feed the same shelly_meter schema as the
    native writer: grid_power = meter exchange power, house_load derived as
    max(0, inverter_total + grid_power)."""

    def _run_capture_with_grid_power(self, grid_power, device_outputs):
        capture = load_capture_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "config.json"
            env_path = temp_path / ".env"

            config_path.write_text(json.dumps({
                "devices": [
                    {
                        "name": f"WR{index + 1}",
                        "ip": "127.0.0.1",
                        "sn": f"sn{index + 1}",
                        "max_power": 800
                    }
                    for index in range(len(device_outputs))
                ],
                "shelly": {"ip": "127.0.0.1"}
            }))
            env_path.write_text(
                "INFLUXDB_URL=http://127.0.0.1:8086\n"
                "INFLUXDB_ORG=test-org\n"
                "INFLUXDB_TOKEN=test-token\n"
                "INFLUXDB_BUCKET_RAW=test-bucket\n"
            )

            args = SimpleNamespace(
                config=str(config_path),
                env=str(env_path),
                interval=5,
                duration=1,
                run_id="shelly-schema-test",
                bucket="",
                runtime_state_path="",
                include_runtime_state=False,
                skip_shelly=False,
                log_level="info"
            )
            influx = Mock()
            shelly = Mock()
            shelly.get_power.return_value = grid_power
            states = [device_state(output=output) for output in device_outputs]

            with patch.object(capture, "parse_args", return_value=args), \
                patch.object(capture, "setup_logging"), \
                patch.object(capture, "create_session", return_value=Mock()), \
                patch.object(capture, "InfluxHTTPClient", return_value=influx), \
                patch.object(capture, "ShellyClient", return_value=shelly), \
                patch.object(capture, "fetch_all_devices", return_value=states), \
                patch.object(capture.time, "time", side_effect=[0, 2, 2]), \
                patch.object(capture, "log_event"):
                capture.main()

        influx.write_lines.assert_called_once()
        return influx.write_lines.call_args.args[1]

    def _shelly_line(self, lines):
        shelly_lines = [
            line for line in lines if line.startswith("shelly_meter")
        ]
        self.assertEqual(len(shelly_lines), 1)
        return shelly_lines[0]

    def test_collector_writes_grid_power_and_derived_house_load(self):
        lines = self._run_capture_with_grid_power(
            grid_power=200.0, device_outputs=[100.0, 50.0]
        )
        shelly_line = self._shelly_line(lines)
        # grid_power is the meter exchange value as-is.
        self.assertIn("grid_power=200", shelly_line)
        # house_load = max(0, (100 + 50) + 200) = 350.
        self.assertIn("house_load=350", shelly_line)

    def test_collector_clamps_house_load_to_zero_on_export(self):
        lines = self._run_capture_with_grid_power(
            grid_power=-500.0, device_outputs=[100.0]
        )
        shelly_line = self._shelly_line(lines)
        self.assertIn("grid_power=-500", shelly_line)
        # max(0, 100 - 500) = 0.
        self.assertIn("house_load=0", shelly_line)

    def test_collector_does_not_write_target_output(self):
        # The read-only collector cannot know the EMS output target, so it must
        # not emit a misleading ems_runtime.target_output field.
        lines = self._run_capture_with_grid_power(
            grid_power=200.0, device_outputs=[100.0]
        )
        self.assertFalse(any("target_output" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
