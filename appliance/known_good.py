# SPDX-License-Identifier: AGPL-3.0-or-later
"""Known-good Admin records.

Rollback must never depend on a mutable tag, so every verified Admin is stored
with its resolved digest. At least the current and the previous verified Admin
are retained.
"""

import json
import time
from pathlib import Path

from appliance.paths import atomic_write

HISTORY_FILE = "history.json"
MIN_RETAINED = 2
MAX_RETAINED = 5
HEALTHCHECK_PASSED = "passed"


class KnownGoodStore:
    def __init__(self, directory, *, time_fn=None, max_entries=MAX_RETAINED):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._time = time_fn or time.time
        self.max_entries = max(int(max_entries), MIN_RETAINED)

    @property
    def path(self):
        return self.directory / HISTORY_FILE

    def entries(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        return [entry for entry in payload if isinstance(entry, dict)]

    def current(self):
        entries = self.entries()
        return entries[0] if entries else None

    def previous(self):
        entries = self.entries()
        return entries[1] if len(entries) > 1 else None

    def find_by_digest(self, digest):
        for entry in self.entries():
            if entry.get("admin_digest") == digest:
                return entry
        return None

    def record(
        self,
        *,
        admin_image,
        admin_digest,
        admin_version,
        revision="",
        compose_hash="",
        healthcheck=HEALTHCHECK_PASSED,
    ):
        entry = {
            "admin_image": str(admin_image),
            "admin_digest": str(admin_digest),
            "admin_version": str(admin_version),
            "revision": str(revision or ""),
            "compose_hash": str(compose_hash or ""),
            "verified_at": self._time(),
            "healthcheck": str(healthcheck),
        }
        entries = [
            item
            for item in self.entries()
            if item.get("admin_digest") != entry["admin_digest"]
            or item.get("admin_image") != entry["admin_image"]
        ]
        entries.insert(0, entry)
        atomic_write(
            self.path, json.dumps(entries[: self.max_entries], indent=2, sort_keys=True) + "\n"
        )
        return entry
