# SPDX-License-Identifier: AGPL-3.0-or-later
"""``data/mqtt-mapping-status.json``: what the service is doing, for people to read.

Not authoritative: the Admin shows it next to the mappings, the EMS diagnosis
names a silent device from it, and a container health check reads its age.
Nothing in it is a credential.
"""

import json
import os
import time
from datetime import datetime, timezone

from ems.file_io import atomic_write_bytes
from mqtt_mapping.mapping import SILENCE_REASONS

STATUS_SCHEMA_VERSION = 1
HEALTHY_STATUS_AGE_SECONDS = 60
DELIVERY_GRACE_SECONDS = 5
STATUS_REASONS = (*SILENCE_REASONS, "not_delivered")


def _iso(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _number(value):
    return None if value is None else round(value, 1)


def build_status(*, publisher, brokers, issues, now_wall=None) -> dict:
    """The status document for ``publisher``'s devices and the broker connections.

    ``brokers`` maps a broker reference to ``{"connected", "mqtt5", "error"}``.
    A device counts as publishing only while its broker is connected and took a
    state within two intervals; a state that is computed but not delivered says
    ``not_delivered``.
    """

    now_wall = time.time() if now_wall is None else now_wall
    evaluations = publisher.evaluations()
    window = 2 * publisher.mappings.publish_interval_seconds + DELIVERY_GRACE_SECONDS
    devices = []
    for device in publisher.mappings.devices:
        evaluation = evaluations.get(device.id)
        computed = bool(evaluation and evaluation.state is not None)
        last = publisher.last_published_wall(device.id)
        connected = bool((brokers.get(device.broker_ref) or {}).get("connected"))
        delivered = computed and connected and last is not None and now_wall - last <= window
        reason = "not_delivered" if computed and not delivered else (evaluation.reason if evaluation else None)
        devices.append(
            {
                "id": device.id,
                "label": device.label,
                "enabled": device.enabled,
                "broker_ref": device.broker_ref,
                "topic": device.output_topic,
                "publishing": delivered,
                "reason": reason,
                "state": evaluation.state if evaluation else None,
                "last_published_at": _iso(publisher.last_published_wall(device.id)),
                "targets": [
                    {"key": target.key, "value": target.value, "reason": target.reason}
                    for target in (evaluation.targets if evaluation else ())
                ],
                "sources": [
                    {
                        "topic": source.topic,
                        "path": source.path,
                        "value": source.value,
                        "reason": source.reason,
                        "age_seconds": _number(source.age_seconds),
                        "payload_preview": source.payload_preview,
                    }
                    for source in (evaluation.sources if evaluation else ())
                ],
            }
        )
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "updated_at": _iso(now_wall),
        "publish_interval_seconds": publisher.mappings.publish_interval_seconds,
        "brokers": {
            ref: {
                "connected": bool(state.get("connected")),
                "mqtt5": state.get("mqtt5"),
                "error": state.get("error"),
            }
            for ref, state in sorted(brokers.items())
        },
        "issues": list(issues),
        "devices": devices,
    }


def write_status(path, status) -> None:
    atomic_write_bytes(path, json.dumps(status, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def read_status(path):
    """The status document, or ``None`` when there is none or it cannot be read."""

    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def status_is_fresh(path, *, now=None, max_age=HEALTHY_STATUS_AGE_SECONDS) -> bool:
    """Whether the service wrote its status recently enough to count as alive."""

    now = time.time() if now is None else now
    try:
        return now - os.stat(path).st_mtime <= max_age
    except OSError:
        return False
