# SPDX-License-Identifier: AGPL-3.0-or-later
"""InfluxDB 2.x HTTP client for the history/analytics backend.

Builds on the dependency-light ``InfluxHTTPClient`` already used by the
developer telemetry scripts (``scripts/influx_utils.py``) and adds the
operations the config-driven schema reconciler needs but the script client
lacks: updating retention on an existing bucket, and full task lifecycle
(list / create / update / delete).

Import-side-effect-free.
"""

from scripts.influx_utils import (  # noqa: F401  (re-exported for convenience)
    InfluxHTTPClient,
    build_line_protocol,
    coerce_value,
    parse_influx_csv,
    wait_for_influx_ready,
)


class HistoryInfluxClient(InfluxHTTPClient):
    """InfluxHTTPClient extended with retention-update and task management."""

    def _json_headers(self, accept="application/json"):
        return {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
            "Accept": accept,
        }

    # -- buckets -----------------------------------------------------------

    def update_bucket_retention(self, bucket_id, retention_seconds):
        every_seconds = int(retention_seconds or 0)
        retention_rules = []

        if every_seconds > 0:
            retention_rules.append(
                {"type": "expire", "everySeconds": every_seconds}
            )

        response = self.session.patch(
            f"{self.base_url}/api/v2/buckets/{bucket_id}",
            json={"retentionRules": retention_rules},
            headers=self._json_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def bucket_retention_seconds(self, bucket):
        """Return the configured expiry (seconds) for a bucket payload, 0 = infinite."""
        for rule in bucket.get("retentionRules", []) or []:
            if rule.get("type") == "expire":
                return int(rule.get("everySeconds", 0) or 0)
        return 0

    def ensure_bucket_retention(self, bucket_name, retention_seconds):
        """Create the bucket or align its retention. Returns (bucket, action)."""
        existing = self.find_bucket(bucket_name)

        if existing is None:
            bucket, _ = self.ensure_bucket(bucket_name, retention_seconds)
            return bucket, "created"

        current = self.bucket_retention_seconds(existing)
        if current == int(retention_seconds or 0):
            return existing, "unchanged"

        updated = self.update_bucket_retention(
            existing.get("id"), retention_seconds
        )
        return updated, "updated"

    # -- tasks -------------------------------------------------------------

    def list_tasks(self, limit=500):
        response = self.session.get(
            f"{self.base_url}/api/v2/tasks",
            params={"org": self.org, "limit": limit},
            headers=self._json_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json().get("tasks", [])

    def find_task(self, name):
        for task in self.list_tasks():
            if task.get("name") == name:
                return task
        return None

    def create_task(self, flux, status="active", org_id=None):
        response = self.session.post(
            f"{self.base_url}/api/v2/tasks",
            json={
                "orgID": org_id or self.get_org_id(),
                "flux": flux,
                "status": status,
            },
            headers=self._json_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def update_task(self, task_id, flux=None, status=None):
        body = {}
        if flux is not None:
            body["flux"] = flux
        if status is not None:
            body["status"] = status

        response = self.session.patch(
            f"{self.base_url}/api/v2/tasks/{task_id}",
            json=body,
            headers=self._json_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def delete_task(self, task_id):
        response = self.session.delete(
            f"{self.base_url}/api/v2/tasks/{task_id}",
            headers=self._json_headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
