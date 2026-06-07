# SPDX-License-Identifier: AGPL-3.0-or-later
import unittest
from unittest.mock import Mock, patch

import requests

from scripts.influx_utils import InfluxHTTPClient, wait_for_influx_ready


class HealthResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if self.payload is None:
            raise ValueError("no json")

        return self.payload


class InfluxReadinessTest(unittest.TestCase):
    def test_wait_retries_until_health_passes(self):
        session = Mock()
        session.get.side_effect = [
            HealthResponse(503, {"status": "fail"}),
            HealthResponse(200, {"status": "pass"})
        ]
        client = InfluxHTTPClient(
            "http://influxdb:8086",
            "org",
            "token",
            session=session,
            timeout=15
        )

        with patch("scripts.influx_utils.time.sleep") as sleep:
            wait_for_influx_ready(client, timeout_s=60, interval_s=2)

        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_wait_times_out_after_transient_connection_errors(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("refused")
        client = InfluxHTTPClient(
            "http://influxdb:8086",
            "org",
            "token",
            session=session,
            timeout=15
        )

        with patch(
            "scripts.influx_utils.time.monotonic",
            side_effect=[0, 0, 3]
        ), patch("scripts.influx_utils.time.sleep"):
            with self.assertRaises(TimeoutError) as raised:
                wait_for_influx_ready(client, timeout_s=2, interval_s=1)

        self.assertIn("InfluxDB did not become ready", str(raised.exception))
        self.assertIn("http://influxdb:8086", str(raised.exception))
        self.assertNotIn("token", str(raised.exception).lower())


if __name__ == "__main__":
    unittest.main()
