# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import unittest

import pytest

from ems.config import (
    INFLUXDB_DEFAULTS,
    influx_duration_seconds,
    is_influx_duration,
    is_valid_influx_name,
    normalize_influxdb_config,
    resolve_influx_token,
    sanitize_bucket_prefix,
)

pytestmark = [
    pytest.mark.config,
    pytest.mark.unit,
]


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
        self.assertEqual(len(cfg["query_profiles"]), 5)

    def test_none_is_handled(self):
        cfg = normalize_influxdb_config(None)
        self.assertFalse(cfg["enabled"])

    def test_raw_write_interval_defaults_to_every_loop(self):
        cfg = normalize_influxdb_config({})
        self.assertEqual(cfg["raw_write_interval_seconds"], 0)

    def test_raw_write_interval_null_means_every_loop(self):
        cfg = normalize_influxdb_config({"raw_write_interval_seconds": None})
        self.assertEqual(cfg["raw_write_interval_seconds"], 0)

    def test_raw_write_interval_explicit_value(self):
        cfg = normalize_influxdb_config({"raw_write_interval_seconds": 10})
        self.assertEqual(cfg["raw_write_interval_seconds"], 10)

    def test_raw_write_interval_negative_clamped(self):
        cfg = normalize_influxdb_config({"raw_write_interval_seconds": -5})
        self.assertEqual(cfg["raw_write_interval_seconds"], 0)

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

    def test_zero_config_defaults(self):
        cfg = normalize_influxdb_config({})
        self.assertEqual(cfg["mode"], "bundled")
        self.assertTrue(cfg["auto_init"])
        self.assertTrue(cfg["auto_sync"])
        self.assertEqual(cfg["secret_file"], "deploy/docker/influxdb.env")

    def test_legacy_config_without_new_fields_still_normalizes(self):
        # An existing config that predates mode/auto_init/auto_sync/secret_file.
        cfg = normalize_influxdb_config(
            {
                "enabled": True,
                "url": "http://influxdb:8086",
                "org": "ems",
                "token_env": "INFLUXDB_TOKEN",
                "bucket_prefix": "ems",
            }
        )
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["mode"], "bundled")
        self.assertTrue(cfg["auto_init"])
        self.assertTrue(cfg["auto_sync"])
        self.assertEqual(cfg["secret_file"], "deploy/docker/influxdb.env")

    def test_host_url_default_for_bundled(self):
        cfg = normalize_influxdb_config({})
        self.assertEqual(cfg["host_url"], "http://127.0.0.1:8086")

    def test_host_url_custom_value_kept(self):
        cfg = normalize_influxdb_config({"host_url": "http://10.0.0.5:8086"})
        self.assertEqual(cfg["host_url"], "http://10.0.0.5:8086")

    def test_host_url_empty_falls_back_to_default(self):
        cfg = normalize_influxdb_config({"host_url": "   "})
        self.assertEqual(cfg["host_url"], "http://127.0.0.1:8086")

    def test_legacy_config_without_host_url_defaults(self):
        # A config predating host_url still normalizes to the loopback default.
        cfg = normalize_influxdb_config(
            {"enabled": True, "url": "http://influxdb:8086"}
        )
        self.assertEqual(cfg["url"], "http://influxdb:8086")
        self.assertEqual(cfg["host_url"], "http://127.0.0.1:8086")

    def test_external_mode_preserved(self):
        cfg = normalize_influxdb_config({"mode": "external"})
        self.assertEqual(cfg["mode"], "external")

    def test_unknown_mode_falls_back_to_bundled(self):
        cfg = normalize_influxdb_config({"mode": "weird"})
        self.assertEqual(cfg["mode"], "bundled")

    def test_auto_flags_can_be_disabled(self):
        cfg = normalize_influxdb_config({"auto_init": False, "auto_sync": False})
        self.assertFalse(cfg["auto_init"])
        self.assertFalse(cfg["auto_sync"])

    def test_secret_file_relative_kept(self):
        cfg = normalize_influxdb_config({"secret_file": "deploy/docker/x.env"})
        self.assertEqual(cfg["secret_file"], "deploy/docker/x.env")

    def test_secret_file_absolute_rejected(self):
        cfg = normalize_influxdb_config({"secret_file": "/etc/secrets/x.env"})
        self.assertEqual(cfg["secret_file"], "deploy/docker/influxdb.env")

    def test_secret_file_parent_escape_rejected(self):
        cfg = normalize_influxdb_config({"secret_file": "../outside.env"})
        self.assertEqual(cfg["secret_file"], "deploy/docker/influxdb.env")

    def test_secret_file_empty_uses_default(self):
        cfg = normalize_influxdb_config({"secret_file": "   "})
        self.assertEqual(cfg["secret_file"], "deploy/docker/influxdb.env")


class ValidInfluxNameTest(unittest.TestCase):
    def test_accepts_normal_names(self):
        for value in ("raw", "1m", "5m", "1h", "ems_home-1", "bucket.v2"):
            self.assertTrue(is_valid_influx_name(value), value)

    def test_rejects_unsafe_names(self):
        for value in (
            "",
            None,
            "raw bucket",
            'raw"',
            "raw\nbucket",
            'raw") |> drop(',
            "../raw",
            "raw/1m",
            "raw;rm -rf",
            "raw|cat",
            "$(whoami)",
            ".",
            "..",
        ):
            self.assertFalse(is_valid_influx_name(value), value)


class NormalizeInfluxNameValidationTest(unittest.TestCase):
    def test_default_config_remains_valid(self):
        cfg = normalize_influxdb_config(copy.deepcopy(INFLUXDB_DEFAULTS))
        self.assertEqual(len(cfg["downsampling"]), 3)
        self.assertEqual(len(cfg["query_profiles"]), 5)

    def test_drops_downsampling_with_unsafe_source(self):
        cfg = normalize_influxdb_config(
            {
                "downsampling": [
                    {"source": "raw", "target": "1m", "window": "1m"},
                    {"source": "raw bucket", "target": "5m", "window": "5m"},
                    {"source": "raw", "target": 'x" |> yield(', "window": "1h"},
                    {"source": "raw\nx", "target": "1h", "window": "1h"},
                ]
            }
        )
        self.assertEqual(
            cfg["downsampling"],
            [{"source": "raw", "target": "1m", "window": "1m"}],
        )

    def test_drops_query_profile_with_unsafe_bucket(self):
        cfg = normalize_influxdb_config(
            {
                "query_profiles": [
                    {"max_range": "6h", "bucket": "raw", "window": "10s"},
                    {"max_range": "24h", "bucket": "bad bucket", "window": "1m"},
                    {"max_range": "30d", "bucket": 'raw"', "window": "5m"},
                    {"max_range": "365d", "bucket": "raw\nx", "window": "1h"},
                ]
            }
        )
        self.assertEqual(
            [p["bucket"] for p in cfg["query_profiles"]],
            ["raw"],
        )


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
