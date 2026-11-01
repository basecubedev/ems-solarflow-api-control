# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin side of the MQTT mapping: read, check, preview and apply the mapping file.

The service's own validator decides what a mapping may say, and its own
calculation answers a preview, so the editor shows exactly what the service
will do. Applying writes ``config/mqtt/mappings.json`` atomically; the running
service reloads it by itself and the EMS does not restart.
"""

import json

from admin.mqtt_broker import BuiltinBroker, local_profile_endpoints
from ems.file_io import atomic_write_bytes
from ems.zendure_mqtt.builtin_broker import BUILTIN_PROFILE_REF, mapping_status_path, mappings_path
from mqtt_mapping.config import (
    DEFAULT_PUBLISH_INTERVAL_SECONDS,
    MAX_DEVICES,
    MAX_SOURCES,
    TARGETS,
    empty_document,
    load_mappings_file,
    parse_mappings,
    saved_brokers,
)
from mqtt_mapping.mapping import preview_device
from mqtt_mapping.status import read_status


def _evaluation_dict(evaluation):
    return {
        "state": evaluation.state,
        "reason": evaluation.reason,
        "targets": [
            {"key": target.key, "value": target.value, "reason": target.reason}
            for target in evaluation.targets
        ],
        "sources": [
            {"topic": source.topic, "path": source.path, "value": source.value, "reason": source.reason}
            for source in evaluation.sources
        ],
    }


class MappingEditor:
    """The mapping file as the Admin edits it.

    ``found`` returns the brokers a search found and a mapping may read now
    (``ref -> entry``). A mapping on one of them is valid, and saving it
    remembers the broker under ``brokers``, so the mapping keeps reading it.
    """

    def __init__(self, broker: BuiltinBroker, found=None):
        self.broker = broker
        self._found = found or dict

    @property
    def path(self):
        return mappings_path(self.broker.config_dir)

    def _document(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return empty_document()
        except (OSError, ValueError):
            return None

    def _refs(self):
        return self.broker.mapping_broker_refs() | set(self._found())

    def view(self) -> dict:
        refs = self._refs()
        _mappings, issues = load_mappings_file(self.path, broker_refs=refs)
        return {
            "document": self._document(),
            "issues": issues,
            "broker_refs": sorted(refs),
            "status": read_status(mapping_status_path(self.broker.data_dir)),
            "targets": list(TARGETS),
            "limits": {
                "devices": MAX_DEVICES,
                "sources": MAX_SOURCES,
                "publish_interval_seconds": DEFAULT_PUBLISH_INTERVAL_SECONDS,
            },
        }

    def preview(self, document) -> dict:
        _mappings, issues = parse_mappings(document, broker_refs=self._refs())
        return {"ok": not issues, "issues": issues}

    def _remembered(self, mappings) -> dict:
        """``brokers`` for the file: each broker a mapping reads that is neither built in nor an EMS profile."""

        saved = saved_brokers(self._document())
        found = self._found()
        profiles = set(local_profile_endpoints(self.broker._config_loader(), self.broker.store))
        remembered = {}
        for ref in sorted({device.broker_ref for device in mappings.devices}):
            if ref == BUILTIN_PROFILE_REF or ref in profiles and ref not in saved:
                continue
            entry = saved.get(ref) or found.get(ref)
            if entry is not None:
                remembered[ref] = entry
        return remembered

    def apply(self, document) -> dict:
        mappings, issues = parse_mappings(document, broker_refs=self._refs())
        if issues:
            return {"ok": False, "status": "invalid", "issues": issues}
        document = {key: value for key, value in document.items() if key != "brokers"}
        remembered = self._remembered(mappings)
        if remembered:
            document["brokers"] = remembered
        raw = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
        atomic_write_bytes(self.path, raw)
        container = self.broker.sync_mapping_container()
        return {"ok": True, "status": "applied", "issues": [], "container": container.as_dict()}

    def value_preview(self, device, payloads) -> dict:
        """What one draft device would publish from the given payloads (topic → text)."""

        document = {"schema_version": 1, "devices": [device]}
        mappings, issues = parse_mappings(document, broker_refs=self._refs())
        if issues:
            return {"ok": False, "issues": issues}
        evaluation = preview_device(mappings.devices[0], payloads)
        return {"ok": True, "issues": [], "evaluation": _evaluation_dict(evaluation)}

    def mapped_values(self, broker_ref) -> dict:
        """``(topic, path) -> {device ids}`` for the mappings on ``broker_ref``."""

        mappings, _issues = load_mappings_file(self.path)
        mapped = {}
        for device in mappings.devices if mappings else ():
            if device.broker_ref != broker_ref:
                continue
            for source in device.all_sources():
                mapped.setdefault((source.topic, source.path), set()).add(device.id)
        return mapped
