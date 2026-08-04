# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin-only discovery preparation settings (source priority + enable flags).

This is a small setup-orchestration view model: it stores which discovery
sources are enabled and the order in which they are scanned / resolved. It is
kept deliberately separate from the EMS runtime config — nothing here is ever
written into ``config.json`` and it never creates a runtime fallback.

Only non-secret preparation state lives here (priority list and per-source
``enabled`` flags). Credentials/tokens stay in their own stores (the Zendure
token store, for example) and are never mirrored into this file.
"""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from admin.releases import default_admin_data_dir

SOURCE_LOCAL_API = "local_api"
SOURCE_LOCAL_MQTT = "local_mqtt"
SOURCE_ZENDURE_MQTT = "zendure_mqtt"

# Ordered tuple of every discovery source Admin knows about. The default
# priority intentionally matches this order (local first, cloud last).
DISCOVERY_SOURCES = (SOURCE_LOCAL_API, SOURCE_LOCAL_MQTT, SOURCE_ZENDURE_MQTT)
DEFAULT_PRIORITY = list(DISCOVERY_SOURCES)

PREPARATION_FILENAME = "discovery-preparation.json"


class DiscoveryPreparationError(Exception):
    """A save failure whose message is safe to show to the operator."""


@dataclass
class DiscoverySourceSettings:
    source: str
    enabled: bool
    priority: int

    def to_dict(self):
        return {"source": self.source, "enabled": self.enabled, "priority": self.priority}


def default_preparation():
    return {
        "discovery_priority": list(DEFAULT_PRIORITY),
        "sources": {source: {"enabled": True} for source in DISCOVERY_SOURCES},
    }


def normalize_preparation(raw):
    """Coerce arbitrary input into a complete, valid preparation payload.

    The priority is always a full permutation of the known sources: unknown or
    duplicate entries are dropped and any missing source is appended in default
    order, so a partial or hand-edited file can never leave a source unlisted.
    """

    if not isinstance(raw, dict):
        raw = {}

    priority = []
    seen = set()
    for entry in raw.get("discovery_priority") or []:
        if entry in DISCOVERY_SOURCES and entry not in seen:
            seen.add(entry)
            priority.append(entry)
    for source in DEFAULT_PRIORITY:
        if source not in seen:
            priority.append(source)

    raw_sources = raw.get("sources")
    raw_sources = raw_sources if isinstance(raw_sources, dict) else {}
    sources = {}
    for source in DISCOVERY_SOURCES:
        entry = raw_sources.get(source)
        enabled = True
        if isinstance(entry, dict) and "enabled" in entry:
            enabled = bool(entry["enabled"])
        elif isinstance(entry, bool):
            enabled = entry
        sources[source] = {"enabled": enabled}

    return {"discovery_priority": priority, "sources": sources}


def source_settings(preparation):
    """Return ``DiscoverySourceSettings`` for each source, ordered by priority."""

    normalized = normalize_preparation(preparation)
    sources = normalized["sources"]
    return [
        DiscoverySourceSettings(
            source=source,
            enabled=bool(sources[source]["enabled"]),
            priority=index + 1,
        )
        for index, source in enumerate(normalized["discovery_priority"])
    ]


def enabled_sources_in_priority(preparation):
    """Return the enabled source keys in configured priority order."""

    return [s.source for s in source_settings(preparation) if s.enabled]


class DiscoveryPreparationStore:
    """Persists discovery preparation settings under the Admin data dir.

    Reads degrade to defaults on a missing/corrupt file; a write failure raises
    :class:`DiscoveryPreparationError` so the caller can report it without a 500.
    """

    def __init__(self, data_dir=None):
        base = Path(data_dir) if data_dir else default_admin_data_dir()
        self.state_dir = base / "state"
        self.path = self.state_dir / PREPARATION_FILENAME

    def load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return default_preparation()
        try:
            parsed = json.loads(raw)
        except ValueError:
            return default_preparation()
        return normalize_preparation(parsed)

    def save(self, preparation):
        payload = normalize_preparation(preparation)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".discovery-preparation.", suffix=".tmp", dir=self.state_dir
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise DiscoveryPreparationError(
                "Could not save the discovery preparation settings."
            ) from exc
        return payload
