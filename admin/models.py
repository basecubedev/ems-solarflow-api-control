# SPDX-License-Identifier: AGPL-3.0-or-later
"""Normalized data model for admin device discovery.

The discovery result model is intentionally forward-compatible with the future
"active device" list (see ``future_active_device``) so later config-assistant
phases can promote a discovered device without a schema migration. This module
never touches the real EMS config.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


ROLE_INVERTER = "inverter"
ROLE_GRID_METER = "grid_meter"
ROLE_UNKNOWN = "unknown"


def utc_now_iso():
    """Return a timezone-aware UTC timestamp string (``...Z``)."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class DiscoveredDevice:
    """A single device found during a discovery scan.

    ``config_ready`` stays ``False`` until every field the final EMS config
    needs is present (for Zendure that includes a stable serial number). The id
    falls back to ``api_family:ip`` when no serial is known, so the same host is
    stable across scans without inventing a serial.
    """

    ip: str
    api_family: str
    device_type: str
    role_suggestion: str
    port: int = 80
    protocol: str = "http"
    display_name: str = ""
    serial_number: str | None = None
    firmware: str | None = None
    model: str | None = None
    confidence: float = 0.0
    config_ready: bool = False
    missing_config_fields: list[str] = field(default_factory=list)
    source: str = "http_probe"
    source_detail: str | None = None
    verified: bool = True
    last_seen: str = field(default_factory=utc_now_iso)

    @property
    def id(self):
        key = self.serial_number if self.serial_number else self.ip
        return f"{self.api_family}:{key}"

    def to_dict(self):
        data = asdict(self)
        data["id"] = self.id
        data["usable_for_config"] = self.config_ready
        data["sources"] = [self.source]
        return data


@dataclass
class MqttBrokerCandidate:
    """A possible MQTT endpoint, kept separate from EMS devices."""

    host: str
    port: int
    source: str
    hostname: str | None = None
    service_name: str | None = None
    status: str = "candidate"
    reachable: bool = True
    confidence: float = 0.6
    last_seen: str = field(default_factory=utc_now_iso)
    details: dict = field(default_factory=dict)

    @property
    def id(self):
        return f"mqtt:{self.host}:{self.port}"

    def to_dict(self):
        data = asdict(self)
        data["id"] = self.id
        data["sources"] = [self.source]
        return data


def future_active_device(device):
    """Preview shape for a future promoted "active device" entry.

    This is planning-only: it is never written into the real EMS config. Kept
    here so the result model is demonstrably promotable later without reshaping
    discovery output.
    """

    return {
        "source_id": device.id,
        "config_name": "",
        "display_name": device.display_name,
        "role": device.role_suggestion,
        "ip": device.ip,
        "serial_number": device.serial_number,
        "device_type": device.device_type,
        "enabled": False,
        "parameters": {},
    }
