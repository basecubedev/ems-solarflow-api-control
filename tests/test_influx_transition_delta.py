# SPDX-License-Identifier: AGPL-3.0-or-later
import unittest

from scripts.analyze_influx_state_transitions import (
    availability_change_flux,
    build_report,
    state_change_flux,
)


class InfluxTransitionDeltaTest(unittest.TestCase):
    def test_flux_renames_difference_value_to_delta(self):
        state_flux = state_change_flux("zendure_raw", "-24h", 10)
        availability_flux = availability_change_flux("zendure_raw", "-24h", 10)

        self.assertIn('rename(columns: {_value: "delta"})', state_flux)
        self.assertIn('rename(columns: {_value: "delta"})', availability_flux)

    def test_report_labels_transition_values_as_delta(self):
        report = build_report(
            "zendure_raw",
            "-24h",
            [
                {
                    "_time": "2026-05-14T10:00:00Z",
                    "device": "WR1",
                    "_field": "dc_status",
                    "_value": "-1"
                }
            ],
            [
                {
                    "_time": "2026-05-14T10:05:00Z",
                    "device": "WR1",
                    "_field": "available",
                    "delta": "1"
                }
            ],
            []
        )

        self.assertIn("| _time | device | _field | delta |", report)
        self.assertIn("| 2026-05-14T10:00:00Z | WR1 | dc_status | -1 |", report)
        self.assertIn("It is not the new firmware status value", report)
        self.assertIn("`-1` usually means `1 -> 0`", report)
        self.assertNotIn("| _time | device | _field | _value |", report)


if __name__ == "__main__":
    unittest.main()
