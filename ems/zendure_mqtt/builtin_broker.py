# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the EMS knows about the built-in MQTT broker, which the Admin owns.

The Admin switches the broker and writes ``config/mqtt/broker.json``; the EMS
only reads that file to say whether the broker is on, and sees the broker
itself as an ordinary local MQTT profile named ``ems-mqtt``. The names here are
the one place both sides take them from. Import-side-effect-free.
"""

import json
import os
import time
from collections.abc import Mapping

from ems.config import grid_meter_mqtt_settings
from ems.zendure_mqtt.config_entries import zendure_mqtt_broker_ref

BUILTIN_PROFILE_REF = "ems-mqtt"
BUILTIN_CREDENTIALS_REF = "ems-mqtt"
BROKER_SETTINGS_PARTS = ("mqtt", "broker.json")
MAPPINGS_PARTS = ("mqtt", "mappings.json")
MAPPING_STATUS_FILENAME = "mqtt-mapping-status.json"
MAPPING_STATUS_STALE_SECONDS = 120
NATIVE_TOPIC_ROOTS = ("iot", "Zendure", "")
NATIVE_TOPIC_TREES = tuple(f"{root}/#" for root in NATIVE_TOPIC_ROOTS)
_UNREADABLE = object()


def broker_settings_path(config_dir) -> str:
    return os.path.join(str(config_dir), *BROKER_SETTINGS_PARTS)


def mappings_path(config_dir) -> str:
    return os.path.join(str(config_dir), *MAPPINGS_PARTS)


def mapping_status_path(data_dir) -> str:
    return os.path.join(str(data_dir), MAPPING_STATUS_FILENAME)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return _UNREADABLE


def broker_enabled(config_dir) -> bool | None:
    """``True``/``False`` from ``broker.json``; no file is off; an unreadable file is ``None``."""

    document = _read_json(broker_settings_path(config_dir))
    if document is None:
        return False
    if not isinstance(document, Mapping):
        return None
    enabled = document.get("enabled", False)
    return enabled if isinstance(enabled, bool) else None


def builtin_profile_refs(config) -> set[str]:
    """The profiles that reach the built-in broker: by its name, or by its account.

    Adding a mapped device through discovery mints a local profile of its own
    name; the account the Admin gave the EMS is what marks it as this broker.
    """

    if not isinstance(config, Mapping):
        return set()
    section = config.get("zendure_mqtt")
    brokers = section.get("brokers") if isinstance(section, Mapping) else None
    if not isinstance(brokers, Mapping):
        return set()
    return {
        str(ref)
        for ref, profile in brokers.items()
        if ref == BUILTIN_PROFILE_REF
        or (isinstance(profile, Mapping) and profile.get("credentials_ref") == BUILTIN_CREDENTIALS_REF)
    }


def builtin_profile_configured(config) -> bool:
    return bool(builtin_profile_refs(config))


def builtin_profile_users(config) -> list[str]:
    """Names of the active devices and the grid meter that read from ``ems-mqtt``."""

    if not isinstance(config, Mapping):
        return []
    refs = builtin_profile_refs(config) | {BUILTIN_PROFILE_REF}
    users = []
    devices = config.get("devices")
    for index, item in enumerate(devices if isinstance(devices, list) else []):
        if not isinstance(item, Mapping) or item.get("enabled") is False:
            continue
        mqtt = item.get("mqtt")
        if isinstance(mqtt, Mapping) and zendure_mqtt_broker_ref(item) in refs:
            users.append(str(item.get("name") or f"device {index + 1}"))
    grid_meter = config.get("grid_meter")
    if isinstance(grid_meter, Mapping) and grid_meter_mqtt_settings(grid_meter).get("broker_ref") in refs:
        users.append("grid meter")
    return users


def _cause(code, title, message, suggested):
    return {
        "code": code,
        "severity": "warning",
        "title": title,
        "message": message,
        "suggested_next_check": suggested,
    }


def builtin_broker_root_causes(config, config_dir, data_dir, *, now=None) -> list[dict]:
    """The diagnosis findings about the built-in broker and the MQTT mapping."""

    now = time.time() if now is None else now
    causes = []
    enabled = broker_enabled(config_dir)
    users = builtin_profile_users(config)
    if users and enabled is False:
        causes.append(
            _cause(
                "mqtt_broker_stopped_in_use",
                "The built-in MQTT broker is stopped while devices use it",
                "Stopped, so these read nothing over it: " + ", ".join(users) + ".",
                "Start it again under Admin, Settings, Add-ons, or deactivate the devices.",
            )
        )
    status_file = mapping_status_path(data_dir)
    status = _read_json(status_file)
    if not isinstance(status, Mapping):
        return causes
    try:
        age = now - os.stat(status_file).st_mtime
    except OSError:
        age = None
    devices = [item for item in status.get("devices") or [] if isinstance(item, Mapping)]
    active = [item for item in devices if item.get("enabled") is not False]
    if age is not None and age > MAPPING_STATUS_STALE_SECONDS and active:
        causes.append(
            _cause(
                "mqtt_mapping_device_silent",
                "The MQTT mapping has stopped reporting",
                f"Its status is {int(age // 60)} minutes old, so no mapped device is published.",
                "Check the ems-mqtt-mapping container on the Services page of the Admin.",
            )
        )
        return causes
    silent = [item for item in active if not item.get("publishing")]
    if silent:
        causes.append(
            _cause(
                "mqtt_mapping_device_silent",
                "A mapped device publishes nothing",
                "Not published: "
                + ", ".join(f"{item.get('id')} ({item.get('reason') or 'unknown'})" for item in silent)
                + ".",
                "Open the MQTT mapping in the Admin and look at the device's sources.",
            )
        )
    publishing_builtin = any(
        item.get("publishing") and item.get("broker_ref") == BUILTIN_PROFILE_REF for item in active
    )
    if enabled is True and publishing_builtin and not builtin_profile_configured(config):
        causes.append(
            _cause(
                "mqtt_broker_profile_missing",
                "Mapped devices are published but not yet added",
                "The MQTT mapping publishes devices on the built-in broker, and the EMS has no "
                f"profile {BUILTIN_PROFILE_REF} to read them from.",
                "Add the devices under Find devices in the Admin.",
            )
        )
    return causes
