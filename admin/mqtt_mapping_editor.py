# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Admin side of the MQTT mapping: read, check, preview and apply the mapping file.

The service's own validator decides what a mapping may say, and its own
calculation answers a preview, so the editor shows exactly what the service
will do. Applying writes ``config/mqtt/mappings.json`` atomically; the running
service reloads it by itself and the EMS does not restart.
"""

import json
import os

from admin.mqtt_broker import BuiltinBroker
from ems.file_io import atomic_write_bytes
from ems.zendure_mqtt.builtin_broker import mappings_path
from mqtt_mapping.config import (
    DEFAULT_PUBLISH_INTERVAL_SECONDS,
    MAX_DEVICES,
    MAX_SOURCES,
    TARGETS,
    empty_document,
    load_mappings_file,
    parse_mappings,
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
    def __init__(self, broker: BuiltinBroker):
        self.broker = broker

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

    def view(self) -> dict:
        refs = self.broker.mapping_broker_refs()
        _mappings, issues = load_mappings_file(self.path, broker_refs=refs)
        return {
            "document": self._document(),
            "issues": issues,
            "broker_refs": sorted(refs),
            "status": read_status(os.path.join(self.broker.data_dir, "mqtt-mapping-status.json")),
            "targets": list(TARGETS),
            "limits": {
                "devices": MAX_DEVICES,
                "sources": MAX_SOURCES,
                "publish_interval_seconds": DEFAULT_PUBLISH_INTERVAL_SECONDS,
            },
        }

    def preview(self, document) -> dict:
        _mappings, issues = parse_mappings(document, broker_refs=self.broker.mapping_broker_refs())
        return {"ok": not issues, "issues": issues}

    def apply(self, document) -> dict:
        _mappings, issues = parse_mappings(document, broker_refs=self.broker.mapping_broker_refs())
        if issues:
            return {"ok": False, "status": "invalid", "issues": issues}
        raw = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
        atomic_write_bytes(self.path, raw)
        container = self.broker.sync_mapping_container()
        return {"ok": True, "status": "applied", "issues": [], "container": container.as_dict()}

    def value_preview(self, device, payloads) -> dict:
        """What one draft device would publish from the given payloads (topic → text)."""

        document = {"schema_version": 1, "devices": [device]}
        mappings, issues = parse_mappings(document, broker_refs=self.broker.mapping_broker_refs())
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
