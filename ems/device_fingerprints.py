# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the EMS read for each device when it started, comparable with the saved settings.

EMS reads ``config.json`` once, when it starts, so a device entry saved after
that takes effect only when it starts again. The running EMS keeps one
fingerprint per entry it read and writes them into its control snapshot
(``loaded_devices``); whoever reads the file as saved now takes the same
fingerprints of it and compares. One function makes both, so the two sides
cannot drift apart within a release, and the record carries the version of the
rule that made them, so a reader of another release can tell it cannot compare.
Import-side-effect-free.
"""

import hashlib
import json

FINGERPRINT_VERSION = 1


def _digest(entry):
    text = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:16]


def device_fingerprints(config):
    """One fingerprint per device entry of ``config``, in its order.

    Each is ``{name, device_id, enabled, digest}``: ``device_id`` is the id an
    external MQTT device is published as (``None`` for any other device),
    ``enabled`` is read as the EMS reads it, and ``digest`` covers the whole
    entry exactly as the file holds it, so any change to it, a new name, broker
    or ``enabled`` included, gives another one: an external MQTT device has no
    runtime-state entry, so EMS takes nothing of it live.
    """

    from ems.zendure_mqtt.config_entries import config_entry_enabled, is_external_mqtt_device_config

    devices = config.get("devices") if isinstance(config, dict) else None
    fingerprints = []
    for entry in devices if isinstance(devices, list) else []:
        if not isinstance(entry, dict):
            continue
        mqtt = entry.get("mqtt") if isinstance(entry.get("mqtt"), dict) else {}
        device_id = mqtt.get("device_id") if is_external_mqtt_device_config(entry) else None
        fingerprints.append({
            "name": str(entry.get("name") or ""),
            "device_id": str(device_id) if device_id else None,
            "enabled": config_entry_enabled(entry),
            "digest": _digest(entry),
        })
    return fingerprints


def loaded_devices_record(fingerprints=()):
    """The snapshot's ``loaded_devices``: ``{version, devices}``, the fingerprints with the version of the rule that made them."""

    return {"version": FINGERPRINT_VERSION, "devices": [dict(item) for item in fingerprints]}
