# SPDX-License-Identifier: AGPL-3.0-or-later
import importlib.util
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ems.models import DeviceState


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


def device_state():
    return DeviceState(
        soc=80,
        min_soc=15,
        max_soc=100,
        solar=250,
        output=100,
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


if __name__ == "__main__":
    unittest.main()
