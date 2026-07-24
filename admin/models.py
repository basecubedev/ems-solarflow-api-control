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
    transport: str | None = None
    tls_mode: str | None = None
    auth_mode: str | None = None
    mqtt_connect_status: str | None = None

    @property
    def id(self):
        return f"mqtt:{self.host}:{self.port}"

    def to_dict(self):
        data = asdict(self)
        data["id"] = self.id
        data["sources"] = [self.source]
        return data


SOURCE_LOCAL_MQTT = "local_mqtt"
SOURCE_ZENDURE_CLOUD_MQTT = "zendure_cloud_mqtt"

DISCOVERY_DEVICE_LIST_ONLY = "device_list_only"
DISCOVERY_MQTT_OBSERVED = "mqtt_observed"
DISCOVERY_ERROR = "error"


@dataclass
class MqttHardwareCandidate:
    """A hardware/device candidate observed on an MQTT broker.

    Admin-only and discovery-display-only: never promoted into the EMS config
    and never merged across brokers or sources. The same physical device may
    legitimately appear under a local broker and the Zendure cloud broker, so
    the stable id embeds both the source type and the broker/account scope.
    """

    broker_id: str
    broker_host: str
    broker_port: int
    topic_family: str
    device_id: str | None = None
    serial_number: str | None = None
    model_hint: str | None = None
    display_name: str = "Zendure MQTT device"
    confidence: float = 0.0
    metrics_seen: list[str] = field(default_factory=list)
    topics_seen: list[str] = field(default_factory=list)
    last_seen: str = field(default_factory=utc_now_iso)
    source_type: str = SOURCE_LOCAL_MQTT
    source_label: str | None = None
    broker_label: str | None = None
    discovery_status: str = DISCOVERY_MQTT_OBSERVED
    product_key: str | None = None
    device_key: str | None = None
    device_name: str | None = None
    tls_mode: str | None = None
    auth_mode: str | None = None
    # Non-secret reference to the discovery credential the successful connection
    # used. Never a username/password/token; safe to carry into a proposal so the
    # runtime broker profile can reconnect.
    credentials_ref: str | None = None

    @property
    def id(self):
        key = self.serial_number or self.device_id or self.device_key or "unknown"
        return f"mqtt-device:{self.source_type}:{self.broker_id}:{self.topic_family}:{key}"

    def to_dict(self):
        data = asdict(self)
        data["id"] = self.id
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
