# SPDX-License-Identifier: AGPL-3.0-or-later
import unittest

import pytest

from ems.config import normalize_influxdb_config
from ems.history import schema

pytestmark = [
    pytest.mark.integration,
]


class FakeInfluxClient:
    """In-memory stand-in for HistoryInfluxClient used to test schema sync."""

    def __init__(self):
        self.buckets = {}  # name -> retention_seconds
        self.tasks = {}  # id -> {id, name, flux, status, lastRunStatus, ...}
        self._task_seq = 0
        self.calls = []

    def get_org_id(self):
        return "org-1"

    # buckets
    def find_bucket(self, name):
        if name not in self.buckets:
            return None
        return {"id": f"b-{name}", "name": name, "_retention": self.buckets[name]}

    def bucket_retention_seconds(self, bucket):
        return int(bucket.get("_retention", 0) or 0)

    def ensure_bucket_retention(self, name, retention_seconds):
        retention_seconds = int(retention_seconds or 0)
        if name not in self.buckets:
            self.buckets[name] = retention_seconds
            self.calls.append(("create_bucket", name, retention_seconds))
            return self.find_bucket(name), "created"
        if self.buckets[name] == retention_seconds:
            return self.find_bucket(name), "unchanged"
        self.buckets[name] = retention_seconds
        self.calls.append(("update_bucket", name, retention_seconds))
        return self.find_bucket(name), "updated"

    # tasks
    def list_tasks(self, limit=500):
        return list(self.tasks.values())

    def create_task(self, flux, status="active", org_id=None):
        self._task_seq += 1
        task_id = f"t-{self._task_seq}"
        name = _task_name_from_flux(flux)
        self.tasks[task_id] = {
            "id": task_id,
            "name": name,
            "flux": flux,
            "status": status,
            "lastRunStatus": "success",
        }
        self.calls.append(("create_task", name))
        return self.tasks[task_id]

    def update_task(self, task_id, flux=None, status=None):
        task = self.tasks[task_id]
        if flux is not None:
            task["flux"] = flux
        if status is not None:
            task["status"] = status
        self.calls.append(("update_task", task["name"], status))
        return task


def _task_name_from_flux(flux):
    # extract name: "..." from option task = {name: "...", every: ...}
    marker = 'name: "'
    start = flux.index(marker) + len(marker)
    end = flux.index('"', start)
    return flux[start:end]


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.config = normalize_influxdb_config(
            {"enabled": True, "bucket_prefix": "ems"}
        )

    def test_first_sync_creates_buckets_and_tasks(self):
        client = FakeInfluxClient()
        report = schema.sync(client, self.config)

        # raw + 1m + 5m + 1h
        self.assertEqual(
            sorted(b["name"] for b in report["buckets"]),
            ["ems_1h", "ems_1m", "ems_5m", "ems_raw"],
        )
        self.assertTrue(all(b["action"] == "created" for b in report["buckets"]))

        self.assertEqual(
            sorted(t["name"] for t in report["tasks"]),
            ["ems-downsample-1h", "ems-downsample-1m", "ems-downsample-5m"],
        )
        self.assertTrue(all(t["action"] == "created" for t in report["tasks"]))

    def test_retention_applied_from_config(self):
        client = FakeInfluxClient()
        schema.sync(client, self.config)
        # raw_days default 14
        self.assertEqual(client.buckets["ems_raw"], 14 * 86400)
        self.assertEqual(client.buckets["ems_1h"], 1825 * 86400)

    def test_second_sync_is_idempotent(self):
        client = FakeInfluxClient()
        schema.sync(client, self.config)
        client.calls.clear()

        report = schema.sync(client, self.config)
        self.assertEqual(client.calls, [])
        self.assertTrue(all(b["action"] == "unchanged" for b in report["buckets"]))
        self.assertTrue(all(t["action"] == "unchanged" for t in report["tasks"]))

    def test_retention_change_updates_bucket(self):
        client = FakeInfluxClient()
        schema.sync(client, self.config)

        changed = normalize_influxdb_config(
            {
                "enabled": True,
                "bucket_prefix": "ems",
                "retention": {"raw_days": 30},
            }
        )
        report = schema.sync(client, changed)
        raw = next(b for b in report["buckets"] if b["name"] == "ems_raw")
        self.assertEqual(raw["action"], "updated")
        self.assertEqual(client.buckets["ems_raw"], 30 * 86400)

    def test_obsolete_task_is_disabled(self):
        client = FakeInfluxClient()
        schema.sync(client, self.config)

        # Drop the 5m->1h downsampling step from config.
        reduced = normalize_influxdb_config(
            {
                "enabled": True,
                "bucket_prefix": "ems",
                "downsampling": [
                    {"source": "raw", "target": "1m", "window": "1m"},
                    {"source": "1m", "target": "5m", "window": "5m"},
                ],
            }
        )
        report = schema.sync(client, reduced)
        disabled = [t["name"] for t in report["disabled_tasks"] if t["action"] == "disabled"]
        self.assertIn("ems-downsample-1h", disabled)
        # the disabled task is now inactive
        task = next(t for t in client.tasks.values() if t["name"] == "ems-downsample-1h")
        self.assertEqual(task["status"], "inactive")

    def test_reenabling_disabled_task_reactivates(self):
        client = FakeInfluxClient()
        schema.sync(client, self.config)
        # disable 1h
        reduced = normalize_influxdb_config(
            {
                "enabled": True,
                "bucket_prefix": "ems",
                "downsampling": [
                    {"source": "raw", "target": "1m", "window": "1m"},
                    {"source": "1m", "target": "5m", "window": "5m"},
                ],
            }
        )
        schema.sync(client, reduced)
        # restore full config
        report = schema.sync(client, self.config)
        task = next(t for t in report["tasks"] if t["name"] == "ems-downsample-1h")
        self.assertEqual(task["action"], "updated")
        live = next(t for t in client.tasks.values() if t["name"] == "ems-downsample-1h")
        self.assertEqual(live["status"], "active")

    def test_prefix_changes_bucket_and_task_names(self):
        client = FakeInfluxClient()
        config = normalize_influxdb_config(
            {"enabled": True, "bucket_prefix": "home2"}
        )
        report = schema.sync(client, config)
        self.assertIn("home2_raw", [b["name"] for b in report["buckets"]])
        self.assertIn("home2-downsample-1m", [t["name"] for t in report["tasks"]])


class FluxBuildTest(unittest.TestCase):
    def test_flux_contains_buckets_window_and_org(self):
        config = normalize_influxdb_config(
            {"enabled": True, "bucket_prefix": "ems", "org": "myorg"}
        )
        entry = {"source": "raw", "target": "1m", "window": "1m"}
        flux = schema.build_downsample_flux(config, entry)
        self.assertIn('option task = {name: "ems-downsample-1m", every: 1m}', flux)
        self.assertIn('from(bucket: "ems_raw")', flux)
        self.assertIn('to(bucket: "ems_1m", org: "myorg")', flux)
        self.assertIn("fn: mean", flux)
        self.assertIn("fn: last", flux)


class StatusTest(unittest.TestCase):
    def test_status_reports_missing_and_health(self):
        config = normalize_influxdb_config(
            {"enabled": True, "bucket_prefix": "ems"}
        )
        client = FakeInfluxClient()
        # create only the raw bucket, and one failing task
        client.ensure_bucket_retention("ems_raw", 14 * 86400)
        client.tasks["t-x"] = {
            "id": "t-x",
            "name": "ems-downsample-1m",
            "status": "active",
            "lastRunStatus": "failed",
            "every": "1m",
        }
        report = schema.status(client, config)
        self.assertFalse(report["healthy"])
        self.assertIn("ems_1m", report["missing_buckets"])
        self.assertEqual(len(report["tasks"]), 1)


if __name__ == "__main__":
    unittest.main()
