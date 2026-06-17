# SPDX-License-Identifier: AGPL-3.0-or-later
import unittest

from ems.config import (
    INFLUXDB_DEFAULTS,
    influx_duration_seconds,
    is_influx_duration,
    normalize_influxdb_config,
    resolve_influx_token,
    sanitize_bucket_prefix,
)


class InfluxDurationTest(unittest.TestCase):
    def test_valid_durations(self):
        for value in ("10s", "1m", "2h", "7d", "4w"):
            self.assertTrue(is_influx_duration(value), value)

    def test_invalid_durations(self):
        for value in ("", "m", "10", "10x", "-5m", "0s", None):
            self.assertFalse(is_influx_duration(value), value)

    def test_duration_seconds(self):
        self.assertEqual(influx_duration_seconds("6h"), 21600)
        self.assertEqual(influx_duration_seconds("30d"), 30 * 86400)
        self.assertEqual(influx_duration_seconds("bogus"), 0)


class SanitizePrefixTest(unittest.TestCase):
    def test_keeps_safe_chars(self):
        self.assertEqual(sanitize_bucket_prefix("ems_home-1"), "ems_home-1")

    def test_strips_unsafe_chars(self):
        self.assertEqual(sanitize_bucket_prefix("ems prod!"), "emsprod")

    def test_falls_back_to_default_when_empty(self):
        self.assertEqual(sanitize_bucket_prefix("  "), "ems")
        self.assertEqual(sanitize_bucket_prefix("@@@", default="x"), "x")


class NormalizeInfluxConfigTest(unittest.TestCase):
    def test_empty_uses_defaults(self):
        cfg = normalize_influxdb_config({})
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["bucket_prefix"], "ems")
        self.assertEqual(
            cfg["retention"], INFLUXDB_DEFAULTS["retention"]
        )
        self.assertEqual(len(cfg["downsampling"]), 3)
        self.assertEqual(len(cfg["query_profiles"]), 4)

    def test_none_is_handled(self):
        cfg = normalize_influxdb_config(None)
        self.assertFalse(cfg["enabled"])

    def test_drops_invalid_downsampling_entries(self):
        cfg = normalize_influxdb_config(
            {
                "downsampling": [
                    {"source": "raw", "target": "1m", "window": "1m"},
                    {"source": "raw", "target": "", "window": "5m"},
                    {"source": "raw", "target": "5m", "window": "bogus"},
                    "not-a-dict",
                ]
            }
        )
        self.assertEqual(
            cfg["downsampling"],
            [{"source": "raw", "target": "1m", "window": "1m"}],
        )

    def test_drops_invalid_query_profiles_and_sorts(self):
        cfg = normalize_influxdb_config(
            {
                "query_profiles": [
                    {"max_range": "30d", "bucket": "5m", "window": "5m"},
                    {"max_range": "6h", "bucket": "raw", "window": "10s"},
                    {"max_range": "bad", "bucket": "raw", "window": "1m"},
                    {"max_range": "24h", "bucket": "", "window": "1m"},
                ]
            }
        )
        self.assertEqual(
            [p["max_range"] for p in cfg["query_profiles"]],
            ["6h", "30d"],
        )

    def test_retention_coerced_to_int(self):
        cfg = normalize_influxdb_config(
            {"retention": {"raw_days": "21", "one_minute_days": -5}}
        )
        self.assertEqual(cfg["retention"]["raw_days"], 21)
        # negative clamps to minimum 0
        self.assertEqual(cfg["retention"]["one_minute_days"], 0)

    def test_bucket_prefix_sanitized(self):
        cfg = normalize_influxdb_config({"bucket_prefix": "my prefix!"})
        self.assertEqual(cfg["bucket_prefix"], "myprefix")


class ResolveTokenTest(unittest.TestCase):
    def test_explicit_token_wins(self):
        cfg = normalize_influxdb_config({"token": "secret"})
        self.assertEqual(resolve_influx_token(cfg, environ={}), "secret")

    def test_falls_back_to_env(self):
        cfg = normalize_influxdb_config({"token_env": "MY_TOKEN"})
        self.assertEqual(
            resolve_influx_token(cfg, environ={"MY_TOKEN": "envtok"}),
            "envtok",
        )

    def test_missing_returns_empty(self):
        cfg = normalize_influxdb_config({"token_env": "MISSING"})
        self.assertEqual(resolve_influx_token(cfg, environ={}), "")


if __name__ == "__main__":
    unittest.main()
