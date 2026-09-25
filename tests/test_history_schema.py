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

    def test_a_duplicated_task_is_disabled_down_to_one(self):
        """InfluxDB does not make task names unique, so a race can leave two.

        Two syncs overlapping -- an Admin sync killed by its ceiling and the
        retry its own message asks for -- can both find the task missing and
        both create it. A name-keyed lookup then sees only one of the two, so
        the other would keep writing the same window for the life of the
        installation without ever appearing in a report.
        """

        client = FakeInfluxClient()
        schema.sync(client, self.config)
        original = next(
            t for t in client.tasks.values() if t["name"] == "ems-downsample-1m"
        )
        client.tasks["t-99"] = dict(original, id="t-99")

        report = schema.sync(client, self.config)

        active = [
            t
            for t in client.tasks.values()
            if t["name"] == "ems-downsample-1m" and t["status"] == "active"
        ]
        self.assertEqual([t["id"] for t in active], [original["id"]])
        self.assertIn(
            {
                "name": "ems-downsample-1m",
                "action": "disabled",
                "reason": "duplicate",
            },
            report["disabled_tasks"],
        )

    def test_a_disabled_duplicate_says_why_it_was_disabled(self):
        """Its name is also in ``tasks``, saying `unchanged` on the same pass.

        Without the reason the two lines report one name saying opposite
        things, and from the second sync onwards they are identical.
        """

        client = FakeInfluxClient()
        schema.sync(client, self.config)
        original = next(
            t for t in client.tasks.values() if t["name"] == "ems-downsample-1m"
        )
        client.tasks["t-99"] = dict(original, id="t-99")

        schema.sync(client, self.config)
        report = schema.sync(client, self.config)

        self.assertEqual(
            report["disabled_tasks"],
            [{
                "name": "ems-downsample-1m",
                "action": "unchanged",
                "reason": "duplicate",
            }],
        )
        self.assertIn(
            {"name": "ems-downsample-1m", "action": "unchanged"},
            report["tasks"],
        )

    def test_the_survivor_is_chosen_by_rank_and_not_by_the_order_listed(self):
        """Two syncs must not disable each other's winner.

        Reading the API order would flip which copy is active on every pass,
        and each flip costs the newly activated task a catch-up run.
        """

        client = FakeInfluxClient()
        schema.sync(client, self.config)
        original = next(
            t for t in client.tasks.values() if t["name"] == "ems-downsample-1m"
        )
        # Listed ahead of the original, so a sweep that trusted the listing
        # order would keep a copy instead.
        client.tasks = {
            "t-98": dict(original, id="t-98"),
            "t-99": dict(original, id="t-99"),
            **client.tasks,
        }

        schema.sync(client, self.config)
        schema.sync(client, self.config)

        active = [
            t
            for t in client.tasks.values()
            if t["name"] == "ems-downsample-1m" and t["status"] == "active"
        ]
        self.assertEqual([t["id"] for t in active], [original["id"]])

    def test_an_active_task_outranks_a_disabled_copy_of_its_name(self):
        """Activating the stale copy is not free.

        InfluxDB resumes an activated task from its last completion, so keeping
        the one that slept makes it backfill the whole gap while the copy that
        was actually running gets switched off.
        """

        client = FakeInfluxClient()
        schema.sync(client, self.config)
        stale = next(
            t for t in client.tasks.values() if t["name"] == "ems-downsample-1m"
        )
        stale["status"] = "inactive"
        client.tasks["t-99"] = dict(stale, id="t-99", status="active")

        schema.sync(client, self.config)

        active = [
            t["id"]
            for t in client.tasks.values()
            if t["name"] == "ems-downsample-1m" and t["status"] == "active"
        ]
        self.assertEqual(active, ["t-99"])

    def test_an_obsolete_task_that_exists_twice_is_disabled_twice(self):
        """The sweep must not stop at the first copy of a name it is retiring.

        A step dropped from the config that the same race duplicated would
        otherwise keep one task writing into a bucket nobody expects any more.
        """

        client = FakeInfluxClient()
        schema.sync(client, self.config)
        obsolete = next(
            t for t in client.tasks.values() if t["name"] == "ems-downsample-1h"
        )
        client.tasks["t-99"] = dict(obsolete, id="t-99")

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

        still_active = [
            t["id"]
            for t in client.tasks.values()
            if t["name"] == "ems-downsample-1h" and t["status"] == "active"
        ]
        self.assertEqual(still_active, [])
        self.assertEqual(
            [t["reason"] for t in report["disabled_tasks"]],
            ["not_configured", "not_configured"],
        )

    def test_a_config_change_still_reaches_the_surviving_duplicate(self):
        """The realistic sequence is a killed sync first, a config edit after.

        If the duplicate made the sweep skip the update, the operator would see
        a clean report while the new window never reached InfluxDB.
        """

        client = FakeInfluxClient()
        schema.sync(client, self.config)
        original = next(
            t for t in client.tasks.values() if t["name"] == "ems-downsample-5m"
        )
        client.tasks["t-99"] = dict(original, id="t-99")

        widened = normalize_influxdb_config(
            {
                "enabled": True,
                "bucket_prefix": "ems",
                "downsampling": [
                    {"source": "raw", "target": "1m", "window": "1m"},
                    {"source": "1m", "target": "5m", "window": "10m"},
                    {"source": "5m", "target": "1h", "window": "1h"},
                ],
            }
        )
        report = schema.sync(client, widened)

        self.assertIn(
            {"name": "ems-downsample-5m", "action": "updated"}, report["tasks"]
        )
        self.assertIn("10m", client.tasks[original["id"]]["flux"])

    def test_a_task_without_an_id_never_becomes_the_survivor(self):
        """It is the one thing that cannot receive the write it would be sent.

        Ranking it first would leave the addressable copy disabled and the
        unusable one as the only active task, on every later sync as well.
        """

        client = FakeInfluxClient()
        schema.sync(client, self.config)
        original = next(
            t for t in client.tasks.values() if t["name"] == "ems-downsample-1m"
        )
        client.tasks["ghost"] = dict(original, id="")

        schema.sync(client, self.config)

        # The unusable one cannot be switched off either, so it stays; what
        # matters is that it did not cost the addressable task its place.
        self.assertEqual(client.tasks[original["id"]]["status"], "active")

    def test_a_task_without_an_id_is_left_alone_rather_than_written_to(self):
        """Disabling it means a PATCH to a task route that does not exist.

        The 404 would fail the whole sync, so the buckets would be reconciled
        and the tasks would not.
        """

        client = FakeInfluxClient()
        schema.sync(client, self.config)
        original = next(
            t for t in client.tasks.values() if t["name"] == "ems-downsample-1m"
        )
        client.tasks["ghost"] = dict(original, id=None)

        report = schema.sync(client, self.config)

        self.assertEqual(report["disabled_tasks"], [])
        self.assertNotIn(None, [call[1] for call in client.calls if call[0] == "update_task"])

    def test_a_name_with_no_addressable_task_is_treated_as_missing(self):
        """Reconciling the unusable one means a write it cannot receive.

        Creating a task that can be addressed is the only move left; the
        unusable one cannot even be switched off, so it stays where it is.
        """

        client = FakeInfluxClient()
        client.tasks["ghost"] = {
            "id": "",
            "name": "ems-downsample-1m",
            "flux": "stale",
            "status": "active",
        }

        report = schema.sync(client, self.config)

        addressable = [
            t
            for t in client.tasks.values()
            if t["name"] == "ems-downsample-1m" and t["id"]
        ]
        self.assertEqual(len(addressable), 1)
        self.assertIn({"name": "ems-downsample-1m", "action": "created"}, report["tasks"])

    def test_a_target_named_twice_in_the_config_makes_one_task(self):
        """Two entries want one task name, and the second must not add a twin.

        Creating both would leave the sync disabling on its next pass something
        it had just created itself.
        """

        client = FakeInfluxClient()
        doubled = normalize_influxdb_config(
            {
                "enabled": True,
                "bucket_prefix": "ems",
                "downsampling": [
                    {"source": "raw", "target": "1m", "window": "1m"},
                    {"source": "raw", "target": "1m", "window": "5m"},
                ],
            }
        )

        report = schema.sync(client, doubled)

        named_1m = [t for t in client.tasks.values() if t["name"] == "ems-downsample-1m"]
        self.assertEqual(len(named_1m), 1)
        self.assertEqual(
            [t["action"] for t in report["tasks"]], ["created", "updated"]
        )

    def test_a_config_caused_duplicate_is_not_reported_as_a_race(self):
        """The doc defines `duplicate` as two syncs running at once.

        Saying that about a twin the config itself asks for sends the operator
        looking for a race instead of at the two entries naming one target.
        """

        client = FakeInfluxClient()
        schema.sync(client, self.config)
        original = next(
            t for t in client.tasks.values() if t["name"] == "ems-downsample-1m"
        )
        client.tasks["t-99"] = dict(original, id="t-99")

        doubled = normalize_influxdb_config(
            {
                "enabled": True,
                "bucket_prefix": "ems",
                "downsampling": [
                    {"source": "raw", "target": "1m", "window": "1m"},
                    {"source": "raw", "target": "1m", "window": "5m"},
                    {"source": "1m", "target": "5m", "window": "5m"},
                    {"source": "5m", "target": "1h", "window": "1h"},
                ],
            }
        )
        report = schema.sync(client, doubled)

        self.assertEqual(
            [t["reason"] for t in report["disabled_tasks"]], ["duplicate_target"]
        )

    def test_a_clean_install_reports_no_disabled_tasks(self):
        """The duplicate sweep walks every owned task; it must stay silent when
        each name has exactly one."""

        client = FakeInfluxClient()
        schema.sync(client, self.config)

        report = schema.sync(client, self.config)

        self.assertEqual(report["disabled_tasks"], [])

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
